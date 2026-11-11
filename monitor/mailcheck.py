"""
mailcheck.py - цель типа mail: отчёты о бэкапах по письмам в IMAP.

Для каждой ожидаемой машины (раздел expect) ищем письмо от нужного отправителя,
которое пришло за "ночь" проверяемой даты: с night_from часов предыдущего вечера до
night_to часов утра. Статус берётся из темы письма (успех или ошибка), имя машины тоже.
Если писем по машине несколько, побеждает ошибка, а среди равных - более позднее.

Модуль не знает про ВК, демон и конфигурацию: на вход простые значения, на выход
словарь результатов и готовый текст отчёта.
"""

from __future__ import annotations

import base64
import email
import imaplib
import os
import re
import ssl
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.header import decode_header
from email.utils import parsedate_to_datetime

WEEKDAYS_RU = ('Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс')
_DAYS = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}

MESSAGE_LIMIT = 4000        # у ВК лимит около 4096 символов на сообщение

DEFAULT_OK_WORDS = ('успешно', 'success', 'succeeded', 'completed')
DEFAULT_FAIL_WORDS = ('ошибк', 'не выполнено', 'не удалось', 'сбой', 'предупрежд',
                      'fail', 'error', 'warning')

_MONTHS = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
           'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12}


# Расписание ожидаемых писем

def check_rule(rule: str) -> str | None:
    """None, если правило верное, иначе текст ошибки.

    Правило - дни через запятую: mon,tue / daily / sun#1 (первое воскресенье месяца)."""
    parts = [p.strip().lower() for p in str(rule).split(',') if p.strip()]
    if not parts:
        return 'пустое правило'
    for part in parts:
        if part == 'daily':
            continue
        name, _, nth = part.partition('#')
        if name not in _DAYS:
            return f'неизвестный день {part!r} (mon..sun, daily, sun#1)'
        if '#' in part and (not nth.isdigit() or not 1 <= int(nth) <= 5):
            return f'номер недели в {part!r} должен быть от 1 до 5'
    return None


def _part_matches(part: str, day: date) -> bool:
    if part == 'daily':
        return True
    name, _, nth = part.partition('#')
    if _DAYS.get(name) != day.weekday():
        return False
    return not nth or (day.day - 1) // 7 + 1 == int(nth)


def expected_names(expect: Mapping[str, str], check_date: date) -> list[str]:
    """Машины, чей бэкап проверяется на check_date.

    Правило из дней недели относится ко дню старта бэкапа (вчера), правило с # - к самому
    дню проверки: так "первое воскресенье" совпадает с письмом, пришедшим в то воскресенье."""
    yesterday = check_date - timedelta(days=1)
    out = []
    for name, rule in expect.items():
        for part in (p.strip().lower() for p in rule.split(',')):
            if not part:
                continue
            day = check_date if '#' in part else yesterday
            if part == 'daily' or _part_matches(part, day):
                out.append(name)
                break
    return out


def night_window(check_date: date, from_hour: int, to_hour: int) -> tuple[datetime, datetime]:
    start = datetime.combine(check_date - timedelta(days=1), datetime.min.time()).replace(hour=from_hour)
    end = datetime.combine(check_date, datetime.min.time()).replace(hour=to_hour)
    return start, end


def parse_date_arg(arg: str | None, today: date | None = None) -> date:
    """Аргумент команды: пусто или сегодня, вчера, ДД.ММ, ДД.ММ.ГГГГ. Непонятное - сегодня."""
    today = today or date.today()
    text = (arg or '').strip().lower()
    if text in ('', 'сегодня', 'today'):
        return today
    if text in ('вчера', 'yesterday'):
        return today - timedelta(days=1)
    m = re.fullmatch(r'(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?', text)
    if m:
        try:
            return date(int(m.group(3) or today.year), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    return today


# Разбор письма

def decode_mime(value: str | None) -> str:
    """Тема письма -> строка. Сырой 8-битный заголовок читаем как UTF-8, а не падаем:
    одно такое письмо не должно ронять всю проверку."""
    if not value:
        return ''
    parts = []
    for chunk, charset in decode_header(value):
        if isinstance(chunk, bytes):
            try:
                chunk = chunk.decode(charset or 'utf-8', 'ignore')
            except LookupError:
                chunk = chunk.decode('utf-8', 'ignore')
        parts.append(chunk)
    return ''.join(parts)


IMAP_TIMEOUT = 30              # секунд на любую операцию с сервером: молчащий сервер не должен держать цель вечно
BODY_LIMIT = 200_000           # символов тела письма, которые разбираются


def connect_imap(query: MailQuery) -> imaplib.IMAP4:
    """Соединение с проверкой сертификата и имени хоста (если не отключено verify = false) и таймаутом."""
    if not query.ssl:
        return imaplib.IMAP4(query.host, query.port, timeout=IMAP_TIMEOUT)
    context = ssl.create_default_context()
    if not query.verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return imaplib.IMAP4_SSL(query.host, query.port, ssl_context=context, timeout=IMAP_TIMEOUT)


def body_text(msg: email.message.Message) -> str:
    plain = html = ''
    for part in msg.walk() if msg.is_multipart() else [msg]:
        kind = part.get_content_type()
        if kind not in ('text/plain', 'text/html') or (plain if kind == 'text/plain' else html):
            continue
        try:
            raw = part.get_payload(decode=True)
            text = raw.decode(part.get_content_charset() or 'utf-8', 'ignore') if isinstance(raw, bytes) else ''
        except LookupError:
            text = ''
        if kind == 'text/plain':
            plain = text
        else:
            html = text
    # [^<>]: тег не может содержать '<', поэтому разбор линейный (на '<<<<...' шаблон '<[^>]+>' квадратичный)
    text = re.sub(r'<[^<>]+>', ' ', (plain or html)[:BODY_LIMIT]).replace('&nbsp;', ' ')
    return re.sub(r'\s+', ' ', text).strip()


def parse_status(subject: str, ok_words: Iterable[str] = DEFAULT_OK_WORDS,
                 fail_words: Iterable[str] = DEFAULT_FAIL_WORDS) -> str | None:
    """'ok', 'fail' или None, если по теме не понять. Слова ошибки сильнее слов успеха."""
    low = subject.lower()
    if any(word.lower() in low for word in fail_words):
        return 'fail'
    if any(word.lower() in low for word in ok_words):
        return 'ok'
    return None


_DEVICE_PATTERNS = (
    re.compile(r'Device:\s*(\S+)', re.IGNORECASE),
    re.compile(r':\s*([^\s(:]+)\s*\('),
)


def find_device(subject: str, custom: re.Pattern[str] | None = None) -> str | None:
    for pattern in ((custom,) if custom else _DEVICE_PATTERNS):
        m = pattern.search(subject)
        if m:
            return m.group(1).strip()
    return None


_FINISH_RE = re.compile(
    r'(?:Время\s+завершения|Finish\s+time|Completed)\s*[A-Za-z]{0,3}\s*'
    r'([A-Za-z]{3})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})\s+(\d{4})')
_DURATION_RE = re.compile(r'(?:Продолжительность|Duration)\s*(\d{1,2}):(\d{2}):(\d{2})')


def parse_finish(body: str, header_date: str | None = None) -> datetime | None:
    """Время завершения из текста письма, а если его там нет - из заголовка Date."""
    m = _FINISH_RE.search(body)
    if m:
        month = _MONTHS.get(m.group(1).lower())
        if month:
            try:
                return datetime(int(m.group(6)), month, int(m.group(2)),
                                int(m.group(3)), int(m.group(4)), int(m.group(5)))
            except ValueError:
                pass
    if header_date:
        try:
            moment = parsedate_to_datetime(header_date)
        except (TypeError, ValueError):
            return None
        return moment.astimezone().replace(tzinfo=None) if moment.tzinfo else moment
    return None


def parse_duration(body: str) -> str | None:
    m = _DURATION_RE.search(body)
    if not m:
        return None
    hours, minutes, seconds = (int(x) for x in m.groups())
    return f'{hours}ч{minutes:02d}м' if hours else f'{minutes}:{seconds:02d}'


# IMAP

def imap_utf7_encode(name: str) -> str:
    """Имя папки в модифицированном UTF-7 (RFC 3501): imaplib сам этого не делает, а
    папки бывают с кириллицей. ASCII-имена без & возвращаются как есть."""
    out: list[str] = []
    chunk = ''

    def flush() -> None:
        nonlocal chunk
        if chunk:
            data = base64.b64encode(chunk.encode('utf-16-be')).decode('ascii').rstrip('=')
            out.append('&' + data.replace('/', ',') + '-')
            chunk = ''

    for char in name:
        if 0x20 <= ord(char) <= 0x7e:
            flush()
            out.append('&-' if char == '&' else char)
        else:
            chunk += char
    flush()
    return ''.join(out)


@dataclass(frozen=True)
class MailQuery:
    host: str
    port: int
    user: str
    password: str
    ssl: bool = True
    verify: bool = True            # проверять сертификат сервера (false - только для своего самоподписанного)
    folders: tuple[str, ...] = ('INBOX',)
    mail_from: str = ''
    ok_words: tuple[str, ...] = DEFAULT_OK_WORDS
    fail_words: tuple[str, ...] = DEFAULT_FAIL_WORDS
    subject_re: str = ''


def _safe_name(text: str) -> str:
    return re.sub(r'[^\w\-.]', '_', text)


def save_eml(raw: bytes, device: str, status: str, moment: datetime, save_dir: str, log) -> None:
    """Письмо кладётся в <save_dir>/<день>/ как доказательство; прежние файлы не затираются."""
    folder = os.path.join(save_dir, moment.strftime('%Y-%m-%d'))
    base = f'{_safe_name(device)}_{status}'
    try:
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, base + '.eml')
        n = 0
        while os.path.exists(path):
            n += 1
            path = os.path.join(folder, f'{base}_{moment:%H-%M-%S}_{n}.eml')
        with open(path, 'wb') as fh:
            fh.write(raw)
        log.info('Письмо сохранено: %s', path)
    except OSError as exc:
        log.warning('Не удалось сохранить письмо: %s', exc)


def fetch_results(query: MailQuery, expected: Iterable[str], win_start: datetime, win_end: datetime,
                  log, *, save_dir: str = '', connect: Callable[..., imaplib.IMAP4] | None = None,
                  ) -> dict[str, dict]:
    """Письма за окно -> {машина: {status, finish, dur, dt}} (только ожидаемые машины)."""
    wanted = set(expected)
    custom = re.compile(query.subject_re) if query.subject_re else None
    results: dict[str, dict] = {}
    conn = connect(query.host, query.port) if connect else connect_imap(query)
    try:
        conn.login(query.user, query.password)
        since = (win_start - timedelta(days=1)).strftime('%d-%b-%Y')
        criteria = f'(SINCE {since} FROM "{query.mail_from}")' if query.mail_from else f'(SINCE {since})'
        for folder in query.folders:
            kind, _ = conn.select(imap_utf7_encode(folder), readonly=True)
            if kind != 'OK':
                log.warning('Папка не открылась, пропуск: %s', folder)
                continue
            kind, data = conn.search(None, criteria)
            if kind != 'OK' or not data or not data[0]:
                continue
            for num in data[0].split():
                _, parts = conn.fetch(num, '(BODY.PEEK[])')
                if not parts or not parts[0] or not isinstance(parts[0], tuple):
                    continue
                raw = parts[0][1]
                msg = email.message_from_bytes(raw)
                subject = decode_mime(msg['Subject'])
                status = parse_status(subject, query.ok_words, query.fail_words)
                device = find_device(subject, custom)
                if not (device and status and device in wanted):
                    continue
                text = body_text(msg)
                moment = parse_finish(text, msg['Date'])
                if moment is None or not win_start <= moment <= win_end:
                    continue
                if save_dir:
                    save_eml(raw, device, status, moment, save_dir, log)
                found = {'status': status, 'finish': f'{moment:%H:%M}',
                         'dur': parse_duration(text), 'dt': moment}
                prev = results.get(device)
                if (prev is None or (status == 'fail' and prev['status'] != 'fail')
                        or (status == prev['status'] and moment > prev['dt'])):
                    results[device] = found
    finally:
        try:
            conn.logout()
        except Exception:          # соединение уже могло оборваться
            pass
    return results


# Отчёт

def build_report(title: str, check_date: date, expected: list[str], results: Mapping[str, dict]) -> str:
    lines = [f'{title}: ночь на {check_date:%d.%m} ({WEEKDAYS_RU[check_date.weekday()]})', '']
    if not expected:
        lines.append('В эту ночь бэкапы не запланированы.')
        return '\n'.join(lines)
    good = 0
    for name in expected:
        info = results.get(name)
        if info and info['status'] == 'ok':
            good += 1
            extra = []
            if info.get('finish'):
                extra.append(f'в {info["finish"]}')
            if info.get('dur'):
                extra.append(f'за {info["dur"]}')
            lines.append(f'[ok] {name}' + (f' ({", ".join(extra)})' if extra else ''))
        elif info:
            lines.append(f'[!] {name} - ошибка')
        else:
            lines.append(f'[?] {name} - письма нет')
    lines += ['', f'Итог: {good} из {len(expected)} успешно']
    return '\n'.join(lines)


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Режет по строкам, чтобы уложиться в лимит сообщения ВК."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ''
    for line in text.split('\n'):
        if len(current) + len(line) + 1 > limit and current:
            parts.append(current)
            current = ''
        current = f'{current}\n{line}' if current else line
        while len(current) > limit:
            parts.append(current[:limit])
            current = current[limit:]
    if current:
        parts.append(current)
    return parts

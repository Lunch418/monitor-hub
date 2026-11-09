"""
engine.py - проверка одной цели: вход через PAM, `ls -la` по всем путям в одном
сеансе, разбор, картинки в стиле Midnight Commander, отправка в ВК.

Различия между целями (формат размеров, цвета, сортировка, подпись, sudo,
число путей) задаются полями Target, а не отдельными скриптами.
"""

from __future__ import annotations

import imaplib
import os
import re
import shlex
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import date, datetime
from io import BytesIO

from monitor import app as _app
from monitor import ls_report as _ls
from monitor import mailcheck, runlog
from monitor import pam_session as _pam
from monitor.config import Settings, Target
from monitor.messengers import make_messenger


class HubError(_app.MonitorError):
    """Ошибка проверки (см. MonitorError: подробности для лога, коротко для чата)."""


class ConfigError(HubError):
    pass


# Стили отчёта

@dataclass(frozen=True)
class Style:
    size_display: Callable[[int, bool], str]
    mark_exec: bool            # '*' к имени исполняемого файла прямо в name
    sort: Callable[[list[dict]], list[dict]]
    noun: str                  # слово в подписи: "Файлов: 12"
    count_files_only: bool     # считать только файлы (без папок)
    bold_fallback: bool        # жирный шрифт не найден -> обычный, а не растровый
    row_style: _ls.RowStyle
    header_marker: str         # значок сортировки в шапке колонки Name


_C_WHITE = (210, 232, 255)
_C_FILE = (255, 95, 210)
_C_STALE = (255, 60, 90)
_C_DATE_OLD = (170, 205, 232)

_L_WHITE = (200, 228, 255)
_L_STALE = (255, 120, 120)
_L_DATE_OLD = (160, 200, 230)


def _listing_row_style(entry: dict, is_stale: bool):
    if is_stale:
        return entry['name'], _L_STALE, _L_STALE
    if entry['is_exec']:
        return entry['name'], _ls.C_GREEN, _ls.C_GREEN
    return entry['name'], _L_WHITE, _L_DATE_OLD


def _backups_row_style(entry: dict, is_stale: bool):
    if entry['is_dir']:
        return '/' + entry['name'], _C_WHITE, _C_DATE_OLD
    if entry['is_exec']:
        return '*' + entry['name'], _ls.C_GREEN, _ls.C_GREEN
    if is_stale:
        return entry['name'], _C_STALE, _C_STALE
    return entry['name'], _C_FILE, _C_FILE


def _sort_listing(entries: list[dict]) -> list[dict]:
    """Каталоги первыми, затем по имени без учёта регистра (как в Midnight Commander)."""
    return sorted(entries, key=lambda e: (not e['is_dir'], e['raw_name'].lower()))


def _sort_backups(entries: list[dict]) -> list[dict]:
    """Папки первыми по алфавиту, файлы по убыванию имени (свежие даты обычно в начале)."""
    return _ls.sort_dirs_then_files_desc(entries)[0]


STYLES: dict[str, Style] = {
    # Файлы и каталоги как есть: сертификаты, отчёты.
    'listing': Style(
        size_display=_ls.size_display_listing, mark_exec=True, sort=_sort_listing,
        noun='Файлов', count_files_only=False, bold_fallback=False,
        row_style=_listing_row_style, header_marker='.n'),
    # Бэкапы: каталоги отмечены DIR.
    'backups': Style(
        size_display=_ls.size_display_backups, mark_exec=False, sort=_sort_backups,
        noun='Бэкапов', count_files_only=True, bold_fallback=True,
        row_style=_backups_row_style, header_marker="'n"),
    # Бэкапы, у каталогов виден настоящий размер в байтах.
    'backups-sizes': Style(
        size_display=_ls.size_display_backups_sizes, mark_exec=False, sort=_sort_backups,
        noun='Бэкапов', count_files_only=True, bold_fallback=True,
        row_style=_backups_row_style, header_marker='.n'),
}


# Команда ls

def ls_command(idx: int, path: str, sudo: bool = False) -> tuple[str, str, str]:
    """(маркер_начала, маркер_конца, команда) для одного пути.

    Маркеры собираются на удалённой стороне (printf), поэтому в самой набранной
    команде их нет: ожидание не зависит от того, отэхоит ли терминал команду, и
    эхо не принимается за вывод.

    Путь экранируется shlex.quote. sudo: `sudo -n` никогда не спрашивает пароль
    (сразу 'a password is required'), `env LANG=C` вместо `sudo LANG=C`, потому
    что sudo с env_reset отказывается проставлять переменные; sudo применяется
    ровно к одному `ls -la`."""
    begin = f'__HUB{idx}_BEG__'
    end = f'__HUB{idx}_END__'
    quoted = shlex.quote(path)
    listing = f'sudo -n env LANG=C LC_ALL=C ls -la {quoted}' if sudo else f'LANG=C ls -la {quoted}'
    command = (
        f"printf '__HUB%s__\\n' {idx}_BEG; "
        f'{listing}; '
        f"printf '__HUB%s__\\n' {idx}_END"
    )
    return begin, end, command


# Живой просмотр: команда повторяется в одном сеансе, каждый раз выводится свежий кадр.
# Настоящий htop рисует экран терминала, поэтому берём пакетные аналоги (top -b и т.п.).

LIVE_PRESETS = {
    'top': ('top -b -n 1 -w 160 | head -n 30', 'нагрузка и самые прожорливые процессы'),
    'cpu': ('ps -eo pid,user,pcpu,pmem,etime,comm --sort=-pcpu | head -n 15', 'процессы по CPU'),
    'mem': ('free -h; echo; ps -eo pid,user,pmem,rss,comm --sort=-pmem | head -n 12', 'память и процессы по ней'),
    'df': ('df -hT -x tmpfs -x devtmpfs -x squashfs', 'свободное место на дисках'),
    'net': ('ss -s; echo; ss -tn state established | head -n 15', 'сетевые соединения'),
    'uptime': ('uptime; echo; w -h | head -n 15', 'аптайм и кто вошёл'),
}
LIVE_MAX_COMMAND = 300
LIVE_MAX_LINES = 200
_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07')


def live_command(text: str) -> str:
    """Имя набора (top, df...) или своя команда -> команда для оболочки. ValueError, если она не подходит."""
    text = text.strip()
    if text in LIVE_PRESETS:
        return LIVE_PRESETS[text][0]
    if not text:
        raise ValueError('укажите набор (' + ', '.join(LIVE_PRESETS) + ') или свою команду')
    if len(text) > LIVE_MAX_COMMAND or any(ord(ch) < 32 for ch in text):
        raise ValueError(f'команда: одна строка, не длиннее {LIVE_MAX_COMMAND} символов')
    return text


def clean_frame(raw: bytes, limit: int = LIVE_MAX_LINES) -> str:
    """Вывод удалённой команды -> безопасный текст: без управляющих последовательностей, не длиннее limit строк."""
    text = _ANSI_RE.sub('', raw.decode('utf-8', 'replace')).replace('\r', '')
    text = ''.join(ch for ch in text if ch in '\n\t' or ch.isprintable())
    lines = text.strip('\n').split('\n')
    if len(lines) > limit:
        lines = [*lines[:limit], f'... (ещё строк: {len(lines) - limit})']
    return '\n'.join(lines)


def exec_command(idx: int, command: str) -> tuple[str, str, str]:
    """(маркер_начала, маркер_конца, команда): как ls_command, но для любой команды."""
    begin = f'__HUB{idx}_BEG__'
    end = f'__HUB{idx}_END__'
    full = f"printf '__HUB%s__\\n' {idx}_BEG; {command}; printf '__HUB%s__\\n' {idx}_END"
    return begin, end, full


# Проверка путей

VERDICTS = {
    'ok': 'ok',
    'noent': 'нет такого пути',
    'notdir': 'не папка',
    'denied': 'нет прав',
    'sudo': 'нужен sudo',
}


@dataclass(frozen=True)
class PathVerdict:
    path: str
    code: str            # ключ VERDICTS
    entries: int | None = None

    @property
    def ok(self) -> bool:
        return self.code == 'ok'

    def __str__(self) -> str:
        text = VERDICTS.get(self.code, self.code)
        if self.entries is not None:
            text += f', записей: {self.entries}'
        return text


def probe_command(idx: int, path: str) -> tuple[str, str]:
    """(маркер конца, команда), которая печатает вердикт для пути.

    Как и у ls_command, все маркеры собираются на удалённой стороне, поэтому
    эхо команды не путается с ответом. sudo только `-n` и только для `test`/`ls`."""
    q = shlex.quote(path)
    say = "printf '__HUB%s__ %s\\n'"
    command = (
        f'if [ ! -e {q} ]; then {say} V_noent 0; '
        f'elif [ ! -d {q} ]; then {say} V_notdir 0; '
        f'elif [ -r {q} ] && [ -x {q} ]; then {say} V_ok "$(ls -A {q} | wc -l)"; '
        f'elif sudo -n test -r {q} 2>/dev/null; then {say} V_sudo "$(sudo -n ls -A {q} | wc -l)"; '
        f'else {say} V_denied 0; fi; '
        f"printf '__HUB%s__\\n' {idx}_PEND"
    )
    return f'__HUB{idx}_PEND__', command


_VERDICT_RE = re.compile(rb'__HUBV_([a-z]+)__ *(\d*)')


def parse_verdict(path: str, raw: bytes, sudo_allowed: bool) -> PathVerdict:
    match = _VERDICT_RE.search(raw)
    if not match:
        return PathVerdict(path, 'denied')
    code = match.group(1).decode()
    count = int(match.group(2)) if match.group(2) else None
    if code == 'sudo' and sudo_allowed:
        code = 'ok'                  # у цели sudo = true: читать через sudo и нужно
    if code not in ('ok', 'sudo'):
        count = None
    return PathVerdict(path, code, count)


# Ограничитель входов на шлюз

class GatewayLimiter:
    """Не больше max_parallel одновременных входов и пауза gap секунд между их началами.

    Шлюз старый и не любит всплески: когда несколько целей по расписанию стартуют в
    одну минуту, входы идут по очереди, а не одновременно."""

    def __init__(self, max_parallel: int, gap: float, *, sleep=time.sleep,
                 monotonic=time.monotonic) -> None:
        self._sem = threading.BoundedSemaphore(max(1, max_parallel))
        self._gap = max(0.0, gap)
        self._sleep = sleep
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._last: float | None = None

    def __enter__(self) -> GatewayLimiter:
        self._sem.acquire()
        try:
            with self._lock:
                if self._last is not None:
                    wait = self._gap - (self._monotonic() - self._last)
                    if wait > 0:
                        self._sleep(wait)
                self._last = self._monotonic()
        except BaseException:
            self._sem.release()
            raise
        return self

    def __exit__(self, *exc_info) -> None:
        self._sem.release()


# Движок

@dataclass
class Report:
    buffers: list[BytesIO]
    caption: Callable[[], str]
    # (путь, записи, имена устаревших) - для текстового предпросмотра в интерфейсе
    listings: list[tuple[str, list[dict], set[str]]] = field(default_factory=list)


class Engine:
    """Общий для всех целей контекст: настройки, логгер, сессия ВК, шрифты."""

    def __init__(self, settings: Settings, env: Mapping[str, str], log,
                 targets: Mapping[str, Target] | None = None) -> None:
        self.settings = settings
        self.env = env
        # Имена переменных с паролями всех целей: нужны, чтобы замаскировать пароль
        # и в сообщениях, не привязанных к одной цели.
        self.password_names = tuple(t.password_env for t in (targets or {}).values())
        self.log = log
        self.chat = make_messenger(settings, log, error_cls=HubError, config_error_cls=ConfigError)
        self._limiters: dict[tuple[str, str], GatewayLimiter] = {}
        self._renderers: dict[str, _ls.McRenderer] = {}
        self.last_report: Report | None = None
        self._lock = threading.Lock()

    # секреты и сообщения
    def password(self, target: Target) -> str:
        return self.env.get(target.password_env, '')

    def mask(self, text: str, target: Target | None = None) -> str:
        secrets = self.chat.secrets()
        if target is not None:
            secrets.append(self.password(target))
        else:
            secrets.extend(self.env.get(name, '') for name in self.password_names)
        return _app.mask_secrets(text, secrets)

    def user_facing(self, exc: BaseException) -> str:
        return _app.user_facing(exc, HubError, 'Не удалось получить список (подробности в логе)')

    # вход и листинги
    def pam_session(self, target: Target) -> _pam.PamSession:
        s = self.settings
        if target.via == 'ssh':
            cfg = _pam.PamConfig(
                host=target.target_host, port=target.ssh_port, user=target.user,
                password=self.password(target), target_host=target.target_host, target_sel='',
                strict_host_key=s.strict_host_key, known_hosts=s.known_hosts,
                kex='', hostkey_algs='', strict_algs=s.strict_algs,
                prompt_timeout=target.prompt_timeout, prompt_nudge=s.prompt_nudge,
                connect_retries=target.connect_retries, connect_retry_delay=target.connect_retry_delay,
                connect_message=f'[{target.name}] SSH -> %s:%s (проверка ключа хоста: %s)', direct=True,
            )
        else:
            cfg = _pam.PamConfig(
                host=target.pam_host or s.pam_host, port=target.pam_port or s.pam_port,
                user=target.pam_user, password=self.password(target),
                target_host=target.target_host, target_sel=target.target_sel,
                strict_host_key=s.strict_host_key, known_hosts=s.known_hosts,
                kex=s.kex, hostkey_algs=s.hostkey_algs, strict_algs=s.strict_algs,
                prompt_timeout=target.prompt_timeout, prompt_nudge=s.prompt_nudge,
                connect_retries=target.connect_retries, connect_retry_delay=target.connect_retry_delay,
                connect_message=f'[{target.name}] SSH -> PAM %s:%s (проверка ключа хоста: %s)',
            )
        return _pam.PamSession(cfg, log=self.log, error_cls=HubError,
                               mask_secrets=lambda text: self.mask(text, target))

    def gateway(self, target: Target):
        """Ограничитель входов для шлюза цели. Прямой ssh не ограничивается: это разные машины."""
        if target.via == 'ssh':
            return nullcontext()
        s = self.settings
        key = (target.pam_host or s.pam_host, str(target.pam_port or s.pam_port))
        with self._lock:
            if key not in self._limiters:
                self._limiters[key] = GatewayLimiter(s.max_parallel, s.login_gap)
            return self._limiters[key]

    def fetch_listings(self, target: Target, paths: tuple[str, ...] | None = None) -> dict[str, list[str]]:
        """{путь: строки листинга}. Один вход на все пути; при срыве соединения
        сессия повторяется целиком (см. pam_session.PamSession.fetch)."""
        from monitor.config import validate_environment
        session = self.pam_session(target)

        def once(px) -> dict[str, list[str]]:
            results: dict[str, list[str]] = {}
            with session.login(px) as sh:
                for idx, path in enumerate(paths or target.paths):
                    begin, end, command = ls_command(idx, path, target.sudo)
                    sh.send_command(command)
                    self.log.info('[%s] ls -la запущен: %s', target.name, path)
                    # retryable=False: до сюда дошли с рабочим shell, срыв на самом ls -
                    # это висящая команда, а не заминка подключения.
                    sh.expect_any([begin.encode()], f'ls start ({path})', timeout=30, retryable=False)
                    sh.expect_any([end.encode()], f'ls output ({path})', timeout=60, retryable=False)
                    raw = sh.child.before or b''
                    self.log.info('[%s] путь %s: получено байт %d', target.name, path, len(raw))
                    results[path] = _ls.parse_ls_block(
                        raw, self.log, path=path, mask=lambda text: self.mask(text, target))
            return results

        with self.gateway(target):
            return session.fetch(
                once,
                validate_config=lambda: validate_environment(
                    self.settings, {target.name: target}, self.env, need_chat=False),
                config_error_cls=ConfigError,
            )

    def live(self, target: Target, command: str, on_frame: Callable[[str], None], stop: threading.Event, *,
             interval: float = 2.0, max_seconds: float = 900.0) -> int:
        """Повторяет команду на машине в одном сеансе, пока не сработает stop или не пройдёт max_seconds.
        Каждый кадр (вывод команды) отдаётся в on_frame. Возвращает число кадров."""
        from monitor.config import validate_environment
        command = live_command(command)
        session = self.pam_session(target)
        frames = 0

        def once(px) -> int:
            nonlocal frames
            deadline = time.monotonic() + max_seconds
            with session.login(px) as sh:
                while not stop.is_set() and time.monotonic() < deadline:
                    begin, end, full = exec_command(frames, command)
                    sh.send_command(full)
                    sh.expect_any([begin.encode()], 'live start', timeout=30, retryable=False)
                    sh.expect_any([end.encode()], 'live output', timeout=60, retryable=False)
                    frames += 1
                    on_frame(self.mask(clean_frame(sh.child.before or b''), target))
                    stop.wait(interval)
            return frames

        with self.gateway(target):
            return session.fetch(
                once,
                validate_config=lambda: validate_environment(
                    self.settings, {target.name: target}, self.env, need_chat=False),
                config_error_cls=ConfigError,
            )

    def check_paths(self, target: Target, paths: tuple[str, ...] | None = None) -> list[PathVerdict]:
        """Один вход на ВМ: для каждого пути - существует ли, папка ли, можно ли читать."""
        from monitor.config import validate_environment
        paths = paths or target.paths
        session = self.pam_session(target)

        def once(px) -> list[PathVerdict]:
            out = []
            with session.login(px) as sh:
                for idx, path in enumerate(paths):
                    end, command = probe_command(idx, path)
                    sh.send_command(command)
                    sh.expect_any([end.encode()], f'check path ({path})', timeout=30, retryable=False)
                    out.append(parse_verdict(path, sh.child.before or b'', target.sudo))
            return out

        with self.gateway(target):
            return session.fetch(
                once,
                validate_config=lambda: validate_environment(
                    self.settings, {target.name: target}, self.env, need_chat=False),
                config_error_cls=ConfigError,
            )

    # картинки
    def renderer(self, style_name: str) -> _ls.McRenderer:
        with self._lock:
            if style_name not in self._renderers:
                style = STYLES[style_name]
                fonts = _ls.FontProvider(
                    self.log, _ls.FONT_DIRS, bold_fallback=style.bold_fallback,
                    missing_message='Моноширинный шрифт не найден в %s - использую встроенный растровый.',
                    missing_args=(', '.join(_ls.FONT_DIRS[:2]),),
                )
                self._renderers[style_name] = _ls.McRenderer(
                    fonts, header_marker=style.header_marker, row_style=style.row_style)
            return self._renderers[style_name]

    def build_report(self, target: Target, listings: dict[str, list[str]]) -> Report:
        """Листинги -> картинки и подпись (без сети). Выделено для тестов."""
        style = STYLES[target.style]
        renderer = self.renderer(target.style)
        buffers: list[BytesIO] = []
        shown: list[tuple[str, list[dict], set[str]]] = []
        summary: list[str] = []
        total_stale = 0
        counted = 0
        any_entries = False

        for path in target.paths:
            entries, parent_date = _ls.split_entries(
                listings.get(path, []),
                lambda line: _ls.parse_ls_line(line, style.size_display, mark_exec=style.mark_exec))
            if not entries:
                self.log.warning('[%s] путь %s: список пуст после разбора', target.name, path)
                summary.append(f'{path}: пусто')
                continue
            any_entries = True
            entries = style.sort(entries)

            stale = _ls.stale_entries(entries, target.stale_days)
            stale_names = {e['raw_name'] for e in stale}
            total_stale += len(stale)
            if stale:
                self.log.warning('[%s] путь %s: старше %d дн.: %d',
                                 target.name, path, target.stale_days, len(stale))

            shown.append((path, entries, stale_names))
            buffers.extend(renderer.render_pages(
                path, entries, parent_date, stale_names, self.settings.max_rows))

            files = sum(1 for e in entries if not e['is_dir'])
            dirs = len(entries) - files
            counted += files if style.count_files_only else len(entries)
            summary.append(f'{path}: {files} файл(ов), {dirs} папок')

        if not any_entries:
            raise HubError(
                f'[{target.name}] все пути дали пустой список после разбора листинга',
                'Каталог пуст или листинг не удалось разобрать (проверьте пути и подключение)',
            )

        def caption() -> str:
            stamp = datetime.now().strftime('%d.%m.%Y %H:%M')
            details = '  |  '.join(summary)
            if total_stale:
                details += f'  |  Устарело (>{target.stale_days} дн.): {total_stale}'
            self.log.info('[%s] сводка: %s', target.name, details)
            if len(target.paths) > 1:
                return stamp          # подробности по путям - только в логе
            text = f'{stamp}  |  {style.noun}: {counted}'
            if total_stale:
                text += f'  |  Устарело (>{target.stale_days} дн.): {total_stale}'
            return text

        return Report(buffers, caption, shown)

    def collect(self, target: Target) -> tuple[list[BytesIO], Callable[[], str]]:
        try:
            report = self.build_report(target, self.fetch_listings(target))
        except Exception as exc:
            self._note(target, False, self.user_facing(exc))
            raise
        self._note(target, True, f'картинок: {len(report.buffers)}')
        self.last_report = report
        if self.settings.state_dir:
            try:
                runlog.save_listing(self.settings.state_dir, target.name, target.style, report.listings)
            except OSError as exc:
                self.log.warning('не удалось сохранить листинг %s: %s', target.name, exc)
        return report.buffers, report.caption

    def _note(self, target: Target, ok: bool, message: str) -> None:
        if not self.settings.state_dir:
            return
        try:
            runlog.record(self.settings.state_dir, target.name, ok, message)
        except OSError as exc:
            self.log.warning('не удалось записать результат %s: %s', target.name, exc)

    # запуск
    def save_screenshots(self, target: Target, buffers: list[BytesIO]) -> list[str]:
        return _ls.save_screenshots(
            buffers, save_dir=self.settings.save_dir, prefix=target.name,
            keep=self.settings.keep_screenshots, log=self.log)

    def peers_for(self, target: Target) -> tuple[int, list[str]]:
        """(основной получатель, дополнительные) для плановой отправки."""
        default = int(target.peers[0]) if target.peers else self.settings.default_peer
        extras = [*target.peers[1:], *self.settings.extra_peers]
        return default, extras

    # письма (type = "mail")
    def mail_query(self, target: Target) -> mailcheck.MailQuery:
        return mailcheck.MailQuery(
            host=target.imap_host, port=target.imap_port, user=target.imap_user,
            password=self.password(target), ssl=target.imap_ssl, folders=target.folders,
            mail_from=target.mail_from, subject_re=target.subject_re,
            ok_words=target.ok_words or mailcheck.DEFAULT_OK_WORDS,
            fail_words=target.fail_words or mailcheck.DEFAULT_FAIL_WORDS)

    def mail_report(self, target: Target, check_date: date | None = None) -> tuple[str, list[str], dict]:
        """(текст отчёта, ожидаемые машины, результаты). Нечего ожидать - в IMAP не ходим."""
        check_date = check_date or date.today()
        expected = mailcheck.expected_names(dict(target.expect), check_date)
        results: dict = {}
        if expected:
            start, end = mailcheck.night_window(check_date, target.night_from, target.night_to)
            try:
                results = mailcheck.fetch_results(
                    self.mail_query(target), expected, start, end, self.log,
                    save_dir=os.path.join(self.settings.save_dir, 'mail', target.name))
            except (OSError, imaplib.IMAP4.error) as exc:
                raise HubError(
                    f'[{target.name}] IMAP: {self.mask(str(exc), target)}',
                    'Не удалось получить письма (проверьте настройки почты и пароль)') from exc
        report = mailcheck.build_report(target.title, check_date, expected, results)
        if self.settings.state_dir:
            try:
                runlog.save_report(self.settings.state_dir, target.name, report)
            except OSError as exc:
                self.log.warning('не удалось сохранить отчёт %s: %s', target.name, exc)
        good = sum(1 for v in results.values() if v['status'] == 'ok')
        self._note(target, True, f'{good} из {len(expected)} успешно' if expected else 'бэкапов не ждали')
        return report, expected, results

    def check_mail(self, target: Target, *, send: bool = True, peer_id: int | None = None,
                   check_date: date | None = None) -> str:
        """Одна проверка писем. Возвращает текст отчёта; send=False в мессенджер не пишет."""
        try:
            report, _, _ = self.mail_report(target, check_date)
        except Exception as exc:
            self._note(target, False, self.user_facing(exc))
            raise
        if send:
            for part in mailcheck.split_message(report):
                self.chat.send_message(peer_id or self.peers_for(target)[0], part)
        return report

    def _run_mail(self, target: Target, peer_id: int | None, arg: str) -> None:
        default, extras = self.peers_for(target)
        peer = peer_id or default
        self.log.info('=== Проверка: %s ===', target.title)
        try:
            check_date = mailcheck.parse_date_arg(arg)
            report, expected, _ = self.mail_report(target, check_date)
            if not expected and peer_id is None:
                self.log.info('[%s] в ночь на %s бэкапы не ждали - молчу', target.name, check_date)
            else:
                recipients = [peer]
                if peer_id is None:
                    recipients += [int(x) for x in extras if re.fullmatch(r'-?\d+', x)]
                for recipient in recipients:
                    for part in mailcheck.split_message(report):
                        self.chat.send_message(recipient, part)
        except Exception as exc:
            self._note(target, False, self.user_facing(exc))
            self.log.error('Ошибка: %s', self.mask(str(exc), target), exc_info=True)
            self.chat.send_message(peer, f'Ошибка мониторинга ({target.title}): {self.user_facing(exc)}')
        self.log.info('=== Готово ===')

    def check(self, target: Target, *, send: bool = True, open_after: bool = False,
              peer_id: int | None = None) -> list[str]:
        """Одна проверка. Исключения не гасит: вызывающий решает, что показать.
        Возвращает пути сохранённых картинок. send=False: в ВК не пишет вообще."""
        buffers, make_caption = self.collect(target)
        saved = self.save_screenshots(target, buffers)
        if open_after and saved:
            _ls.open_screenshot(saved[0])
        if send:
            default, _ = self.peers_for(target)
            self.chat.send_photos(saved, make_caption(), peer_id or default)
        else:
            self.log.info('[%s] подпись: %s (отправка отключена)', target.name, make_caption())
        return saved

    def run(self, target: Target, *, peer_id: int | None = None, open_after: bool = False,
            arg: str = '') -> None:
        """Проверка с отправкой и сообщением об ошибке в чат (для расписания и команд).
        peer_id=None - плановая рассылка (подключаются дополнительные получатели).
        arg - слова после команды в беседе (для mail это дата: вчера, 26.07)."""
        if target.type == 'mail':
            self._run_mail(target, peer_id, arg)
            return
        default, extras = self.peers_for(target)
        _app.run_check(
            log=self.log, title=f'=== Проверка: {target.title} ===',
            error_title=f'Ошибка мониторинга ({target.title})',
            target_peer_id=peer_id, open_after=open_after, default_peer=default,
            collect=lambda: self.collect(target),
            save_screenshots=lambda buffers: self.save_screenshots(target, buffers),
            open_screenshot=_ls.open_screenshot,
            send_photos=self.chat.send_photos, send_message=self.chat.send_message,
            user_facing=self.user_facing,
            mask_secrets=lambda text: self.mask(text, target), extra_peers=extras,
        )


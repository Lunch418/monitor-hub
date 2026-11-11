"""
formspec.py - что можно править в интерфейсе: поля цели, общие настройки, команды строки `>`.

Здесь только данные и чистые функции (без Textual), поэтому всё проверяется обычными
тестами. Интерфейс (tui.py) лишь рисует эти поля и отдаёт сюда введённый текст.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from monitor.config import STYLES, Target

YES = ('yes', 'да', 'true', '1', 'on')
NO = ('no', 'нет', 'false', '0', 'off')


@dataclass(frozen=True)
class Field:
    id: str
    label: str
    hint: str = ''
    kinds: tuple[str, ...] = ('ls', 'mail')      # для каких типов цели поле нужно
    via: str | None = None                        # только при таком способе входа (ls)
    widget: str = 'input'                         # input | password | select
    choices: tuple[tuple[str, str], ...] = ()     # для select: (подпись, значение)
    number: bool = False                          # целое число; пусто - значение по умолчанию


LS, MAIL = ('ls',), ('mail',)
YESNO = (('нет', 'no'), ('да', 'yes'))

FIELDS: tuple[Field, ...] = (
    Field('name', 'имя', 'латиница, цифры, - и _'),
    Field('type', 'тип', widget='select', choices=(('ls: папки по ssh', 'ls'), ('mail: письма IMAP', 'mail'))),
    Field('title', 'название', 'как цель называется в беседе'),
    Field('via', 'вход', kinds=LS, widget='select', choices=(('через PAM-шлюз', 'pam'), ('напрямую ssh', 'ssh'))),
    Field('pam_user', 'pam', 'учётка на шлюзе', LS, 'pam'),
    Field('user', 'логин', 'учётка на машине', LS, 'ssh'),
    Field('password', 'пароль', widget='password'),
    Field('target_host', 'вм', 'IP или имя машины', LS),
    Field('target_sel', 'пункт', 'номер пользователя в меню PAM (обычно 1)', LS, 'pam'),
    Field('ssh_port', 'порт ssh', '22', LS, 'ssh', number=True),
    Field('pam_host', 'шлюз', 'пусто - общий PAM_HOST', LS, 'pam'),
    Field('pam_port', 'порт шлюза', 'пусто - общий PAM_PORT', LS, 'pam'),
    Field('paths', 'папки', '/путь/один, /путь/два', LS),
    Field('style', 'стиль', kinds=LS, widget='select', choices=tuple((s, s) for s in STYLES)),
    Field('sudo', 'sudo', kinds=LS, widget='select', choices=YESNO),
    Field('stale_days', 'устарело, дн', '0 - не отмечать', LS, number=True),
    Field('imap_host', 'imap', 'mail.example.org', MAIL),
    Field('imap_port', 'imap порт', '993', MAIL, number=True),
    Field('imap_ssl', 'imap ssl', kinds=MAIL, widget='select', choices=(('да', 'yes'), ('нет', 'no'))),
    Field('imap_user', 'почта', 'логин ящика', MAIL),
    Field('mail_from', 'от кого', 'часть адреса отправителя; пусто - любые', MAIL),
    Field('folders', 'папки почты', 'INBOX, Бэкапы', MAIL),
    Field('night_from', 'ночь с, ч', '18', MAIL, number=True),
    Field('night_to', 'ночь до, ч', '12', MAIL, number=True),
    Field('expect', 'ждём', 'app-01=mon,tue; db-01=daily; web-01=sun#1', MAIL),
    Field('subject_re', 'тема', 'регулярка с именем машины в ( ); пусто - стандартная', MAIL),
    Field('ok_words', 'слова ok', 'через запятую; пусто - стандартные', MAIL),
    Field('fail_words', 'слова сбоя', 'через запятую; пусто - стандартные', MAIL),
    Field('schedule', 'время', 'daily 09:00, wed 10:00'),
    Field('command', 'команда', '/get'),
    Field('info', 'состояние', 'в беседе: uptime, disk, mem, top, cpu, net', LS),
    Field('peers', 'кому', 'id беседы через запятую; пусто - по умолчанию'),
    Field('prompt_timeout', 'ждать shell', 'секунд, по умолчанию 120', LS, number=True),
    Field('connect_retries', 'попыток', 'по умолчанию 2', LS, number=True),
    Field('connect_retry_delay', 'пауза, с', 'по умолчанию 60', LS, number=True),
    Field('run_timeout', 'лимит, с', 'до предупреждения в беседе, по умолчанию 900', number=True),
)
FIELD_BY_ID = {f.id: f for f in FIELDS}
LIST_FIELDS = ('paths', 'folders', 'schedule', 'peers', 'ok_words', 'fail_words', 'info')


def visible(field: Field, kind: str, via: str) -> bool:
    return kind in field.kinds and (field.via is None or field.via == via)


def split_list(text: str) -> list[str]:
    return [part.strip() for part in text.replace('\n', ',').split(',') if part.strip()]


def parse_expect(text: str) -> tuple[dict[str, str], list[str]]:
    """'app-01=mon,tue; db-01=daily' -> ({'app-01': 'mon,tue', ...}, проблемы)."""
    result: dict[str, str] = {}
    problems: list[str] = []
    for chunk in re.split(r'[;\n]', text):
        chunk = chunk.strip()
        if not chunk:
            continue
        found = re.fullmatch(r'([^=:\s]+)\s*[=:]\s*(.+)', chunk)
        if not found:
            problems.append(f'ждём: {chunk!r} - нужен вид "машина=правило"')
            continue
        result[found.group(1)] = found.group(2).strip()
    return result, problems


def format_expect(pairs: tuple[tuple[str, str], ...]) -> str:
    return '; '.join(f'{name}={rule}' for name, rule in pairs)


def default_password_env(name: str, kind: str = 'ls', via: str = 'pam') -> str:
    suffix = 'MAIL' if kind == 'mail' else 'SSH' if via == 'ssh' else 'PAM'
    return f'{name.upper().replace("-", "_")}_{suffix}_PASSWORD'


def values_from_target(target: Target) -> dict[str, str]:
    """Текст для каждого поля формы."""
    values = {
        'name': target.name, 'type': target.type, 'title': target.title, 'via': target.via,
        'pam_user': target.pam_user, 'user': target.user, 'target_host': target.target_host,
        'target_sel': target.target_sel, 'ssh_port': str(target.ssh_port), 'pam_host': target.pam_host,
        'pam_port': target.pam_port, 'paths': ', '.join(target.paths), 'style': target.style,
        'sudo': 'yes' if target.sudo else 'no', 'stale_days': str(target.stale_days),
        'imap_host': target.imap_host, 'imap_port': str(target.imap_port),
        'imap_ssl': 'yes' if target.imap_ssl else 'no', 'imap_user': target.imap_user,
        'mail_from': target.mail_from, 'folders': ', '.join(target.folders),
        'night_from': str(target.night_from), 'night_to': str(target.night_to),
        'expect': format_expect(target.expect), 'subject_re': target.subject_re,
        'ok_words': ', '.join(target.ok_words), 'fail_words': ', '.join(target.fail_words),
        'schedule': ', '.join(str(s) for s in target.schedule), 'command': target.command,
        'info': ', '.join(target.info),
        'peers': ', '.join(target.peers), 'prompt_timeout': str(target.prompt_timeout),
        'connect_retries': str(target.connect_retries), 'connect_retry_delay': str(target.connect_retry_delay),
        'run_timeout': str(target.run_timeout),
    }
    return values


def blank_values(kind: str = 'ls') -> dict[str, str]:
    values = {f.id: '' for f in FIELDS}
    values.update(type=kind, via='pam', style='backups', sudo='no', imap_ssl='yes', target_sel='1')
    return values


def raw_from_values(values: Mapping[str, str], *, kind: str, via: str, password_env: str
                    ) -> tuple[dict[str, object], list[str]]:
    """Текст формы -> сырые поля цели для parse_targets. Лишние для типа поля не берутся.

    Числовое поле: пусто - значение по умолчанию (из [defaults]), не число - остаётся
    текстом, и разбор конфигурации сам скажет, что с ним не так."""
    raw: dict[str, object] = {'password_env': password_env}
    problems: list[str] = []
    if kind != 'ls':
        raw['type'] = kind
    if kind == 'ls' and via != 'pam':
        raw['via'] = via
    for field in FIELDS:
        if field.id in ('name', 'password', 'type', 'via') or not visible(field, kind, via):
            continue
        text = values.get(field.id, '').strip()
        if field.id == 'expect':
            expect, bad = parse_expect(text)
            problems += bad
            raw['expect'] = expect
        elif field.id in LIST_FIELDS:
            if text or field.id == 'paths':          # пустой список остальных полей - значение по умолчанию
                raw[field.id] = split_list(text)
        elif field.id == 'sudo' or field.id == 'imap_ssl':
            raw[field.id] = text in YES
        elif field.number:
            if text:
                raw[field.id] = int(text) if text.lstrip('-').isdigit() else text
        elif field.id == 'target_sel':
            raw[field.id] = text or '1'
        elif text or field.id in ('title', 'style'):
            raw[field.id] = text
    return raw, problems


def apply_set(values: dict[str, str], field_id: str, text: str, *, kind: str, via: str) -> str:
    """Команда /set: записывает текст в поле формы. Возвращает сообщение об ошибке или ''."""
    field = FIELD_BY_ID.get(field_id)
    if field is None:
        return f'нет такого поля: {field_id}. Есть: ' + ', '.join(f.id for f in FIELDS if f.id != 'password')
    if field.id == 'password':
        return 'пароль вводится в форме (поле "пароль"), в журнал и историю он не попадает'
    if not visible(field, kind, via):
        return f'поле {field_id} не относится к цели этого типа (тип {kind}, вход {via})'
    if field.widget == 'select':
        options = {value for _, value in field.choices}
        low = text.strip().lower()
        if field.id in ('sudo', 'imap_ssl'):
            low = 'yes' if low in YES else 'no' if low in NO else low
        if low not in options:
            return f'{field_id}: допустимо {", ".join(sorted(options))}'
        values[field_id] = low
        return ''
    values[field_id] = text.strip()
    return ''


# Команды строки ">"

@dataclass(frozen=True)
class Command:
    name: str            # без косой черты, в нижнем регистре
    args: tuple[str, ...] = ()
    rest: str = ''       # всё после команды одной строкой (для /set: значение с пробелами)


HELP = (
    ('/new [имя] [mail]', 'новая цель (по умолчанию тип ls)'),
    ('/run [цель] [дата]', 'проверить без отправки (для mail дата: вчера, 26.07)'),
    ('/send [цель] [дата]', 'проверить и отправить в беседу'),
    ('/ls [цель] <путь> [send]', 'сразу показать содержимое папки (send - ещё и отправить)'),
    ('/paths [цель]', 'проверить, что папки есть и читаются'),
    ('/live [цель] <набор|команда>', 'следить за командой на машине (top, cpu, mem, df, net, uptime или своя)'),
    ('/top, /stop', 'быстро: top по выбранной цели; остановить просмотр (или escape)'),
    ('/set <поле> <значение>', 'изменить поле выбранной цели (потом /save)'),
    ('/save', 'сохранить цель'),
    ('/delete', 'удалить выбранную цель (два раза)'),
    ('/settings', 'общие настройки: мессенджер, шлюз, токены'),
    ('/status', 'состояние демона'),
    ('/open', 'открыть последнюю картинку'),
    ('/help', 'эта справка'),
    ('<имя цели> или её команда', 'выбрать цель; команда цели (например /get) запускает проверку'),
)


def parse_prompt(text: str) -> Command | None:
    text = text.strip()
    if not text:
        return None
    head, _, rest = text.partition(' ')
    return Command(head.lstrip('/').lower(), tuple(rest.split()), rest.strip())


# Общие настройки (.env)

@dataclass(frozen=True)
class EnvField:
    key: str
    label: str
    hint: str = ''
    secret: bool = False
    choices: tuple[tuple[str, str], ...] = ()
    kind: str = 'text'          # text | int | float | ids


ENV_FIELDS: tuple[EnvField, ...] = (
    EnvField('MESSENGER', 'мессенджер', choices=(('ВКонтакте', 'vk'), ('Telegram', 'telegram'))),
    EnvField('VK_TOKEN', 'ВК токен', 'ключ доступа группы', secret=True),
    EnvField('VK_GROUP_ID', 'ВК группа', 'число', kind='int'),
    EnvField('VK_PEER_ID', 'ВК беседа', 'peer_id по умолчанию', kind='int'),
    EnvField('TELEGRAM_TOKEN', 'TG токен', 'от @BotFather', secret=True),
    EnvField('TELEGRAM_CHAT_ID', 'TG чат', 'id чата по умолчанию (у групп отрицательный)', kind='int'),
    EnvField('ALLOWED_USERS', 'кому можно', 'id пользователей через запятую; пусто - никому', kind='ids'),
    EnvField('ALLOW_ANY_USER', 'всем можно', choices=(('нет', ''), ('да', '1'))),
    EnvField('ALLOWED_CHATS', 'беседы для команд', 'пусто - только свои беседы', kind='ids'),
    EnvField('EXTRA_PEERS', 'доп. беседы', 'для плановой рассылки, через запятую', kind='ids'),
    EnvField('PAM_HOST', 'PAM шлюз', 'общий для целей через PAM'),
    EnvField('PAM_PORT', 'PAM порт', '2222', kind='int'),
    EnvField('SSH_STRICT_HOST_KEY', 'ключ хоста',
             choices=(('accept-new', 'accept-new'), ('yes', 'yes'), ('no', 'no'))),
    EnvField('MAX_PARALLEL_LOGINS', 'входов сразу', 'на один шлюз, по умолчанию 2', kind='int'),
    EnvField('GATEWAY_LOGIN_GAP', 'пауза входов', 'секунд, по умолчанию 5', kind='float'),
)
ENV_BY_KEY = {f.key: f for f in ENV_FIELDS}


def check_env_value(field: EnvField, text: str) -> str:
    """Текст ошибки или пустая строка. Пустое значение допустимо всегда (параметр не задан)."""
    text = text.strip()
    if not text:
        return ''
    if '\n' in text or '\r' in text:
        return 'без перевода строки'
    if "'" in text and '"' in text:
        return 'нельзя одновременно \' и "'
    if field.choices and text not in {v for _, v in field.choices}:
        return 'допустимо: ' + ', '.join(v or "''" for _, v in field.choices)
    if field.kind == 'int' and not re.fullmatch(r'-?\d+', text):
        return 'нужно целое число'
    if field.kind == 'float':
        try:
            float(text)
        except ValueError:
            return 'нужно число'
    if field.kind == 'ids' and not all(re.fullmatch(r'-?\d+', p) for p in split_list(text)):
        return 'нужны числа через запятую'
    if field.key == 'PAM_HOST' and not re.fullmatch(r'[A-Za-z0-9._:-]+', text):
        return 'недопустимые символы'
    return ''

"""
config.py - настройки и список целей.

Settings: общее и секреты из окружения (.env): шлюз PAM, ВКонтакте, SSH.
Target: одна цель из targets.toml (ВМ, учётка, папки, расписание, команда).
load_targets: разбор и проверка файла. Ошибки не бросаются по одной, а
собираются списком с указанием цели и поля, чтобы за один запуск исправить всё.

Пароль в targets.toml не хранится: у цели указано только имя переменной
окружения (password_env), сам пароль берётся из .env.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from monitor import mailcheck
from monitor.app import SAFE_HOST_RE, SAFE_PATH_RE, SAFE_USER_RE, SEND_TIME_RE, state_dir
from monitor.common import _env, _env_bool, _env_float, _env_int, _env_list
from monitor.messengers import NAMES

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - ветка зависит от версии Python
    import tomli as tomllib

STYLES = ('listing', 'backups', 'backups-sizes')
TYPES = ('ls', 'mail')       # ls - каталоги по ssh, mail - письма о бэкапах по IMAP
VIAS = ('pam', 'ssh')        # как заходить на ВМ: через PAM-шлюз или напрямую
WEEKDAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
RESERVED_COMMANDS = ('/targets', '/run', '/help')

_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,31}$')
_COMMAND_RE = re.compile(r'^/[a-z0-9_]{1,32}$')
_ENV_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_IMAP_USER_RE = re.compile(r'^[^\s"\\\x00-\x1f]{1,128}$')
_MAIL_FROM_RE = re.compile(r'^[^"\\\x00-\x1f]{0,128}$')
_FOLDER_RE = re.compile(r'^[^\x00-\x1f]{1,128}$')
_EXPECT_NAME_RE = re.compile(r'^[\w.\-]{1,64}$')

# Значения по умолчанию для [defaults]; ключи те же, что у цели.
DEFAULT_OPTIONS: dict[str, object] = {
    'prompt_timeout': 120,      # ждать приглашение shell после "Connecting to device"
    'connect_retries': 2,       # сколько раз пробовать сессию целиком
    'connect_retry_delay': 60,  # пауза между такими попытками, секунды
    'run_timeout': 900,         # сколько секунд проверка может идти до предупреждения в чат
    'stale_days': 0,            # 0 = не отмечать устаревшие файлы
    'style': 'backups',
    'sudo': False,
    'target_sel': 1,
    'via': 'pam',
    'ssh_port': 22,
    'imap_port': 993,
    'imap_ssl': True,
    'folders': ['INBOX'],
    'night_from': 18,
    'night_to': 12,
}

# Поля по типам. Лишнее поле у цели - ошибка: опечатка или копия не оттуда не должна
# молча игнорироваться.
_COMMON_KEYS = {'type', 'title', 'password_env', 'schedule', 'command', 'peers', 'run_timeout'}
_LS_KEYS = {
    'via', 'pam_user', 'user', 'target_host', 'target_sel', 'ssh_port', 'pam_host', 'pam_port',
    'paths', 'style', 'sudo', 'stale_days', 'prompt_timeout', 'connect_retries', 'connect_retry_delay',
}
_MAIL_KEYS = {
    'imap_host', 'imap_port', 'imap_user', 'imap_ssl', 'mail_from', 'folders', 'night_from',
    'night_to', 'expect', 'subject_re', 'ok_words', 'fail_words',
}
_TARGET_KEYS = _COMMON_KEYS | _LS_KEYS | _MAIL_KEYS
# Эти поля у каждой цели свои и в [defaults] не допускаются.
_IDENTITY_KEYS = ('type', 'title', 'command', 'password_env', 'pam_user', 'user', 'target_host', 'paths',
                  'peers', 'schedule', 'imap_user', 'expect', 'mail_from', 'subject_re')
_VIA_PAM_ONLY = ('pam_user', 'target_sel', 'pam_host', 'pam_port')
_VIA_SSH_ONLY = ('user', 'ssh_port')


@dataclass(frozen=True)
class Schedule:
    """Одно расписание: weekday=None - каждый день, иначе 'mon'..'sun'."""
    time: str
    weekday: str | None = None

    def __str__(self) -> str:
        return f'{self.weekday or "daily"} {self.time}'


@dataclass(frozen=True)
class Target:
    """Одна цель. Поля разбиты по типам: type='ls' - каталоги по ssh (via='pam' через шлюз,
    via='ssh' напрямую), type='mail' - письма о бэкапах по IMAP."""
    name: str
    title: str
    password_env: str = ''
    type: str = 'ls'
    # ls
    via: str = 'pam'
    pam_user: str = ''           # via='pam': учётка на шлюзе
    user: str = ''               # via='ssh': логин на ВМ
    target_host: str = ''
    target_sel: str = '1'
    ssh_port: int = 22
    pam_host: str = ''           # пусто - общий PAM_HOST из .env
    pam_port: str = ''
    paths: tuple[str, ...] = ()
    # mail
    imap_host: str = ''
    imap_port: int = 993
    imap_user: str = ''
    imap_ssl: bool = True
    mail_from: str = ''
    folders: tuple[str, ...] = ('INBOX',)
    night_from: int = 18
    night_to: int = 12
    expect: tuple[tuple[str, str], ...] = ()
    subject_re: str = ''
    ok_words: tuple[str, ...] = ()
    fail_words: tuple[str, ...] = ()
    # общее
    schedule: tuple[Schedule, ...] = ()
    command: str = ''
    sudo: bool = False
    peers: tuple[str, ...] = ()
    style: str = 'backups'
    stale_days: int = 0
    prompt_timeout: int = 120
    connect_retries: int = 2
    connect_retry_delay: int = 60
    run_timeout: int = 900


@dataclass(frozen=True)
class Settings:
    """Общее для всех целей. Секреты (token) в логи и сообщения не попадают."""
    pam_host: str = ''
    pam_port: str = '2222'
    messenger: str = 'vk'          # куда слать отчёты и откуда принимать команды: vk | telegram
    telegram_token: str = field(default='', repr=False)
    telegram_chat_id: int = 0
    telegram_api_url: str = 'https://api.telegram.org'
    vk_token: str = field(default='', repr=False)
    vk_group_id: int = 0
    vk_peer_id: int = 0
    allowed_users: frozenset[str] = frozenset()
    allow_any_user: bool = False   # явное разрешение команд всем в беседе
    extra_peers: tuple[str, ...] = ()
    strict_host_key: str = 'accept-new'
    kex: str = 'diffie-hellman-group14-sha1,diffie-hellman-group1-sha1'
    hostkey_algs: str = 'ssh-rsa'
    strict_algs: bool = False
    prompt_nudge: int = 10
    max_parallel: int = 2          # одновременных входов на шлюз
    login_gap: float = 5.0         # минимальная пауза между началами входов, секунды
    attach_delay: float = 1.5
    max_rows: int = 400
    keep_screenshots: int = 30
    state_dir: str = ''
    known_hosts: str = ''
    save_dir: str = ''

    @property
    def default_peer(self) -> int:
        """Беседа (чат) по умолчанию для выбранного мессенджера."""
        return self.telegram_chat_id if self.messenger == 'telegram' else self.vk_peer_id

    @classmethod
    def from_env(cls) -> Settings:
        """Читает os.environ (.env уже загружен через monitor.app.load_env)."""
        state = state_dir('monitor-hub')
        default = cls()
        return cls(
            pam_host=_env('PAM_HOST'),
            pam_port=_env('PAM_PORT', default.pam_port),
            messenger=_env('MESSENGER', default.messenger).lower(),
            telegram_token=_env('TELEGRAM_TOKEN'),
            telegram_chat_id=_env_int('TELEGRAM_CHAT_ID'),
            telegram_api_url=_env('TELEGRAM_API_URL', default.telegram_api_url),
            vk_token=_env('VK_TOKEN'),
            vk_group_id=_env_int('VK_GROUP_ID'),
            vk_peer_id=_env_int('VK_PEER_ID'),
            allowed_users=frozenset(_env_list('ALLOWED_USERS')),
            allow_any_user=_env_bool('ALLOW_ANY_USER', False),
            extra_peers=tuple(_env_list('EXTRA_PEERS')),
            strict_host_key=_env('SSH_STRICT_HOST_KEY', default.strict_host_key),
            kex=_env('SSH_KEX', default.kex),
            hostkey_algs=_env('SSH_HOSTKEY_ALGS', default.hostkey_algs),
            strict_algs=_env_bool('SSH_STRICT_ALGS', False),
            prompt_nudge=max(0, _env_int('PROMPT_NUDGE', default.prompt_nudge)),
            max_parallel=max(1, _env_int('MAX_PARALLEL_LOGINS', default.max_parallel)),
            login_gap=max(0.0, _env_float('GATEWAY_LOGIN_GAP', default.login_gap)),
            attach_delay=max(0.0, _env_float('VK_ATTACH_DELAY', default.attach_delay)),
            max_rows=max(20, _env_int('MAX_ROWS_PER_IMAGE', default.max_rows)),
            keep_screenshots=max(1, _env_int('KEEP_SCREENSHOTS', default.keep_screenshots)),
            state_dir=str(state),
            known_hosts=_env('SSH_KNOWN_HOSTS', str(state / 'known_hosts')),
            save_dir=_env('SAVE_DIR') or str(Path.home() / '.local' / 'share' / 'monitor-hub' / 'screenshots'),
        )


# Расписание

def parse_schedule(text: str) -> Schedule:
    """'daily 09:00' или 'wed 10:00'. ValueError с понятным текстом при ошибке."""
    parts = str(text).strip().lower().split()
    if len(parts) != 2:
        raise ValueError(f'ожидается "daily HH:MM" или "<день> HH:MM", получено {text!r}')
    day, hhmm = parts
    if day != 'daily' and day not in WEEKDAYS:
        raise ValueError(f'день {day!r}: допустимо daily или {", ".join(WEEKDAYS)}')
    if not SEND_TIME_RE.match(hhmm):
        raise ValueError(f'время {hhmm!r} должно быть в формате HH:MM')
    return Schedule(time=hhmm, weekday=None if day == 'daily' else day)


# targets.toml

def _as_int(value: object, label: str, problems: list[str], minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        problems.append(f'{label}: нужно целое число, получено {value!r}')
        return minimum
    if value < minimum:
        problems.append(f'{label}: не меньше {minimum}, получено {value}')
        return minimum
    return value


def _as_str_list(value: object, label: str, problems: list[str]) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    problems.append(f'{label}: нужна строка или список строк, получено {value!r}')
    return []


def _build_target(name: str, raw: Mapping[str, object], defaults: Mapping[str, object],
                  problems: list[str]) -> Target | None:
    where = f'[targets.{name}]'
    before = len(problems)

    if not _NAME_RE.match(name):
        problems.append(f'{where}: имя цели - латиница, цифры, - и _ (до 32 символов)')
    for key in raw:
        if key not in _TARGET_KEYS:
            problems.append(f'{where}: неизвестное поле {key!r}')

    merged: dict[str, object] = {**DEFAULT_OPTIONS, **defaults, **raw}

    kind = str(merged.get('type') or 'ls')
    if kind not in TYPES:
        problems.append(f'{where} type: допустимо {", ".join(TYPES)}, получено {kind!r}')
        return None
    via = str(merged.get('via') or 'pam')
    if kind == 'ls' and via not in VIAS:
        problems.append(f'{where} via: допустимо {", ".join(VIAS)}, получено {via!r}')
        return None

    allowed = _COMMON_KEYS | (_LS_KEYS if kind == 'ls' else _MAIL_KEYS)
    for key in raw:
        if key in _TARGET_KEYS and key not in allowed:
            problems.append(f'{where}: поле {key!r} не относится к type = "{kind}"')
        elif kind == 'ls' and via == 'ssh' and key in _VIA_PAM_ONLY:
            problems.append(f'{where}: поле {key!r} нужно только при via = "pam"')
        elif kind == 'ls' and via == 'pam' and key in _VIA_SSH_ONLY:
            problems.append(f'{where}: поле {key!r} нужно только при via = "ssh"')

    def text(key: str, safe_re: re.Pattern[str] | None = None, required: bool = True) -> str:
        value = merged.get(key)
        if value is None or value == '':
            if required:
                problems.append(f'{where} {key}: не задано')
            return ''
        value = str(value)
        if safe_re is not None and not safe_re.match(value):
            problems.append(f'{where} {key}: недопустимые символы: {value!r}')
        return value

    title = text('title', required=False) or name
    password_env = text('password_env')
    if password_env and not _ENV_NAME_RE.match(password_env):
        problems.append(f'{where} password_env: это имя переменной окружения, не пароль: {password_env!r}')

    fields: dict[str, Any]
    if kind == 'ls':
        fields = _ls_fields(where, merged, via, text, problems)
    else:
        fields = _mail_fields(where, merged, text, problems)

    schedule: list[Schedule] = []
    for item in _as_str_list(merged.get('schedule', []), f'{where} schedule', problems):
        try:
            schedule.append(parse_schedule(item))
        except ValueError as exc:
            problems.append(f'{where} schedule: {exc}')

    command = str(merged.get('command') or '')
    if command and not _COMMAND_RE.match(command):
        problems.append(f'{where} command: ожидается вид /name (латиница, цифры, _), получено {command!r}')
    if command in RESERVED_COMMANDS:
        problems.append(f'{where} command: {command} зарезервирована')

    peers = _as_str_list(merged.get('peers', []), f'{where} peers', problems)
    for peer in peers:
        if not re.fullmatch(r'-?\d+', peer):
            problems.append(f'{where} peers: {peer!r} не является числом')

    run_timeout = _as_int(merged.get('run_timeout'), f'{where} run_timeout', problems, 60)

    if len(problems) > before:
        return None
    return Target(
        name=name, title=title, password_env=password_env, type=kind,
        schedule=tuple(schedule), command=command, peers=tuple(peers), run_timeout=run_timeout,
        **fields,
    )


def _ls_fields(where: str, merged: Mapping[str, object], via: str, text, problems: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {'via': via, 'target_host': text('target_host', SAFE_HOST_RE)}
    if via == 'pam':
        out['pam_user'] = text('pam_user', SAFE_USER_RE)
        out['target_sel'] = text('target_sel', SAFE_HOST_RE)
        out['pam_host'] = text('pam_host', SAFE_HOST_RE, required=False)
        port = text('pam_port', required=False)
        if port and not (port.isdigit() and 1 <= int(port) <= 65535):
            problems.append(f'{where} pam_port: не номер порта: {port!r}')
        out['pam_port'] = port
    else:
        out['user'] = text('user', SAFE_USER_RE)
        ssh_port = _as_int(merged.get('ssh_port'), f'{where} ssh_port', problems, 1)
        if ssh_port > 65535:
            problems.append(f'{where} ssh_port: не номер порта: {ssh_port}')
        out['ssh_port'] = ssh_port

    paths = _as_str_list(merged.get('paths', []), f'{where} paths', problems)
    if not paths:
        problems.append(f'{where} paths: нужен хотя бы один путь')
    for path in paths:
        if not path.startswith('/') or not SAFE_PATH_RE.match(path):
            problems.append(f'{where} paths: нужен абсолютный путь без спецсимволов: {path!r}')
    out['paths'] = tuple(paths)

    style = str(merged.get('style'))
    if style not in STYLES:
        problems.append(f'{where} style: допустимо {", ".join(STYLES)}, получено {style!r}')
    out['style'] = style
    sudo = merged.get('sudo')
    if not isinstance(sudo, bool):
        problems.append(f'{where} sudo: нужно true или false, получено {sudo!r}')
        sudo = False
    out['sudo'] = sudo
    out['stale_days'] = _as_int(merged.get('stale_days'), f'{where} stale_days', problems)
    out['prompt_timeout'] = _as_int(merged.get('prompt_timeout'), f'{where} prompt_timeout', problems, 30)
    out['connect_retries'] = _as_int(merged.get('connect_retries'), f'{where} connect_retries', problems, 1)
    out['connect_retry_delay'] = _as_int(merged.get('connect_retry_delay'), f'{where} connect_retry_delay', problems)
    return out


def _mail_fields(where: str, merged: Mapping[str, object], text, problems: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {
        'imap_host': text('imap_host', SAFE_HOST_RE),
        'imap_user': text('imap_user', _IMAP_USER_RE),
        'mail_from': text('mail_from', _MAIL_FROM_RE, required=False),
    }
    port = _as_int(merged.get('imap_port'), f'{where} imap_port', problems, 1)
    if port > 65535:
        problems.append(f'{where} imap_port: не номер порта: {port}')
    out['imap_port'] = port
    ssl = merged.get('imap_ssl')
    if not isinstance(ssl, bool):
        problems.append(f'{where} imap_ssl: нужно true или false, получено {ssl!r}')
        ssl = True
    out['imap_ssl'] = ssl

    folders = _as_str_list(merged.get('folders', []), f'{where} folders', problems)
    if not folders:
        problems.append(f'{where} folders: нужна хотя бы одна папка')
    for folder in folders:
        if not _FOLDER_RE.match(folder):
            problems.append(f'{where} folders: недопустимое имя папки: {folder!r}')
    out['folders'] = tuple(folders)

    for key in ('night_from', 'night_to'):
        hour = _as_int(merged.get(key), f'{where} {key}', problems)
        if hour > 23:
            problems.append(f'{where} {key}: час 0-23, получено {hour}')
        out[key] = hour

    expect = merged.get('expect')
    pairs: list[tuple[str, str]] = []
    if not isinstance(expect, dict) or not expect:
        problems.append(f'{where} expect: нужна таблица "машина = правило" (например app-01 = "mon,tue")')
    else:
        for vm, rule in expect.items():
            if not _EXPECT_NAME_RE.match(str(vm)):
                problems.append(f'{where} expect: недопустимое имя машины {vm!r}')
            error = mailcheck.check_rule(str(rule)) if isinstance(rule, str) else 'нужна строка'
            if error:
                problems.append(f'{where} expect.{vm}: {error}')
            pairs.append((str(vm), str(rule)))
    out['expect'] = tuple(pairs)

    subject_re = text('subject_re', required=False)
    if subject_re:
        try:
            if re.compile(subject_re).groups < 1:
                problems.append(f'{where} subject_re: нужна группа ( ) с именем машины')
        except re.error as exc:
            problems.append(f'{where} subject_re: неверное выражение: {exc}')
    out['subject_re'] = subject_re
    for key in ('ok_words', 'fail_words'):
        words = _as_str_list(merged.get(key, []), f'{where} {key}', problems)
        out[key] = tuple(w for w in words if w)
    return out


def parse_targets(data: Mapping[str, object]) -> tuple[dict[str, Target], list[str]]:
    """Разобранный TOML -> (цели, проблемы). Цели с ошибками в результат не входят."""
    problems: list[str] = []
    for key in data:
        if key not in ('defaults', 'targets'):
            problems.append(f'неизвестный раздел {key!r} (ожидаются [defaults] и [targets.<имя>])')

    defaults = data.get('defaults', {})
    if not isinstance(defaults, dict):
        problems.append('[defaults]: ожидается таблица')
        defaults = {}
    for key in defaults:
        if key not in _TARGET_KEYS:
            problems.append(f'[defaults]: неизвестное поле {key!r}')
    for key in _IDENTITY_KEYS:
        if key in defaults:
            problems.append(f'[defaults] {key}: это поле задаётся только в цели')

    raw_targets = data.get('targets', {})
    if not isinstance(raw_targets, dict) or not raw_targets:
        problems.append('нет ни одной цели: добавьте раздел [targets.<имя>]')
        return {}, problems

    targets: dict[str, Target] = {}
    for name, raw in raw_targets.items():
        if not isinstance(raw, dict):
            problems.append(f'[targets.{name}]: ожидается таблица')
            continue
        target = _build_target(str(name), raw, defaults, problems)
        if target is not None:
            targets[target.name] = target

    seen: dict[str, str] = {}
    for target in targets.values():
        if target.command:
            if target.command in seen:
                problems.append(f'команда {target.command} задана у целей {seen[target.command]} и {target.name}')
            seen[target.command] = target.name
    return targets, problems


def load_targets(path: str | Path) -> tuple[dict[str, Target], list[str]]:
    """Читает targets.toml. Нет файла или битый TOML - тоже проблема, а не исключение."""
    try:
        with open(path, 'rb') as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return {}, [f'файл целей не найден: {path} (образец: targets.example.toml)']
    except tomllib.TOMLDecodeError as exc:
        return {}, [f'{path}: ошибка синтаксиса TOML: {exc}']
    except OSError as exc:
        return {}, [f'{path}: не удалось прочитать: {exc}']
    return parse_targets(data)


# Проверка вместе с окружением

def validate_environment(settings: Settings, targets: Mapping[str, Target], env: Mapping[str, str], *,
                         need_ssh: bool = True, need_chat: bool = True) -> list[str]:
    """Что нельзя проверить по одному targets.toml: шлюз, пароли, ВК. Без сети."""
    problems: list[str] = []
    if need_ssh:
        pam = [t for t in targets.values() if t.type == 'ls' and t.via == 'pam']
        if any(not t.pam_host for t in pam):
            if not settings.pam_host:
                problems.append('PAM_HOST не задан (или укажите pam_host у цели)')
            elif not SAFE_HOST_RE.match(settings.pam_host):
                problems.append(f'PAM_HOST содержит недопустимые символы: {settings.pam_host!r}')
        port = settings.pam_port
        if any(not t.pam_port for t in pam) and not (port.isdigit() and 1 <= int(port) <= 65535):
            problems.append(f'PAM_PORT не является номером порта: {port!r}')
        if any(t.type == 'ls' for t in targets.values()) and \
                settings.strict_host_key not in ('yes', 'no', 'accept-new'):
            problems.append(
                f'SSH_STRICT_HOST_KEY: ожидается yes|no|accept-new, получено {settings.strict_host_key!r}')
        for target in targets.values():
            if not env.get(target.password_env):
                problems.append(
                    f'[targets.{target.name}]: пароль не найден, переменная {target.password_env} '
                    'не задана в .env')
    if need_chat:
        if settings.messenger not in NAMES:
            problems.append(f'MESSENGER: допустимо {", ".join(NAMES)}, получено {settings.messenger!r}')
        elif settings.messenger == 'telegram':
            if not settings.telegram_token:
                problems.append('TELEGRAM_TOKEN не задан')
            if not settings.telegram_chat_id:
                problems.append('TELEGRAM_CHAT_ID не задан')
        else:
            if not settings.vk_token:
                problems.append('VK_TOKEN не задан')
            if not settings.vk_peer_id:
                problems.append('VK_PEER_ID не задан')
        for extra in settings.extra_peers:
            if not re.fullmatch(r'-?\d+', extra):
                problems.append(f'EXTRA_PEERS: {extra!r} не является числом')
    return problems


# Запись targets.toml (для интерфейса)

_FIELD_ORDER = (
    'type', 'title', 'via', 'pam_user', 'user', 'password_env', 'target_host', 'target_sel', 'ssh_port',
    'pam_host', 'pam_port', 'paths', 'style', 'sudo', 'stale_days', 'imap_host', 'imap_port', 'imap_user',
    'imap_ssl', 'mail_from', 'folders', 'night_from', 'night_to', 'subject_re', 'ok_words', 'fail_words',
    'schedule', 'command', 'peers', 'prompt_timeout', 'connect_retries', 'connect_retry_delay', 'run_timeout',
)


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return '[' + ', '.join(_toml_value(v) for v in value) + ']'
    text = str(value).replace('\\', '\\\\').replace('"', '\\"')
    return f'"{text}"'


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r'[A-Za-z0-9_-]+', key) else _toml_value(key)


def target_to_dict(target: Target, defaults: Mapping[str, object] | None = None) -> dict[str, object]:
    """Поля цели для записи: нужные её типу и не совпадающие с [defaults]."""
    base = {**DEFAULT_OPTIONS, **(defaults or {})}
    raw: dict[str, object] = {
        'type': target.type, 'title': target.title, 'password_env': target.password_env,
        'via': target.via, 'pam_user': target.pam_user, 'user': target.user,
        'target_host': target.target_host,
        'target_sel': int(target.target_sel) if target.target_sel.isdigit() else target.target_sel,
        'ssh_port': target.ssh_port, 'pam_host': target.pam_host, 'pam_port': target.pam_port,
        'paths': list(target.paths), 'style': target.style, 'sudo': target.sudo,
        'stale_days': target.stale_days, 'imap_host': target.imap_host, 'imap_port': target.imap_port,
        'imap_user': target.imap_user, 'imap_ssl': target.imap_ssl, 'mail_from': target.mail_from,
        'folders': list(target.folders), 'night_from': target.night_from, 'night_to': target.night_to,
        'subject_re': target.subject_re, 'ok_words': list(target.ok_words), 'fail_words': list(target.fail_words),
        'schedule': [str(s) for s in target.schedule], 'command': target.command,
        'peers': list(target.peers), 'prompt_timeout': target.prompt_timeout,
        'connect_retries': target.connect_retries, 'connect_retry_delay': target.connect_retry_delay,
        'run_timeout': target.run_timeout,
    }
    allowed = _COMMON_KEYS | (_LS_KEYS if target.type == 'ls' else _MAIL_KEYS)
    if target.type == 'ls':
        allowed -= set(_VIA_SSH_ONLY if target.via == 'pam' else _VIA_PAM_ONLY)
    out: dict[str, object] = {}
    for key in _FIELD_ORDER:
        if key not in allowed or key not in raw:
            continue
        value = raw[key]
        if key == 'type' and value == 'ls':
            continue
        if key == 'via' and value == 'pam':
            continue
        if key in base and base[key] == value:
            continue
        if value in ('', [], ()):
            continue
        if key == 'title' and value == target.name:
            continue
        if key == 'schedule' and isinstance(value, list) and len(value) == 1:
            value = value[0]
        out[key] = value
    return out


def dump_targets(targets: Mapping[str, Target], defaults: Mapping[str, object] | None = None) -> str:
    lines = ['# Цели monitor-hub. Пароли здесь не хранятся: только имя переменной из .env.', '']
    if defaults:
        lines.append('[defaults]')
        lines += [f'{k} = {_toml_value(v)}' for k, v in defaults.items()]
        lines.append('')
    for target in targets.values():
        lines.append(f'[targets.{target.name}]')
        lines += [f'{k} = {_toml_value(v)}' for k, v in target_to_dict(target, defaults).items()]
        lines.append('')
        if target.expect:
            lines.append(f'[targets.{target.name}.expect]')
            lines += [f'{_toml_key(vm)} = {_toml_value(rule)}' for vm, rule in target.expect]
            lines.append('')
    return '\n'.join(lines)


def read_defaults(path: str | Path) -> dict[str, object]:
    try:
        with open(path, 'rb') as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    defaults = data.get('defaults', {})
    return dict(defaults) if isinstance(defaults, dict) else {}


def save_targets(path: str | Path, targets: Mapping[str, Target],
                 defaults: Mapping[str, object] | None = None) -> Path | None:
    """Атомарная запись: временный файл + rename. Предыдущая версия -> <файл>.bak.
    Перед записью результат разбирается заново: битый файл не сохраняется."""
    text = dump_targets(targets, defaults)
    parsed, problems = parse_targets(tomllib.loads(text))
    if problems or set(parsed) != set(targets):
        raise ValueError('; '.join(problems) or 'цели не совпали после записи')
    from monitor.envfile import atomic_write
    return atomic_write(Path(path), text, mode=0o640)

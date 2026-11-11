"""
app.py - общий каркас: .env, каталог состояния, логи, маскирование секретов,
ошибки с двумя текстами (для лога и для беседы) и схема одной проверки
(собрать -> сохранить картинки -> отправить).
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import stat
import time
from collections.abc import Callable, Iterable
from pathlib import Path

# Настройки: .env, каталоги
_ENV_LINE_RE = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$')


def unquote(value: str) -> str:
    """Снимает парные кавычки вокруг значения из .env: VK_TOKEN="vk1.a.xxx"
    не должен уйти в API вместе с кавычками."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def load_env(script_dir: str) -> str | None:
    """Читает первый найденный .env (рядом со скриптом или на уровень выше).

    Поддерживает 'export KEY=value' и кавычки. Переменные, уже выставленные в
    окружении (systemd, обёртки), важнее файла. Возвращает путь к прочитанному
    файлу или None."""
    for candidate in (
        os.path.join(script_dir, '.env'),
        os.path.join(script_dir, '..', '.env'),
    ):
        path = os.path.normpath(candidate)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding='utf-8') as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    m = _ENV_LINE_RE.match(line)
                    if not m:
                        continue
                    k, v = m.group(1), unquote(m.group(2))
                    if k and k not in os.environ:
                        os.environ[k] = v
        except OSError:
            continue
        return path
    return None


def state_dir(app_name: str) -> Path:
    """Каталог для PID/лога/known_hosts.

    Не /tmp: предсказуемое имя в общедоступном каталоге позволяло любому
    локальному пользователю заранее подложить симлинк, и запись PID-файла
    или дозапись лога уходила по нему - перезапись чужого файла от нашего
    имени. Здесь каталог создаётся с правами 0700 в домашнем каталоге."""
    base = os.environ.get('STATE_DIR') or os.environ.get('XDG_STATE_HOME')
    if os.environ.get('STATE_DIR'):
        path = Path(os.environ['STATE_DIR'])
    elif base:
        path = Path(base) / app_name
    else:
        path = Path.home() / '.local' / 'state' / app_name
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


# Логи и секреты
_FILE_FMT = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', '%Y-%m-%d %H:%M:%S')
_CON_FMT = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', '%H:%M:%S')


def setup_logging(name: str, log_file: str) -> tuple[logging.Logger, logging.Handler]:
    """Логгер с ротируемым файлом и консолью. Возвращает (логгер, консольный
    обработчик): его снимают перед уходом в фон (detach_console_logging)."""
    log = logging.getLogger(name)
    log.setLevel(logging.INFO)
    log.propagate = False

    # С ротацией: демон работает месяцами, лог не должен расти бесконечно.
    try:
        file_handler: logging.Handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8'
        )
    except OSError:
        file_handler = logging.NullHandler()
    file_handler.setFormatter(_FILE_FMT)
    log.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(_CON_FMT)
    log.addHandler(console_handler)
    return log, console_handler


def detach_console_logging(log: logging.Logger, console_handler: logging.Handler) -> None:
    """Снимает консольный обработчик перед уходом в фон.

    Он захватывает объект sys.stderr в момент создания, поэтому после
    демонизации продолжал бы писать в дескриптор уже закрытого терминала."""
    log.removeHandler(console_handler)


def mask_secrets(text: str, secrets: Iterable[str]) -> str:
    """Пароль PAM и токен ВК не должны попадать ни в лог, ни в сообщение."""
    for secret in secrets:
        if secret and len(secret) >= 4:
            # repr() байтов и строк экранирует кириллицу и обратную косую: прячем и такие записи
            forms = {secret, repr(secret.encode())[2:-1], repr(secret)[1:-1], json.dumps(secret)[1:-1]}
            for form in sorted(forms, key=len, reverse=True):
                text = text.replace(form, '***')
    return text


# Ошибки
class MonitorError(RuntimeError):
    """Ошибка с двумя представлениями: подробным для лога и коротким для чата.

    В тексте исключения бывает сырой вывод jump-хоста (баннер, имя узла,
    меню PAM) - в беседу он не должен попадать, только в лог.

    retryable=True означает "сорвалось на установлении сессии": такую
    попытку имеет смысл повторить целиком, потому что причина обычно
    временная (цель дольше обычного отдаёт shell, занят слот сессий)."""

    def __init__(self, detail: str, user_message: str, retryable: bool = False) -> None:
        super().__init__(detail)
        self.user_message = user_message
        self.retryable = retryable


def user_facing(exc: BaseException, error_cls: type[MonitorError], default_message: str) -> str:
    if isinstance(exc, error_cls):
        return exc.user_message
    return default_message


# Проверка конфигурации
SAFE_HOST_RE = re.compile(r'^[A-Za-z0-9._][A-Za-z0-9._-]*$')     # не с '-': ssh принял бы за опцию
SAFE_USER_RE = re.compile(r'^[A-Za-z0-9._@][A-Za-z0-9._@-]*$')
SAFE_PATH_RE = re.compile(r'^[A-Za-z0-9 _./~-]+$')
SEND_TIME_RE = re.compile(r'^([01]\d|2[0-3]):([0-5]\d)$')


def warn_on_insecure_env_file(log: logging.Logger, env_path: str | None, secrets_note: str) -> None:
    if not env_path:
        return
    try:
        mode = os.stat(env_path).st_mode
    except OSError:
        return
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        log.warning(
            '%s доступен группе/остальным (права %o). Внутри ' + secrets_note + '. '
            'Исправьте: chmod 600 %s', env_path, stat.S_IMODE(mode), env_path
        )


# Проверка: сбор -> скриншоты -> отправка
def run_check(*, log, title: str, error_title: str, target_peer_id: int | None,
              open_after: bool, default_peer: int,
              collect: Callable[[], tuple[list, Callable[[], str]]],
              save_screenshots: Callable, open_screenshot: Callable,
              send_photos: Callable, send_message: Callable,
              user_facing: Callable[[BaseException], str],
              mask_secrets: Callable[[str], str], extra_peers: list[str]) -> None:
    """target_peer_id=None означает плановую рассылку (тогда работают
    extra_peers). open_after - открыть картинку в просмотрщике после проверки.

    collect() возвращает (картинки, функция подписи): подпись считается
    после сохранения скриншотов, перед отправкой."""
    log.info(title)
    peer = target_peer_id or default_peer
    try:
        buffers, make_caption = collect()
        saved = save_screenshots(buffers)

        if open_after and saved:
            open_screenshot(saved[0])

        caption = make_caption()

        try:
            send_photos(saved, caption, peer)
        except Exception as exc:
            log.error('Не удалось отправить фото: %s', exc)
            send_message(peer, user_facing(exc))

        if target_peer_id is None:
            for extra in extra_peers:
                try:
                    peer_int = int(extra)
                except ValueError:
                    log.error('EXTRA_PEERS: %r не является числом - пропускаю', extra)
                    continue
                try:
                    time.sleep(3)
                    send_photos(saved, caption, peer_int)
                except Exception as exc:
                    log.warning('Не удалось отправить доп. получателю %s: %s', extra, exc)

    except Exception as exc:
        log.error('Ошибка: %s', mask_secrets(str(exc)), exc_info=True)
        send_message(peer, f'{error_title}: {user_facing(exc)}')

    log.info('=== Готово ===')

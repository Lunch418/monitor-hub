"""
pam_session.py - вход на целевую машину через PAM jump-хост (ssh + pexpect).

Сценарий: пароль PAM -> меню -> IP цели -> выбор пользователя -> приглашение
shell. Команды выполняет вызывающий внутри `with session.login(px) as sh:`.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PamConfig:
    """Параметры входа. strict_algs=True: KexAlgorithms/HostKeyAlgorithms
    ЗАМЕНЯЮТ дефолты ssh; False - добавляются к ним (префикс '+')."""
    host: str
    port: object
    user: str
    password: str
    target_host: str
    target_sel: str
    strict_host_key: str
    known_hosts: str
    kex: str
    hostkey_algs: str
    strict_algs: bool
    prompt_timeout: int
    prompt_nudge: int
    connect_retries: int
    connect_retry_delay: int
    # Строка для лога в начале сессии; три %s: хост, порт, проверка ключа хоста.
    connect_message: str = 'SSH -> PAM %s:%s (проверка ключа хоста: %s)'


PROMPT_PATTERNS = [b'\\$ ', b'# ', b'> ', b'% ', b'] ', b'~\\$', b'~#']


def ssh_argv(cfg: PamConfig) -> list[str]:
    """argv списком, а не одной строкой.

    pexpect разбивает строку на аргументы сам, поэтому значение переменной
    окружения могло превратиться в дополнительную опцию ssh. Со списком
    каждый элемент гарантированно остаётся одним аргументом.

    Если strict_algs=False, KexAlgorithms/HostKeyAlgorithms ДОБАВЛЯЮТСЯ к
    дефолтам ssh (префикс '+'), а не заменяют их - иначе на свежем OpenSSH,
    где нет общего алгоритма из перечисленных, коннект падает с 'no matching
    kex/host key algorithm found'."""
    known_hosts = cfg.known_hosts if cfg.strict_host_key != 'no' else '/dev/null'
    if cfg.strict_host_key != 'no':
        try:
            Path(known_hosts).parent.mkdir(parents=True, exist_ok=True)
            Path(known_hosts).touch(exist_ok=True)
            os.chmod(known_hosts, 0o600)
        except OSError:
            pass
    prefix = '' if cfg.strict_algs else '+'
    return [
        'ssh', '-tt',
        '-p', str(cfg.port),
        '-F', '/dev/null',
        '-o', f'StrictHostKeyChecking={cfg.strict_host_key}',
        '-o', f'UserKnownHostsFile={known_hosts}',
        # 30 x 4 = 120 с молчания до разрыва: дольше, чем ожидание приглашения
        # shell, чтобы наш таймаут не путался с разрывом со стороны ssh.
        '-o', 'ServerAliveInterval=30',
        '-o', 'ServerAliveCountMax=4',
        '-o', 'ConnectTimeout=20',
        '-o', f'KexAlgorithms={prefix}{cfg.kex}',
        '-o', f'HostKeyAlgorithms={prefix}{cfg.hostkey_algs}',
        '-o', f'PubkeyAcceptedAlgorithms=+{cfg.hostkey_algs}',
        '-o', 'PubkeyAuthentication=no',
        '-o', 'NumberOfPasswordPrompts=1',
        f'{cfg.user}@{cfg.host}',
    ]


def close_child(child) -> None:
    """Закрытие в фиксированном порядке, без os.kill по сохранённому pid.

    После close(force=True) pid ребёнка уже может достаться чужому процессу,
    поэтому сигналы по сохранённому pid не шлём."""
    try:
        child.send(b'exit\r')
        time.sleep(0.5)
    except Exception:
        pass
    try:
        child.close(force=True)
    except Exception:
        pass


class PamShell:
    """Открытая сессия на целевой машине: отправка команд и ожидание вывода."""

    def __init__(self, child, px, cfg: PamConfig, log, error_cls,
                 mask_secrets: Callable[[str], str]) -> None:
        self.child = child
        self.px = px
        self.cfg = cfg
        self.log = log
        self.error_cls = error_cls
        self.mask_secrets = mask_secrets

    def expect_any(self, patterns: list[bytes], stage: str, timeout: int = 30,
                   retryable: bool = True) -> int:
        px = self.px
        idx = self.child.expect([*patterns, px.TIMEOUT, px.EOF], timeout=timeout)
        if idx >= len(patterns):
            reason = 'TIMEOUT' if idx == len(patterns) else 'EOF'
            ctx = self.mask_secrets(repr((self.child.before or b'')[-400:]))
            raise self.error_cls(
                f'{stage}: {reason} | {ctx}',
                f'Не удалось пройти этап "{stage}" на jump-хосте',
                retryable=retryable,
            )
        return idx

    def send_line(self, data: str) -> None:
        self.child.send(data.encode() + b'\r')

    def send_command(self, command: str) -> None:
        self.child.send(command.encode() + b'\r')

    def wait_prompt(self) -> None:
        """Ждёт приглашение shell; если его не видно - нажимает Enter."""
        px, child, cfg = self.px, self.child, self.cfg
        patterns = PROMPT_PATTERNS
        deadline = time.monotonic() + cfg.prompt_timeout
        while cfg.prompt_nudge > 0 and deadline - time.monotonic() > cfg.prompt_nudge:
            idx = child.expect([*patterns, px.TIMEOUT, px.EOF], timeout=cfg.prompt_nudge)
            if idx < len(patterns):
                return
            if idx > len(patterns):
                break
            self.log.info('Приглашение не видно, нажимаю Enter')
            child.send(b'\r')
        self.expect_any(patterns, 'shell prompt',
                        timeout=max(1, int(deadline - time.monotonic())))


class PamSession:
    def __init__(self, cfg: PamConfig, *, log, error_cls,
                 mask_secrets: Callable[[str], str]) -> None:
        self.cfg = cfg
        self.log = log
        self.error_cls = error_cls
        self.mask_secrets = mask_secrets

    @contextmanager
    def login(self, px):
        """Вход до приглашения shell; по выходу из with сессия закрывается."""
        cfg, log = self.cfg, self.log
        log.info(cfg.connect_message, cfg.host, cfg.port, cfg.strict_host_key)

        argv = ssh_argv(cfg)
        try:
            child = px.spawn(argv[0], argv[1:], timeout=180, encoding=None)
        except px.ExceptionPexpect as exc:
            raise self.error_cls(
                f'не удалось запустить ssh: {exc}',
                'Не удалось подключиться к jump-хосту',
                retryable=True,
            ) from exc
        child.setwinsize(50, 220)

        sh = PamShell(child, px, cfg, log, self.error_cls, self.mask_secrets)
        try:
            sh.expect_any([b'[Pp]assword:'], 'PAM password', timeout=25)
            sh.send_line(cfg.password)
            log.info('PAM: авторизация отправлена')

            sh.expect_any([b'select one:'], 'PAM menu', timeout=30)
            sh.send_line(cfg.target_host)
            log.info('PAM: введён IP %s', cfg.target_host)

            sh.expect_any([b'[Tt]ype one of', b'[Uu]sername', b'Connecting'],
                          'user select', timeout=40)
            sh.send_line(cfg.target_sel)
            log.info('PAM: выбран пользователь %s', cfg.target_sel)

            sh.wait_prompt()
            log.info('Shell получен на %s', cfg.target_host)

            time.sleep(0.3)
            try:
                child.read_nonblocking(4096, timeout=1)
            except Exception:
                pass

            yield sh
        finally:
            close_child(child)

    def fetch(self, once: Callable, *, validate_config: Callable[[], list[str]],
              config_error_cls):
        """once(px) с повтором ВСЕЙ сессии, если она сорвалась на установлении
        соединения.

        Изредка PAM печатает 'Connecting to device "..."' и замолкает: целевой
        хост в этот момент отдаёт shell дольше обычного (логин через LDAP/SSSD,
        нагрузка, занятый слот сессий). Одна такая минута стоила всего отчёта -
        в беседу уходила ошибка, хотя следующая попытка прошла бы. Повторяется
        именно вся сессия: после срыва PTY в неизвестном состоянии."""
        cfg, log = self.cfg, self.log
        try:
            import pexpect as px
        except ImportError as exc:
            raise self.error_cls(
                f'pexpect не установлен: {exc}',
                'На сервере не установлен pexpect - обратитесь к администратору',
            ) from exc

        problems = validate_config()
        if problems:
            raise config_error_cls(
                'Ошибки конфигурации: ' + '; '.join(problems),
                'Инструмент настроен неверно (подробности в логе)',
            )

        for attempt in range(1, cfg.connect_retries + 1):
            try:
                return once(px)
            except self.error_cls as exc:
                if not exc.retryable or attempt >= cfg.connect_retries:
                    raise
                log.warning(
                    'Сессия PAM сорвалась (попытка %d из %d): %s - повтор через %d с',
                    attempt, cfg.connect_retries, self.mask_secrets(str(exc)),
                    cfg.connect_retry_delay,
                )
                time.sleep(cfg.connect_retry_delay)

        raise AssertionError('недостижимо')   # цикл либо вернул, либо бросил

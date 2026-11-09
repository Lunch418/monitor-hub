"""
Поддельный PAM jump-хост для сквозного теста.

Настоящий SSH-сервер (paramiko) с тем же диалогом, что у реального шлюза:
пароль -> меню "select one:" -> выбор пользователя -> приглашение shell.
В shell понимает ровно то, что шлёт monitor-hub: printf с маркерами, ls -la
(в том числе через sudo -n env), echo и exit.

Режимы (для проверки устойчивости):
  prompt_needs_enter  приглашение shell появляется только после Enter
  drop_first          первое соединение рвётся сразу после входа
"""

from __future__ import annotations

import re
import shlex
import socket
import threading
import time

import paramiko

LISTING = (
    'total 12\r\n'
    'drwxr-xr-x  2 root root 4096 Oct 19 06:00 .\r\n'
    'drwxr-xr-x 18 root root 4096 Sep  5  2025 ..\r\n'
    '-rw-r--r--  1 root root 2085 Oct 19 06:54 issuing-ca.crl\r\n'
    '-rw-r--r--  1 root root 9061 Oct 18 07:33 root-ca.crl\r\n'
    '-rwxr-xr-x  1 root root 3505 Oct 10 10:12 update-crl.sh\r\n'
    'drwx------  2 postgres postgres 4096 Oct 19 02:00 backup_0\r\n'
)

HOST_KEY = paramiko.RSAKey.generate(2048)


class _Auth(paramiko.ServerInterface):
    def __init__(self, user, password):
        self.user, self.password = user, password
        self.shell_requested = threading.Event()

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == 'session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def get_allowed_auths(self, username):
        return 'password'

    def check_auth_password(self, username, password):
        ok = username == self.user and password == self.password
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def check_channel_pty_request(self, channel, term, width, height, pw, ph, modes):
        return True

    def check_channel_shell_request(self, channel):
        self.shell_requested.set()
        return True


def _readline(chan) -> str | None:
    """Читает строку до Enter, эхом возвращая символы (как терминал)."""
    buf = b''
    while True:
        chunk = chan.recv(1)
        if not chunk:
            return None
        if chunk in (b'\r', b'\n'):
            chan.send(b'\r\n')
            return buf.decode('utf-8', 'replace')
        chan.send(chunk)
        buf += chunk


def _probe(line: str, chan, log: list, states) -> str:
    """Проверка пути (if [ ! -e ... fi): печатает вердикт, возвращает остаток строки.

    states: {путь: 'ok' | 'noent' | 'notdir' | 'denied' | 'sudo'}, по умолчанию ok."""
    head, rest = line.split(' fi;', 1)
    path = shlex.split(head.split(';', 1)[0])[4]
    code = (states or {}).get(path, 'ok')
    log.append(('probe', path))
    count = 3 if code in ('ok', 'sudo') else 0
    chan.send(f'__HUBV_{code}__ {count}\r\n'.encode())
    return rest


def _run_command(line: str, chan, log: list, listings=None, denied=(), states=None) -> bool:
    """Выполняет строку из команд через ';'. False - выход из shell."""
    if line.startswith('if [ ! -e '):
        line = _probe(line, chan, log, states)
    for part in line.split(';'):
        part = part.strip()
        if not part:
            continue
        if part == 'exit':
            return False
        m = re.match(r"printf '([^']*)' (\S+)$", part)
        if m:
            text = m.group(1).replace('%s', m.group(2)).replace('\\n', '\r\n')
            chan.send(text.encode())
            continue
        m = re.match(r"echo '([^']*)'$", part)
        if m:
            chan.send(m.group(1).encode() + b'\r\n')
            continue
        words = shlex.split(part)
        if words and words[0] == 'top':
            log.append(('top',))
            chan.send(b'top - 12:00:01 up 3 days,  1 user,  load average: 0.10\r\n  PID USER  %CPU COMMAND\r\n')
            continue
        if 'ls' in words:
            path = words[-1]
            log.append(('ls', path, 'sudo' in words))
            if path in denied:
                chan.send(f"ls: cannot open directory '{path}': Permission denied\r\n".encode())
            else:
                chan.send((listings or {}).get(path, LISTING).encode())
            continue
        chan.send(f'bash: {part}: command not found\r\n'.encode())
    return True


def _serve_connection(sock, user, password, mode, conn_number, events, listings=None, denied=(), states=None):
    transport = paramiko.Transport(sock)
    transport.add_server_key(HOST_KEY)
    auth = _Auth(user, password)
    try:
        transport.start_server(server=auth)
        chan = transport.accept(20)
        if chan is None or not auth.shell_requested.wait(10):
            return
        events.append(('login', conn_number))
        if mode.get('drop_first') and conn_number == 1:
            chan.close()
            return
        if not mode.get('direct'):       # direct: сразу машина, без меню шлюза
            chan.send(b'Welcome to the gateway\r\nselect one:\r\n')
            target = _readline(chan)
            events.append(('target', target))
            chan.send(b'Type one of the users: 1\r\n')
            selection = _readline(chan)
            events.append(('selection', selection))
            chan.send(b'Connecting to device "target"\r\n')
            time.sleep(0.2)
        prompt = b'user@target:~$ '
        if mode.get('prompt_needs_enter'):
            events.append(('waiting_enter', None))
            if _readline(chan) is None:
                return
            events.append(('enter_received', None))
        chan.send(prompt)
        while True:
            line = _readline(chan)
            if line is None:
                return
            events.append(('command', line))
            if not _run_command(line, chan, events, listings, denied, states):
                return
            chan.send(prompt)
    except (EOFError, OSError, paramiko.SSHException):
        pass
    finally:
        transport.close()


class FakePamServer:
    """Слушает 127.0.0.1 на свободном порту; events - что видел сервер."""

    def __init__(self, user='pamuser', password='pampass', listings=None, denied=(), states=None, **mode):
        self.user, self.password, self.mode = user, password, mode
        self.states = states
        self.listings, self.denied = listings, tuple(denied)
        self.events: list = []
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(('127.0.0.1', 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        self._count = 0
        self._stop = False
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self):
        while not self._stop:
            try:
                client, _ = self._sock.accept()
            except OSError:
                return
            self._count += 1
            threading.Thread(
                target=_serve_connection,
                args=(client, self.user, self.password, self.mode, self._count, self.events,
                      self.listings, self.denied, self.states),
                daemon=True,
            ).start()

    def close(self):
        self._stop = True
        self._sock.close()

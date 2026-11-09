"""Цель mail и мессенджер Telegram как настоящие процессы: поддельные IMAP, Telegram и PAM."""
import os
import shutil
import signal
import subprocess
import sys
import time
from email.message import EmailMessage
from pathlib import Path

import pytest

from tests.fakes.imap_server import FakeImap
from tests.fakes.pam_server import FakePamServer
from tests.fakes.telegram_server import FakeTelegram

ROOT = Path(__file__).resolve().parent.parent
FAKES = Path(__file__).resolve().parent / 'fakes'

pytestmark = pytest.mark.skipif(shutil.which('ssh') is None, reason='нужен openssh-client')

TOML = '''
[targets.rk]
type = "mail"
title = "Backups"
password_env = "MAIL_PW"
imap_host = "127.0.0.1"
imap_port = {imap_port}
imap_ssl = false
imap_user = "mailuser"
mail_from = "backup"
command = "/rk"
schedule = "daily 09:10"

[targets.rk.expect]
app-01 = "mon"
db-01 = "mon,tue"
web-01 = "mon"

[targets.web]
title = "Web"
pam_user = "pamuser"
password_env = "WEB_PW"
target_host = "10.0.0.1"
paths = ["/srv/crl"]
style = "listing"
command = "/web"
connect_retry_delay = 0
'''


def letter(subject, finish):
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = 'backup@example.org'
    msg['Date'] = 'Tue, 10 Nov 2026 03:00:00 +0000'
    msg.set_content(f'Время завершения {finish} Продолжительность 00:12:34')
    return msg.as_bytes()


LETTERS = [
    letter('Backup completed successfully: app-01 (x)', 'Tue Nov 10 03:10:00 2026'),
    letter('Backup failed: db-01 (x)', 'Tue Nov 10 04:00:00 2026'),
]


@pytest.fixture
def stack(tmp_path):
    imap = FakeImap(LETTERS)
    gateway = FakePamServer('pamuser', 'pampass')
    telegram = FakeTelegram()
    (tmp_path / 'targets.toml').write_text(TOML.format(imap_port=imap.port), encoding='utf-8')
    env = {
        'PATH': os.environ['PATH'], 'HOME': str(tmp_path),
        'PYTHONPATH': os.pathsep.join([str(FAKES), str(ROOT)]),
        'STATE_DIR': str(tmp_path / 'state'), 'SAVE_DIR': str(tmp_path / 'shots'),
        'TARGETS_FILE': str(tmp_path / 'targets.toml'),
        'PAM_HOST': '127.0.0.1', 'PAM_PORT': str(gateway.port), 'WEB_PW': 'pampass', 'MAIL_PW': 'mailpass',
        'SSH_STRICT_HOST_KEY': 'no', 'SSH_KEX': 'ecdh-sha2-nistp256', 'SSH_HOSTKEY_ALGS': 'rsa-sha2-256',
        'SSH_STRICT_ALGS': '1', 'PROMPT_NUDGE': '1', 'GATEWAY_LOGIN_GAP': '0',
        'MESSENGER': 'telegram', 'TELEGRAM_TOKEN': telegram.token, 'TELEGRAM_CHAT_ID': '-1001',
        'TELEGRAM_API_URL': telegram.url, 'ALLOWED_USERS': '7', 'NO_OPEN_SCREENSHOT': '1',
    }

    def run(*args, **extra):
        return subprocess.run([sys.executable, str(ROOT / 'monitor-hub'), *args], env={**env, **extra},
                              cwd=tmp_path, capture_output=True, text=True, timeout=120)

    yield run, env, tmp_path, imap, telegram
    imap.close()
    gateway.close()
    telegram.close()


def test_mail_check_prints_and_sends_to_telegram(stack):
    run, _, tmp, _imap, telegram = stack
    done = run('check', 'rk', '--date', '10.11.2026')
    assert done.returncode == 0, done.stdout + done.stderr
    assert '[ok] app-01 (в 03:10, за 12:34)' in done.stdout
    assert '[!] db-01 - ошибка' in done.stdout and '[?] web-01 - письма нет' in done.stdout
    text = telegram.methods('sendMessage')[0]['fields']
    assert text['chat_id'] == '-1001' and '[ok] app-01' in text['text']
    assert 'Отправлено: Telegram' in done.stdout
    assert list((tmp / 'shots' / 'mail' / 'rk').glob('*/app-01_ok.eml'))
    assert 'mailpass' not in done.stdout + done.stderr


def test_mail_check_no_send_touches_nothing(stack):
    run, _, _, _, telegram = stack
    done = run('check', 'rk', '--no-send', '--date', 'вчера')
    assert done.returncode == 0, done.stderr
    assert telegram.methods('sendMessage') == []


def test_mail_wrong_password_is_clean(stack):
    run, *_ = stack
    done = run('check', 'rk', '--no-send', '--date', '10.11.2026', MAIL_PW='wrong-secret')
    assert done.returncode == 1
    assert 'Traceback' not in done.stderr and 'wrong-secret' not in done.stdout + done.stderr


def test_list_and_status_show_mail_target(stack):
    run, *_ = stack
    done = run('list')
    assert 'rk' in done.stdout and 'письма: 3 маш.' in done.stdout
    assert run('check-paths', 'rk').returncode == 2


def test_ls_command_prints_listing(stack):
    run, *_ = stack
    done = run('ls', 'web', '/srv/crl')
    assert done.returncode == 0, done.stderr
    assert 'issuing-ca.crl' in done.stdout
    bad = run('ls', 'web', '/srv/x;rm')
    assert bad.returncode == 2 and 'Путь не подходит' in bad.stderr
    assert run('ls', 'rk', '/srv').returncode == 2


def test_daemon_over_telegram(stack):
    _, env, tmp, _, telegram = stack
    proc = subprocess.Popen([sys.executable, str(ROOT / 'monitor-hub'), 'run'], env=env, cwd=tmp,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and len(telegram.methods('getUpdates')) < 2:
            time.sleep(0.1)                     # демон прочитал старое и ждёт новое
        telegram.push(-1001, 99, '/rk 10.11')        # чужой
        telegram.push(-1001, 7, '/rk@hub_bot 10.11')
        telegram.push(-1001, 7, '/web')
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not (telegram.methods('sendPhoto') and any(
                '[ok] app-01' in c['fields'].get('text', '') for c in telegram.methods('sendMessage'))):
            time.sleep(0.2)
    finally:
        proc.send_signal(signal.SIGTERM)
        out = proc.communicate(timeout=20)[0].decode('utf-8', 'replace')
    texts = [c['fields'].get('text', '') for c in telegram.methods('sendMessage')]
    assert 'Доступ запрещён' in texts
    assert any('[ok] app-01' in t for t in texts), (texts, out)
    assert telegram.methods('sendPhoto'), out
    assert 'Проверяю: Backups...' in texts and 'Проверяю: Web...' in texts
    assert proc.returncode == 0 and telegram.token not in out

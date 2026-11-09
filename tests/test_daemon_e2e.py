"""Сквозной тест: настоящий процесс `monitor-hub run`, настоящий ssh, поддельный
PAM-шлюз и поддельный vk_api (пишет действия в файл). Команда из беседы -> картинка."""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.fakes.pam_server import FakePamServer

ROOT = Path(__file__).resolve().parent.parent
FAKES = Path(__file__).resolve().parent / 'fakes'

pytestmark = pytest.mark.skipif(shutil.which('ssh') is None, reason='нужен openssh-client')

TOML = '''
[targets.crl]
title = "CRL"
pam_user = "pamuser"
password_env = "CRL_PW"
target_host = "10.0.0.1"
paths = ["/srv/crl"]
style = "listing"
command = "/get"
schedule = "daily 03:00"
connect_retry_delay = 0

[targets.pg]
title = "PG"
pam_user = "pamuser"
password_env = "CRL_PW"
target_host = "10.0.0.2"
paths = ["/srv/a", "/srv/b"]
sudo = true
command = "/pg"
'''


def wait_for(predicate, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def read_events(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line]


def test_daemon_answers_chat_commands(tmp_path):
    server = FakePamServer('pamuser', 'pampass')
    (tmp_path / 'targets.toml').write_text(TOML)
    events_file = tmp_path / 'events.json'
    events_file.write_text(json.dumps([
        {'text': 'привет', 'peer_id': 2000000001, 'from_id': 7},
        {'text': '/get', 'peer_id': 2000000001, 'from_id': 99},      # чужой: отказ
        {'text': '/get', 'peer_id': 2000000001, 'from_id': 7},
        {'text': '/run pg', 'peer_id': 2000000001, 'from_id': 7},
        {'text': '/targets', 'peer_id': 2000000001, 'from_id': 7},
    ]))
    vk_log = tmp_path / 'vk.jsonl'
    env = {
        'PATH': os.environ['PATH'], 'HOME': str(tmp_path),
        'PYTHONPATH': os.pathsep.join([str(FAKES), str(ROOT)]),
        'STATE_DIR': str(tmp_path / 'state'), 'SAVE_DIR': str(tmp_path / 'shots'),
        'TARGETS_FILE': str(tmp_path / 'targets.toml'),
        'PAM_HOST': '127.0.0.1', 'PAM_PORT': str(server.port), 'CRL_PW': 'pampass',
        'SSH_STRICT_HOST_KEY': 'no', 'SSH_KEX': 'ecdh-sha2-nistp256', 'SSH_HOSTKEY_ALGS': 'rsa-sha2-256',
        'SSH_STRICT_ALGS': '1', 'PROMPT_NUDGE': '1', 'GATEWAY_LOGIN_GAP': '0', 'VK_ATTACH_DELAY': '0',
        'VK_TOKEN': 'tok', 'VK_GROUP_ID': '1', 'VK_PEER_ID': '2000000001', 'ALLOWED_USERS': '7',
        'NO_OPEN_SCREENSHOT': '1', 'E2E_VK_LOG': str(vk_log), 'E2E_EVENTS': str(events_file),
    }
    proc = subprocess.Popen([sys.executable, str(ROOT / 'monitor-hub'), 'run'], env=env, cwd=tmp_path,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        def photos_sent():
            return [e for e in read_events(vk_log) if e['kind'] == 'send' and e.get('attachment')]

        assert wait_for(lambda: len(photos_sent()) >= 2), read_events(vk_log)
        assert (tmp_path / 'state' / 'monitor-hub.pid').exists()
    finally:
        proc.send_signal(signal.SIGTERM)
        output = proc.communicate(timeout=20)[0].decode('utf-8', 'replace')
        server.close()

    events = read_events(vk_log)
    texts = [e['message'] for e in events if e['kind'] == 'send' and not e.get('attachment')]
    assert 'Доступ запрещён' in texts
    assert 'Проверяю: CRL...' in texts and 'Проверяю: PG...' in texts
    assert any('/get - CRL (daily 03:00)' in t for t in texts)
    uploads = [e for e in events if e['kind'] == 'upload']
    assert len(uploads) == 3 and all(e['png'] for e in uploads)     # 1 картинка CRL + 2 пути PG
    assert ('ls', '/srv/a', True) in server.events                  # sudo у pg
    assert proc.returncode == 0, output
    assert not (tmp_path / 'state' / 'monitor-hub.pid').exists()   # PID-файл убран по SIGTERM
    assert 'pampass' not in (tmp_path / 'state' / 'monitor-hub.log').read_text(encoding='utf-8')

"""Команды CLI как настоящие процессы: validate, list, check-paths, check, status
против поддельного шлюза (без ВК и без окон на рабочем столе)."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fakes.pam_server import FakePamServer

ROOT = Path(__file__).resolve().parent.parent
FAKES = Path(__file__).resolve().parent / 'fakes'

pytestmark = pytest.mark.skipif(shutil.which('ssh') is None, reason='нужен openssh-client')

TOML = '''
[targets.web]
title = "Web"
pam_user = "pamuser"
password_env = "WEB_PW"
target_host = "10.0.0.1"
paths = ["/srv/ok", "/srv/none"]
command = "/web"
connect_retry_delay = 0
'''


@pytest.fixture
def hub(tmp_path):
    server = FakePamServer('pamuser', 'pampass', states={'/srv/none': 'noent'})
    (tmp_path / 'targets.toml').write_text(TOML)
    env = {
        'PATH': os.environ['PATH'], 'HOME': str(tmp_path),
        'PYTHONPATH': os.pathsep.join([str(FAKES), str(ROOT)]),
        'STATE_DIR': str(tmp_path / 'state'), 'SAVE_DIR': str(tmp_path / 'shots'),
        'TARGETS_FILE': str(tmp_path / 'targets.toml'),
        'PAM_HOST': '127.0.0.1', 'PAM_PORT': str(server.port), 'WEB_PW': 'pampass',
        'SSH_STRICT_HOST_KEY': 'no', 'SSH_KEX': 'ecdh-sha2-nistp256', 'SSH_HOSTKEY_ALGS': 'rsa-sha2-256',
        'SSH_STRICT_ALGS': '1', 'PROMPT_NUDGE': '1', 'GATEWAY_LOGIN_GAP': '0',
        'NO_OPEN_SCREENSHOT': '1',
    }

    def run(*args):
        return subprocess.run([sys.executable, str(ROOT / 'monitor-hub'), *args], env=env, cwd=tmp_path,
                              capture_output=True, text=True, timeout=120)

    yield run, tmp_path
    server.close()


def test_validate_and_list(hub):
    run, _ = hub
    done = run('validate', '--no-vk')
    assert done.returncode == 0, done.stdout + done.stderr
    listing = run('list')
    assert listing.returncode == 0 and 'web' in listing.stdout and '/web' in listing.stdout


def test_check_paths_reports_each_path(hub):
    run, _ = hub
    done = run('check-paths', 'web')
    assert done.returncode != 0                      # один путь не найден
    assert '/srv/ok' in done.stdout and '/srv/none' in done.stdout
    assert 'pampass' not in done.stdout + done.stderr


def test_check_without_send_saves_picture_and_status_shows_it(hub):
    run, tmp = hub
    done = run('check', 'web', '--no-send')
    assert done.returncode == 0, done.stdout + done.stderr
    assert list((tmp / 'shots').glob('*.png'))
    status = run('status')
    assert 'web' in status.stdout
    assert 'pampass' not in status.stdout + status.stderr


def test_unknown_target_is_a_clean_error(hub):
    run, _ = hub
    done = run('check', 'nope')
    assert done.returncode != 0
    assert 'Traceback' not in done.stdout + done.stderr


def test_live_prints_frames_and_stops(hub):
    run, _ = hub
    done = run('live', 'web', 'top', '-n', '2', '-i', '0.1')
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.count('load average') == 2
    assert run('live', 'web', 'bad\tcmd').returncode == 2
    assert run('live', 'nope', 'top').returncode == 2

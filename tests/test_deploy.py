import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def project(tmp_path):
    """Копия нужных файлов в каталоге без пробелов, с venv-заглушкой."""
    proj = tmp_path / 'hub'
    (proj / 'deploy').mkdir(parents=True)
    for name in ('install.sh', 'monitor-hub.service'):
        (proj / 'deploy' / name).write_text((ROOT / 'deploy' / name).read_text())
    (proj / 'venv' / 'bin').mkdir(parents=True)
    python = proj / 'venv' / 'bin' / 'python'
    python.write_text('#!/bin/sh\n')
    python.chmod(0o755)
    (proj / '.env').write_text('')
    (proj / 'targets.toml').write_text('')
    return proj


def run_install(proj, tmp_path, **env):
    calls = tmp_path / 'systemctl.log'
    fake = tmp_path / 'systemctl'
    fake.write_text(f'#!/bin/sh\necho "$@" >> {calls}\necho active\n')
    fake.chmod(0o755)
    full_env = {**os.environ, 'DRY_RUN': '1', 'UNIT_DIR': str(tmp_path), 'SYSTEMCTL': str(fake),
                'START_WAIT': '0', 'RUN_USER': 'svc', **env}
    result = subprocess.run(['bash', str(proj / 'deploy' / 'install.sh')], env=full_env,
                            capture_output=True, text=True)
    return result, calls


def test_install_writes_unit_with_paths_and_user(project, tmp_path):
    result, calls = run_install(project, tmp_path)
    assert result.returncode == 0, result.stderr
    unit = (tmp_path / 'monitor-hub.service').read_text()
    assert 'User=svc' in unit and f'ExecStart={project}/venv/bin/python {project}/monitor-hub run' in unit
    assert '/opt/monitor-hub' not in unit
    assert calls.read_text().splitlines() == ['daemon-reload', 'enable --now monitor-hub', 'is-active monitor-hub']


def test_install_backs_up_previous_unit(project, tmp_path):
    (tmp_path / 'monitor-hub.service').write_text('old')
    run_install(project, tmp_path)
    backups = list(tmp_path.glob('monitor-hub.service.bak-*'))
    assert len(backups) == 1 and backups[0].read_text() == 'old'


@pytest.mark.parametrize('missing', ['.env', 'targets.toml', 'venv/bin/python'])
def test_install_refuses_without_required_files(project, tmp_path, missing):
    (project / missing).unlink()
    result, _ = run_install(project, tmp_path)
    assert result.returncode != 0 and 'ОШИБКА' in result.stderr


def test_install_refuses_root_owner(project, tmp_path):
    result, _ = run_install(project, tmp_path, RUN_USER='root')
    assert result.returncode != 0 and 'root' in result.stderr

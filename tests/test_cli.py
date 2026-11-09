import pytest

from monitor import cli, engine

TOML = '''
[targets.demo]
title = "Demo"
pam_user = "u"
password_env = "DEMO_PW"
target_host = "192.0.2.5"
paths = ["/data"]
schedule = "daily 09:00"
command = "/demo"
'''

LINES = ['-rw-r--r-- 1 root root 5 Oct 18 09:00 a.bak']


@pytest.fixture
def project(tmp_path, monkeypatch):
    toml = tmp_path / 'targets.toml'
    toml.write_text(TOML)
    monkeypatch.setenv('STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('SAVE_DIR', str(tmp_path / 'shots'))
    monkeypatch.setenv('PAM_HOST', 'pam.example.org')
    monkeypatch.setenv('DEMO_PW', 'pw')
    monkeypatch.setenv('VK_TOKEN', 'tok')
    monkeypatch.setenv('VK_PEER_ID', '1')
    monkeypatch.setattr(cli._app, 'load_env', lambda d: None)
    monkeypatch.setattr(cli, '_LOG', None)
    return toml


def run(project, *args):
    return cli.main(['--config', str(project), *args])


def test_validate_ok(project, capsys):
    assert run(project, 'validate') == 0
    assert 'Целей: 1' in capsys.readouterr().out


def test_validate_reports_missing_password_without_network(project, monkeypatch, capsys):
    monkeypatch.delenv('DEMO_PW')
    assert run(project, 'validate') == 1
    assert 'DEMO_PW' in capsys.readouterr().err


def test_validate_no_vk(project, monkeypatch):
    monkeypatch.delenv('VK_TOKEN')
    assert run(project, 'validate') == 1
    assert run(project, 'validate', '--no-vk') == 0


def test_validate_missing_file(tmp_path, capsys):
    assert cli.main(['--config', str(tmp_path / 'no.toml'), 'validate']) == 1
    assert 'не найден' in capsys.readouterr().err


def test_list(project, capsys):
    assert run(project, 'list') == 0
    out = capsys.readouterr().out
    assert 'demo' in out and '/demo' in out and 'daily 09:00' in out


def test_check_unknown_target(project, capsys):
    assert run(project, 'check', 'nope') == 2
    assert 'Нет такой цели' in capsys.readouterr().err


def test_check_no_send_works_without_vk(project, monkeypatch, capsys):
    monkeypatch.delenv('VK_TOKEN')
    monkeypatch.setattr(engine.Engine, 'fetch_listings', lambda self, t: {'/data': LINES})
    assert run(project, 'check', 'demo', '--no-send') == 0
    out = capsys.readouterr().out
    assert '.png' in out and 'не отправлялось' in out


def test_check_failure_returns_1_and_masks_password(project, monkeypatch, capsys):
    def boom(self, target):
        raise engine.HubError('failed with pw inside', 'x')

    monkeypatch.setattr(engine.Engine, 'fetch_listings', boom)
    assert run(project, 'check', 'demo', '--no-send') == 1
    assert 'Ошибка' in capsys.readouterr().err


def test_status_stop_logs_without_daemon(project, capsys):
    assert run(project, 'status') == 0
    assert 'Не запущен' in capsys.readouterr().out
    assert run(project, 'stop') == 0
    assert 'не запущен' in capsys.readouterr().out
    assert run(project, 'logs', '-n', '5') == 0


def test_status_shows_config_problems(project, monkeypatch, capsys):
    monkeypatch.delenv('DEMO_PW')
    run(project, 'status')
    assert 'DEMO_PW' in capsys.readouterr().out


def test_run_and_start_refuse_bad_config(project, monkeypatch, capsys):
    monkeypatch.delenv('VK_TOKEN')
    assert run(project, 'run') == 2
    assert run(project, 'start') == 2
    assert 'VK_TOKEN' in capsys.readouterr().err


def test_run_serves_until_stopped(project, monkeypatch):
    from monitor import hub

    served = []
    monkeypatch.setattr(hub.Hub, 'serve', lambda self: served.append(len(self.targets)))
    monkeypatch.setattr(cli._daemon(cli.Settings.from_env()).__class__, 'install_signal_handlers', lambda self: None)
    assert run(project, 'run') == 0
    assert served == [1]


def test_check_paths_prints_verdicts(project, monkeypatch, capsys):
    from monitor.engine import PathVerdict
    monkeypatch.setattr(engine.Engine, 'check_paths',
                        lambda self, t, paths=None: [PathVerdict('/data', 'ok', 2), PathVerdict('/x', 'denied')])
    assert run(project, 'check-paths', 'demo') == 1
    out = capsys.readouterr().out
    assert '+ /data  ok, записей: 2' in out and '! /x     нет прав' in out
    monkeypatch.setattr(engine.Engine, 'check_paths', lambda self, t, paths=None: [PathVerdict('/data', 'ok', 2)])
    assert run(project, 'check-paths', 'demo') == 0


def test_check_paths_unknown_target_and_failure(project, monkeypatch, capsys):
    assert run(project, 'check-paths', 'nope') == 2

    def boom(self, t, paths=None):
        raise engine.HubError('pw leaked', 'x')

    monkeypatch.setattr(engine.Engine, 'check_paths', boom)
    assert run(project, 'check-paths', 'demo') == 1
    assert 'Ошибка' in capsys.readouterr().err


def test_status_shows_last_runs(project, capsys, tmp_path):
    from monitor import runlog
    runlog.record(tmp_path / 'state', 'demo', False, 'Не удалось войти')
    run(project, 'status')
    out = capsys.readouterr().out
    assert 'Последние проверки' in out and 'Не удалось войти' in out


def test_tui_without_textual(project, monkeypatch, capsys):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == 'monitor' and args and args[2] and 'tui' in args[2]:
            raise ImportError('no textual')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', fake_import)
    assert run(project, 'tui') == 2
    assert 'textual' in capsys.readouterr().err


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(['--version'])
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith('monitor-hub 1.')

"""monitor.app: .env, каталоги, логи, ошибки, проверка конфигурации, run_check, selftest, main."""
import logging
import os
import stat

import pytest

from monitor import app


# --- .env и каталоги ---
@pytest.mark.parametrize('raw,expected', [
    ('abc', 'abc'), ('"abc"', 'abc'), ("'abc'", 'abc'), ('  "a b"  ', 'a b'),
    ('"abc', '"abc'), ('"a\'', '"a\''), ('"', '"'), ('', ''),
])
def test_unquote(raw, expected):
    assert app.unquote(raw) == expected


def test_load_env_reads_file_and_keeps_existing_environment(tmp_path, monkeypatch):
    (tmp_path / '.env').write_text(
        '# comment\n\nexport A_T="quoted value"\nB_T=\'single\'\nC_T=9999\nbroken line\n  D_T = spaced  \n',
        encoding='utf-8')
    for name in ('A_T', 'B_T', 'D_T'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('C_T', 'from-env')
    path = app.load_env(str(tmp_path))
    assert path == str(tmp_path / '.env')
    assert (os.environ['A_T'], os.environ['B_T'], os.environ['D_T']) == ('quoted value', 'single', 'spaced')
    assert os.environ['C_T'] == 'from-env'                      # окружение важнее файла
    for name in ('A_T', 'B_T', 'D_T'):
        monkeypatch.delenv(name)


def test_load_env_looks_one_level_up(tmp_path, monkeypatch):
    sub = tmp_path / 'sub'
    sub.mkdir()
    (tmp_path / '.env').write_text('UP_T=1\n', encoding='utf-8')
    monkeypatch.delenv('UP_T', raising=False)
    assert os.path.samefile(app.load_env(str(sub)), tmp_path / '.env')
    monkeypatch.delenv('UP_T')


def test_load_env_without_file(tmp_path):
    assert app.load_env(str(tmp_path / 'nothing')) is None


def test_state_dir_priority_and_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('STATE_DIR', raising=False)
    monkeypatch.delenv('XDG_STATE_HOME', raising=False)
    default = app.state_dir('mon')
    assert default == tmp_path / 'home' / '.local' / 'state' / 'mon'
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'xdg'))
    assert app.state_dir('mon') == tmp_path / 'xdg' / 'mon'
    monkeypatch.setenv('STATE_DIR', str(tmp_path / 'explicit'))
    explicit = app.state_dir('mon')
    assert explicit == tmp_path / 'explicit'
    assert stat.S_IMODE(explicit.stat().st_mode) == 0o700


# --- логи, секреты, ошибки ---
def test_setup_logging_and_detach(tmp_path):
    log, console = app.setup_logging('t_app_log', str(tmp_path / 'x.log'))
    assert log.propagate is False and console in log.handlers
    log.info('hello')
    assert 'hello' in (tmp_path / 'x.log').read_text(encoding='utf-8')
    app.detach_console_logging(log, console)
    assert console not in log.handlers


def test_setup_logging_survives_unwritable_log(tmp_path):
    log, _ = app.setup_logging('t_app_log2', str(tmp_path / 'missing-dir' / 'x.log'))
    log.info('still works')


def test_mask_secrets():
    assert app.mask_secrets('a secret1 b', ['secret1', 'abc', '']) == 'a *** b'


def test_monitor_error_and_user_facing():
    class MyError(app.MonitorError):
        pass

    err = MyError('detail', 'for chat', True)
    assert (str(err), err.user_message, err.retryable) == ('detail', 'for chat', True)
    assert isinstance(err, RuntimeError)
    assert app.user_facing(err, MyError, 'default') == 'for chat'
    assert app.user_facing(ValueError('x'), MyError, 'default') == 'default'


# --- проверка конфигурации ---
def test_warn_on_insecure_env_file(tmp_path, caplog):
    log = logging.getLogger('t_app_warn')
    secure, loose = tmp_path / 'a', tmp_path / 'b'
    secure.write_text('x')
    loose.write_text('x')
    os.chmod(secure, 0o600)
    os.chmod(loose, 0o644)
    with caplog.at_level(logging.WARNING, logger='t_app_warn'):
        app.warn_on_insecure_env_file(log, str(secure), 'секреты')
        app.warn_on_insecure_env_file(log, None, 'секреты')
        app.warn_on_insecure_env_file(log, str(tmp_path / 'gone'), 'секреты')
        assert caplog.text == ''
        app.warn_on_insecure_env_file(log, str(loose), 'секреты')
    assert 'chmod 600' in caplog.text and 'секреты' in caplog.text and '644' in caplog.text


# --- run_check / run_locked ---
class Env:
    def __init__(self, fail_send=False, fail_collect=None, extra=()):
        self.calls, self.log = [], logging.getLogger('t_app_run')
        self.fail_send, self.fail_collect, self.extra = fail_send, fail_collect, list(extra)

    def run(self, target=None, open_after=False):
        def collect():
            if self.fail_collect:
                raise self.fail_collect
            return ['buf'], lambda: 'caption'

        def send_photos(paths, caption, peer):
            self.calls.append(('photos', paths, caption, peer))
            if self.fail_send:
                raise RuntimeError('vk down')

        app.run_check(
            log=self.log, title='== t ==', error_title='Ошибка', target_peer_id=target, open_after=open_after,
            default_peer=100, collect=collect, save_screenshots=lambda b: ['a.png'],
            open_screenshot=lambda p: self.calls.append(('open', p)), send_photos=send_photos,
            send_message=lambda peer, text: self.calls.append(('msg', peer, text)),
            user_facing=lambda e: 'user-text', mask_secrets=lambda t: t.replace('SECRET', '***'),
            extra_peers=self.extra)


def test_run_check_scheduled_goes_to_default_and_extra_peers(monkeypatch):
    import time
    sleeps = []
    monkeypatch.setattr(time, 'sleep', lambda s: sleeps.append(s))
    env = Env(extra=['7', 'x', '8'])
    env.run()
    assert [c[3] for c in env.calls if c[0] == 'photos'] == [100, 7, 8]
    assert sleeps == [3, 3]


def test_run_check_targeted_skips_extra_peers_and_opens():
    env = Env(extra=['7'])
    env.run(target=555, open_after=True)
    assert env.calls == [('open', 'a.png'), ('photos', ['a.png'], 'caption', 555)]


def test_run_check_send_failure_reports_to_chat():
    env = Env(fail_send=True)
    env.run(target=1)
    assert ('msg', 1, 'user-text') in env.calls


def test_run_check_collect_failure_reports_error_without_secret(caplog):
    env = Env(fail_collect=RuntimeError('boom SECRET'))
    with caplog.at_level(logging.ERROR, logger='t_app_run'):
        env.run()
    assert env.calls == [('msg', 100, 'Ошибка: user-text')]
    assert 'SECRET' not in caplog.text.split('Traceback')[0]


# --- selftest и main ---

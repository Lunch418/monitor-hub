"""pam_session: аргументы ssh, вход через PAM, Enter при невидимом приглашении, повтор сессии."""
import logging
import types

import pytest

from monitor import pam_session as pam


class FakeTimeout:
    pass


class FakeEof:
    pass


class SpawnError(Exception):
    pass


class FakeChild:
    """Фальшивый pexpect.spawn: отвечает на expect по плану {этап: [исходы]}."""

    def __init__(self, plan, events):
        self.plan, self.events, self.before = plan, events, b''

    def setwinsize(self, *args):
        pass

    def send(self, data):
        self.events.append(('send', data))

    def read_nonblocking(self, size, timeout=None):
        raise TimeoutError

    def close(self, force=False):
        self.events.append(('close', force))

    def expect(self, patterns, timeout=None):
        stages = {b'[Pp]assword:': 'password', b'select one:': 'menu',
                  b'[Tt]ype one of': 'user', b'\\$ ': 'prompt'}
        stage = stages.get(patterns[0], 'ls')
        self.events.append(('expect', stage, timeout))
        queue = self.plan.get(stage) or []
        outcome = queue.pop(0) if queue else 'ok'
        self.before = b'listing'
        # в списке patterns в конце всегда два служебных элемента: TIMEOUT и EOF
        return {'ok': 0, 'timeout': len(patterns) - 2, 'eof': len(patterns) - 1}[outcome]


@pytest.fixture
def fake_px():
    px = types.ModuleType('pexpect')
    px.TIMEOUT, px.EOF, px.ExceptionPexpect = FakeTimeout, FakeEof, SpawnError
    px.events, px.plan, px.spawn_failures = [], {}, 0

    def spawn(cmd, args, timeout=None, encoding=None):
        px.events.append(('spawn', cmd, tuple(args)))
        if px.spawn_failures:
            px.spawn_failures -= 1
            raise SpawnError('no ssh')
        return FakeChild(px.plan, px.events)

    px.spawn = spawn
    return px


class PamError(RuntimeError):
    def __init__(self, detail, user_message, retryable=False):
        super().__init__(detail)
        self.user_message, self.retryable = user_message, retryable


class PamConfigError(PamError):
    pass


def make_config(tmp_path, **overrides):
    values = dict(
        host='pam.example', port=2222, user='u', password='secret',
        target_host='10.0.0.5', target_sel='1',
        strict_host_key='accept-new', known_hosts=str(tmp_path / 'known_hosts'),
        kex='kex1', hostkey_algs='ssh-rsa', strict_algs=False,
        prompt_timeout=120, prompt_nudge=10, connect_retries=2, connect_retry_delay=60,
    )
    values.update(overrides)
    return pam.PamConfig(**values)


def make_session(cfg):
    return pam.PamSession(cfg, log=logging.getLogger('t_pam'), error_cls=PamError,
                          mask_secrets=lambda text: text.replace('secret', '***'))


@pytest.fixture
def clock(monkeypatch):
    import time
    ticks = {'now': 0.0, 'sleeps': []}

    def monotonic():
        ticks['now'] += 3.0
        return ticks['now']

    monkeypatch.setattr(time, 'monotonic', monotonic)
    monkeypatch.setattr(time, 'sleep', lambda s: ticks['sleeps'].append(s))
    return ticks


def test_ssh_argv_adds_algorithms_by_default(tmp_path):
    argv = pam.ssh_argv(make_config(tmp_path))
    assert argv[:2] == ['ssh', '-tt'] and argv[-1] == 'u@pam.example'
    assert 'KexAlgorithms=+kex1' in argv and 'HostKeyAlgorithms=+ssh-rsa' in argv
    assert 'StrictHostKeyChecking=accept-new' in argv


def test_ssh_argv_strict_algs_replace_defaults(tmp_path):
    argv = pam.ssh_argv(make_config(tmp_path, strict_algs=True))
    assert 'KexAlgorithms=kex1' in argv and 'HostKeyAlgorithms=ssh-rsa' in argv
    assert 'PubkeyAcceptedAlgorithms=+ssh-rsa' in argv        # здесь '+' всегда


def test_ssh_argv_host_key_checking_off_uses_devnull(tmp_path):
    argv = pam.ssh_argv(make_config(tmp_path, strict_host_key='no'))
    assert 'UserKnownHostsFile=/dev/null' in argv
    assert not (tmp_path / 'known_hosts').exists()


def test_ssh_argv_creates_private_known_hosts(tmp_path):
    pam.ssh_argv(make_config(tmp_path))
    assert (tmp_path / 'known_hosts').stat().st_mode & 0o777 == 0o600


def test_login_sequence_and_close(tmp_path, fake_px, clock):
    session = make_session(make_config(tmp_path))
    with session.login(fake_px) as sh:
        sh.send_command('echo hi')
    sent = [e[1] for e in fake_px.events if e[0] == 'send']
    assert sent == [b'secret\r', b'10.0.0.5\r', b'1\r', b'echo hi\r', b'exit\r']
    assert fake_px.events[-1] == ('close', True)


def test_enter_is_pressed_when_prompt_not_seen(tmp_path, fake_px, clock):
    fake_px.plan['prompt'] = ['timeout', 'timeout']
    with make_session(make_config(tmp_path)).login(fake_px):
        pass
    sent = [e[1] for e in fake_px.events if e[0] == 'send']
    assert sent.count(b'\r') == 2


def test_nudge_disabled_waits_once_and_fails(tmp_path, fake_px, clock):
    fake_px.plan['prompt'] = ['timeout']
    with pytest.raises(PamError, match='shell prompt'):
        with make_session(make_config(tmp_path, prompt_nudge=0)).login(fake_px):
            pass
    assert [e for e in fake_px.events if e[0] == 'send'].count(('send', b'\r')) == 0


def test_stage_error_masks_secrets_and_is_retryable(tmp_path, fake_px, clock):
    fake_px.plan['menu'] = ['timeout']
    with pytest.raises(PamError) as info:
        with make_session(make_config(tmp_path)).login(fake_px):
            pass
    assert info.value.retryable and 'PAM menu' in info.value.user_message
    assert 'TIMEOUT' in str(info.value)
    assert fake_px.events[-1] == ('close', True)                # сессия закрыта и при ошибке


def test_spawn_failure_is_retryable_error(tmp_path, fake_px, clock):
    fake_px.spawn_failures = 1
    with pytest.raises(PamError, match='не удалось запустить ssh') as info:
        with make_session(make_config(tmp_path)).login(fake_px):
            pass
    assert info.value.retryable


def fetch(session, once, problems=None):
    return session.fetch(once, validate_config=lambda: problems or [],
                         config_error_cls=PamConfigError)


@pytest.fixture
def pexpect_module(fake_px, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, 'pexpect', fake_px)
    return fake_px


def test_fetch_retries_whole_session_then_succeeds(tmp_path, pexpect_module, clock):
    calls = []

    def once(px):
        calls.append(1)
        if len(calls) == 1:
            raise PamError('stage: TIMEOUT', 'x', retryable=True)
        return 'result'

    assert fetch(make_session(make_config(tmp_path)), once) == 'result'
    assert len(calls) == 2 and clock['sleeps'] == [60]


def test_fetch_does_not_retry_non_retryable(tmp_path, pexpect_module, clock):
    def once(px):
        raise PamError('ls hang', 'x', retryable=False)

    with pytest.raises(PamError):
        fetch(make_session(make_config(tmp_path)), once)
    assert clock['sleeps'] == []


def test_fetch_gives_up_after_configured_attempts(tmp_path, pexpect_module, clock):
    calls = []

    def once(px):
        calls.append(1)
        raise PamError('boom secret', 'x', retryable=True)

    with pytest.raises(PamError):
        fetch(make_session(make_config(tmp_path, connect_retries=3)), once)
    assert len(calls) == 3 and clock['sleeps'] == [60, 60]


def test_fetch_reports_config_problems(tmp_path, pexpect_module, clock):
    with pytest.raises(PamConfigError, match='PAM_HOST не задан'):
        fetch(make_session(make_config(tmp_path)), lambda px: 1, problems=['PAM_HOST не задан'])


def test_fetch_without_pexpect(tmp_path, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, 'pexpect', None)
    with pytest.raises(PamError, match='pexpect не установлен'):
        fetch(make_session(make_config(tmp_path)), lambda px: 1)

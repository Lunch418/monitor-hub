import logging
import threading

import pytest

from monitor import engine as eng
from monitor.config import Schedule, Settings, Target
from monitor.hub import Hub


def target(name='crl', command='/get', run_timeout=900, peers=()):
    return Target(name=name, title=name.upper(), pam_user='u', password_env='P', target_host='h',
                  target_sel='1', paths=('/p',), schedule=(Schedule('09:00'),), command=command,
                  run_timeout=run_timeout, peers=peers)


class FakeVk:
    def __init__(self):
        self.messages = []

    def send_message(self, peer, text):
        self.messages.append((peer, text))


@pytest.fixture
def make_hub(tmp_path):
    def _make(targets=None, runner=None, **settings):
        settings = Settings(vk_peer_id=100, save_dir=str(tmp_path), allowed_users=frozenset({'7'}), **settings)
        targets = targets or {'crl': target(), 'pg': target('pg', '/pg')}
        engine = eng.Engine(settings, {}, logging.getLogger('hub-test'), targets)
        engine.chat = FakeVk()
        engine.run = runner or (lambda t, peer_id=None, arg="": None)
        hub = Hub(engine, targets, settings, logging.getLogger('hub-test'))
        hub.spawned = []
        spawn = hub._spawn

        def recording_spawn(*args):
            thread = spawn(*args)
            hub.spawned.append(thread)
            return thread

        hub._spawn = recording_spawn
        return hub
    return _make


def test_command_from_chat_runs_target_and_acks(make_hub):
    calls = []
    hub = make_hub(runner=lambda t, peer_id=None, arg="": calls.append((t.name, peer_id)))
    hub.on_message(55, '7', '/get')
    for thread in hub.spawned:
        thread.join(timeout=2)
    assert calls == [('crl', 55)]
    assert hub.engine.chat.messages == [(55, 'Проверяю: CRL...')]


def test_denied_and_reply(make_hub):
    hub = make_hub()
    hub.on_message(55, '8', '/get')
    hub.on_message(55, '7', '/targets')
    assert hub.engine.chat.messages[0] == (55, 'Доступ запрещён')
    assert '/get - CRL' in hub.engine.chat.messages[1][1]


def test_same_target_is_not_run_twice_at_once(make_hub):
    started, release = threading.Event(), threading.Event()

    def slow(t, peer_id=None, arg=""):
        started.set()
        release.wait(5)

    hub = make_hub(runner=slow)
    first = hub.run_target(hub.targets['crl'], 55)
    started.wait(2)
    assert hub.run_target(hub.targets['crl'], 56) is None
    assert hub.engine.chat.messages == [(56, 'Подождите, CRL уже проверяется...')]
    assert hub.run_target(hub.targets['crl']) is None            # плановый: только в лог
    other = hub.run_target(hub.targets['pg'], 57)                # другая цель не ждёт
    assert other is not None
    release.set()
    first.join(2)
    other.join(2)
    assert hub.run_target(hub.targets['crl']) is not None        # лок освобождён


def test_crash_in_one_target_releases_lock_and_others_work(make_hub):
    done = []

    def runner(t, peer_id=None, arg=""):
        if t.name == 'crl':
            raise RuntimeError('boom')
        done.append(t.name)

    hub = make_hub(runner=runner)
    hub.run_target(hub.targets['crl']).join(2)
    hub.run_target(hub.targets['pg']).join(2)
    assert done == ['pg']
    assert not hub.locks['crl'].locked()


def test_watchdog_reports_slow_check(make_hub):
    release = threading.Event()
    targets = {'crl': target(run_timeout=60)}
    hub = make_hub(targets=targets, runner=lambda t, peer_id=None, arg="": release.wait(5))
    original = threading.Timer

    class FastTimer(original):
        def __init__(self, interval, fn, args=None, kwargs=None):
            super().__init__(0.05, fn, args, kwargs)

    threading.Timer = FastTimer
    try:
        thread = hub.run_target(targets['crl'])
        for _ in range(100):
            if hub.engine.chat.messages:
                break
            release.wait(0.02)
    finally:
        threading.Timer = original
        release.set()
        thread.join(2)
    peer, text = hub.engine.chat.messages[0]
    assert peer == 100 and 'дольше 1 мин' in text


def test_serve_runs_due_jobs(make_hub):
    from datetime import datetime
    calls = []
    hub = make_hub(runner=lambda t, peer_id=None, arg="": calls.append(t.name))
    for job in hub.scheduler.jobs:
        job.next_run = datetime.now()
    hub.serve(ticks=1, sleep=lambda s: None, start_poll=False)
    for thread in hub.spawned:
        thread.join(timeout=2)
    assert sorted(calls) == ['crl', 'pg']
    assert len(hub.describe_schedule()) == 2


def test_poll_disabled_without_allowed_users(make_hub, caplog):
    hub = make_hub()
    hub.settings = Settings()
    with caplog.at_level(logging.WARNING, logger='hub-test'):
        hub.poll()
    assert 'команды из беседы отключены' in caplog.text


def test_gateway_limiter_spaces_logins():
    now = [0.0]
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        now[0] += s

    limiter = eng.GatewayLimiter(2, 5.0, sleep=sleep, monotonic=lambda: now[0])
    with limiter:
        pass
    now[0] += 1
    with limiter:
        pass
    assert sleeps == [4.0]


def test_gateway_limiter_caps_parallel_logins():
    limiter = eng.GatewayLimiter(1, 0)
    inside = threading.Event()
    release = threading.Event()
    order = []

    def first():
        with limiter:
            inside.set()
            release.wait(2)
            order.append('first')

    def second():
        with limiter:
            order.append('second')

    t1 = threading.Thread(target=first)
    t1.start()
    inside.wait(2)
    t2 = threading.Thread(target=second)
    t2.start()
    t2.join(0.2)
    assert order == []            # второй ждёт слот
    release.set()
    t1.join(2)
    t2.join(2)
    assert order == ['first', 'second']


def test_command_from_foreign_chat_is_denied(make_hub):
    calls = []
    hub = make_hub(allowed_chats=frozenset({'100'}),
                   runner=lambda t, peer_id=None, arg="": calls.append(peer_id))
    hub.on_message(55, '7', '/get')            # разрешённый пользователь, но чужая беседа
    hub.on_message(55, '7', 'просто текст')
    assert hub.engine.chat.messages == [(55, 'Из этой беседы команды не принимаются')]
    assert calls == []
    hub.on_message(100, '7', '/targets')
    assert '/get - CRL' in hub.engine.chat.messages[-1][1]

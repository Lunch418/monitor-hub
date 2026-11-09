from datetime import datetime

from monitor.config import Schedule, Settings, Target
from monitor.router import Router
from monitor.scheduler import build_scheduler, next_runs

DAILY_9 = (Schedule('09:00'),)


def target(name='crl', command='/get', schedule=DAILY_9, title='CRL'):
    return Target(name=name, title=title, pam_user='u', password_env='P', target_host='h',
                  target_sel='1', paths=('/p',), schedule=schedule, command=command)


# ---------------------------------------------------------------- планировщик

def test_each_schedule_becomes_a_job_tagged_with_target():
    a = target('a', schedule=(Schedule('09:00'), Schedule('10:30', 'wed')))
    b = target('b', command='/b', schedule=())
    sched = build_scheduler({'a': a, 'b': b}, lambda t: None)
    assert len(sched.jobs) == 2
    assert {next(iter(j.tags)) for j in sched.jobs} == {'a'}
    weekly = next(j for j in sched.jobs if j.start_day == 'wednesday')
    assert weekly.next_run.weekday() == 2 and weekly.next_run.strftime('%H:%M') == '10:30'


def test_due_job_fires_with_its_target():
    fired = []
    t = target(schedule=(Schedule('09:00'),))
    sched = build_scheduler([t], fired.append)
    job = sched.jobs[0]
    job.next_run = datetime.now().replace(microsecond=0)   # срок наступил
    sched.run_pending()
    assert fired == [t]
    assert job.next_run > datetime.now()                     # перенесено на следующий день


def test_next_runs_sorted():
    a = target('a', schedule=(Schedule('23:59'),))
    b = target('b', command='/b', schedule=(Schedule('00:00'),))
    rows = next_runs(build_scheduler([a, b], lambda t: None))
    assert {r[0] for r in rows} == {'a', 'b'}
    assert [r[1] for r in rows] == sorted(r[1] for r in rows)


# ---------------------------------------------------------------- роутер

def router(**settings):
    targets = {'crl': target(), 'pg': target('pg', command='/pg', title='PG', schedule=())}
    return Router(targets, Settings(**settings))


def test_plain_messages_are_ignored():
    r = router(allowed_users=frozenset({'1'}))
    for text in ('', 'привет', 'get', '/unknown', '   '):
        assert r.handle(text, '1') is None


def test_unauthorized_command_is_denied_but_noise_is_silent():
    r = router(allowed_users=frozenset({'1'}))
    assert r.handle('/get', '2').kind == 'deny'
    assert r.handle('просто текст', '2') is None


def test_empty_allowed_users_means_nobody_unless_explicit():
    assert router().handle('/get', '1').kind == 'deny'
    assert router(allow_any_user=True).handle('/get', '1').kind == 'run'


def test_target_command_runs_target():
    action = router(allowed_users=frozenset({'1'})).handle('  /GET ', '1')
    assert action.kind == 'run' and action.target.name == 'crl'


def test_run_by_name_and_errors():
    r = router(allowed_users=frozenset({'1'}))
    assert r.handle('/run pg', '1').target.name == 'pg'
    assert 'Нет такой цели' in r.handle('/run nope', '1').text
    assert 'Укажите цель' in r.handle('/run', '1').text


def test_targets_and_help_list_everything():
    r = router(allowed_users=frozenset({'1'}))
    listing = r.handle('/targets', '1').text
    assert '/get - CRL (daily 09:00)' in listing and '/pg - PG (вручную)' in listing
    assert '/run <имя>' in r.handle('/help', '1').text

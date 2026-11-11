"""Команды состояния из беседы: /uptime, /disk и свои слова из commands."""
import logging
import tomllib

import pytest

from monitor import config as cfg
from monitor import engine as eng
from monitor.config import Settings, Target
from monitor.formspec import blank_values, raw_from_values, values_from_target
from monitor.hub import INFO_CACHE_SECONDS, Hub
from monitor.router import Router

BASE = {'pam_user': 'u', 'password_env': 'P', 'target_host': 'h.example.org', 'paths': ['/p']}


def parse(**targets):
    return cfg.parse_targets({'targets': targets})


def make(name='crl', **extra):
    parsed, problems = parse(**{name: {**BASE, **extra}})
    assert problems == [], problems
    return parsed[name]


# ---------------------------------------------------------------- конфигурация

def test_info_and_commands_parse():
    t = make(info=['uptime', '/DISK', 'uptime'], commands={'nginx': 'pgrep -c nginx'})
    assert t.info == ('uptime', 'disk')                      # без повторов, без косой черты, в нижнем регистре
    assert t.commands == (('nginx', 'pgrep -c nginx'),)


def test_info_is_empty_by_default():
    t = make()
    assert t.info == () and t.commands == ()


@pytest.mark.parametrize('extra, fragment', [
    ({'info': ['reboot']}, 'неизвестное слово'),
    ({'info': 'uptime;rm'}, 'неизвестное слово'),
    ({'commands': 'ls'}, 'нужна таблица'),
    ({'commands': {'Bad Word': 'ls'}}, 'латиница'),
    ({'commands': {'uptime': 'ls'}}, 'зарезервировано'),
    ({'commands': {'run': 'ls'}}, 'зарезервировано'),
    ({'commands': {'x': ''}}, 'одна строка'),
    ({'commands': {'x': 'a\nb'}}, 'одна строка'),
    ({'commands': {'x': 'y' * 301}}, 'одна строка'),
    ({'commands': {'x': 5}}, 'одна строка'),
    ({'command': '/disk'}, 'зарезервирована'),
])
def test_bad_info_settings_are_rejected(extra, fragment):
    targets, problems = parse(crl={**BASE, **extra})
    assert any(fragment in p for p in problems), problems
    assert 'crl' not in targets


def test_custom_word_may_not_take_another_targets_command():
    _targets, problems = parse(a={**BASE, 'command': '/web'}, b={**BASE, 'commands': {'web': 'ls'}})
    assert any('/web' in p and 'уже занята' in p for p in problems)


def test_info_not_allowed_for_mail_target():
    data = {'type': 'mail', 'password_env': 'P', 'imap_host': 'h', 'imap_user': 'u',
            'expect': {'vm': 'daily'}, 'info': ['uptime']}
    _, problems = parse(m=data)
    assert any('не относится' in p for p in problems)


def test_commands_table_not_allowed_in_defaults():
    _, problems = cfg.parse_targets({'defaults': {'commands': {'a': 'ls'}}, 'targets': {'crl': BASE}})
    assert any('только в цели' in p for p in problems)


def test_info_may_be_a_default():
    targets, problems = cfg.parse_targets({'defaults': {'info': ['uptime']}, 'targets': {'crl': BASE}})
    assert problems == [] and targets['crl'].info == ('uptime',)


def test_dump_and_reload_keep_info_and_commands():
    t = make(info=['uptime', 'disk'], commands={'nginx': 'pgrep -c "nginx"', 'big': 'df -h | head'})
    text = cfg.dump_targets({'crl': t})
    again, problems = cfg.parse_targets(tomllib.loads(text))
    assert problems == [] and again['crl'].info == t.info and again['crl'].commands == t.commands
    assert '[targets.crl.commands]' in text


def test_target_copy_survives_parse_targets_with_tables():
    """Правка одной цели в интерфейсе не должна ломаться из-за таблиц других целей."""
    mail = {'type': 'mail', 'password_env': 'P', 'imap_host': 'h', 'imap_user': 'u', 'expect': {'vm': 'daily'}}
    targets, problems = parse(a={**BASE, 'commands': {'x': 'ls'}}, m=mail)
    assert problems == []
    copies = {n: cfg.target_to_dict(t) for n, t in targets.items()}
    again, problems = cfg.parse_targets({'targets': copies})
    assert problems == [] and again['a'].commands == targets['a'].commands and again['m'].expect == targets['m'].expect


def test_form_roundtrip_of_info_field():
    t = make(info=['uptime', 'mem'])
    values = values_from_target(t)
    assert values['info'] == 'uptime, mem'
    raw, _ = raw_from_values(values, kind='ls', via='pam', password_env='P')
    assert raw['info'] == ['uptime', 'mem']
    blank = blank_values('ls')
    raw, _ = raw_from_values({**blank, 'info': ''}, kind='ls', via='pam', password_env='P')
    assert 'info' not in raw                                  # пустое поле - не включено


# ---------------------------------------------------------------- команда для машины

def test_info_command_only_for_allowed_words():
    t = make(info=['disk'], commands={'nginx': 'pgrep -c nginx'})
    assert eng.info_command(t, 'disk').startswith('df ')
    assert eng.info_command(t, 'nginx') == 'pgrep -c nginx'
    for word in ('uptime', 'reboot', 'rm -rf /', ''):
        with pytest.raises(ValueError):
            eng.info_command(t, word)


# ---------------------------------------------------------------- роутер

def router(**targets):
    data = {n: {**BASE, **v} for n, v in targets.items()}
    parsed, problems = cfg.parse_targets({'targets': data})
    assert problems == [], problems
    return Router(parsed, Settings(allowed_users=frozenset({'1'}), vk_peer_id=100))


def test_info_for_all_allowed_targets_and_single_target():
    r = router(a={'info': ['uptime'], 'command': '/a'}, b={'info': ['uptime', 'disk']}, c={})
    everyone = r.handle('/uptime', '1')
    assert everyone.kind == 'info' and [t.name for t in everyone.targets] == ['a', 'b']
    assert [t.name for t in r.handle('/uptime b', '1').targets] == ['b']
    assert [t.name for t in r.handle('/DISK', '1').targets] == ['b']


def test_info_errors_are_replies_not_runs():
    r = router(a={'info': ['uptime']}, b={})
    assert 'не разрешено' in r.handle('/uptime b', '1').text
    assert 'Нет такой цели' in r.handle('/uptime zzz', '1').text
    assert 'не включено' in r.handle('/mem', '1').text


def test_info_needs_the_same_rights_as_other_commands():
    r = router(a={'info': ['uptime']})
    assert r.handle('/uptime', '2').kind == 'deny'
    assert r.handle('/uptime', '1', peer_id=555).kind == 'deny' and r.handle('/uptime', '1', peer_id=555).text == ''
    assert r.handle('/uptime', '1', peer_id=100).kind == 'info'


def test_custom_words_and_unknown_words():
    r = router(a={'commands': {'nginx': 'pgrep -c nginx'}})
    assert r.handle('/nginx', '1').kind == 'info'
    assert r.handle('/nginx2', '1') is None and r.handle('/reboot', '1') is None


def test_mail_targets_never_get_info():
    parsed, _ = cfg.parse_targets({'targets': {'a': {**BASE, 'info': ['uptime']}}})
    mail = Target(name='m', title='M', type='mail', info=('uptime',))
    r = Router({**parsed, 'm': mail}, Settings(allowed_users=frozenset({'1'})))
    assert [t.name for t in r.handle('/uptime', '1').targets] == ['a']


def test_help_lists_enabled_info_commands():
    r = router(a={'info': ['uptime'], 'commands': {'nginx': 'pgrep -c nginx'}})
    text = r.handle('/help', '1').text
    assert '/uptime [цель]' in text and '/nginx [цель] - своя команда' in text and '/mem' not in text


# ---------------------------------------------------------------- хаб

class FakeChat:
    def __init__(self):
        self.messages = []

    def send_message(self, peer, text):
        self.messages.append((peer, text))

    def secrets(self):
        return []


@pytest.fixture
def hub(tmp_path):
    parsed, problems = cfg.parse_targets({'targets': {
        'crl': {**BASE, 'title': 'CRL', 'info': ['uptime']},
        'pg': {**BASE, 'title': 'PG', 'info': ['uptime']},
        'off': {**BASE, 'title': 'OFF'},
    }})
    assert problems == []
    settings = Settings(vk_peer_id=100, save_dir=str(tmp_path), allowed_users=frozenset({'7'}))
    engine = eng.Engine(settings, {}, logging.getLogger('info-test'), parsed)
    engine.chat = FakeChat()
    h = Hub(engine, parsed, settings, logging.getLogger('info-test'))
    h.spawned = []
    spawn = h._spawn

    def recording(*args):
        thread = spawn(*args)
        h.spawned.append(thread)
        return thread

    h._spawn = recording
    return h


def settle(hub):
    seen = 0
    while seen < len(hub.spawned):             # потоки порождают потоки
        hub.spawned[seen].join(timeout=5)
        seen += 1


def test_uptime_collects_all_targets_in_config_order(hub):
    ran = []
    hub.engine.run_command = lambda t, cmd: ran.append(t.name) or f'up {t.name}'
    hub.on_message(100, '7', '/uptime')
    settle(hub)
    assert sorted(ran) == ['crl', 'pg']
    sent = [text for _, text in hub.engine.chat.messages]
    assert sent[0] == 'Запрашиваю: /uptime...'
    assert sent[1] == '[CRL] /uptime\nup crl\n\n[PG] /uptime\nup pg'


def test_one_failing_target_does_not_hide_the_others(hub):
    def run(target, cmd):
        if target.name == 'pg':
            raise eng.HubError('Не удалось войти')
        return 'up crl'

    hub.engine.run_command = run
    hub.on_message(100, '7', '/uptime')
    settle(hub)
    text = hub.engine.chat.messages[-1][1]
    assert '[CRL] /uptime\nup crl' in text and '[PG] /uptime\nошибка:' in text


def test_denied_word_never_reaches_the_machine(hub):
    hub.engine.run_command = lambda t, cmd: pytest.fail('команда не должна выполняться')
    hub.on_message(100, '7', '/uptime off')
    hub.on_message(100, '8', '/uptime')
    settle(hub)
    assert 'не разрешено' in hub.engine.chat.messages[0][1]
    assert hub.spawned == []


def test_second_request_within_cache_window_reuses_answer(hub):
    calls = []
    hub.engine.run_command = lambda t, cmd: calls.append(t.name) or 'up'
    now = [1000.0]
    hub._clock = lambda: now[0]
    for _ in range(2):
        hub.on_message(100, '7', '/uptime crl')
        settle(hub)
    assert calls == ['crl']
    assert 'данные' in hub.engine.chat.messages[-1][1]
    now[0] += INFO_CACHE_SECONDS + 1
    hub.on_message(100, '7', '/uptime crl')
    settle(hub)
    assert calls == ['crl', 'crl']


def test_empty_output_is_reported(hub):
    hub.engine.run_command = lambda t, cmd: ''
    hub.on_message(100, '7', '/uptime crl')
    settle(hub)
    assert '(пустой вывод)' in hub.engine.chat.messages[-1][1]


def test_busy_target_is_not_queried_twice_at_once(hub):
    lock = hub.info_locks['crl']
    lock.acquire()
    try:
        hub.engine.run_command = lambda t, cmd: pytest.fail('не должно выполняться')
        hub.on_message(100, '7', '/uptime crl')
        settle(hub)
        assert 'подождите' in hub.engine.chat.messages[-1][1]
    finally:
        lock.release()


def test_run_command_returns_first_frame_and_stops(monkeypatch):
    settings = Settings(vk_peer_id=100)
    engine = eng.Engine(settings, {}, logging.getLogger('rc'), {})
    seen = {}

    def fake_live(target, command, on_frame, stop, **kw):
        seen['command'] = command
        on_frame('first')
        assert stop.is_set()                      # после первого кадра просмотр останавливается
        return 1

    monkeypatch.setattr(engine, 'live', fake_live)
    assert engine.run_command(make(), 'uptime') == 'first'
    assert seen['command'] == 'uptime'

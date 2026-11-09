import pytest

from monitor import config as cfg

GOOD = {
    'defaults': {'stale_days': 7},
    'targets': {
        'crl': {
            'title': 'CRL', 'pam_user': 'u', 'password_env': 'CRL_PAM_PASSWORD',
            'target_host': '192.0.2.10', 'paths': ['/var/crl'],
            'schedule': 'daily 09:00', 'command': '/get', 'style': 'listing',
        },
        'pg': {
            'pam_user': 'u', 'password_env': 'PG_PAM_PASSWORD', 'target_host': 'h.example.org',
            'paths': ['/mnt/bkp/a', '/mnt/bkp'], 'sudo': True,
            'schedule': ['wed 10:00', 'fri 10:00'], 'command': '/pg', 'peers': ['2000000002'],
        },
    },
}


def parse(data):
    return cfg.parse_targets(data)


def test_good_config_parses_and_applies_defaults():
    targets, problems = parse(GOOD)
    assert problems == []
    crl, pg = targets['crl'], targets['pg']
    assert crl.stale_days == 7 and crl.prompt_timeout == 120 and crl.target_sel == '1'
    assert crl.schedule == (cfg.Schedule('09:00'),)
    assert pg.sudo and pg.title == 'pg' and pg.peers == ('2000000002',)
    assert [str(s) for s in pg.schedule] == ['wed 10:00', 'fri 10:00']


def test_target_overrides_default():
    data = {'defaults': {'stale_days': 7}, 'targets': {'a': {**GOOD['targets']['crl'], 'stale_days': 1}}}
    assert parse(data)[0]['a'].stale_days == 1


@pytest.mark.parametrize('field,value,fragment', [
    ('pam_user', '-oProxyCommand=x', 'недопустимые символы'),
    ('target_host', 'a b', 'недопустимые символы'),
    ('paths', ['relative'], 'абсолютный путь'),
    ('paths', ['/a;rm -rf /'], 'абсолютный путь'),
    ('paths', [], 'хотя бы один путь'),
    ('schedule', 'monday 09:00', 'день'),
    ('schedule', 'daily 25:00', 'HH:MM'),
    ('schedule', 'daily', 'ожидается'),
    ('command', 'get', 'ожидается вид /name'),
    ('command', '/targets', 'зарезервирована'),
    ('style', 'fancy', 'допустимо'),
    ('sudo', 'yes', 'true или false'),
    ('peers', ['abc'], 'не является числом'),
    ('stale_days', -1, 'не меньше'),
    ('prompt_timeout', 5, 'не меньше'),
    ('connect_retries', 'x', 'целое число'),
    ('password_env', 'secret value!', 'имя переменной'),
    ('bogus', 1, 'неизвестное поле'),
])
def test_invalid_field_is_reported_with_target_and_field(field, value, fragment):
    raw = {**GOOD['targets']['crl'], field: value}
    targets, problems = parse({'targets': {'crl': raw}})
    assert targets == {}
    assert any('[targets.crl]' in p and fragment in p for p in problems), problems


def test_all_problems_collected_at_once():
    raw = {'pam_user': 'bad user', 'paths': ['x'], 'schedule': 'nope'}
    _, problems = parse({'targets': {'t': raw}})
    assert len(problems) >= 5


def test_missing_required_fields():
    _, problems = parse({'targets': {'t': {}}})
    joined = ' '.join(problems)
    for key in ('pam_user', 'password_env', 'target_host', 'paths'):
        assert key in joined


def test_duplicate_command_rejected():
    data = {'targets': {'a': GOOD['targets']['crl'], 'b': GOOD['targets']['crl']}}
    _, problems = parse(data)
    assert any('/get' in p and 'a и b' in p for p in problems)


def test_bad_target_name_and_unknown_sections():
    _, problems = parse({'targets': {'Bad Name': GOOD['targets']['crl']}, 'extra': {}})
    assert any('имя цели' in p for p in problems)
    assert any("'extra'" in p for p in problems)


def test_password_in_defaults_is_rejected():
    _, problems = parse({'defaults': {'password_env': 'X'}, 'targets': {'a': GOOD['targets']['crl']}})
    assert any('только в цели' in p for p in problems)


def test_no_targets():
    assert 'нет ни одной цели' in parse({})[1][0]
    assert parse({'targets': {'a': 5}})[1]


def test_load_targets_file_errors(tmp_path):
    assert 'не найден' in cfg.load_targets(tmp_path / 'none.toml')[1][0]
    bad = tmp_path / 'bad.toml'
    bad.write_text('[targets\n')
    assert 'синтаксиса' in cfg.load_targets(bad)[1][0]


def test_load_targets_ok(tmp_path):
    f = tmp_path / 't.toml'
    f.write_text('[targets.a]\npam_user="u"\npassword_env="P"\ntarget_host="h"\npaths=["/x"]\n')
    targets, problems = cfg.load_targets(f)
    assert problems == [] and targets['a'].paths == ('/x',)


def test_example_file_is_valid():
    from pathlib import Path
    targets, problems = cfg.load_targets(Path(__file__).parent.parent / 'targets.example.toml')
    assert problems == [] and 'example' in targets


def test_validate_environment(monkeypatch):
    targets, _ = parse(GOOD)
    settings = cfg.Settings(pam_host='bad host', pam_port='99999', strict_host_key='maybe', extra_peers=('x',))
    problems = cfg.validate_environment(settings, targets, {})
    text = ' | '.join(problems)
    for fragment in ('PAM_HOST', 'PAM_PORT', 'SSH_STRICT_HOST_KEY', 'CRL_PAM_PASSWORD', 'PG_PAM_PASSWORD',
                     'VK_TOKEN', 'VK_PEER_ID', 'EXTRA_PEERS'):
        assert fragment in text
    ok = cfg.Settings(pam_host='pam.example.org', vk_token='t', vk_peer_id=1)
    env = {'CRL_PAM_PASSWORD': 'a', 'PG_PAM_PASSWORD': 'b'}
    assert cfg.validate_environment(ok, targets, env) == []
    assert cfg.validate_environment(cfg.Settings(), targets, {}, need_ssh=False, need_vk=False) == []


def test_settings_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv('STATE_DIR', str(tmp_path / 's'))
    for k, v in {'PAM_HOST': 'g', 'VK_TOKEN': 'tok', 'VK_PEER_ID': '5', 'ALLOWED_USERS': '1, 2',
                 'EXTRA_PEERS': '7', 'MAX_ROWS_PER_IMAGE': '3'}.items():
        monkeypatch.setenv(k, v)
    s = cfg.Settings.from_env()
    assert s.pam_host == 'g' and s.vk_peer_id == 5 and s.allowed_users == {'1', '2'}
    assert s.extra_peers == ('7',) and s.max_rows == 20          # нижняя граница
    assert 'tok' not in repr(s)                                  # токен не светится в repr


def test_parse_schedule():
    assert cfg.parse_schedule('Daily 09:05') == cfg.Schedule('09:05')
    assert cfg.parse_schedule('wed 10:00').weekday == 'wed'

import pytest

from monitor import config as cfg
from monitor import formspec as fs


def target_from(raw, name='t'):
    targets, problems = cfg.parse_targets({'targets': {name: raw}})
    assert problems == [], problems
    return targets[name]


LS = {'pam_user': 'u', 'password_env': 'T_PW', 'target_host': 'h.example.org', 'paths': ['/a', '/b'],
      'schedule': ['daily 09:00', 'wed 10:00'], 'command': '/t', 'peers': ['5'], 'sudo': True}
MAIL = {'type': 'mail', 'password_env': 'M_PW', 'imap_host': 'm.example.org', 'imap_user': 'u@example.org',
        'expect': {'app-01': 'mon,tue', 'db': 'daily'}, 'folders': ['INBOX', 'Входящие'],
        'ok_words': ['done'], 'night_from': 20}


def roundtrip(raw):
    target = target_from(raw)
    values = fs.values_from_target(target)
    back, problems = fs.raw_from_values(values, kind=target.type, via=target.via, password_env=target.password_env)
    assert problems == []
    again, errors = cfg.parse_targets({'targets': {'t': back}})
    assert errors == [], (errors, back)
    return target, again['t']


def test_form_roundtrip_ls_pam():
    before, after = roundtrip(LS)
    assert before == after


def test_form_roundtrip_ls_ssh():
    before, after = roundtrip({'via': 'ssh', 'user': 'm', 'password_env': 'T_PW', 'target_host': 'h',
                               'ssh_port': 2200, 'paths': ['/a']})
    assert before == after and after.via == 'ssh'


def test_form_roundtrip_mail():
    before, after = roundtrip(MAIL)
    assert before == after and after.night_from == 20 and after.ok_words == ('done',)


def test_hidden_fields_are_not_collected():
    values = fs.blank_values('mail')
    values.update(imap_host='m', imap_user='u', expect='a=mon', paths='/should/not/appear', pam_user='nope')
    raw, _ = fs.raw_from_values(values, kind='mail', via='pam', password_env='X_PW')
    assert 'paths' not in raw and 'pam_user' not in raw and raw['type'] == 'mail'
    values = fs.blank_values('ls')
    values.update(via='ssh', user='m', pam_user='ignored', target_host='h', paths='/a')
    raw, _ = fs.raw_from_values(values, kind='ls', via='ssh', password_env='X_PW')
    assert raw['via'] == 'ssh' and 'pam_user' not in raw and 'target_sel' not in raw


def test_numbers_blank_means_default_and_text_is_reported_by_config():
    values = fs.blank_values('ls')
    values.update(pam_user='u', target_host='h', paths='/a', run_timeout='', prompt_timeout='abc')
    raw, _ = fs.raw_from_values(values, kind='ls', via='pam', password_env='X_PW')
    assert 'run_timeout' not in raw and raw['prompt_timeout'] == 'abc'
    _, problems = cfg.parse_targets({'targets': {'t': raw}})
    assert any('prompt_timeout' in p for p in problems)


def test_parse_expect():
    assert fs.parse_expect('a=mon,tue; b : daily;\nc=sun#1') == ({'a': 'mon,tue', 'b': 'daily', 'c': 'sun#1'}, [])
    result, problems = fs.parse_expect('bad; x=')
    assert result == {} and len(problems) == 2


def test_apply_set():
    values = fs.blank_values()
    assert fs.apply_set(values, 'command', ' /pg ', kind='ls', via='pam') == ''
    assert values['command'] == '/pg'
    assert fs.apply_set(values, 'sudo', 'да', kind='ls', via='pam') == '' and values['sudo'] == 'yes'
    assert fs.apply_set(values, 'style', 'listing', kind='ls', via='pam') == ''
    assert 'допустимо' in fs.apply_set(values, 'style', 'x', kind='ls', via='pam')
    assert 'нет такого поля' in fs.apply_set(values, 'nope', '1', kind='ls', via='pam')
    assert 'не относится' in fs.apply_set(values, 'imap_host', 'x', kind='ls', via='pam')
    assert 'не относится' in fs.apply_set(values, 'user', 'x', kind='ls', via='pam')
    assert fs.apply_set(values, 'user', 'x', kind='ls', via='ssh') == ''
    assert 'пароль' in fs.apply_set(values, 'password', 'secret', kind='ls', via='pam')
    assert 'secret' not in values['password']


@pytest.mark.parametrize('text, name, args, rest', [
    ('/run web вчера', 'run', ('web', 'вчера'), 'web вчера'),
    ('/SET command /x y', 'set', ('command', '/x', 'y'), 'command /x y'),
    ('web', 'web', (), ''),
    ('/ls /var/log send', 'ls', ('/var/log', 'send'), '/var/log send'),
])
def test_parse_prompt(text, name, args, rest):
    cmd = fs.parse_prompt(text)
    assert (cmd.name, cmd.args, cmd.rest) == (name, args, rest)
    assert fs.parse_prompt('   ') is None


def test_default_password_env_names():
    assert fs.default_password_env('my-web') == 'MY_WEB_PAM_PASSWORD'
    assert fs.default_password_env('rk', 'mail') == 'RK_MAIL_PASSWORD'
    assert fs.default_password_env('x', 'ls', 'ssh') == 'X_SSH_PASSWORD'


@pytest.mark.parametrize('key, text, ok', [
    ('MESSENGER', 'telegram', True), ('MESSENGER', 'icq', False), ('VK_GROUP_ID', '12', True),
    ('VK_GROUP_ID', 'x', False), ('TELEGRAM_CHAT_ID', '-100123', True), ('ALLOWED_USERS', '1, 2', True),
    ('ALLOWED_USERS', '1, a', False), ('GATEWAY_LOGIN_GAP', '2.5', True), ('GATEWAY_LOGIN_GAP', 'x', False),
    ('PAM_HOST', 'gw.example.org', True), ('PAM_HOST', 'bad host', False), ('PAM_PORT', '', True),
    ('VK_TOKEN', 'a"b\'c', False), ('SSH_STRICT_HOST_KEY', 'maybe', False),
])
def test_env_value_check(key, text, ok):
    assert (fs.check_env_value(fs.ENV_BY_KEY[key], text) == '') is ok


def test_every_field_has_unique_id_and_visible_somewhere():
    ids = [f.id for f in fs.FIELDS]
    assert len(ids) == len(set(ids))
    for f in fs.FIELDS:
        assert any(fs.visible(f, k, v) for k in ('ls', 'mail') for v in ('pam', 'ssh'))

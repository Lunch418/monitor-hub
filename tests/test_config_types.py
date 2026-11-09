"""Типы целей (ls/mail), способы входа (pam/ssh), свой шлюз у цели, запись targets.toml."""
import pytest
from monitor.config import tomllib

from monitor import config as cfg

LS = {'pam_user': 'u', 'password_env': 'A_PW', 'target_host': '192.0.2.10', 'paths': ['/srv']}
MAIL = {
    'type': 'mail', 'title': 'Backup mail', 'password_env': 'MAIL_PW', 'imap_host': 'mail.example.org',
    'imap_user': 'backup@example.org', 'mail_from': 'noreply@example.org',
    'expect': {'app-01': 'mon,tue', 'db-01': 'daily', 'web-01': 'sun#1'},
    'schedule': 'daily 09:10', 'command': '/rk',
}


def parse(**targets):
    return cfg.parse_targets({'targets': targets})


def problems_of(**targets):
    return parse(**targets)[1]


def test_mail_target_parses_with_defaults():
    targets, problems = parse(rk=MAIL)
    assert problems == []
    rk = targets['rk']
    assert rk.type == 'mail' and rk.imap_port == 993 and rk.imap_ssl and rk.folders == ('INBOX',)
    assert (rk.night_from, rk.night_to) == (18, 12)
    assert dict(rk.expect) == {'app-01': 'mon,tue', 'db-01': 'daily', 'web-01': 'sun#1'}


@pytest.mark.parametrize('patch, fragment', [
    ({'imap_host': ''}, 'imap_host'),
    ({'imap_user': 'a b'}, 'imap_user'),
    ({'imap_port': 70000}, 'imap_port'),
    ({'imap_ssl': 'yes'}, 'imap_ssl'),
    ({'folders': []}, 'folders'),
    ({'night_from': 25}, 'night_from'),
    ({'expect': {}}, 'expect'),
    ({'expect': {'a b': 'mon'}}, 'expect'),
    ({'expect': {'a': 'xyz'}}, 'expect.a'),
    ({'subject_re': '('}, 'subject_re'),
    ({'subject_re': 'no group'}, 'subject_re'),
    ({'mail_from': 'a"b'}, 'mail_from'),
    ({'paths': ['/x']}, 'не относится'),
    ({'pam_user': 'u'}, 'не относится'),
    ({'password_env': ''}, 'password_env'),
])
def test_mail_target_errors(patch, fragment):
    problems = problems_of(rk={**MAIL, **patch})
    assert any(fragment in p for p in problems), problems


def test_ls_fields_rejected_in_mail_and_back():
    assert any('не относится' in p for p in problems_of(a={**LS, 'imap_host': 'x'}))
    assert any('type' in p for p in problems_of(a={**LS, 'type': 'ftp'}))


def test_direct_ssh_target():
    ok = {'via': 'ssh', 'user': 'monitor', 'password_env': 'A_PW', 'target_host': 'vm.example.org',
          'ssh_port': 2200, 'paths': ['/srv']}
    targets, problems = parse(a=ok)
    assert problems == []
    assert targets['a'].via == 'ssh' and targets['a'].user == 'monitor' and targets['a'].ssh_port == 2200
    assert any('via = "pam"' in p for p in problems_of(a={**ok, 'pam_user': 'u'}))
    assert any('user' in p for p in problems_of(a={**ok, 'user': ''}))
    assert any('ssh_port' in p for p in problems_of(a={**ok, 'ssh_port': 70000}))
    assert any('via = "ssh"' in p for p in problems_of(a={**LS, 'user': 'u'}))
    assert any('via' in p for p in problems_of(a={**LS, 'via': 'telnet'}))


def test_own_gateway_per_target():
    targets, problems = parse(a={**LS, 'pam_host': 'gw2.example.org', 'pam_port': '2200'})
    assert problems == [] and targets['a'].pam_host == 'gw2.example.org' and targets['a'].pam_port == '2200'
    assert any('pam_port' in p for p in problems_of(a={**LS, 'pam_port': 'x'}))
    assert any('pam_host' in p for p in problems_of(a={**LS, 'pam_host': 'bad host'}))


def test_identity_fields_not_allowed_in_defaults():
    for key in ('type', 'user', 'imap_user', 'expect', 'mail_from'):
        _, problems = cfg.parse_targets({'defaults': {key: 'x'}, 'targets': {'a': LS}})
        assert any(key in p for p in problems), key


def test_environment_checks_follow_target_kinds():
    settings = cfg.Settings(vk_token='t', vk_peer_id=1)
    targets, _ = parse(rk=MAIL)
    # у письменной цели шлюз не нужен, но пароль нужен
    assert cfg.validate_environment(settings, targets, {'MAIL_PW': 'x'}, need_chat=False) == []
    assert any('MAIL_PW' in p for p in cfg.validate_environment(settings, targets, {}, need_chat=False))
    own, _ = parse(a={**LS, 'pam_host': 'gw.example.org'})
    assert cfg.validate_environment(settings, own, {'A_PW': 'x'}, need_chat=False) == []
    shared, _ = parse(a=LS)
    assert any('PAM_HOST' in p for p in cfg.validate_environment(settings, shared, {'A_PW': 'x'}, need_chat=False))
    direct, _ = parse(a={'via': 'ssh', 'user': 'u', 'password_env': 'A_PW', 'target_host': 'h', 'paths': ['/s']})
    assert cfg.validate_environment(settings, direct, {'A_PW': 'x'}, need_chat=False) == []


def roundtrip(targets):
    text = cfg.dump_targets(targets)
    again, problems = cfg.parse_targets(tomllib.loads(text))
    assert problems == [], (problems, text)
    return again, text


def test_dump_roundtrip_for_all_kinds():
    ssh = {'via': 'ssh', 'user': 'monitor', 'password_env': 'S_PW', 'target_host': 'vm.example.org',
           'ssh_port': 2200, 'paths': ['/srv']}
    targets, problems = parse(web=LS, rk={**MAIL, 'folders': ['INBOX', 'Входящие'], 'ok_words': ['done']},
                              direct=ssh, gw={**LS, 'pam_host': 'gw.example.org', 'pam_port': '2200'})
    assert problems == []
    again, text = roundtrip(targets)
    assert again == targets
    assert '[targets.rk.expect]' in text and 'sun#1' in text
    assert 'type = "mail"' in text and 'type =' not in text.split('[targets.web]')[1].split('[targets')[0]
    assert 'pam_user' not in text.split('[targets.direct]')[1].split('[targets')[0]


def test_dump_expect_key_quoting():
    targets, problems = parse(rk={**MAIL, 'expect': {'app.01': 'mon', 'x-y': 'daily'}})
    assert problems == []
    again, _ = roundtrip(targets)
    assert dict(again['rk'].expect) == {'app.01': 'mon', 'x-y': 'daily'}

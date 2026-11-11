"""Проверки по результатам внешнего разбора: TLS у IMAP, таймаут, regex, отказы, маскировка, ssh-аргументы."""
import json
import socket
import ssl
import time
import tomllib

import pytest

from monitor import config as cfg
from monitor import mailcheck
from monitor.app import mask_secrets
from monitor.messengers import telegram as tg
from monitor.pam_session import ssh_argv
from monitor.router import Router
from tests.test_info_commands import BASE, hub  # noqa: F401  (фикстура)
from tests.test_pam_session import make_config

QUERY = mailcheck.MailQuery(host='mail.example.org', port=993, user='u', password='p')


# ---------------------------------------------------------------- IMAP

def test_imap_checks_certificate_and_hostname_by_default(monkeypatch):
    seen = {}

    def fake(host, port, **kw):
        seen.update(kw)
        return object()

    monkeypatch.setattr(mailcheck.imaplib, 'IMAP4_SSL', fake)
    mailcheck.connect_imap(QUERY)
    assert seen['ssl_context'].verify_mode == ssl.CERT_REQUIRED and seen['ssl_context'].check_hostname
    assert seen['timeout'] == mailcheck.IMAP_TIMEOUT


def test_imap_verify_can_be_turned_off_explicitly(monkeypatch):
    seen = {}
    monkeypatch.setattr(mailcheck.imaplib, 'IMAP4_SSL', lambda host, port, **kw: seen.update(kw))
    mailcheck.connect_imap(mailcheck.MailQuery(**{**QUERY.__dict__, 'verify': False}))
    assert seen['ssl_context'].verify_mode == ssl.CERT_NONE


def test_silent_imap_server_times_out_instead_of_hanging(monkeypatch):
    monkeypatch.setattr(mailcheck, 'IMAP_TIMEOUT', 1)
    server = socket.socket()
    server.bind(('127.0.0.1', 0))
    server.listen(1)
    query = mailcheck.MailQuery(host='127.0.0.1', port=server.getsockname()[1], user='u', password='p', ssl=False)
    started = time.monotonic()
    try:
        with pytest.raises(OSError):
            mailcheck.connect_imap(query)
    finally:
        server.close()
    assert time.monotonic() - started < 10


def test_body_text_is_linear_on_unclosed_tags():
    import email
    msg = email.message_from_string('Content-Type: text/html\n\n' + '<' * 1_000_000)
    started = time.monotonic()
    mailcheck.body_text(msg)
    assert time.monotonic() - started < 2
    assert mailcheck.body_text(email.message_from_string('Content-Type: text/html\n\n<b>a</b> &nbsp;<i>b</i>')) == 'a b'


def test_imap_verify_in_config_and_roundtrip():
    mail = {'type': 'mail', 'password_env': 'P', 'imap_host': 'h', 'imap_user': 'u', 'expect': {'vm': 'daily'}}
    targets, problems = cfg.parse_targets({'targets': {'m': mail, 'n': {**mail, 'imap_verify': False}}})
    assert problems == [] and targets['m'].imap_verify and not targets['n'].imap_verify
    again, problems = cfg.parse_targets(tomllib.loads(cfg.dump_targets(targets)))
    assert problems == [] and not again['n'].imap_verify
    _, problems = cfg.parse_targets({'targets': {'m': {**mail, 'imap_verify': 'no'}}})
    assert any('imap_verify' in p for p in problems)


# ---------------------------------------------------------------- роутер и хаб

def test_foreign_chat_never_gets_an_answer_even_from_unknown_user():
    parsed, _ = cfg.parse_targets({'targets': {'a': {**BASE, 'command': '/a'}}})
    router = Router(parsed, cfg.Settings(allowed_users=frozenset({'1'}), vk_peer_id=100))
    for text in ('/help', '/a', '/targets'):
        action = router.handle(text, '999', peer_id=555)
        assert action.kind == 'deny' and action.text == ''
    assert router.handle('/help', '999', peer_id=100).text == 'Доступ запрещён'


def test_refusals_are_not_flooded(hub):  # noqa: F811
    for _ in range(5):
        hub.on_message(100, '8', '/uptime')
    assert [text for _, text in hub.engine.chat.messages] == ['Доступ запрещён']
    hub._clock = lambda: time.monotonic() + 61
    hub.on_message(100, '8', '/uptime')
    assert len(hub.engine.chat.messages) == 2


# ---------------------------------------------------------------- секреты

@pytest.mark.parametrize('secret', ['пароль123', 'pa\\ss"word', 'abcd'])
def test_secret_is_hidden_in_every_representation(secret):
    for text in (f'echo {secret}', repr(f'echo {secret}'), repr(f'echo {secret}'.encode()),
                 json.dumps(f'echo {secret}')):
        masked = mask_secrets(text, [secret])
        assert '***' in masked
        assert secret not in masked and repr(secret.encode())[2:-1] not in masked


def test_short_secrets_are_not_masked_to_avoid_mangling_text():
    assert mask_secrets('abc abc', ['abc']) == 'abc abc'


# ---------------------------------------------------------------- ssh

def test_ssh_address_follows_double_dash(tmp_path):
    argv = ssh_argv(make_config(tmp_path))
    assert argv[-2] == '--' and '@' in argv[-1]


@pytest.mark.parametrize('field, value', [
    ('pam_user', '-Eloot'), ('target_host', '-oProxyCommand'), ('pam_host', '-x'),
])
def test_values_starting_with_dash_are_rejected(field, value):
    _, problems = cfg.parse_targets({'targets': {'a': {**BASE, field: value}}})
    assert any('недопустимые символы' in p for p in problems), problems


def test_dash_inside_names_is_still_fine():
    _, problems = cfg.parse_targets({'targets': {'a': {**BASE, 'pam_user': 'svc-mon', 'target_host': 'a-b.example.org'}}})
    assert problems == []


# ---------------------------------------------------------------- Telegram

def test_telegram_api_url_must_be_https_except_loopback():
    def problems(url):
        s = cfg.Settings(messenger='telegram', telegram_token='1:x', telegram_chat_id=5, telegram_api_url=url,
                         allowed_users=frozenset({'1'}))
        return [p for p in cfg.validate_environment(s, {}, {}, need_ssh=False) if 'TELEGRAM_API_URL' in p]

    assert problems('https://api.telegram.org') == []
    assert problems('http://127.0.0.1:8081') == [] and problems('http://localhost/') == []
    assert problems('http://api.telegram.org') and problems('http://evil.example.org') and problems('ftp://x')


def test_stale_telegram_commands_are_dropped():
    now = 1_000_000.0
    fresh = {'message': {'date': int(now) - 30, 'text': '/x', 'chat': {'id': 1}}}
    old = {'message': {'date': int(now) - 3600, 'text': '/x', 'chat': {'id': 1}}}
    assert not tg.TelegramMessenger.is_stale(fresh, now) and tg.TelegramMessenger.is_stale(old, now)
    assert not tg.TelegramMessenger.is_stale({'message': {'text': '/x'}}, now)      # без даты - не отбрасываем


def test_split_text_is_the_shared_splitter():
    text = 'line\n' * 2000
    assert tg.split_text(text, 100) == mailcheck.split_message(text, 100)


# ---------------------------------------------------------------- прочее

def test_osc_with_string_terminator_is_stripped():
    from monitor import engine as eng
    assert eng.clean_frame(b'\x1b]0;title\x1b\\ok\x1b]0;t2\x07!') == 'ok!'


def test_gitignore_covers_secret_backups():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / '.gitignore').read_text()
    assert '*.bak' in text and '.env.*' in text and '!.env.example' in text

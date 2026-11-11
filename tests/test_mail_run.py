"""Плановый и командный запуск почтовой цели (Engine.run): отчёт, молчание без ожидаемых машин, сбой IMAP."""
import logging
from datetime import date

import pytest

from monitor import engine as eng
from monitor.config import Settings, Target


class Chat:
    def __init__(self):
        self.messages = []

    def send_message(self, peer, text):
        self.messages.append((peer, text))

    def secrets(self):
        return []


def make(peers=()):
    target = Target(name='rk', title='RK', type='mail', password_env='P', imap_host='mail.example.org',
                    imap_user='u', expect=(('vm', 'daily'),), peers=peers)
    engine = eng.Engine(Settings(vk_peer_id=100), {'P': 'secretpass'}, logging.getLogger('mail-run'), {'rk': target})
    engine.chat = Chat()
    return engine, target


def test_scheduled_run_sends_report_to_default_and_extra_peers():
    engine, target = make(peers=('5', '6'))
    engine.mail_report = lambda t, d=None: ('отчёт', ['vm'], {})
    engine.run(target)
    assert engine.chat.messages == [(5, 'отчёт'), (6, 'отчёт')]


def test_scheduled_run_is_silent_when_nothing_was_expected():
    engine, target = make()
    engine.mail_report = lambda t, d=None: ('отчёт', [], {})
    engine.run(target)
    assert engine.chat.messages == []


def test_command_answers_even_when_nothing_expected_and_only_to_asker():
    engine, target = make(peers=('5',))
    engine.mail_report = lambda t, d=None: ('отчёт', [], {})
    engine.run(target, peer_id=42, arg='вчера')
    assert engine.chat.messages == [(42, 'отчёт')]


def test_imap_failure_is_reported_without_secrets():
    engine, target = make()

    def boom(t, d=None):
        raise OSError('imap down, password secretpass rejected')

    engine.mail_report = boom
    engine.run(target, peer_id=42)
    assert len(engine.chat.messages) == 1
    peer, text = engine.chat.messages[0]
    assert peer == 42 and text.startswith('Ошибка мониторинга (RK)') and 'secretpass' not in text


@pytest.mark.parametrize('arg', ['не-дата', '32.13'])
def test_unclear_date_argument_means_today(arg):
    engine, target = make()
    seen = []
    engine.mail_report = lambda t, d=None: (seen.append(d), ('отчёт', ['vm'], {}))[1]
    engine.run(target, peer_id=42, arg=arg)
    assert seen == [date.today()] and engine.chat.messages == [(42, 'отчёт')]

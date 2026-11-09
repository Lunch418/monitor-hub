"""Движок против настоящего ssh и поддельного PAM-шлюза (paramiko): вход, меню, ls."""
import logging
import shutil

import pytest

from monitor import engine as eng
from monitor.config import Settings, Target
from tests.fakes.pam_server import FakePamServer

pytestmark = pytest.mark.skipif(shutil.which('ssh') is None, reason='нужен openssh-client')

USER, PASSWORD = 'pamuser', 'pampass'


def make(port, tmp_path, **target_kw):
    settings = Settings(
        pam_host='127.0.0.1', pam_port=str(port), strict_host_key='no',
        kex='ecdh-sha2-nistp256', hostkey_algs='rsa-sha2-256', strict_algs=True,
        prompt_nudge=1, login_gap=0, known_hosts=str(tmp_path / 'kh'), save_dir=str(tmp_path / 'shots'),
        vk_token='tok', vk_peer_id=1)
    base = dict(name='gw', title='GW', pam_user=USER, password_env='GW_PW', target_host='10.0.0.1',
                target_sel='1', paths=('/srv/crl',), prompt_timeout=30, connect_retries=2,
                connect_retry_delay=0)
    base.update(target_kw)
    target = Target(**base)
    return eng.Engine(settings, {'GW_PW': PASSWORD}, logging.getLogger('gw'), {'gw': target}), target


@pytest.fixture
def server_factory():
    servers = []

    def _make(**kw):
        server = FakePamServer(USER, PASSWORD, **kw)
        servers.append(server)
        return server

    yield _make
    for server in servers:
        server.close()


def test_login_and_listing(server_factory, tmp_path):
    server = server_factory()
    engine, target = make(server.port, tmp_path)
    listings = engine.fetch_listings(target)
    assert any('issuing-ca.crl' in line for line in listings['/srv/crl'])
    assert ('target', '10.0.0.1') in server.events


def test_all_paths_in_one_session(server_factory, tmp_path):
    server = server_factory()
    engine, target = make(server.port, tmp_path, paths=('/srv/a', '/srv/b', '/srv/c'))
    listings = engine.fetch_listings(target)
    assert set(listings) == {'/srv/a', '/srv/b', '/srv/c'}
    assert [e for e in server.events if e[0] == 'login'] == [('login', 1)]
    assert [e[1] for e in server.events if e[0] == 'ls'] == ['/srv/a', '/srv/b', '/srv/c']


def test_sudo_flag_reaches_the_remote_command(server_factory, tmp_path):
    server = server_factory()
    engine, target = make(server.port, tmp_path, sudo=True)
    engine.fetch_listings(target)
    assert ('ls', '/srv/crl', True) in server.events


def test_prompt_nudge_when_prompt_is_not_visible(server_factory, tmp_path):
    server = server_factory(prompt_needs_enter=True)
    engine, target = make(server.port, tmp_path)
    assert engine.fetch_listings(target)['/srv/crl']
    assert ('enter_received', None) in server.events


def test_dropped_first_connection_is_retried(server_factory, tmp_path):
    server = server_factory(drop_first=True)
    engine, target = make(server.port, tmp_path)
    assert engine.fetch_listings(target)['/srv/crl']
    assert len([e for e in server.events if e[0] == 'login']) == 2


def test_wrong_password_is_a_clean_error_without_secret(server_factory, tmp_path):
    server = server_factory()
    engine, target = make(server.port, tmp_path, connect_retries=1)
    engine.env = {'GW_PW': 'wrong-password'}
    with pytest.raises(eng.HubError) as exc:
        engine.fetch_listings(target)
    assert 'wrong-password' not in str(exc.value) and 'wrong-password' not in exc.value.user_message


def test_permission_denied_gives_empty_listing_and_clear_error(server_factory, tmp_path):
    server = server_factory(denied=['/srv/crl'])
    engine, target = make(server.port, tmp_path)
    with pytest.raises(eng.HubError):
        engine.collect(target)


def test_full_check_without_sending(server_factory, tmp_path):
    server = server_factory()
    engine, target = make(server.port, tmp_path, style='listing')
    saved = engine.check(target, send=False)
    assert len(saved) == 1


def test_invalid_config_stops_before_any_connection(server_factory, tmp_path):
    server = server_factory()
    engine, target = make(server.port, tmp_path)
    engine.settings = Settings(pam_host='', pam_port='2222')
    with pytest.raises(eng.ConfigError):
        engine.fetch_listings(target)
    assert server.events == []


def test_check_paths_one_login_and_verdicts(server_factory, tmp_path):
    server = server_factory(states={'/srv/none': 'noent', '/srv/file': 'notdir',
                                    '/srv/closed': 'denied', '/srv/pg': 'sudo'})
    engine, target = make(server.port, tmp_path)
    paths = ('/srv/ok', '/srv/none', '/srv/file', '/srv/closed', '/srv/pg')
    verdicts = engine.check_paths(target, paths)
    assert [v.code for v in verdicts] == ['ok', 'noent', 'notdir', 'denied', 'sudo']
    assert verdicts[0].entries == 3 and str(verdicts[0]) == 'ok, записей: 3'
    assert str(verdicts[4]) == 'нужен sudo, записей: 3'
    assert [e for e in server.events if e[0] == 'login'] == [('login', 1)]


def test_check_paths_with_sudo_target_accepts_sudo(server_factory, tmp_path):
    server = server_factory(states={'/srv/pg': 'sudo'})
    engine, target = make(server.port, tmp_path, sudo=True, paths=('/srv/pg',))
    assert engine.check_paths(target)[0].ok

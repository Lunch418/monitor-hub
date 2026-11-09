import os

import pytest

from monitor import config as cfg
from monitor import runlog
from monitor.app import load_env, unquote
from monitor.envfile import atomic_write, set_env_value


def test_set_env_value_replaces_and_appends_keeping_the_rest(tmp_path):
    env = tmp_path / '.env'
    env.write_text('# комментарий\nA=1\nexport B=2\n\nC=3\n')
    set_env_value(env, 'B', 'new')
    set_env_value(env, 'D', 'x y')
    assert env.read_text() == "# комментарий\nA=1\nB=new\n\nC=3\nD='x y'\n"
    assert oct(os.stat(env).st_mode)[-3:] == '600'
    assert (tmp_path / '.env.bak').exists()


@pytest.mark.parametrize('value', ['plain', 'with space', "it's", 'say "hi"', '', 'a=b#c', '$HOME'])
def test_set_env_value_roundtrips_through_load_env(tmp_path, monkeypatch, value):
    set_env_value(tmp_path / '.env', 'SECRET_X', value)
    monkeypatch.delenv('SECRET_X', raising=False)
    load_env(str(tmp_path))
    assert os.environ['SECRET_X'] == value
    monkeypatch.delenv('SECRET_X')


@pytest.mark.parametrize('key,value', [('bad key', 'x'), ('K', 'a\nb'), ('K', 'both \' and "')])
def test_set_env_value_rejects(tmp_path, key, value):
    with pytest.raises(ValueError):
        set_env_value(tmp_path / '.env', key, value)


def test_atomic_write_without_previous_file(tmp_path):
    assert atomic_write(tmp_path / 'f', 'x', mode=0o640) is None
    assert (tmp_path / 'f').read_text() == 'x' and oct(os.stat(tmp_path / 'f').st_mode)[-3:] == '640'
    assert not list(tmp_path.glob('.f.*'))


def test_unquote_matches_writer():
    assert unquote("'a b'") == 'a b' and unquote('"it\'s"') == "it's"


def test_dump_and_parse_roundtrip(tmp_path):
    data = {
        'defaults': {'stale_days': 3},
        'targets': {
            'crl': {'title': 'CRL "главный"', 'pam_user': 'u', 'password_env': 'P', 'target_host': 'h',
                    'paths': ['/a b', '/c'], 'schedule': ['mon 09:00', 'fri 10:00'], 'command': '/get',
                    'style': 'listing', 'peers': ['5'], 'stale_days': 7, 'run_timeout': 120},
            'pg': {'pam_user': 'u', 'password_env': 'Q', 'target_host': 'h', 'paths': ['/p'], 'sudo': True,
                   'target_sel': 'admin'},
        },
    }
    targets, problems = cfg.parse_targets(data)
    assert problems == []
    path = tmp_path / 'targets.toml'
    cfg.save_targets(path, targets, data['defaults'])
    again, problems = cfg.load_targets(path)
    assert problems == [] and again == targets
    text = path.read_text()
    assert 'stale_days = 3' in text and 'title = "CRL \\"главный\\""' in text
    assert 'sudo' not in text.split('[targets.pg]')[0]          # значение по умолчанию не пишется
    assert cfg.read_defaults(path) == {'stale_days': 3}


def test_save_targets_refuses_broken_data(tmp_path):
    bad = cfg.Target(name='x', title='x', pam_user='bad user', password_env='P', target_host='h',
                     target_sel='1', paths=('/p',))
    with pytest.raises(ValueError):
        cfg.save_targets(tmp_path / 't.toml', {'x': bad})
    assert not (tmp_path / 't.toml').exists()


def test_read_defaults_of_missing_or_broken_file(tmp_path):
    assert cfg.read_defaults(tmp_path / 'none') == {}
    (tmp_path / 'b.toml').write_text('[x')
    assert cfg.read_defaults(tmp_path / 'b.toml') == {}


def test_runlog_record_read_and_age(tmp_path):
    assert runlog.read(tmp_path) == {}
    runlog.record(tmp_path, 'a', True, 'ok', now=100.0)
    runlog.record(tmp_path, 'b', False, 'x' * 500)
    data = runlog.read(tmp_path)
    assert data['a'] == {'ok': True, 'message': 'ok', 'at': 100.0} and len(data['b']['message']) == 300
    (tmp_path / runlog.FILE_NAME).write_text('[1, 2]')
    assert runlog.read(tmp_path) == {}
    assert [runlog.age_text(s) for s in (5, 125, 7300, 200000, -3)] == ['5с', '2м', '2ч', '2д', '0с']


def test_runlog_listing_roundtrip_and_garbage(tmp_path):
    entry = {'name': 'a', 'raw_name': 'a', 'size_disp': '1', 'date_str': 'Nov  9 06:00',
             'is_dir': False, 'is_exec': False, 'is_link': False, 'mtime': object()}
    runlog.save_listing(tmp_path, 't', 'listing', [('/p', [entry], {'a'})])
    style, _, listings = runlog.read_listing(tmp_path, 't')
    assert style == 'listing' and listings[0][0] == '/p' and listings[0][2] == {'a'}
    assert 'mtime' not in listings[0][1][0]
    (tmp_path / 'listings' / 't.json').write_text('{}')
    assert runlog.read_listing(tmp_path, 't') is None
    assert runlog.read_listing(tmp_path, 'none') is None

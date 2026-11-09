import io
import logging

import pytest
from PIL import Image

from monitor import engine as eng
from monitor.config import Settings, Target

LINES = [
    'drwxr-xr-x 2 root root 4096 Jan  5  2024 .',
    'drwxr-xr-x 9 root root 4096 Sep  5  2025 ..',
    'drwxr-xr-x 2 root root 4096 Oct 18 09:00 sub',
    '-rwxr-xr-x 1 root root 1234 Oct 18 09:00 run.sh',
    '-rw-r--r-- 1 root root 77 Mar  1  2020 old.bak',
    '-rw-r--r-- 1 root root 7 Oct 18 09:00 new.bak',
]


def make_target(**kw):
    base = dict(name='t', title='T', pam_user='u', password_env='T_PW', target_host='h',
                target_sel='1', paths=('/p',))
    base.update(kw)
    return Target(**base)


@pytest.fixture
def engine(tmp_path):
    settings = Settings(vk_token='vk-secret-token', save_dir=str(tmp_path / 'shots'), vk_peer_id=11)
    return eng.Engine(settings, {'T_PW': 'pw-secret-value'}, logging.getLogger('test_engine'),
                      {'t': make_target()})


def test_ls_command_markers_are_not_in_typed_text():
    begin, end, command = eng.ls_command(0, '/a b/c')
    assert begin == '__HUB0_BEG__' and end == '__HUB0_END__'
    assert begin not in command and end not in command
    assert "'/a b/c'" in command and 'sudo' not in command


def test_ls_command_sudo_is_noninteractive_and_scoped():
    command = eng.ls_command(2, '/x', sudo=True)[2]
    assert 'sudo -n env LANG=C LC_ALL=C ls -la /x;' in command


def test_ls_command_quotes_path():
    command = eng.ls_command(0, "/a'; reboot; '")[2]
    assert 'ls -la \'/a\'"\'"\'; reboot; \'"\'"\'\'' in command


def test_report_png_and_caption_single_path(engine):
    report = engine.build_report(make_target(style='listing', stale_days=30), {'/p': LINES})
    assert len(report.buffers) == 1
    assert Image.open(io.BytesIO(report.buffers[0].getvalue())).format == 'PNG'
    caption = report.caption()
    assert 'Файлов: 4' in caption and 'Устарело (>30 дн.): 1' in caption


def test_backups_caption_counts_files_only(engine):
    report = engine.build_report(make_target(style='backups'), {'/p': LINES})
    assert 'Бэкапов: 3' in report.caption() and 'Устарело' not in report.caption()


def test_multiple_paths_caption_is_only_timestamp(engine):
    report = engine.build_report(make_target(paths=('/a', '/b')), {'/a': LINES, '/b': LINES})
    assert len(report.buffers) == 2
    assert '|' not in report.caption()


def test_empty_path_is_skipped_but_all_empty_is_error(engine):
    report = engine.build_report(make_target(paths=('/a', '/b')), {'/a': LINES, '/b': []})
    assert len(report.buffers) == 1
    with pytest.raises(eng.HubError) as exc:
        engine.build_report(make_target(), {'/p': []})
    assert 'пуст' in exc.value.user_message


def test_long_listing_split_into_pages(engine):
    rows = [f'-rw-r--r-- 1 root root 1 Oct 18 09:00 f{i:04d}' for i in range(50)]
    engine.settings = Settings(vk_token='x', save_dir=engine.settings.save_dir, max_rows=20)
    report = engine.build_report(make_target(), {'/p': rows})
    assert len(report.buffers) == 3


def test_secrets_are_masked(engine):
    target = make_target()
    text = engine.mask('boom pw-secret-value and vk-secret-token', target)
    assert 'pw-secret-value' not in text and 'vk-secret-token' not in text
    assert 'pw-secret-value' not in engine.mask('x pw-secret-value')      # без цели: пароли всех целей


def test_user_facing_hides_details(engine):
    assert engine.user_facing(eng.HubError('raw banner', 'коротко')) == 'коротко'
    assert 'banner' not in engine.user_facing(RuntimeError('raw banner'))


def test_peers_for(engine):
    assert engine.peers_for(make_target()) == (11, [])
    default, extras = engine.peers_for(make_target(peers=('5', '6')))
    assert default == 5 and extras == ['6']


def test_check_no_send_saves_and_never_touches_vk(engine, monkeypatch):
    target = make_target()
    monkeypatch.setattr(engine, 'fetch_listings', lambda t: {'/p': LINES})
    monkeypatch.setattr(engine.vk, 'send_photos', lambda *a, **k: pytest.fail('sent'))
    saved = engine.check(target, send=False)
    assert len(saved) == 1 and saved[0].endswith('.png')


def test_check_sends_to_default_peer(engine, monkeypatch):
    sent = []
    monkeypatch.setattr(engine, 'fetch_listings', lambda t: {'/p': LINES})
    monkeypatch.setattr(engine.vk, 'send_photos', lambda paths, caption, peer: sent.append((paths, peer)))
    engine.check(make_target(peers=('9',)), send=True)
    assert sent[0][1] == 9


def test_run_reports_error_to_chat_without_raising(engine, monkeypatch):
    messages = []

    def boom(target):
        raise eng.HubError('detail pw-secret-value', 'Не получилось')

    monkeypatch.setattr(engine, 'collect', boom)
    monkeypatch.setattr(engine.vk, 'send_message', lambda peer, text: messages.append((peer, text)))
    engine.run(make_target(title='Моя цель'), peer_id=42)
    assert messages == [(42, 'Ошибка мониторинга (Моя цель): Не получилось')]


def test_run_scheduled_goes_to_default_and_extras(engine, monkeypatch, no_sleep):
    sent = []
    monkeypatch.setattr(engine, 'fetch_listings', lambda t: {'/p': LINES})
    monkeypatch.setattr(engine.vk, 'send_photos', lambda paths, caption, peer: sent.append(peer))
    engine.run(make_target(peers=('5', '6')))
    assert sent == [5, 6]

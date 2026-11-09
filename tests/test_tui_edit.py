"""Редактирование всего через интерфейс: типы целей, /set, /ls, общие настройки, заголовок вкладки."""
import asyncio
from pathlib import Path

import pytest

pytest.importorskip('textual')

from textual.widgets import Input, RichLog, Select

from monitor import engine, runlog, tui
from monitor.config import load_targets
from tests.test_tui import log_text, run, settle
from tests.tui_common import LINES


def make_app(project):
    return tui.HubApp(project / 'targets.toml', project, env_path=project / '.env')


def visible_ids(app):
    return {f.id for f in tui.FIELDS if app.query_one(f'#row-{f.id}').display}


async def type_into(pilot, app, widget_id, text):
    box = app.query_one(widget_id, Input)
    box.focus()
    box.value = text
    await pilot.pause()


def test_rows_follow_type_and_login_method(project):
    async def scenario():
        app = make_app(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            ids = visible_ids(app)
            assert {'pam_user', 'pam_host', 'paths', 'sudo'} <= ids
            assert not ({'user', 'ssh_port', 'imap_host', 'expect'} & ids)
            app.query_one('#f-via', Select).value = 'ssh'
            await pilot.pause()
            ids = visible_ids(app)
            assert {'user', 'ssh_port', 'paths'} <= ids and not ({'pam_user', 'pam_host', 'target_sel'} & ids)
            app.query_one('#f-type', Select).value = 'mail'
            await pilot.pause()
            ids = visible_ids(app)
            assert {'imap_host', 'imap_user', 'expect', 'folders', 'night_from'} <= ids
            assert not ({'paths', 'via', 'user', 'pam_user', 'sudo', 'style'} & ids)
    run(scenario())


def test_create_mail_target_and_save(project):
    async def scenario():
        app = make_app(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            await type_into(pilot, app, '#prompt-input', '/new rk mail')
            await pilot.press('enter')
            await pilot.pause()
            assert app.current is None and app._kind() == 'mail'
            assert app.query_one('#f-name', Input).value == 'rk'
            for field, value in (('imap_host', 'mail.example.org'), ('imap_user', 'bk@example.org'),
                                 ('expect', 'app-01=mon,tue; db-01=daily'), ('command', '/rk'),
                                 ('schedule', 'daily 09:10'), ('password', 'p@ss word')):
                await type_into(pilot, app, f'#f-{field}', value)
            await pilot.press('ctrl+s')
            await pilot.pause()
            assert 'Сохранено: rk' in log_text(app), log_text(app)
            targets, problems = load_targets(project / 'targets.toml')
            assert problems == []
            rk = targets['rk']
            assert rk.type == 'mail' and dict(rk.expect) == {'app-01': 'mon,tue', 'db-01': 'daily'}
            assert rk.password_env == 'RK_MAIL_PASSWORD'
            assert "RK_MAIL_PASSWORD='p@ss word'" in (project / '.env').read_text(encoding='utf-8')
            assert (project / '.env').stat().st_mode & 0o777 == 0o600
            assert set(targets) == {'crl', 'pg', 'rk'}                    # остальные цели не пострадали
    run(scenario())


def test_save_direct_ssh_target(project):
    async def scenario():
        app = make_app(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            app.action_new('ls', 'vm1')
            await pilot.pause()
            app.query_one('#f-via', Select).value = 'ssh'
            await pilot.pause()
            for field, value in (('user', 'monitor'), ('target_host', 'vm.example.org'), ('ssh_port', '2200'),
                                 ('paths', '/srv/data'), ('password', 'pw')):
                await type_into(pilot, app, f'#f-{field}', value)
            await pilot.press('ctrl+s')
            await pilot.pause()
            targets, problems = load_targets(project / 'targets.toml')
            assert problems == [], log_text(app)
            vm = targets['vm1']
            assert (vm.via, vm.user, vm.ssh_port, vm.password_env) == ('ssh', 'monitor', 2200, 'VM1_SSH_PASSWORD')
            assert vm.pam_user == ''
    run(scenario())


def test_set_command_changes_form_then_save(project):
    async def scenario():
        app = make_app(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            await type_into(pilot, app, '#prompt-input', '/set command /newcmd')
            await pilot.press('enter')
            await pilot.pause()
            assert app.query_one('#f-command', Input).value == '/newcmd'
            await type_into(pilot, app, '#prompt-input', '/set sudo да')
            await pilot.press('enter')
            await type_into(pilot, app, '#prompt-input', '/set nope 1')
            await pilot.press('enter')
            await pilot.pause()
            assert 'нет такого поля' in log_text(app)
            await type_into(pilot, app, '#prompt-input', '/save')
            await pilot.press('enter')
            await pilot.pause()
            targets, _ = load_targets(project / 'targets.toml')
            assert targets['crl'].command == '/newcmd' and targets['crl'].sudo
    run(scenario())


def test_set_rejects_field_of_other_type(project):
    async def scenario():
        app = make_app(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            await type_into(pilot, app, '#prompt-input', '/set imap_host x')
            await pilot.press('enter')
            await pilot.pause()
            assert 'не относится' in log_text(app)
    run(scenario())


def test_ls_shows_folder_now_without_changing_config(project, monkeypatch):
    seen = []
    monkeypatch.setattr(engine.Engine, 'fetch_listings',
                        lambda self, t, paths=None: seen.append(paths) or dict.fromkeys(paths or t.paths, LINES))

    async def scenario():
        app = make_app(project)
        before = (project / 'targets.toml').read_text(encoding='utf-8')
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            await type_into(pilot, app, '#prompt-input', '/ls /var/log/app')
            await pilot.press('enter')
            await settle(pilot, app)
            assert seen == [('/var/log/app',)]
            preview = str(app.query_one('#preview').render())
            assert '/var/log/app' in preview and 'issuing-ca.crl' in preview
            assert app._window_title.startswith('[ok] ls /var/log/app')
            assert (project / 'targets.toml').read_text(encoding='utf-8') == before
            assert runlog.read(project / 'state').get('crl') is None        # одноразовый просмотр не пишет итог
            await type_into(pilot, app, '#prompt-input', '/ls /x;rm -rf')
            await pilot.press('enter')
            await pilot.pause()
            assert 'не подходит' in log_text(app) and len(seen) == 1
    run(scenario())


def test_ls_on_mail_target_is_refused(project):
    async def scenario():
        app = make_app(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            app.query_one('#f-type', Select).value = 'mail'
            await pilot.pause()
            app.action_ls('/var')
            await pilot.pause()
            assert 'типа ls' in log_text(app)
    run(scenario())


def test_mail_check_shows_report(project, monkeypatch):
    (project / 'targets.toml').write_text('''
[targets.rk]
type = "mail"
title = "Backups"
password_env = "RK_PW"
imap_host = "mail.example.org"
imap_user = "u"
[targets.rk.expect]
app-01 = "daily"
''', encoding='utf-8')
    monkeypatch.setenv('RK_PW', 'x')
    monkeypatch.setattr(engine.mailcheck, 'fetch_results', lambda *a, **k: {
        'app-01': {'status': 'ok', 'finish': '03:10', 'dur': '5:09', 'dt': None}})

    async def scenario():
        app = make_app(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            await type_into(pilot, app, '#prompt-input', '/run rk')
            await pilot.press('enter')
            await settle(pilot, app)
            preview = str(app.query_one('#preview').render())
            assert '[ok] app-01 (в 03:10, за 5:09)' in preview, log_text(app)
            assert app._window_title.startswith('[ok] готово: rk')
            assert runlog.read(project / 'state')['rk']['ok']
            assert runlog.read_report(project / 'state', 'rk') is not None
    run(scenario())


def test_settings_screen_writes_env(project, monkeypatch):
    async def scenario():
        app = make_app(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            await pilot.press('ctrl+e')
            await pilot.pause()
            assert isinstance(app.screen, tui.SettingsScreen)
            app.screen.query_one('#e-MESSENGER', Select).value = 'telegram'
            app.screen.query_one('#e-TELEGRAM_TOKEN', Input).value = '123:secret-token'
            app.screen.query_one('#e-TELEGRAM_CHAT_ID', Input).value = '-1001'
            await pilot.press('ctrl+s')
            await pilot.pause()
            assert not isinstance(app.screen, tui.SettingsScreen)
            env = (project / '.env').read_text(encoding='utf-8')
            assert 'MESSENGER=telegram' in env and 'TELEGRAM_CHAT_ID=-1001' in env
            assert app.settings.messenger == 'telegram' and app.settings.telegram_chat_id == -1001
            text = log_text(app)
            assert 'MESSENGER' in text and 'secret-token' not in text        # токен в журнал не попадает
    run(scenario())


def test_settings_validation_blocks_save(project):
    async def scenario():
        app = make_app(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            await pilot.press('ctrl+e')
            await pilot.pause()
            app.screen.query_one('#e-VK_GROUP_ID', Input).value = 'abc'
            await pilot.press('ctrl+s')
            await pilot.pause()
            assert isinstance(app.screen, tui.SettingsScreen)               # окно осталось
            assert 'нужно целое число' in str(app.screen.query_one('#settings-problems').render())
            assert 'VK_GROUP_ID' not in (project / '.env').read_text(encoding='utf-8')
            await pilot.press('escape')
            await pilot.pause()
            assert not isinstance(app.screen, tui.SettingsScreen)
    run(scenario())


def test_help_lists_commands_and_idle_title(project):
    async def scenario():
        app = make_app(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            assert app._window_title == 'целей: 2 - monitor-hub'
            runlog.record(project / 'state', 'pg', False, 'сбой')
            app._idle_title()
            assert app._window_title == '[!] целей: 2, с ошибкой: 1 - monitor-hub'
            await type_into(pilot, app, '#prompt-input', '/help')
            await pilot.press('enter')
            await pilot.pause()
            text = log_text(app)
            for word in ('/ls', '/set', '/settings', '/send', '/new'):
                assert word in text
    run(scenario())


def test_settings_screen_has_every_env_field(project):
    async def scenario():
        app = make_app(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            await pilot.press('ctrl+e')
            await pilot.pause()
            for field in tui.ENV_FIELDS:
                assert app.screen.query_one(f'#e-{field.key}')
    run(scenario())


def test_log_widget_exists_and_asyncio_unused_import_guard():
    assert asyncio and Path and RichLog


def test_live_view_streams_and_stops(project, monkeypatch):
    seen = {}

    def fake_live(self, target, command, on_frame, stop, **kw):
        seen['command'] = command
        for i in range(3):
            on_frame(f'top - 12:00:0{i} up 3 days')
            stop.wait(0.05)
        stop.wait(5)
        return 3

    monkeypatch.setattr(engine.Engine, 'live', fake_live)

    async def scenario():
        app = make_app(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            await type_into(pilot, app, '#prompt-input', '/live top')
            await pilot.press('enter')
            await pilot.pause(0.5)
            assert seen['command'] == 'top' and app._live_stop is not None
            assert 'top - 12:00:0' in str(app.query_one('#preview').render())
            assert app._window_title.startswith('[..] live: top')
            await pilot.press('escape')                       # escape останавливает просмотр
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._live_stop is None
            assert 'Просмотр остановлен' in log_text(app)
            assert app._window_title.startswith('[ok] live стоп')
    run(scenario())


def test_live_rejects_bad_command_and_mail_target(project):
    async def scenario():
        app = make_app(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            app.action_live('')
            await pilot.pause()
            assert 'Команда не подходит' in log_text(app) and app._live_stop is None
            app.query_one('#f-type', Select).value = 'mail'
            await pilot.pause()
            app.action_live('top')
            await pilot.pause()
            assert '/live работает для целей типа ls' in log_text(app)
    run(scenario())

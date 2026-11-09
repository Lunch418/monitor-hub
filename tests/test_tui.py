import asyncio
import os

import pytest

pytest.importorskip('textual')

from textual.widgets import DataTable, Input, RichLog, Select

from monitor import engine, runlog, tui
from monitor.config import tomllib
from monitor.formspec import split_list
from tests.tui_common import LINES  # noqa: F401


def make_app(project):
    return tui.HubApp(project / 'targets.toml', project, env_path=project / '.env')


def run(coro):
    asyncio.run(coro)


def log_text(app):
    return '\n'.join(line.text for line in app.query_one('#log', RichLog).lines)


async def settle(pilot, app, seconds=0.1):
    await pilot.pause(seconds)
    await app.workers.wait_for_complete()
    await pilot.pause()


def test_targets_listed_and_first_selected(project):
    async def scenario():
        app = make_app(project)
        async with app.run_test(size=(160, 44)) as pilot:
            await pilot.pause()
            table = app.query_one('#targets', DataTable)
            assert table.row_count == 2
            assert app.current == 'crl'
            assert app.query_one('#f-paths', Input).value == '/var/crl'
            assert app.query_one('#f-name', Input).disabled
            assert app.query_one('#f-style', Select).value == 'listing'
            assert 'PG_PW' in log_text(app)                 # нет пароля у второй цели
            await pilot.press('down')
            assert app.current == 'pg'
            assert app.query_one('#f-sudo', Select).value == 'yes'
            assert app.query_one('#f-paths', Input).value == '/mnt/a, /mnt/b'
    run(scenario())


def test_check_shows_listing_and_sets_title(project):
    async def scenario():
        app = make_app(project)
        async with app.run_test(size=(160, 44)) as pilot:
            await pilot.pause()
            await pilot.press('ctrl+r')
            await settle(pilot, app)
            assert app._window_title.startswith('[ok] готово: crl')
            assert 'Готово (ничего не отправлялось)' in log_text(app)
            preview = str(app.query_one('#preview').render())
            assert 'issuing-ca.crl' in preview and '*update.sh' in preview
            assert runlog.read(project / 'state')['crl']['ok']
            row = app.query_one('#targets', DataTable).get_row('crl')
            assert str(row[0]) == '+'
    run(scenario())


def test_check_failure_is_reported_without_crash(project, monkeypatch):
    def boom(self, target):
        raise engine.HubError('detail secret', 'Не удалось пройти этап на jump-хосте')

    monkeypatch.setattr(engine.Engine, 'fetch_listings', boom)

    async def scenario():
        app = make_app(project)
        async with app.run_test(size=(160, 44)) as pilot:
            await pilot.pause()
            await pilot.press('ctrl+r')
            await settle(pilot, app)
            assert app._window_title.startswith('[!] ошибка: crl')
            text = log_text(app)
            assert 'Не удалось пройти этап' in text and 'secret' not in text
            assert str(app.query_one('#targets', DataTable).get_row('crl')[0]) == '!'
    run(scenario())


def test_check_without_password_asks_for_it(project):
    async def scenario():
        app = make_app(project)
        async with app.run_test(size=(160, 44)) as pilot:
            await pilot.pause()
            await pilot.press('down', 'ctrl+r')
            await pilot.pause()
            assert 'PG_PW' in log_text(app).split('Не хватает настроек')[1]
    run(scenario())


def test_send_requires_vk_settings(project):
    async def scenario():
        app = make_app(project)
        async with app.run_test(size=(160, 44)) as pilot:
            await pilot.pause()
            await pilot.press('ctrl+k')
            await pilot.pause()
            assert 'VK_TOKEN не задан' in log_text(app)
    run(scenario())


def test_new_target_saved_to_toml_and_password_to_env(project):
    async def scenario():
        app = make_app(project)
        async with app.run_test(size=(160, 44)) as pilot:
            await pilot.pause()
            await pilot.press('ctrl+n')
            assert app.current is None and not app.query_one('#f-name', Input).disabled
            values = {'name': 'new', 'title': 'Новая', 'pam_user': 'svc', 'password': "p'w d",
                      'target_host': '192.0.2.99', 'paths': '/data, /data/old',
                      'schedule': 'mon 08:30, fri 08:30', 'command': '/new'}
            for key, value in values.items():
                app.query_one(f'#f-{key}', Input).value = value
            await pilot.press('ctrl+s')
            await pilot.pause()
            assert 'Сохранено: new' in log_text(app)
            assert app.current == 'new'
            assert app.query_one('#targets', DataTable).row_count == 3
    run(scenario())
    data = tomllib.loads((project / 'targets.toml').read_text(encoding='utf-8'))
    new = data['targets']['new']
    assert new['paths'] == ['/data', '/data/old'] and new['schedule'] == ['mon 08:30', 'fri 08:30']
    assert new['password_env'] == 'NEW_PAM_PASSWORD'
    assert "p'w d" not in (project / 'targets.toml').read_text()   # пароль только в .env
    assert data['targets']['pg']['sudo'] is True                   # остальные цели на месте
    env_text = (project / '.env').read_text()
    assert 'CRL_PW=secret' in env_text and 'NEW_PAM_PASSWORD="p\'w d"' in env_text
    assert oct(os.stat(project / '.env').st_mode)[-3:] == '600'
    assert (project / 'targets.toml.bak').exists()
    from monitor.app import unquote
    assert unquote(env_text.splitlines()[-1].split('=', 1)[1]) == "p'w d"


def test_invalid_form_is_not_saved(project):
    before = (project / 'targets.toml').read_text()

    async def scenario():
        app = make_app(project)
        async with app.run_test(size=(160, 44)) as pilot:
            await pilot.pause()
            app.query_one('#f-paths', Input).value = 'relative/path'
            app.query_one('#f-schedule', Input).value = 'someday 25:00'
            await pilot.press('ctrl+s')
            await pilot.pause()
            text = log_text(app)
            assert 'Не сохранено' in text and 'абсолютный путь' in text and 'день' in text
    run(scenario())
    assert (project / 'targets.toml').read_text() == before


def test_new_target_needs_password_and_unique_name(project):
    async def scenario():
        app = make_app(project)
        async with app.run_test(size=(160, 44)) as pilot:
            await pilot.pause()
            await pilot.press('ctrl+n')
            for key, value in {'name': 'x', 'pam_user': 'u', 'target_host': 'h', 'paths': '/p'}.items():
                app.query_one(f'#f-{key}', Input).value = value
            await pilot.press('ctrl+s')
            await pilot.pause()
            assert 'введите пароль' in log_text(app)
            app.query_one('#f-name', Input).value = 'crl'
            await pilot.press('ctrl+s')
            await pilot.pause()
            assert 'цель crl уже есть' in log_text(app)
    run(scenario())


def test_delete_needs_second_press(project):
    async def scenario():
        app = make_app(project)
        async with app.run_test(size=(160, 44)) as pilot:
            await pilot.pause()
            await pilot.press('ctrl+d')
            await pilot.pause()
            assert 'ещё раз' in log_text(app)
            assert 'crl' in app.targets
            await pilot.press('ctrl+d')
            await pilot.pause()
            assert 'crl' not in app.targets and app.current == 'pg'
    run(scenario())
    assert 'crl' not in tomllib.loads((project / 'targets.toml').read_text())['targets']


def test_broken_file_disables_saving(project):
    (project / 'targets.toml').write_text('[targets.a]\npam_user = "bad user"\n')

    async def scenario():
        app = make_app(project)
        async with app.run_test(size=(160, 44)) as pilot:
            await pilot.pause()
            assert 'сохранение отключено' in log_text(app)
            await pilot.press('ctrl+s')
            await pilot.pause()
            assert 'исправьте его вручную' in log_text(app)
    run(scenario())
    assert 'bad user' in (project / 'targets.toml').read_text()


def test_missing_file_starts_with_new_target(project):
    (project / 'targets.toml').unlink()

    async def scenario():
        app = make_app(project)
        async with app.run_test(size=(160, 44)) as pilot:
            await pilot.pause()
            assert app.current is None
            assert 'ещё нет' in log_text(app)
    run(scenario())


def test_prompt_selects_and_runs(project):
    async def scenario():
        app = make_app(project)
        async with app.run_test(size=(160, 44)) as pilot:
            await pilot.pause()
            prompt = app.query_one('#prompt-input', Input)
            prompt.focus()
            prompt.value = 'pg'
            await pilot.press('enter')
            assert app.current == 'pg'
            prompt.value = '/get'
            await pilot.press('enter')
            await settle(pilot, app)
            assert app.current == 'crl' and app._window_title.startswith('[ok]')
            prompt.value = 'nope'
            await pilot.press('enter')
            assert 'Нет такой цели или команды: nope' in log_text(app)
            prompt.value = '/new extra'
            await pilot.press('enter')
            assert app.current is None and app.query_one('#f-name', Input).value == 'extra'
    run(scenario())


def test_check_paths_shows_verdicts(project, monkeypatch):
    from monitor.engine import PathVerdict
    monkeypatch.setattr(engine.Engine, 'check_paths',
                        lambda self, t: [PathVerdict('/var/crl', 'ok', 3), PathVerdict('/x', 'noent')])

    async def scenario():
        app = make_app(project)
        async with app.run_test(size=(160, 44)) as pilot:
            await pilot.pause()
            await pilot.press('ctrl+p')
            await settle(pilot, app)
            text = log_text(app)
            assert '+ /var/crl: ok, записей: 3' in text and '! /x: нет такого пути' in text
            assert 'Проблемных путей: 1 из 2' in text
            assert app._window_title.startswith('[!] пути: crl')
    run(scenario())


def test_status_and_saved_listing_preview(project):
    runlog.save_listing(project / 'state', 'pg', 'backups-sizes',
                        [('/mnt/a', [{'name': 'base.tar.gz', 'raw_name': 'base.tar.gz', 'size_disp': '9K',
                                      'date_str': 'Nov  9 06:00', 'is_dir': False, 'is_exec': False,
                                      'is_link': False}], set())])

    async def scenario():
        app = make_app(project)
        async with app.run_test(size=(160, 44)) as pilot:
            await pilot.pause()
            await pilot.press('ctrl+l')
            await pilot.pause()
            assert 'Демон не запущен' in log_text(app)
            assert 'следующий запуск crl' in log_text(app)
            await pilot.press('down')
            await pilot.pause()
            assert 'base.tar.gz' in str(app.query_one('#preview').render())
    run(scenario())


def test_screen_has_no_emoji_or_stray_symbols(project):
    async def scenario():
        app = make_app(project)
        async with app.run_test(size=(160, 44)) as pilot:
            await pilot.pause()
            await pilot.press('ctrl+r')
            await settle(pilot, app)
            svg = app.export_screenshot()
            text = ''.join(ch for ch in svg if ord(ch) > 127)
            allowed = ({chr(c) for c in range(0x400, 0x500)}      # кириллица
                       | {chr(c) for c in range(0x2500, 0x2580)}   # рамки панелей
                       | {chr(0x25bc), '\xa0'})                       # стрелка у Select
            assert set(text) <= allowed, set(text) - allowed
    run(scenario())


def test_image_preview_and_helpers(tmp_path):
    from PIL import Image
    path = tmp_path / 'x.png'
    Image.new('RGB', (100, 50), (19, 72, 140)).save(path)
    text = tui.image_preview(path, width=20)
    assert text.plain.count('\n') == 5 and set(text.plain) <= {' ', '\n'}
    assert split_list('/a, /b\n/c,,') == ['/a', '/b', '/c']
    assert tui.default_password_env('pg-main') == 'PG_MAIN_PAM_PASSWORD'

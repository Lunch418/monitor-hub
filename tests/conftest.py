"""Общие фикстуры: фальшивый vk_api, без сна и случайности."""
import itertools
import sys
import types
from pathlib import Path

import pytest

from tests.tui_common import LINES as TUI_LINES
from tests.tui_common import TOML as TUI_TOML

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class VkApiError(Exception):
    """Ошибка API ВК: у настоящей есть .code, по нему решается, повторять ли."""

    def __init__(self, code, message):
        super().__init__(f'[{code}] {message}')
        self.code = code


class FakeVk:
    """Фальшивый vk_api: пишет все вызовы в events и отдаёт заранее заданные ответы."""

    def __init__(self):
        self.events = []
        self.send_plan = []      # по элементу на вызов messages.send: 'ok' или (code, text)
        self.upload_plan = []    # 'ok' | 'empty' | 'broken' | 'key'
        self._photo_id = 0
        fake = self

        class Http:
            def __init__(self, name):
                self.name = name

            def request(self, method, url, **kwargs):
                fake.events.append(('http', self.name, method, url, kwargs.get('timeout')))
                return 'response'

        class Messages:
            def send(self, **kwargs):
                fake.events.append(('send', kwargs))
                action = fake.send_plan.pop(0) if fake.send_plan else 'ok'
                if action != 'ok':
                    raise VkApiError(*action)

        class Api:
            messages = Messages()

        class VkApi:
            def __init__(self, token=None):
                self.http = Http('session')
                fake.events.append(('VkApi', token))

            def get_api(self):
                return Api()

        class VkUpload:
            def __init__(self, session):
                self.http = Http('upload')

            def photo_messages(self, photos, peer_id=None):
                fake.events.append(('upload', photos, peer_id))
                action = fake.upload_plan.pop(0) if fake.upload_plan else 'ok'
                if action == 'empty':
                    return []
                if action == 'broken':
                    return [{'owner_id': 0, 'id': 0}]
                count = 1 if isinstance(photos, str) else len(photos)
                result = []
                for _ in range(count):
                    fake._photo_id += 1
                    photo = {'owner_id': 10, 'id': fake._photo_id}
                    if action == 'key':
                        photo['access_key'] = 'k'
                    result.append(photo)
                return result

        self.module = types.ModuleType('vk_api')
        self.module.VkApi = VkApi
        self.module.VkUpload = VkUpload


@pytest.fixture
def fake_vk(monkeypatch):
    fake = FakeVk()
    monkeypatch.setitem(sys.modules, 'vk_api', fake.module)
    return fake


@pytest.fixture
def no_sleep(monkeypatch):
    """Не ждём по-настоящему; возвращает список запрошенных пауз."""
    import time
    sleeps = []
    monkeypatch.setattr(time, 'sleep', lambda s: sleeps.append(s))
    return sleeps


@pytest.fixture
def fixed_random(monkeypatch):
    import random
    monkeypatch.setattr(random, 'randint', lambda a, b: 4242)


@pytest.fixture
def clock(monkeypatch):
    """Детерминированные часы для замера медленных запросов: каждый вызов +0.5 с."""
    import time
    ticks = itertools.count(0, 0.5)
    monkeypatch.setattr(time, 'monotonic', lambda: next(ticks))


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / 'targets.toml').write_text(TUI_TOML, encoding='utf-8')
    (tmp_path / '.env').write_text('CRL_PW=secret\n', encoding='utf-8')
    for key in ('CRL_PW', 'PG_PW', 'NEW_PAM_PASSWORD', 'VK_TOKEN', 'VK_PEER_ID', 'MESSENGER', 'TELEGRAM_TOKEN',
                'TELEGRAM_CHAT_ID', 'ALLOWED_USERS', 'ALLOW_ANY_USER'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('SAVE_DIR', str(tmp_path / 'shots'))
    monkeypatch.setenv('PAM_HOST', 'pam.example.org')
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('NO_OPEN_SCREENSHOT', '1')
    from monitor import engine
    monkeypatch.setattr(engine.Engine, 'fetch_listings',
                        lambda self, t, paths=None: dict.fromkeys(paths or t.paths, TUI_LINES))
    return tmp_path


@pytest.fixture(scope='session', autouse=True)
def _ssh_required_in_ci():
    """REQUIRE_SSH=1 (CI, Docker): без openssh-client тесты падают, а не пропускаются молча."""
    import os
    import shutil
    if os.environ.get('REQUIRE_SSH') == '1' and shutil.which('ssh') is None:
        pytest.fail('REQUIRE_SSH=1, но openssh-client не установлен: e2e-тесты были бы пропущены')

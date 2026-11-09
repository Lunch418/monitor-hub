"""Общие фикстуры: фальшивый vk_api, без сна и случайности."""
import itertools
import sys
import types
from pathlib import Path

import pytest

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

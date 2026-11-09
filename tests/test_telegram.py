import logging
import threading
import time

import pytest

from monitor.messengers import make_messenger
from monitor.messengers import telegram as tg
from tests.fakes.telegram_server import FakeTelegram

LOG = logging.getLogger('tg')


class Boom(Exception):
    def __init__(self, detail='', user_message='', **kw):
        super().__init__(detail)
        self.user_message = user_message


@pytest.fixture
def fake():
    server = FakeTelegram()
    yield server
    server.close()


@pytest.fixture
def messenger(fake, monkeypatch):
    monkeypatch.setattr(tg.time, 'sleep', lambda s: None)
    return tg.TelegramMessenger(token=fake.token, api_url=fake.url, log=LOG, error_cls=Boom, config_error_cls=Boom)


@pytest.fixture
def png(tmp_path):
    path = tmp_path / 'a.png'
    path.write_bytes(b'\x89PNG fake')
    return str(path)


def test_send_message_and_split(messenger, fake):
    messenger.send_message(-100, 'hello')
    assert fake.methods('sendMessage')[0]['fields'] == {'chat_id': '-100', 'text': 'hello'}
    long = '\n'.join(['x' * 100] * 100)
    messenger.send_message(5, long)
    parts = [c['fields']['text'] for c in fake.methods('sendMessage')[1:]]
    assert len(parts) >= 3 and all(len(p) <= tg.TEXT_LIMIT for p in parts)


def test_send_message_error_does_not_raise_and_hides_token(messenger, fake, caplog):
    fake.errors['sendMessage'] = [(500, {'ok': False, 'error_code': 500, 'description': 'oops'})]
    with caplog.at_level(logging.WARNING):
        messenger.send_message(1, 'x')
    assert 'oops' in caplog.text and fake.token not in caplog.text


def test_send_single_photo_with_caption(messenger, fake, png):
    messenger.send_photos([png], 'подпись', 7)
    call = fake.methods('sendPhoto')[0]
    assert call['fields']['caption'] == 'подпись' and call['files'][0][0] == 'photo'


def test_send_group_of_photos(messenger, fake, png):
    messenger.send_photos([png] * 12, 'cap', 7)
    groups = fake.methods('sendMediaGroup')
    assert len(groups) == 2 and len(groups[0]['files']) == 10 and len(groups[1]['files']) == 2
    assert '"caption": "cap"' in groups[0]['fields']['media'] and 'caption' not in groups[1]['fields']['media']


def test_long_caption_is_split_into_message(messenger, fake, png):
    messenger.send_photos([png], 'z' * 1500, 7)
    assert len(fake.methods('sendPhoto')[0]['fields']['caption']) <= tg.CAPTION_LIMIT
    assert fake.methods('sendMessage')[0]['fields']['text'] == 'z' * 1500


def test_photo_rejected_falls_back_to_document(messenger, fake, png):
    fake.errors['sendPhoto'] = [(400, {'ok': False, 'error_code': 400,
                                       'description': 'Bad Request: PHOTO_INVALID_DIMENSIONS'})]
    messenger.send_photos([png], 'c', 7)
    assert fake.methods('sendDocument')[0]['files'][0][0] == 'document'


def test_retry_after_then_success(messenger, fake, png, monkeypatch):
    waits = []
    monkeypatch.setattr(tg.time, 'sleep', waits.append)
    fake.errors['sendPhoto'] = [(429, {'ok': False, 'error_code': 429, 'description': 'Too Many Requests',
                                       'parameters': {'retry_after': 3}})]
    messenger.send_photos([png], 'c', 7)
    assert waits == [3] and len(fake.methods('sendPhoto')) == 2


def test_gives_up_with_user_message_and_no_token(messenger, fake, png):
    fake.errors['sendPhoto'] = [(500, {'ok': False, 'error_code': 500, 'description': 'down'})] * 5
    with pytest.raises(Boom) as exc:
        messenger.send_photos([png], 'c', 7, attempts=3)
    assert exc.value.user_message == 'Не удалось отправить отчёт. Попробуйте позже.'
    assert fake.token not in str(exc.value) and len(fake.methods('sendPhoto')) == 3


def test_fatal_error_is_not_retried(messenger, fake, png):
    fake.errors['sendPhoto'] = [(401, {'ok': False, 'error_code': 401, 'description': 'Unauthorized'})]
    with pytest.raises(Boom):
        messenger.send_photos([png], 'c', 7)
    assert len(fake.methods('sendPhoto')) == 1


def test_network_failure_is_a_clean_error(png):
    bad = tg.TelegramMessenger(token='1:x', api_url='http://127.0.0.1:1', log=LOG, error_cls=Boom, config_error_cls=Boom)
    with pytest.raises(Boom) as exc:
        bad.send_photos([png], 'c', 7, attempts=1)
    assert '1:x' not in str(exc.value)


def test_missing_token_is_config_error(png):
    empty = tg.TelegramMessenger(token='', api_url='http://x', log=LOG, error_cls=Boom, config_error_cls=Boom)
    with pytest.raises(Boom):
        empty.call('getMe')


def test_parse_update():
    parse = tg.TelegramMessenger.parse_update
    assert parse({'message': {'chat': {'id': -5}, 'from': {'id': 9}, 'text': '/get@my_bot arg'}}) == (-5, '9', '/get arg')
    assert parse({'message': {'chat': {'id': 1}, 'from': {'id': 2}, 'text': 'hi'}}) == (1, '2', 'hi')
    assert parse({'message': {'chat': {'id': 1}, 'photo': []}}) is None
    assert parse({'edited_message': {}}) is None


def test_poll_skips_backlog_and_delivers_new(messenger, fake):
    fake.push(1, 5, '/old')
    got, started = [], threading.Event()
    thread = threading.Thread(target=messenger.poll, args=(lambda *a: got.append(a), started.set), daemon=True)
    thread.start()
    assert started.wait(5)
    fake.push(-7, 5, '/get@bot')
    deadline = time.monotonic() + 5
    while not got and time.monotonic() < deadline:
        time.sleep(0.05)
    assert got == [(-7, '5', '/get')]            # старое пропущено


def test_poll_stops_on_bad_token(fake, monkeypatch, caplog):
    monkeypatch.setattr(tg.time, 'sleep', lambda s: None)
    fake.errors['getUpdates'] = [(401, {'ok': False, 'error_code': 401, 'description': 'Unauthorized'})]
    m = tg.TelegramMessenger(token=fake.token, api_url=fake.url, log=LOG, error_cls=Boom, config_error_cls=Boom)
    with caplog.at_level(logging.ERROR):
        m.poll(lambda *a: None, lambda: None)      # возвращается, а не крутится вечно
    assert 'неверный токен' in caplog.text


def test_poll_without_token_returns():
    m = tg.TelegramMessenger(token='', api_url='http://x', log=LOG, error_cls=Boom, config_error_cls=Boom)
    m.poll(lambda *a: None, lambda: None)


def test_factory_picks_messenger():
    from monitor.config import Settings
    t = make_messenger(Settings(messenger='telegram', telegram_token='1:t'), LOG, error_cls=Boom, config_error_cls=Boom)
    assert t.name == 'telegram' and t.secrets() == ['1:t']
    v = make_messenger(Settings(messenger='vk', vk_token='vkt'), LOG, error_cls=Boom, config_error_cls=Boom)
    assert v.name == 'vk' and v.secrets() == ['vkt']

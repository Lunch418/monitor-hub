"""VkClient: загрузка и отправка картинок, повторы, таймауты."""
import logging

import pytest

from monitor import common as mc


class FakeError(RuntimeError):
    def __init__(self, detail, user_message, retryable=False):
        super().__init__(detail)
        self.user_message = user_message


class FakeConfigError(FakeError):
    pass


@pytest.fixture
def client(fake_vk, no_sleep, fixed_random, clock):
    return mc.VkClient(
        token='tok', attach_delay=1.5, log=logging.getLogger('test_vk_client'),
        error_cls=FakeError, config_error_cls=FakeConfigError,
        send_failed_message='не вышло',
    )


def sends(fake_vk):
    return [e[1] for e in fake_vk.events if e[0] == 'send']


def uploads(fake_vk):
    return [e for e in fake_vk.events if e[0] == 'upload']


def test_single_photo_is_sent_with_caption(client, fake_vk, no_sleep):
    client.send_photos(['a.png'], 'caption', 99)
    (msg,) = sends(fake_vk)
    assert msg['peer_id'] == 99
    assert msg['message'] == 'caption'
    assert msg['attachment'] == 'photo10_1'
    assert msg['random_id'] == 4242
    assert no_sleep == [1.5]            # пауза между загрузкой и отправкой


def test_access_key_is_appended_to_attachment(client, fake_vk):
    fake_vk.upload_plan = ['key']
    client.send_photos(['a.png'], 'c', 1)
    assert sends(fake_vk)[0]['attachment'] == 'photo10_1_k'


def test_photos_are_uploaded_by_five(client, fake_vk):
    client.send_photos([f'{i}.png' for i in range(7)], 'c', 1)
    sizes = [1 if isinstance(u[1], str) else len(u[1]) for u in uploads(fake_vk)]
    assert sizes == [5, 2]


def test_more_than_ten_attachments_split_into_messages(client, fake_vk):
    client.send_photos([f'{i}.png' for i in range(12)], 'caption', 1)
    first, second = sends(fake_vk)
    assert first['message'] == 'caption' and second['message'] == ''
    assert len(first['attachment'].split(',')) == 10
    assert len(second['attachment'].split(',')) == 2
    assert second['random_id'] == first['random_id'] + 10


def test_photo_undefined_is_retried_then_reuploaded(client, fake_vk, no_sleep):
    fake_vk.send_plan = [(100, 'photo is undefined')] * 2
    client.send_photos(['a.png'], 'c', 1)
    assert len(uploads(fake_vk)) == 2       # после второй неудачи загрузка повторяется
    # загрузка, пауза 1.5; сбой -> 3; сбой -> 8; повторная загрузка, пауза 1.5
    assert no_sleep == [1.5, 3, 8, 1.5]


def test_single_photo_undefined_is_not_reuploaded(client, fake_vk):
    fake_vk.send_plan = [(100, 'photo is undefined')]
    client.send_photos(['a.png'], 'c', 1)
    assert len(uploads(fake_vk)) == 1


def test_fatal_error_stops_immediately(client, fake_vk, no_sleep):
    fake_vk.send_plan = [(5, 'auth failed')]
    with pytest.raises(FakeError) as info:
        client.send_photos(['a.png'], 'c', 1)
    assert len(sends(fake_vk)) == 1
    assert info.value.user_message == 'не вышло'


def test_gives_up_after_all_attempts(client, fake_vk):
    fake_vk.send_plan = [(6, 'too many requests')] * 5
    with pytest.raises(FakeError, match='после 5 попыток'):
        client.send_photos(['a.png'], 'c', 1)
    assert len(sends(fake_vk)) == 5


@pytest.mark.parametrize('bad', ['empty', 'broken'])
def test_bad_upload_result_is_an_error(client, fake_vk, bad):
    fake_vk.upload_plan = [bad] * 5
    with pytest.raises(FakeError):
        client.send_photos(['a.png'], 'c', 1)
    assert sends(fake_vk) == []


def test_random_id_is_stable_across_retries(client, fake_vk):
    fake_vk.send_plan = [(6, 'x'), (6, 'x')]
    client.send_photos(['a.png'], 'c', 1)
    assert {m['random_id'] for m in sends(fake_vk)} == {4242}


def test_missing_token_raises_config_error(fake_vk):
    empty = mc.VkClient(token='', attach_delay=0, log=logging.getLogger('t'),
                        error_cls=FakeError, config_error_cls=FakeConfigError,
                        send_failed_message='x')
    with pytest.raises(FakeConfigError, match='VK_TOKEN'):
        empty.connect()


def test_send_message_swallows_errors(client, fake_vk, caplog):
    fake_vk.send_plan = [(6, 'boom')]
    with caplog.at_level(logging.WARNING, logger='test_vk_client'):
        client.send_message(5, 'hi')
    assert 'не удалось отправить сообщение' in caplog.text


def test_both_sessions_get_timeouts(client, fake_vk):
    _api, upload = client.connect()
    client.cache['session'].http.request('POST', 'https://api.vk.com/method/x')
    upload.http.request('POST', 'https://pu.vk.com/upload')
    timeouts = {e[1]: e[4] for e in fake_vk.events if e[0] == 'http'}
    assert timeouts == {'session': 30, 'upload': (10, 60)}


def test_slow_request_logs_host_only(client, fake_vk, caplog, monkeypatch):
    import time
    ticks = iter([0.0, 10.0])
    monkeypatch.setattr(time, 'monotonic', lambda: next(ticks))
    _api, upload = client.connect()
    with caplog.at_level(logging.INFO, logger='test_vk_client'):
        upload.http.request('POST', 'https://pu.vk.com/upload?token=secret')
    assert 'медленный запрос' in caplog.text and 'pu.vk.com' in caplog.text
    assert 'secret' not in caplog.text and 'upload?' not in caplog.text


def test_fatal_and_photo_helpers():
    class E(Exception):
        code = 5
    assert mc.is_fatal_vk_error(E())
    assert not mc.is_fatal_vk_error(ValueError())

    class P(Exception):
        code = 100

        def __str__(self):
            return '[100] photo is undefined'
    assert mc.is_photo_undefined(P())
    assert not mc.is_photo_undefined(E())

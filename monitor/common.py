"""
common.py - чтение переменных окружения и клиент ВКонтакте: ленивое
подключение, таймауты, загрузка картинок и отправка с повторами.
"""

from __future__ import annotations

import os
import random
import threading
import time


# Переменные окружения
def _env(name: str, default: str = '') -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int = 0) -> int:
    raw = (os.environ.get(name) or '').strip()
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or '').strip()
    try:
        return float(raw)
    except ValueError:
        return default


def _env_list(name: str) -> list[str]:
    return [s.strip() for s in _env(name).split(',') if s.strip()]


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or '').strip().lower()
    if raw in ('1', 'true', 'yes', 'on'):
        return True
    if raw in ('0', 'false', 'no', 'off'):
        return False
    return default


# ВКонтакте
# Коды ошибок ВК, при которых повторять бессмысленно: проблема в токене или
# правах, а не в сети. Без этого long poll перезапускался бы бесконечно и
# забивал лог одинаковыми предупреждениями.
VK_FATAL_CODES = {3, 5, 15, 27, 28, 200, 203}

# Паузы перед повторными попытками отправки, секунды. ВК обрабатывает
# загруженную картинку в фоне; пока не закончил, messages.send отвечает
# '[100] photo is undefined'. Высокие картинки обрабатываются дольше, поэтому
# паузы растут: в сумме почти минута.
VK_SEND_DELAYS = (3, 8, 15, 30)


def is_fatal_vk_error(exc: BaseException) -> bool:
    code = getattr(exc, 'code', None)
    return isinstance(code, int) and code in VK_FATAL_CODES


def is_photo_undefined(exc: BaseException) -> bool:
    return getattr(exc, 'code', None) == 100 and 'photo' in str(exc).lower()


class VkClient:
    """Сессия ВК: ленивое создание, загрузка и отправка фото.

    error_cls / config_error_cls - классы ошибок скрипта (например ConfigError,
    конструктор (detail, user_message)). send_failed_message - короткий текст
    для чата, если отправка не удалась после всех попыток."""

    def __init__(self, *, token: str, attach_delay: float, log,
                 error_cls, config_error_cls, send_failed_message: str) -> None:
        self.token = token
        self.attach_delay = attach_delay
        self.log = log
        self.error_cls = error_cls
        self.config_error_cls = config_error_cls
        self.send_failed_message = send_failed_message
        self.cache: dict = {}
        self.lock = threading.Lock()

    def connect(self):
        """Ленивая инициализация сессии ВК. Возвращает (api, upload)."""
        log = self.log
        with self.lock:
            if 'api' not in self.cache:
                if not self.token:
                    raise self.config_error_cls('VK_TOKEN не задан (.env)', 'Бот не настроен')
                import vk_api
                session = vk_api.VkApi(token=self.token)
                upload = vk_api.VkUpload(session)

                def with_timeout(http, timeout):
                    original_request = http.request

                    def request_with_timeout(method, url, **kwargs):
                        kwargs.setdefault('timeout', timeout)
                        started = time.monotonic()
                        try:
                            return original_request(method, url, **kwargs)
                        finally:
                            # Только хост: в url/параметрах может быть токен.
                            elapsed = time.monotonic() - started
                            if elapsed > 3:
                                log.info('VK: медленный запрос %s %s - %.1f с',
                                         method, url.split('/')[2] if '//' in url else url, elapsed)

                    http.request = request_with_timeout

                with_timeout(session.http, 30)
                # VkUpload держит СВОЮ сессию requests без таймаута: зависшая
                # загрузка фото висела минутами и ничего не писала в лог.
                with_timeout(upload.http, (10, 60))
                self.cache['session'] = session
                self.cache['api'] = session.get_api()
                self.cache['upload'] = upload
            return self.cache['api'], self.cache['upload']

    def upload_photos(self, upload, paths: list[str], peer_id: int) -> list[str]:
        attachments: list[str] = []
        for i in range(0, len(paths), 5):          # photo_messages принимает до 5 за раз
            batch = paths[i:i + 5]
            photos = upload.photo_messages(batch if len(batch) > 1 else batch[0], peer_id=peer_id)
            if not photos or len(photos) != len(batch):
                raise self.error_cls(
                    f'upload вернул неожиданный результат: {photos!r}',
                    'Не удалось загрузить изображение во ВКонтакте',
                )
            for photo in photos:
                if not photo.get('owner_id') or not photo.get('id'):
                    raise self.error_cls(
                        f'upload вернул битую запись: {photo!r}',
                        'Не удалось загрузить изображение во ВКонтакте',
                    )
                # access_key, когда ВК его вернул: без него такая фотография не
                # вкладывается и messages.send отвечает '[100] photo is undefined'
                # - уже навсегда, никакими повторами не лечится. Сейчас ключ
                # обычно не приходит, поэтому строка вложения не меняется; это
                # защита на случай, если ВК начнёт его отдавать.
                access_key = photo.get('access_key')
                ref = f"photo{photo['owner_id']}_{photo['id']}"
                attachments.append(f'{ref}_{access_key}' if access_key else ref)
        return attachments

    def send_photos(self, paths: list[str], caption: str, peer_id: int, attempts: int = 5) -> None:
        """Отправка одного или нескольких PNG в указанный peer_id.

        random_id генерируется один раз до цикла повторов: иначе при потере
        ответа на успешный messages.send дедупликация ВК не срабатывала бы и в
        беседу пришло бы два одинаковых сообщения.

        Повторы идут с растущими паузами. Если ВК дважды подряд "не видит"
        загруженные фото ('[100] photo is undefined'), они загружаются заново,
        с новыми id: на застрявшие фото повторы бесполезны. Повторная загрузка
        безопасна - при ошибке 100 сообщение точно не ушло."""
        log = self.log
        api, upload = self.connect()
        random_id = random.randint(1, 2 ** 31 - 1000)
        attachments: list[str] | None = None
        last_error: BaseException | None = None

        for attempt in range(1, attempts + 1):
            try:
                if attachments is None:
                    attachments = self.upload_photos(upload, paths, peer_id)
                    # Дать ВК договориться с самим собой: сразу после загрузки
                    # фотография ещё может быть не видна messages.send.
                    if self.attach_delay:
                        time.sleep(self.attach_delay)
                # messages.send принимает до 10 вложений за сообщение
                for i in range(0, len(attachments), 10):
                    chunk = attachments[i:i + 10]
                    api.messages.send(
                        peer_id=peer_id,
                        message=caption if i == 0 else '',
                        attachment=','.join(chunk),
                        random_id=random_id + i,
                    )
                log.info('VK: отправлено %d вложений в peer %s (попытка %d)',
                         len(attachments), peer_id, attempt)
                return
            except Exception as exc:
                last_error = exc
                if is_fatal_vk_error(exc):
                    log.error('VK: неустранимая ошибка (code=%s): %s',
                              getattr(exc, 'code', '?'), exc)
                    break
                if is_photo_undefined(exc) and attempt >= 2:
                    attachments = None
                log.warning('VK: попытка %d/%d не удалась: %s', attempt, attempts, exc)
                if attempt < attempts:
                    time.sleep(VK_SEND_DELAYS[min(attempt - 1, len(VK_SEND_DELAYS) - 1)])

        raise self.error_cls(
            f'Не удалось отправить фото после {attempts} попыток: {last_error}',
            self.send_failed_message,
        )

    def send_message(self, peer_id: int, text: str) -> None:
        try:
            api, _ = self.connect()
            api.messages.send(
                peer_id=peer_id,
                message=text,
                random_id=random.randint(1, 2 ** 31 - 1),
            )
        except Exception as exc:
            self.log.warning('VK: не удалось отправить сообщение в peer %s: %s', peer_id, exc)

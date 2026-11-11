"""
Telegram: Bot API по HTTPS (requests), без сторонних библиотек.

Отправка: sendMessage, sendPhoto, sendMediaGroup (до 10 картинок за раз). Картинку, которую
Telegram не принял как фото (слишком вытянутая), отправляем файлом. Приём команд: getUpdates
с длинным ожиданием; накопившиеся за время простоя сообщения при старте пропускаются.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable

from monitor.mailcheck import split_message

TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
SEND_DELAYS = (2, 5, 10, 20)
FATAL_CODES = (401, 404)          # неверный токен
_BOT_SUFFIX_RE = re.compile(r'^(/\w+)@\w+')
STALE_SECONDS = 300      # дольше - команда считается устаревшей


class TelegramApiError(Exception):
    def __init__(self, code: int, description: str, retry_after: int = 0) -> None:
        super().__init__(f'[{code}] {description}')
        self.code = code
        self.description = description
        self.retry_after = retry_after


def split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Режет по строкам, чтобы уложиться в лимит сообщения."""
    return split_message(text, limit)


class TelegramMessenger:
    name = 'telegram'
    label = 'Telegram'

    def __init__(self, *, token: str, api_url: str, log, error_cls, config_error_cls) -> None:
        self.token = token
        self.api_url = api_url.rstrip('/')
        self.log = log
        self.error_cls = error_cls
        self.config_error_cls = config_error_cls
        self._session = None

    # низкий уровень
    def _http(self):
        if self._session is None:
            if not self.token:
                raise self.config_error_cls('TELEGRAM_TOKEN не задан (.env)', 'Бот не настроен')
            import requests
            self._session = requests.Session()
        return self._session

    def _mask(self, text: str) -> str:
        return text.replace(self.token, '***') if self.token else text

    def call(self, method: str, data: dict | None = None, files: dict | None = None,
             timeout: float | tuple[float, float] = (10, 60)) -> object:
        import requests
        url = f'{self.api_url}/bot{self.token}/{method}'
        try:
            response = self._http().post(url, data=data, files=files, timeout=timeout)
        except requests.RequestException as exc:
            raise TelegramApiError(0, self._mask(f'сеть: {exc.__class__.__name__}')) from None
        try:
            body = response.json()
        except ValueError:
            raise TelegramApiError(response.status_code, 'ответ не в формате JSON') from None
        if not body.get('ok'):
            params = body.get('parameters') or {}
            raise TelegramApiError(int(body.get('error_code', response.status_code)),
                                   str(body.get('description', '')), int(params.get('retry_after', 0)))
        return body.get('result')

    @staticmethod
    def is_fatal(exc: BaseException) -> bool:
        return isinstance(exc, TelegramApiError) and exc.code in FATAL_CODES

    # отправка
    def send_message(self, peer_id: int, text: str) -> None:
        try:
            for part in split_text(text):
                self.call('sendMessage', {'chat_id': peer_id, 'text': part})
        except Exception as exc:
            self.log.warning('Telegram: не удалось отправить сообщение в чат %s: %s', peer_id, self._mask(str(exc)))

    def _send_group(self, paths: list[str], caption: str, peer_id: int, as_document: bool) -> None:
        handles = []
        try:
            if len(paths) == 1:
                handles.append(open(paths[0], 'rb'))
                field = 'document' if as_document else 'photo'
                self.call('sendDocument' if as_document else 'sendPhoto',
                          {'chat_id': peer_id, 'caption': caption}, {field: handles[0]}, timeout=(10, 120))
                return
            media, files = [], {}
            for i, path in enumerate(paths):
                handles.append(open(path, 'rb'))
                files[f'f{i}'] = handles[-1]
                item = {'type': 'document' if as_document else 'photo', 'media': f'attach://f{i}'}
                if i == 0 and caption:
                    item['caption'] = caption
                media.append(item)
            self.call('sendMediaGroup', {'chat_id': peer_id, 'media': json.dumps(media)}, files, timeout=(10, 120))
        finally:
            for handle in handles:
                handle.close()

    def send_photos(self, paths: list[str], caption: str, peer_id: int, attempts: int = 5) -> None:
        extra = ''
        if len(caption) > CAPTION_LIMIT:
            caption, extra = caption[:CAPTION_LIMIT - 1] + '~', caption
        last: BaseException | None = None
        for attempt in range(1, attempts + 1):
            try:
                for i in range(0, len(paths), 10):
                    chunk = paths[i:i + 10]
                    label = caption if i == 0 else ''
                    try:
                        self._send_group(chunk, label, peer_id, as_document=False)
                    except TelegramApiError as exc:
                        if exc.code != 400 or not re.search(r'PHOTO_|IMAGE_|dimension', exc.description, re.I):
                            raise
                        self.log.warning('Telegram: картинка не принята как фото (%s), шлю файлом', exc.description)
                        self._send_group(chunk, label, peer_id, as_document=True)
                if extra:
                    self.send_message(peer_id, extra)
                self.log.info('Telegram: отправлено картинок %d в чат %s (попытка %d)', len(paths), peer_id, attempt)
                return
            except Exception as exc:
                last = exc
                if self.is_fatal(exc):
                    self.log.error('Telegram: неустранимая ошибка: %s', self._mask(str(exc)))
                    break
                self.log.warning('Telegram: попытка %d/%d не удалась: %s', attempt, attempts, self._mask(str(exc)))
                if attempt < attempts:
                    wait = getattr(exc, 'retry_after', 0) or SEND_DELAYS[min(attempt - 1, len(SEND_DELAYS) - 1)]
                    time.sleep(wait)
        raise self.error_cls(
            f'Не удалось отправить картинки после {attempts} попыток: {self._mask(str(last))}',
            'Не удалось отправить отчёт. Попробуйте позже.')

    # приём команд
    def _skip_backlog(self) -> int:
        """Номер, с которого читать новые сообщения (накопившееся пока демон стоял - пропускаем)."""
        result = self.call('getUpdates', {'offset': -1, 'timeout': 0}, timeout=(10, 20))
        if isinstance(result, list) and result:
            return int(result[-1]['update_id']) + 1
        return 0

    @staticmethod
    def parse_update(update: dict) -> tuple[int, str, str] | None:
        message = update.get('message')
        if not isinstance(message, dict) or not message.get('text'):
            return None
        text = _BOT_SUFFIX_RE.sub(r'\1', str(message['text']))
        return int(message['chat']['id']), str((message.get('from') or {}).get('id', '')), text

    @staticmethod
    def is_stale(update: dict, now: float | None = None) -> bool:
        """Сообщение старше STALE_SECONDS (сеть пропадала): команда уже неактуальна, не выполняем."""
        message = update.get('message')
        date = message.get('date') if isinstance(message, dict) else None
        return isinstance(date, int) and (now if now is not None else time.time()) - date > STALE_SECONDS

    def poll(self, on_message: Callable[[int, str, str], None], startup: Callable[[], None]) -> None:
        if not self.token:
            self.log.warning('TELEGRAM_TOKEN не задан - приём команд отключён')
            return
        backoff = 10
        offset = None
        while offset is None:
            try:
                offset = self._skip_backlog()
            except Exception as exc:
                if self.is_fatal(exc):
                    self.log.error('Telegram: приём команд остановлен, неверный токен: %s', self._mask(str(exc)))
                    return
                self.log.warning('Telegram: %s (повтор через %d с)', self._mask(str(exc)), backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)
        startup()
        backoff = 10
        while True:
            try:
                updates = self.call('getUpdates', {'offset': offset, 'timeout': 30,
                                                   'allowed_updates': json.dumps(['message'])}, timeout=(10, 45))
                backoff = 10
                for update in updates if isinstance(updates, list) else []:
                    offset = int(update['update_id']) + 1
                    parsed = self.parse_update(update)
                    if parsed and self.is_stale(update):
                        self.log.warning('Telegram: пропущена устаревшая команда %r', parsed[2][:40])
                    elif parsed:
                        on_message(*parsed)
            except Exception as exc:
                if self.is_fatal(exc):
                    self.log.error('Telegram: приём команд остановлен, неверный токен: %s', self._mask(str(exc)))
                    return
                self.log.warning('Telegram: %s (повтор через %d с)', self._mask(str(exc)), backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)

    def secrets(self) -> list[str]:
        return [self.token]

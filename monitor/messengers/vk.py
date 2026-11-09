"""ВКонтакте: сообщения и фото через vk_api, команды через long poll группы."""

from __future__ import annotations

from collections.abc import Callable

from monitor import daemon as _daemon
from monitor.common import VkClient, is_fatal_vk_error
from monitor.config import Settings


class VkMessenger:
    name = 'vk'
    label = 'ВКонтакте'

    def __init__(self, settings: Settings, log, *, error_cls, config_error_cls) -> None:
        self.settings = settings
        self.log = log
        self.client = VkClient(
            token=settings.vk_token, attach_delay=settings.attach_delay, log=log,
            error_cls=error_cls, config_error_cls=config_error_cls,
            send_failed_message='Не удалось отправить отчёт. Попробуйте позже.')

    def send_message(self, peer_id: int, text: str) -> None:
        self.client.send_message(peer_id, text)

    def send_photos(self, paths: list[str], caption: str, peer_id: int) -> None:
        self.client.send_photos(paths, caption, peer_id)

    def poll(self, on_message: Callable[[int, str, str], None], startup: Callable[[], None]) -> None:
        _daemon.poll_messages(
            log=self.log, vk=self.client, group_id=self.settings.vk_group_id, on_message=on_message,
            label='команд', is_fatal_vk_error=is_fatal_vk_error, startup=startup)

    def secrets(self) -> list[str]:
        return [self.settings.vk_token]

"""
messengers - куда monitor-hub шлёт отчёты и откуда принимает команды.

Один мессенджер выбирается в .env (MESSENGER=vk или telegram). Чтобы добавить свой,
достаточно класса с теми же методами, что у Messenger, и строки в make_messenger.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from monitor.config import Settings

NAMES = ('vk', 'telegram')
LABELS = {'vk': 'ВКонтакте', 'telegram': 'Telegram'}


class Messenger(Protocol):
    """Что нужно движку и демону от мессенджера. peer_id - беседа (ВК) или чат (Telegram)."""

    name: str
    label: str

    def send_message(self, peer_id: int, text: str) -> None:
        """Текст. Ошибки не бросает: пишет в лог (сообщение об ошибке не должно падать само)."""

    def send_photos(self, paths: list[str], caption: str, peer_id: int) -> None:
        """Картинки с подписью. После всех повторов бросает ошибку движка."""

    def poll(self, on_message: Callable[[int, str, str], None], startup: Callable[[], None]) -> None:
        """Приём команд: блокирующий цикл (в своём потоке), on_message(peer_id, from_id, text)."""

    def secrets(self) -> list[str]:
        """Что нельзя показывать в логе и сообщениях (токены)."""


def make_messenger(settings: Settings, log, *, error_cls, config_error_cls) -> Messenger:
    if settings.messenger == 'telegram':
        from monitor.messengers.telegram import TelegramMessenger
        return TelegramMessenger(
            token=settings.telegram_token, api_url=settings.telegram_api_url, log=log,
            error_cls=error_cls, config_error_cls=config_error_cls)
    from monitor.messengers.vk import VkMessenger
    return VkMessenger(settings, log, error_cls=error_cls, config_error_cls=config_error_cls)

"""
router.py - разбор сообщений беседы: команда -> действие.

Команда каждой цели берётся из targets.toml (`command = "/get"`), плюс общие:
/targets (список целей), /run <имя> (запуск по имени), /help. Произвольные команды
не выполняются: запускается только цель из конфигурации.

Права: команды принимаются только от ALLOWED_USERS (пустой список - никому), либо
от всех при явном ALLOW_ANY_USER=1. Отказ получает только тот, кто написал
настоящую команду: на обычные сообщения бот не отвечает.

Беседы: команды принимаются только из своих (основная, EXTRA_PEERS, peers целей)
либо из ALLOWED_CHATS, если он задан. Из чужой беседы бот молчит: листинг не должен
уйти в любую беседу, куда добавлен бот, а бот не должен выдавать, что слушает команды.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from monitor.config import Settings, Target


@dataclass(frozen=True)
class Action:
    kind: str                       # 'run' | 'reply' | 'deny'
    target: Target | None = None
    text: str = ''
    arg: str = ''                   # слова после команды (у mail это дата)


class Router:
    def __init__(self, targets: Mapping[str, Target], settings: Settings) -> None:
        self.targets = dict(targets)
        self.settings = settings
        self.by_command = {t.command: t for t in self.targets.values() if t.command}

    def authorized(self, from_id: str) -> bool:
        if self.settings.allow_any_user:
            return True
        return from_id in self.settings.allowed_users

    def chats(self) -> set[str]:
        """Беседы, откуда принимаются команды: ALLOWED_CHATS, а если он пуст - свои (основная,
        дополнительные и беседы целей)."""
        if self.settings.allowed_chats:
            return set(self.settings.allowed_chats)
        own = {str(self.settings.default_peer), *self.settings.extra_peers}
        for target in self.targets.values():
            own.update(target.peers)
        return own

    def describe(self) -> str:
        lines = []
        for target in self.targets.values():
            when = ', '.join(str(s) for s in target.schedule) or 'вручную'
            command = target.command or f'/run {target.name}'
            lines.append(f'{command} - {target.title} ({when})')
        return '\n'.join(lines)

    def help_text(self) -> str:
        return ('Команды:\n' + self.describe()
                + '\n/targets - список целей\n/run <имя> - запустить цель по имени\n/help - эта справка')

    def handle(self, text: str, from_id: str, peer_id: int | None = None) -> Action | None:
        """None - это не команда, отвечать не нужно."""
        parts = text.strip().lower().split()
        if not parts or not parts[0].startswith('/'):
            return None
        word = parts[0]
        known = word in self.by_command or word in ('/targets', '/run', '/help')
        if not known:
            return None
        if not self.authorized(from_id):
            return Action('deny', text='Доступ запрещён')
        if peer_id is not None and str(peer_id) not in self.chats():
            return Action('deny')       # молча: чужая беседа не должна узнать, что бот слушает команды

        if word == '/help':
            return Action('reply', text=self.help_text())
        if word == '/targets':
            return Action('reply', text=self.describe() or 'Целей нет')
        if word == '/run':
            if len(parts) < 2:
                return Action('reply', text='Укажите цель: /run <имя>\n' + self.describe())
            target = self.targets.get(parts[1])
            if target is None:
                return Action('reply', text=f'Нет такой цели: {parts[1]}\n' + self.describe())
            return Action('run', target, arg=' '.join(parts[2:]))
        return Action('run', self.by_command[word], arg=' '.join(parts[1:]))

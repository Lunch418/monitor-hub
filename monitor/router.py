"""
router.py - разбор сообщений беседы: команда -> действие.

Команда каждой цели берётся из targets.toml (`command = "/get"`), плюс общие:
/targets (список целей), /run <имя> (запуск по имени), /help. Произвольные команды
не выполняются: запускается только цель из конфигурации.

Состояние машин: /uptime, /disk, /mem, /top, /cpu, /net [цель] - только для целей, где слово
разрешено в info, плюс свои слова из [targets.<имя>.commands]. Без имени цели - ответ по всем
целям, где слово разрешено.

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
from monitor.presets import CHAT_WORDS, LIVE_PRESETS


@dataclass(frozen=True)
class Action:
    kind: str                       # 'run' | 'info' | 'reply' | 'deny'
    target: Target | None = None
    targets: tuple[Target, ...] = ()    # info: на каких машинах выполнить слово (text)
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

    def info_words(self) -> dict[str, list[Target]]:
        """Слово -> цели, для которых оно разрешено (info или свои commands). Только type = "ls"."""
        words: dict[str, list[Target]] = {}
        for target in self.targets.values():
            if target.type != 'ls':
                continue
            for word in (*target.info, *(w for w, _ in target.commands)):
                if target not in words.setdefault(word, []):
                    words[word].append(target)
        return words

    def describe_info(self) -> str:
        lines = []
        for word, owners in self.info_words().items():
            what = LIVE_PRESETS[CHAT_WORDS[word]][1] if word in CHAT_WORDS else 'своя команда'
            lines.append(f'/{word} [цель] - {what} ({", ".join(t.name for t in owners)})')
        return '\n'.join(lines)

    def describe(self) -> str:
        lines = []
        for target in self.targets.values():
            when = ', '.join(str(s) for s in target.schedule) or 'вручную'
            command = target.command or f'/run {target.name}'
            lines.append(f'{command} - {target.title} ({when})')
        return '\n'.join(lines)

    def help_text(self) -> str:
        info = self.describe_info()
        return ('Команды:\n' + self.describe() + ('\n' + info if info else '')
                + '\n/targets - список целей\n/run <имя> - запустить цель по имени\n/help - эта справка')

    def handle(self, text: str, from_id: str, peer_id: int | None = None) -> Action | None:
        """None - это не команда, отвечать не нужно."""
        parts = text.strip().lower().split()
        if not parts or not parts[0].startswith('/'):
            return None
        word = parts[0]
        info_words = self.info_words()
        is_info = word not in self.by_command and (word[1:] in info_words or word[1:] in CHAT_WORDS)
        known = word in self.by_command or word in ('/targets', '/run', '/help') or is_info
        if not known:
            return None
        if peer_id is not None and str(peer_id) not in self.chats():
            return Action('deny')       # молча: чужая беседа не должна узнать, что бот слушает команды
        if not self.authorized(from_id):
            return Action('deny', text='Доступ запрещён')

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
        if is_info:
            return self._info(word[1:], info_words.get(word[1:], []), parts[1:])
        return Action('run', self.by_command[word], arg=' '.join(parts[1:]))

    def _info(self, word: str, owners: list[Target], args: list[str]) -> Action:
        if not owners:
            return Action('reply', text=f'/{word} не включено ни для одной цели '
                                        f'(в targets.toml: info = ["{word}"])')
        if not args:
            return Action('info', text=word, targets=tuple(owners))
        target = self.targets.get(args[0])
        if target is None:
            return Action('reply', text=f'Нет такой цели: {args[0]}\n' + self.describe())
        if target not in owners:
            return Action('reply', text=f'Для цели {target.name} /{word} не разрешено. '
                                        f'Разрешено для: {", ".join(t.name for t in owners)}')
        return Action('info', text=word, targets=(target,))

"""
hub.py - демон monitor-hub: расписание всех целей, команды из беседы, изоляция целей.

Каждая проверка идёт в своём потоке. Одна и та же цель не запускается дважды
одновременно (лок цели), входы на шлюз ограничены (Engine.gateway), а сторож
сообщает в беседу, если проверка не уложилась в run_timeout. Зависшая или
упавшая цель не мешает остальным.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping

from monitor.config import Settings, Target
from monitor.engine import Engine
from monitor.router import Router
from monitor.scheduler import build_scheduler, next_runs

TICK_SECONDS = 5


class Hub:
    def __init__(self, engine: Engine, targets: Mapping[str, Target], settings: Settings, log, *,
                 spawn: Callable[..., threading.Thread] | None = None) -> None:
        self.engine = engine
        self.targets = dict(targets)
        self.settings = settings
        self.log = log
        self.router = Router(self.targets, settings)
        self.locks = {name: threading.Lock() for name in self.targets}
        self.scheduler = build_scheduler(self.targets, self.fire_scheduled)
        self._spawn = spawn or _start_thread

    # запуск одной цели
    def run_target(self, target: Target, peer_id: int | None = None, arg: str = '') -> threading.Thread | None:
        """Запускает проверку в потоке. None - цель уже выполняется."""
        lock = self.locks[target.name]
        if not lock.acquire(blocking=False):
            if peer_id:
                self.engine.chat.send_message(peer_id, f'Подождите, {target.title} уже проверяется...')
            else:
                self.log.warning('[%s] плановая проверка пропущена: предыдущая ещё идёт', target.name)
            return None
        return self._spawn(self._worker, target, peer_id, lock, arg)

    def _worker(self, target: Target, peer_id: int | None, lock: threading.Lock, arg: str = '') -> None:
        watchdog = threading.Timer(target.run_timeout, self._too_long, args=(target, peer_id))
        watchdog.daemon = True
        watchdog.start()
        try:
            self.engine.run(target, peer_id=peer_id, arg=arg)
        except Exception:      # Engine.run сам сообщает об ошибках; это последний рубеж
            self.log.critical('[%s] необработанная ошибка проверки', target.name, exc_info=True)
        finally:
            watchdog.cancel()
            lock.release()

    def _too_long(self, target: Target, peer_id: int | None) -> None:
        self.log.error('[%s] проверка идёт дольше %d с', target.name, target.run_timeout)
        peer = peer_id or self.engine.peers_for(target)[0]
        self.engine.chat.send_message(
            peer, f'Проверка "{target.title}" идёт дольше {target.run_timeout // 60} мин. '
                  'Результат придёт, когда она завершится.')

    def fire_scheduled(self, target: Target) -> None:
        self.log.info('[%s] запуск по расписанию', target.name)
        self.run_target(target)

    # беседа
    def on_message(self, peer_id: int, from_id: str, text: str) -> None:
        action = self.router.handle(text, from_id, peer_id)
        if action is None:
            return
        if action.kind == 'deny':
            self.log.warning('Отказ: команда %r от %s (peer %s)', text.strip()[:40], from_id, peer_id)
            if action.text:
                self.engine.chat.send_message(peer_id, action.text)
        elif action.kind == 'reply':
            self.engine.chat.send_message(peer_id, action.text)
        elif action.kind == 'run' and action.target is not None:
            self.log.info('[%s] команда от %s (peer %s)', action.target.name, from_id, peer_id)
            if self.run_target(action.target, peer_id, action.arg) is not None:
                self.engine.chat.send_message(peer_id, f'Проверяю: {action.target.title}...')

    def poll(self) -> None:
        """Приём команд из беседы (в отдельном потоке)."""
        if not (self.settings.allowed_users or self.settings.allow_any_user):
            self.log.warning('ALLOWED_USERS не задан - команды из беседы отключены '
                             '(или ALLOW_ANY_USER=1, чтобы разрешить всем)')
            return

        def startup() -> None:
            who = 'все' if self.settings.allow_any_user else ', '.join(sorted(self.settings.allowed_users))
            commands = [*sorted(self.router.by_command), '/targets', '/run', '/help']
            self.log.info('%s: приём команд запущен. Команды: %s. Пользователи: %s',
                          self.engine.chat.label, ' '.join(commands), who)

        self.engine.chat.poll(self.on_message, startup)

    # главный цикл
    def describe_schedule(self) -> list[str]:
        return [f'{name}: {when:%d.%m.%Y %H:%M}' for name, when in next_runs(self.scheduler)]

    def serve(self, *, ticks: int | None = None, sleep: Callable[[float], None] = time.sleep,
              start_poll: bool = True) -> None:
        """Главный цикл. ticks - для тестов: число итераций (None - бесконечно)."""
        self.log.info('monitor-hub: целей %d', len(self.targets))
        for line in self.describe_schedule():
            self.log.info('Следующий запуск %s', line)
        if start_poll:
            _start_thread(self.poll)
        count = 0
        while ticks is None or count < ticks:
            try:
                self.scheduler.run_pending()
            except Exception as exc:
                self.log.error('Ошибка в планировщике: %s', exc, exc_info=True)
            sleep(TICK_SECONDS)
            count += 1


def _start_thread(fn: Callable, *args) -> threading.Thread:
    thread = threading.Thread(target=fn, args=args, daemon=True)
    thread.start()
    return thread

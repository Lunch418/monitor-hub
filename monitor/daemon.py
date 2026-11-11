"""
daemon.py - процесс демона: PID-файл, запуск в фоне, остановка, статус, лог
и цикл VK long poll. Что делать с сообщениями, решает вызывающий (hub.py).
"""

from __future__ import annotations

import os
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path


class Daemon:
    def __init__(self, *, log, script_name: str, pid_file: str, log_file: str) -> None:
        self.log = log
        self.script_name = script_name
        self.pid_file = pid_file
        self.log_file = log_file

    # PID-файл
    def read_pid(self) -> tuple[int | None, str | None]:
        """Возвращает (pid, путь к PID-файлу) или (None, None)."""
        try:
            return int(Path(self.pid_file).read_text().strip()), self.pid_file
        except (OSError, ValueError):
            return None, None

    def is_our_process(self, pid: int) -> bool:
        """PID принадлежит именно нашему процессу?

        os.kill(pid, 0) мало: после падения демона система может отдать этот PID
        другому процессу, и stop убил бы чужой процесс."""
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as fh:
                cmdline = fh.read().decode('utf-8', 'replace')
        except OSError:
            return False
        return self.script_name in cmdline

    def alive(self, pid: int | None = None) -> bool:
        if pid is None:
            pid, _ = self.read_pid()
        if not pid:
            return False
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return False
        if os.path.isdir('/proc'):
            return self.is_our_process(pid)
        return True

    def write_pid_file(self) -> None:
        """O_EXCL|O_NOFOLLOW: не идти по подложенному симлинку и не затирать
        чужой файл."""
        try:
            os.unlink(self.pid_file)
        except FileNotFoundError:
            pass
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.pid_file, flags, 0o600)
        with os.fdopen(fd, 'w') as fh:
            fh.write(str(os.getpid()))

    def remove_pid_file(self) -> None:
        try:
            os.unlink(self.pid_file)
        except OSError:
            pass

    def install_signal_handlers(self) -> None:
        """SIGTERM по умолчанию убивает процесс мгновенно, минуя finally, -
        PID-файл оставался лежать как "живой"."""
        log = self.log

        def _terminate(signum, _frame):
            log.info('Получен сигнал %s - завершаюсь', signum)
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, _terminate)
        signal.signal(signal.SIGINT, _terminate)

    def start(self, *, preflight: Callable[[], bool], scheduler_loop: Callable,
              detach_console_logging: Callable, started_lines: Callable[[int | None], list[str]]) -> None:
        """started_lines(pid) - строки, которые родитель печатает после успешного запуска."""
        log = self.log
        if not preflight():
            sys.exit(2)

        import fcntl
        lock_path = self.pid_file + '.lock'
        lock_fh = open(lock_path, 'w')
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f'Уже запущен (заблокировано {lock_path})')
            return

        pid, pid_path = self.read_pid()
        if self.alive(pid):
            print(f'Уже запущен (PID {pid})')
            return
        if pid and pid_path:
            log.info('Удаляю устаревший PID-файл %s (PID %s больше не наш)', pid_path, pid)

        if os.fork() > 0:
            # Родитель ждёт, пока ребёнок запишет PID-файл, и только тогда
            # сообщает об успехе: если демон сразу упал, это видно сразу.
            for _ in range(40):
                time.sleep(0.25)
                if self.alive():
                    new_pid, _ = self.read_pid()
                    for line in started_lines(new_pid):
                        print(line)
                    return
            print('Демон не поднялся - смотрите лог:', self.log_file, file=sys.stderr)
            sys.exit(1)

        os.setsid()
        if os.fork() > 0:
            os._exit(0)

        # dup2, а не присваивание sys.stdout: подмена имён оставляла настоящие
        # дескрипторы 0/1/2 привязанными к терминалу, из которого запускали.
        detach_console_logging()
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        if devnull > 2:
            os.close(devnull)

        try:
            self.write_pid_file()
            scheduler_loop()
        except SystemExit:
            pass
        except Exception:
            log.critical('Демон аварийно завершился', exc_info=True)
        finally:
            self.remove_pid_file()
            try:
                lock_fh.close()
            except OSError:
                pass

    def stop(self) -> None:
        pid, pid_path = self.read_pid()
        if not pid:
            print('Демон не запущен')
            return
        if not self.alive(pid):
            print(f'Демон не запущен (PID-файл {pid_path} устарел - удаляю)')
            if pid_path:
                try:
                    os.unlink(pid_path)
                except OSError:
                    pass
            return

        os.kill(pid, signal.SIGTERM)
        for _ in range(30):
            time.sleep(0.5)
            if not self.alive(pid):
                print(f'Демон (PID {pid}) остановлен')
                self.remove_pid_file()
                return
        print(f'Не остановился: kill -9 {pid}')

    def restart(self, start: Callable[[], None]) -> None:
        self.stop()
        time.sleep(2)
        start()

    @staticmethod
    def tail(path: str, count: int) -> list[str]:
        """Хвост файла без чтения его целиком в память."""
        from collections import deque
        try:
            with open(path, encoding='utf-8', errors='replace') as fh:
                return list(deque(fh, maxlen=count))
        except OSError:
            return []

    def log_path_for_reading(self) -> str | None:
        return self.log_file if os.path.exists(self.log_file) else None

    def status(self, *, running_lines: Callable[[], list[str]],
               validate_config: Callable[[], list[str]]) -> None:
        pid, pid_path = self.read_pid()
        if self.alive(pid):
            print(f'Запущен (PID {pid})')
            for line in running_lines():
                print(line)
            print(f'   PID-файл:   {pid_path}')
        else:
            print('Не запущен')

        problems = validate_config()
        if problems:
            print('\n-- Проблемы конфигурации --')
            for problem in problems:
                print('  -', problem)

        path = self.log_path_for_reading()
        if path:
            print(f'\n-- Последние строки лога ({path}) --')
            for line in self.tail(path, 12):
                print(' ', line.rstrip())

    def logs(self, count: int = 50) -> None:
        path = self.log_path_for_reading()
        if not path:
            print('Лог пуст')
            return
        print(f'-- последние {count} строк {path} --')
        for line in self.tail(path, count):
            print(line.rstrip())

    def foreground(self, *, preflight: Callable[[], bool], scheduler_loop: Callable,
                   message: str) -> None:
        if not preflight():
            sys.exit(2)
        pid, _ = self.read_pid()
        if self.alive(pid):
            print(f'Уже запущен в фоне (PID {pid}). Сначала: monitor-hub stop.', file=sys.stderr)
            sys.exit(2)
        self.write_pid_file()
        self.log.info(message)
        try:
            scheduler_loop()
        except SystemExit:
            pass
        finally:
            self.remove_pid_file()


def poll_messages(*, log, vk, group_id, on_message: Callable[[int, str, str], None], label: str,
                  is_fatal_vk_error: Callable, startup: Callable[[], None],
                  enabled: bool = True, disabled_message: str = '') -> None:
    """Цикл VK long poll: каждое новое сообщение отдаётся в on_message(peer_id,
    from_id, text). Разбор команд и права доступа - дело вызывающего.

    label - имя команды в логах ('/get'); startup() пишет в лог строки о
    запуске (команды, пользователи).

    Пока соединение не удаётся создать, пауза растёт 10 -> 20 -> ... -> 300 с;
    уже установленное и оборвавшееся соединение восстанавливается через 10 с.
    Неустранимая ошибка ВК (неверный токен, нет прав) останавливает цикл."""
    if not enabled:
        log.info(disabled_message)
        return
    if not group_id:
        log.warning('VK_GROUP_ID не задан - приём команд %s отключён', label)
        return

    from vk_api.bot_longpoll import VkBotEventType, VkBotLongPoll

    try:
        vk.connect()
    except Exception as exc:
        log.error('VK long poll не запущен: %s', exc)
        return

    session = vk.cache['session']
    startup()

    backoff = 10
    while True:
        try:
            longpoll = VkBotLongPoll(session, group_id=group_id)
            backoff = 10
            for event in longpoll.listen():
                if event.type != VkBotEventType.MESSAGE_NEW:
                    continue
                message = event.obj.message
                on_message(message['peer_id'], str(message.get('from_id', '')),
                           message.get('text') or '')

        except Exception as exc:
            if is_fatal_vk_error(exc):
                log.error('VK long poll остановлен - неустранимая ошибка (code=%s): %s',
                          getattr(exc, 'code', '?'), exc)
                return
            log.warning('VK long poll: %s (повтор через %d с)', exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)



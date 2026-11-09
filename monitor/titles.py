"""
titles.py - заголовок окна (вкладки) терминала, пока monitor-hub работает.

Во вкладке видно, что делает программа, даже когда на неё не смотришь:
`[..] проверяю: web - monitor-hub`, потом `[ok] готово: web - monitor-hub` или `[!] ошибка: web - monitor-hub`.
Пишется только в настоящий терминал; при выводе в файл или канал ничего не добавляется.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager

APP_NAME = 'monitor-hub'
SAVE = '\x1b[22;0t'        # запомнить прежний заголовок
RESTORE = '\x1b[23;0t'     # вернуть его


def clean(text: str) -> str:
    return ''.join(ch for ch in str(text) if ch.isprintable())[:80]


def format_title(text: str = '') -> str:
    text = clean(text)
    return f'{text} - {APP_NAME}' if text else APP_NAME


def osc(title: str) -> str:
    return f'\x1b]0;{title}\x07'


def usable(stream=None) -> bool:
    stream = stream or sys.stdout
    try:
        return stream.isatty() and os.environ.get('TERM', '') not in ('', 'dumb')
    except (AttributeError, ValueError):
        return False


def write(data: str, stream=None) -> None:
    stream = stream or sys.stdout
    if not usable(stream):
        return
    try:
        stream.write(data)
        stream.flush()
    except (OSError, ValueError):
        pass


def set_title(text: str = '', stream=None) -> None:
    write(osc(format_title(text)), stream)


@contextmanager
def tab(running: str, ok: str, failed: str, stream=None) -> Iterator[Callable[[bool], None]]:
    """Заголовок на время дела. done(True/False) ставит итог; он остаётся во вкладке, чтобы
    его можно было увидеть позже, когда команда уже закончилась."""
    result: list[bool] = []
    write(SAVE, stream)
    set_title(f'[..] {running}', stream)
    try:
        yield result.append
    finally:
        if result:
            set_title(f'[ok] {ok}' if result[-1] else f'[!] {failed}', stream)
        else:
            write(RESTORE, stream)

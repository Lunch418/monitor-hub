"""
envfile.py - запись файлов конфигурации на месте: атомарно и с резервной копией.

set_env_value меняет одну переменную в .env и не трогает остальные строки,
комментарии и порядок. Файл всегда остаётся с правами 600.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

_KEY_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def atomic_write(path: Path, text: str, *, mode: int = 0o600) -> Path | None:
    """Пишет text в path через временный файл в том же каталоге. Возвращает путь
    резервной копии предыдущей версии (<файл>.bak) или None, если файла не было."""
    path = Path(path)
    backup = None
    if path.exists():
        backup = path.with_name(path.name + '.bak')
        shutil.copy2(path, backup)
        os.chmod(backup, mode)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return backup


def _quote(value: str) -> str:
    """Кавычки, которые понимает load_env: он снимает одну пару внешних кавычек."""
    if value == '' or re.fullmatch(r'[A-Za-z0-9_./:@,+=-]+', value):
        return value
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    raise ValueError('значение не может содержать одновременно \' и "')


def set_env_value(path: str | Path, key: str, value: str) -> None:
    """KEY=value в .env: заменяет существующую строку или дописывает в конец."""
    if not _KEY_RE.match(key):
        raise ValueError(f'недопустимое имя переменной: {key!r}')
    if '\n' in value or '\r' in value:
        raise ValueError('значение не может содержать перевод строки')
    path = Path(path)
    lines = path.read_text(encoding='utf-8').splitlines() if path.exists() else []
    line_re = re.compile(rf'^\s*(?:export\s+)?{re.escape(key)}\s*=')
    new_line = f'{key}={_quote(value)}'
    for i, line in enumerate(lines):
        if line_re.match(line):
            lines[i] = new_line
            break
    else:
        lines.append(new_line)
    atomic_write(path, '\n'.join(lines) + '\n', mode=0o600)

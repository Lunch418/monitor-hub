"""
runlog.py - последний результат каждой цели (для `status` и интерфейса).

Файл last_runs.json в каталоге состояния. Пишут и демон, и ручные проверки,
поэтому запись под flock и атомарная.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

FILE_NAME = 'last_runs.json'


def _path(state_dir: str | Path) -> Path:
    return Path(state_dir) / FILE_NAME


def read(state_dir: str | Path) -> dict[str, dict]:
    try:
        data = json.loads(_path(state_dir).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record(state_dir: str | Path, name: str, ok: bool, message: str = '',
           now: float | None = None) -> None:
    path = _path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix('.lock'), 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = read(state_dir)
        data[name] = {'ok': ok, 'message': message[:300], 'at': time.time() if now is None else now}
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
        os.replace(tmp, path)


def age_text(seconds: float) -> str:
    """Возраст отчёта коротко: 40с, 12м, 3ч, 2д."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f'{seconds}с'
    if seconds < 3600:
        return f'{seconds // 60}м'
    if seconds < 86400:
        return f'{seconds // 3600}ч'
    return f'{seconds // 86400}д'


_LISTING_KEYS = ('name', 'raw_name', 'size_disp', 'date_str', 'is_dir', 'is_exec', 'is_link')


def save_listing(state_dir: str | Path, name: str, style: str, listings) -> None:
    """Последний листинг цели для предпросмотра в интерфейсе (без сети)."""
    folder = Path(state_dir) / 'listings'
    folder.mkdir(parents=True, exist_ok=True)
    data = {
        'style': style,
        'at': time.time(),
        'paths': [
            {'path': path, 'stale': sorted(stale),
             'entries': [{k: e[k] for k in _LISTING_KEYS} for e in entries]}
            for path, entries, stale in listings
        ],
    }
    tmp = folder / f'.{name}.tmp'
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, folder / f'{name}.json')


def read_listing(state_dir: str | Path, name: str):
    """(стиль, время, [(путь, записи, устаревшие)]) или None."""
    try:
        data = json.loads((Path(state_dir) / 'listings' / f'{name}.json').read_text(encoding='utf-8'))
        listings = [(p['path'], p['entries'], set(p['stale'])) for p in data['paths']]
        return data['style'], float(data['at']), listings
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_report(state_dir: str | Path, name: str, text: str) -> None:
    """Последний текстовый отчёт цели (у mail) для предпросмотра."""
    folder = Path(state_dir) / 'reports'
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f'.{name}.tmp'
    tmp.write_text(text, encoding='utf-8')
    os.replace(tmp, folder / f'{name}.txt')


def read_report(state_dir: str | Path, name: str) -> tuple[float, str] | None:
    """(время, текст) или None."""
    path = Path(state_dir) / 'reports' / f'{name}.txt'
    try:
        return path.stat().st_mtime, path.read_text(encoding='utf-8')
    except OSError:
        return None

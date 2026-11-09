"""
scheduler.py - расписание целей на библиотеке `schedule`.

build_scheduler создаёт отдельный планировщик (не глобальный): его можно проверять
в тестах, не трогая общее состояние. Каждое расписание цели - отдельное задание с
тегом, равным имени цели.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import schedule

from monitor.config import Schedule, Target

_DAY_ATTR = {
    'mon': 'monday', 'tue': 'tuesday', 'wed': 'wednesday', 'thu': 'thursday',
    'fri': 'friday', 'sat': 'saturday', 'sun': 'sunday',
}


def add_job(scheduler: schedule.Scheduler, target: Target, when: Schedule,
            fire: Callable[[Target], object]) -> schedule.Job:
    every = scheduler.every()
    unit = getattr(every, _DAY_ATTR[when.weekday]) if when.weekday else every.day
    return unit.at(when.time).do(fire, target).tag(target.name)


def build_scheduler(targets: dict[str, Target] | list[Target],
                    fire: Callable[[Target], object]) -> schedule.Scheduler:
    """Планировщик со всеми расписаниями всех целей; fire(target) вызывается в срок."""
    scheduler = schedule.Scheduler()
    items = targets.values() if isinstance(targets, dict) else targets
    for target in items:
        for when in target.schedule:
            add_job(scheduler, target, when, fire)
    return scheduler


def next_runs(scheduler: schedule.Scheduler) -> list[tuple[str, datetime]]:
    """[(имя цели, время следующего запуска)] по возрастанию времени."""
    rows = [(str(next(iter(job.tags))), job.next_run) for job in scheduler.jobs if job.next_run]
    return sorted(rows, key=lambda row: row[1])

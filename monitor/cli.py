"""
cli.py - командная строка monitor-hub.

  monitor-hub validate            проверить targets.toml и окружение, без сети
  monitor-hub list                показать цели
  monitor-hub check <цель>        выполнить проверку сейчас (--no-send: не писать в ВК)
  monitor-hub check-paths <цель>  проверить пути цели одним входом (без отчёта)
  monitor-hub tui                 терминальный интерфейс
  monitor-hub run                 демон в терминале (так его запускает systemd)
  monitor-hub start|stop|restart  демон в фоне
  monitor-hub status|logs         состояние, расписание и хвост лога

Файлы: targets.toml (цели, без паролей) и .env (секреты). Ищутся в каталоге
проекта; другой путь к целям: --config или переменная TARGETS_FILE.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from monitor import __version__, mailcheck, titles
from monitor import app as _app
from monitor.config import Settings, Target, load_targets, path_problem, validate_environment
from monitor.engine import Engine
from monitor.titles import tab as _tab

ROOT = Path(__file__).resolve().parent.parent


def _print_problems(problems: Sequence[str]) -> None:
    print('Найдены проблемы:', file=sys.stderr)
    for problem in problems:
        print(f'  - {problem}', file=sys.stderr)


def _load(args: argparse.Namespace, *, need_ssh: bool, need_chat: bool):
    """(settings, targets, env_path) или None, если конфигурация неверна."""
    env_path = _app.load_env(str(ROOT))
    config = args.config or os.environ.get('TARGETS_FILE') or str(ROOT / 'targets.toml')
    targets, problems = load_targets(config)
    settings = Settings.from_env()
    if targets:
        problems.extend(validate_environment(
            settings, targets, os.environ, need_ssh=need_ssh, need_chat=need_chat))
    if problems:
        _print_problems(problems)
        print(f'Цели: {config}', file=sys.stderr)
        print(f'Секреты: {env_path or ".env не найден"}', file=sys.stderr)
        return None
    _app.warn_on_insecure_env_file(_log(settings), env_path, 'пароли и токены')
    return settings, targets, env_path


_LOG = None
_CONSOLE = None


def _log_file(settings: Settings) -> str:
    return str(Path(settings.state_dir) / 'monitor-hub.log')


def _log(settings: Settings):
    global _LOG, _CONSOLE
    if _LOG is None:
        _LOG, _CONSOLE = _app.setup_logging('monitor_hub', _log_file(settings))
    return _LOG


def _daemon(settings: Settings):
    from monitor.daemon import Daemon
    return Daemon(log=_log(settings), script_name='monitor-hub',
                  pid_file=str(Path(settings.state_dir) / 'monitor-hub.pid'),
                  log_file=_log_file(settings))


def _hub(settings: Settings, targets):
    from monitor.hub import Hub
    log = _log(settings)
    return Hub(Engine(settings, os.environ, log, targets), targets, settings, log)


def cmd_validate(args: argparse.Namespace) -> int:
    loaded = _load(args, need_ssh=True, need_chat=not args.no_chat)
    if loaded is None:
        return 1
    _, targets, _ = loaded
    print(f'Конфигурация в порядке. Целей: {len(targets)}')
    return 0


def _describe(target: Target) -> str:
    when = ', '.join(str(s) for s in target.schedule) or 'только вручную'
    if target.type == 'mail':
        host, what = target.imap_host, f'письма: {len(target.expect)} маш.'
    else:
        host = target.target_host + (' (ssh)' if target.via == 'ssh' else '')
        what = ','.join(target.paths)
    return f'{target.name:12} {target.title:24} {host:18} {what}  [{when}]  {target.command or "-"}'


def cmd_list(args: argparse.Namespace) -> int:
    loaded = _load(args, need_ssh=False, need_chat=False)
    if loaded is None:
        return 1
    for target in loaded[1].values():
        print(_describe(target))
    return 0


def _find_target(args: argparse.Namespace, targets: dict[str, Target]) -> Target | None:
    target = targets.get(args.target)
    if target is None:
        print(f'Нет такой цели: {args.target}. Есть: {", ".join(targets)}', file=sys.stderr)
    return target


def _fail(engine: Engine, settings: Settings, target: Target, exc: Exception) -> int:
    text = engine.mask(str(exc), target)
    print(f'Ошибка: {text}', file=sys.stderr)
    message = text if text.startswith('[') else f'[{target.name}] {text}'
    log = _log(settings)
    if _CONSOLE is not None:           # трассировка только в файл лога: терминал не засоряем
        _CONSOLE.setLevel(logging.CRITICAL + 1)
    try:
        log.error('%s', message, exc_info=True)
    finally:
        if _CONSOLE is not None:
            _CONSOLE.setLevel(logging.NOTSET)
    return 1


def cmd_check(args: argparse.Namespace) -> int:
    loaded = _load(args, need_ssh=True, need_chat=not args.no_send)
    if loaded is None:
        return 1
    settings, targets, _ = loaded
    target = _find_target(args, targets)
    if target is None:
        return 2
    engine = Engine(settings, os.environ, _log(settings), targets)
    where = engine.chat.label
    with _tab(f'проверяю: {target.name}', f'готово: {target.name}', f'ошибка: {target.name}') as done:
        try:
            if target.type == 'mail':
                report = engine.check_mail(target, send=not args.no_send,
                                           check_date=mailcheck.parse_date_arg(args.date))
                print(report)
                saved: list[str] = []
            else:
                saved = engine.check(target, send=not args.no_send, open_after=args.open)
        except Exception as exc:
            done(False)
            return _fail(engine, settings, target, exc)
        done(True)
    for path in saved:
        print(path)
    print(f'Отправлено: {where}.' if not args.no_send else f'Готово ({where}: ничего не отправлялось).')
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    """Срочно посмотреть содержимое папки на цели (без правки конфигурации)."""
    loaded = _load(args, need_ssh=True, need_chat=args.send)
    if loaded is None:
        return 1
    settings, targets, _ = loaded
    target = _find_target(args, targets)
    if target is None:
        return 2
    if target.type != 'ls':
        print('Команда ls только для целей с type = "ls"', file=sys.stderr)
        return 2
    problem = path_problem(args.path)
    if problem:
        print(f'Путь не подходит: {problem}', file=sys.stderr)
        return 2
    engine = Engine(settings, os.environ, _log(settings), targets)
    one = replace(target, paths=(args.path,))
    with _tab(f'ls {args.path}', f'готово: {target.name}', f'ошибка: {target.name}') as done:
        try:
            lines = engine.fetch_listings(one, (args.path,))[args.path]
            done(True)
        except Exception as exc:
            done(False)
            return _fail(engine, settings, target, exc)
    print('\n'.join(lines))
    if args.send or args.open:
        try:
            for path in engine.check(one, send=args.send, open_after=args.open):
                print(path)
        except Exception as exc:
            return _fail(engine, settings, target, exc)
    return 0


def cmd_check_paths(args: argparse.Namespace) -> int:
    loaded = _load(args, need_ssh=True, need_chat=False)
    if loaded is None:
        return 1
    settings, targets, _ = loaded
    target = _find_target(args, targets)
    if target is None:
        return 2
    if target.type != 'ls':
        print('Команда check-paths только для целей с type = "ls"', file=sys.stderr)
        return 2
    engine = Engine(settings, os.environ, _log(settings), targets)
    try:
        verdicts = engine.check_paths(target, tuple(args.paths) or None)
    except Exception as exc:
        print(f'Ошибка: {engine.mask(str(exc), target)}', file=sys.stderr)
        return 1
    width = max(len(v.path) for v in verdicts)
    for v in verdicts:
        print(f'{"+" if v.ok else "!"} {v.path:{width}}  {v}')
    return 0 if all(v.ok for v in verdicts) else 1


def cmd_tui(args: argparse.Namespace) -> int:
    try:
        from monitor import tui
    except ImportError:
        print('Для интерфейса нужен пакет textual: pip install -r requirements-tui.txt', file=sys.stderr)
        return 2
    config = args.config or os.environ.get('TARGETS_FILE') or str(ROOT / 'targets.toml')
    return tui.main(config, ROOT)


def cmd_run(args: argparse.Namespace) -> int:
    loaded = _load(args, need_ssh=True, need_chat=True)
    if loaded is None:
        return 2
    settings, targets, _ = loaded
    hub = _hub(settings, targets)
    daemon = _daemon(settings)
    daemon.install_signal_handlers()
    titles.write(titles.SAVE)
    titles.set_title(f'служба, целей: {len(targets)}')
    try:
        daemon.foreground(preflight=lambda: True, scheduler_loop=hub.serve,
                          message=f'monitor-hub запущен в терминале, целей: {len(targets)}')
    finally:
        titles.write(titles.RESTORE)
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    loaded = _load(args, need_ssh=True, need_chat=True)
    if loaded is None:
        return 2
    settings, targets, _ = loaded
    hub = _hub(settings, targets)
    daemon = _daemon(settings)

    def loop() -> None:
        daemon.install_signal_handlers()
        hub.serve()

    daemon.start(
        preflight=lambda: True, scheduler_loop=loop,
        detach_console_logging=_detach_console,
        started_lines=lambda pid: [f'monitor-hub запущен (PID {pid}), целей: {len(targets)}',
                                   f'Лог: {_log_file(settings)}'])
    return 0


def _detach_console() -> None:
    if _LOG is not None and _CONSOLE is not None:
        _app.detach_console_logging(_LOG, _CONSOLE)


def _settings_only() -> Settings:
    _app.load_env(str(ROOT))
    return Settings.from_env()


def cmd_stop(args: argparse.Namespace) -> int:
    _daemon(_settings_only()).stop()
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    settings = _settings_only()
    _daemon(settings).stop()
    time.sleep(2)
    return cmd_start(args)


def cmd_status(args: argparse.Namespace) -> int:
    settings = _settings_only()
    config = args.config or os.environ.get('TARGETS_FILE') or str(ROOT / 'targets.toml')
    targets, problems = load_targets(config)
    if targets:
        problems += validate_environment(settings, targets, os.environ)

    from monitor import runlog
    last = runlog.read(settings.state_dir)

    def running_lines() -> list[str]:
        from monitor.scheduler import build_scheduler, next_runs
        rows = next_runs(build_scheduler(targets, lambda t: None))
        return [f'   следующий запуск {name}: {when:%d.%m.%Y %H:%M}' for name, when in rows]

    _daemon(settings).status(running_lines=running_lines, validate_config=lambda: problems)
    if last:
        print('\n-- Последние проверки --')
        for name, row in sorted(last.items()):
            mark = 'ok' if row.get('ok') else '! '
            age = runlog.age_text(time.time() - float(row.get('at', 0)))
            print(f'  {mark} {name:12} {age:>4} назад  {row.get("message", "")}')
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    _daemon(_settings_only()).logs(args.lines)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='monitor-hub', description='Мониторинг серверов через PAM jump-хост')
    parser.add_argument('--version', action='version', version=f'monitor-hub {__version__}')
    parser.add_argument('--config', help='путь к targets.toml')
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('validate', help='проверить конфигурацию без сети')
    p.add_argument('--no-chat', '--no-vk', dest='no_chat', action='store_true',
                   help='не требовать настроек мессенджера')
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser('list', help='показать цели')
    p.set_defaults(func=cmd_list)

    p = sub.add_parser('check', help='выполнить проверку цели сейчас')
    p.add_argument('target')
    p.add_argument('--no-send', action='store_true', help='сохранить картинки, но ничего не отправлять')
    p.add_argument('--open', action='store_true', help='открыть картинку в просмотрщике')
    p.add_argument('--date', default='', help='для целей mail: ночь на дату (вчера, 26.07, 26.07.2026)')
    p.set_defaults(func=cmd_check)

    p = sub.add_parser('ls', help='показать содержимое папки на цели прямо сейчас')
    p.add_argument('target')
    p.add_argument('path', help='абсолютный путь')
    p.add_argument('--send', action='store_true', help='отправить картинку в мессенджер')
    p.add_argument('--open', action='store_true', help='открыть картинку в просмотрщике')
    p.set_defaults(func=cmd_ls)
    p = sub.add_parser('check-paths', help='проверить пути цели (один вход на ВМ)')
    p.add_argument('target')
    p.add_argument('paths', nargs='*', help='другие пути вместо paths из цели')
    p.set_defaults(func=cmd_check_paths)

    sub.add_parser('tui', help='терминальный интерфейс').set_defaults(func=cmd_tui)

    for name, func, text in (
        ('run', cmd_run, 'демон в терминале (для systemd)'),
        ('start', cmd_start, 'запустить демон в фоне'),
        ('stop', cmd_stop, 'остановить демон'),
        ('restart', cmd_restart, 'перезапустить демон'),
        ('status', cmd_status, 'состояние, расписание, хвост лога'),
    ):
        sub.add_parser(name, help=text).set_defaults(func=func)
    p = sub.add_parser('logs', help='показать лог')
    p.add_argument('-n', '--lines', type=int, default=50)
    p.set_defaults(func=cmd_logs)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main())

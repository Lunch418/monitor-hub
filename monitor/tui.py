"""Терминальный интерфейс monitor-hub (Textual).

    ./monitor-hub tui

Слева список целей и параметры выбранной, справа предпросмотр отчёта и журнал.
Интерфейс правит только targets.toml и .env и проверяет цели вручную; мониторинг
по расписанию остаётся за демоном.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.theme import Theme
from textual.widgets import DataTable, Input, RichLog, Select, Static

from monitor import app as _app
from monitor import runlog
from monitor.config import (
    Settings,
    Target,
    load_targets,
    parse_targets,
    read_defaults,
    save_targets,
    target_to_dict,
    validate_environment,
)
from monitor.engine import STYLES as REPORT_STYLES
from monitor.engine import Engine
from monitor.envfile import set_env_value

ACCENT = '#d97757'
DIM = '#8b8b8b'
LINE = '#3d3a37'
OK = '#6fbf73'
WARN = '#e5c07b'
ERR = '#e06c75'
MC_BG = 'rgb(19,72,140)'

THEME = Theme(
    name='claude-report',
    primary=ACCENT,
    secondary=ACCENT,
    accent=ACCENT,
    background='#1b1a19',
    surface='#232120',
    panel='#232120',
    foreground='#e6e1da',
    dark=True,
)

STYLE_CHOICES = [
    ('listing', 'listing'),
    ('backups', 'backups'),
    ('backups-sizes', 'backups-sizes'),
]
SUDO_CHOICES = [('нет', 'no'), ('да', 'yes')]

# Поля формы: (id, подпись, подсказка)
FIELDS = [
    ('name', 'имя', 'латиница: crl, pgbackup'),
    ('title', 'название', 'как цель называется в беседе'),
    ('pam_user', 'pam', 'учётка на шлюзе'),
    ('password', 'пароль', ''),
    ('target_host', 'вм', 'IP или имя целевой машины'),
    ('target_sel', 'пункт', 'номер пользователя в меню PAM (обычно 1)'),
    ('paths', 'папки', '/путь/один, /путь/два'),
    ('schedule', 'время', 'daily 09:00, wed 10:00'),
    ('command', 'команда', '/get'),
    ('peers', 'кому', 'peer_id через запятую (пусто - VK_PEER_ID)'),
]


def split_list(text: str) -> list[str]:
    return [part.strip() for part in text.replace('\n', ',').split(',') if part.strip()]


def default_password_env(name: str) -> str:
    return f'{name.upper().replace("-", "_")}_PAM_PASSWORD'


def latest_screenshot(save_dir: str, name: str) -> Path | None:
    files = sorted(Path(save_dir).glob(f'{name}_*.png'))
    return files[-1] if files else None


def image_preview(path: Path, width: int = 72) -> Text:
    """PNG -> цветные пробелы: общий вид отчёта прямо в терминале (только ASCII)."""
    from PIL import Image
    with Image.open(path) as source:
        height = max(1, round(source.height / source.width * width / 2))
        raw = source.convert('RGB').resize((width, height)).tobytes()
    text = Text()
    for y in range(height):
        for x in range(width):
            i = (y * width + x) * 3
            text.append(' ', style=f'on rgb({raw[i]},{raw[i + 1]},{raw[i + 2]})')
        text.append('\n')
    return text


def _rgb(color: tuple[int, int, int]) -> str:
    return f'rgb({color[0]},{color[1]},{color[2]})'


def listing_preview(listings, style_name: str, width: int = 72) -> Text:
    """Листинг текстом в цветах отчёта: читается лучше, чем уменьшенная картинка."""
    row_style = REPORT_STYLES[style_name].row_style
    name_w = max(12, width - 24)
    text = Text()
    for path, entries, stale in listings:
        text.append(f' {path} '.ljust(width), style=f'bold #000000 on {_rgb((208, 205, 208))}')
        text.append('\n')
        header = f'{"Name":<{name_w}} {"Size":>8} {"Modify time":<13}'
        text.append(header.ljust(width), style=f'bold #ffd75f on {MC_BG}')
        text.append('\n')
        for entry in entries:
            name, color, date_color = row_style(entry, entry['raw_name'] in stale)
            name = name if len(name) <= name_w else name[:name_w - 1] + '~'
            text.append(f'{name:<{name_w}} ', style=f'{_rgb(color)} on {MC_BG}')
            text.append(f'{entry["size_disp"]:>8} ', style=f'{_rgb(color)} on {MC_BG}')
            text.append(f'{entry["date_str"]:<13}'.ljust(width - name_w - 10),
                        style=f'{_rgb(date_color)} on {MC_BG}')
            text.append('\n')
        text.append('\n')
    return text


class JournalHandler(logging.Handler):
    """Строки лога движка -> журнал интерфейса (ход входа: пароль, меню, shell, ls)."""

    def __init__(self, app: HubApp) -> None:
        super().__init__(logging.INFO)
        self.app = app

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:
            return
        color = ERR if record.levelno >= logging.ERROR else WARN if record.levelno >= logging.WARNING else DIM
        self.app.say_threadsafe(message, color)


class HubApp(App):
    TITLE = 'monitor-hub'
    APP_NAME = 'monitor-hub'
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding('ctrl+r', 'check', 'проверить', priority=True),
        Binding('ctrl+k', 'check_send', 'проверить и отправить', priority=True),
        Binding('ctrl+p', 'check_paths', 'пути', priority=True),
        Binding('ctrl+s', 'save', 'сохранить', priority=True),
        Binding('ctrl+n', 'new', 'новая', priority=True),
        Binding('ctrl+d', 'delete', 'удалить', priority=True),
        Binding('ctrl+l', 'status', 'статус', priority=True),
        Binding('ctrl+o', 'open_image', 'картинка', priority=True),
        Binding('escape', 'focus_targets', 'цели', show=False),
        Binding('ctrl+q', 'quit', 'выход', priority=True),
    ]
    CSS = f"""
    Screen {{ background: $background; padding: 0 1; }}

    #header {{ height: 1; margin-top: 1; }}
    #prompt {{ border: round {LINE}; height: 3; }}
    #prompt:focus-within {{ border: round {ACCENT}; }}
    #prompt-mark {{ width: 3; color: {ACCENT}; text-style: bold; padding-left: 1; }}

    #columns {{ height: 1fr; }}
    #left {{ width: 46%; min-width: 60; }}
    #right {{ width: 1fr; margin-left: 1; }}

    .panel {{ border: round {LINE}; border-title-color: {DIM}; border-title-align: left;
              padding: 0 1; }}
    .panel:focus-within {{ border: round {ACCENT}; border-title-color: {ACCENT}; }}
    #targets-panel {{ height: auto; max-height: 45%; }}
    #params-panel {{ height: 1fr; }}
    #preview-panel {{ height: 1fr; }}
    #log-panel {{ height: 12; }}

    DataTable {{ background: transparent; height: auto; max-height: 100%; scrollbar-size-vertical: 1; }}
    DataTable > .datatable--header {{ background: transparent; color: {DIM}; text-style: none; }}
    DataTable > .datatable--cursor {{ background: {ACCENT} 30%; }}
    DataTable:focus > .datatable--cursor {{ background: {ACCENT} 45%; text-style: bold; }}

    .row {{ height: 1; }}
    .key {{ width: 9; color: {DIM}; background: transparent; }}
    .key-short {{ width: 6; color: {DIM}; margin-left: 2; padding-left: 1; background: transparent; }}
    Input {{ border: none; height: 1; padding: 0 1; background: $surface-lighten-1; }}
    Input:focus {{ border: none; background: $surface-lighten-2; }}
    Input > .input--placeholder {{ color: {DIM}; }}
    .row Input {{ width: 1fr; }}
    #prompt Input {{ background: transparent; }}
    #prompt Input:focus {{ background: transparent; }}
    Select {{ width: 18; height: 1; }}
    #f-sudo {{ width: 10; }}
    Select > SelectCurrent {{ border: none; height: 1; padding: 0 1;
                              background: $surface-lighten-1; }}
    Select:focus > SelectCurrent {{ border: none; background: $surface-lighten-2; }}
    Select > SelectOverlay {{ width: 22; }}

    #preview-scroll {{ scrollbar-size-vertical: 1; }}
    #log {{ background: transparent; scrollbar-size-vertical: 1; }}
    #hints {{ height: 1; color: {DIM}; }}
    """

    def __init__(self, config_path: str | Path, root: str | Path, *, env_path: str | Path | None = None) -> None:
        super().__init__()
        self.config_path = Path(config_path)
        self.root = Path(root)
        self.env_path = Path(env_path) if env_path else None
        self.targets: dict[str, Target] = {}
        self.defaults: dict[str, object] = {}
        self.load_problems: list[str] = []
        self.current: str | None = None        # имя выбранной цели, None - новая
        self._delete_armed: tuple[str, float] | None = None
        self._main_thread = threading.get_ident()
        self._window_title = self.APP_NAME
        self.settings = Settings()
        self.log_ = logging.getLogger('monitor_hub.tui')
        self.engine: Engine | None = None

    # вид
    def compose(self) -> ComposeResult:
        yield Static(self._header_text(), id='header')
        with Horizontal(id='prompt'):
            yield Static('>', id='prompt-mark')
            yield Input(placeholder='имя цели, /run <цель> или /new <имя>', id='prompt-input')
        with Horizontal(id='columns'):
            with Vertical(id='left'):
                with Vertical(id='targets-panel', classes='panel'):
                    yield DataTable(id='targets', cursor_type='row', zebra_stripes=False)
                with Vertical(id='params-panel', classes='panel'):
                    for field_id, label, hint in FIELDS:
                        with Horizontal(classes='row'):
                            yield Static(label, id=f'k-{field_id}', classes='key')
                            yield Input(placeholder=hint, id=f'f-{field_id}',
                                        password=field_id == 'password')
                    with Horizontal(classes='row'):
                        yield Static('стиль', classes='key')
                        yield Select(STYLE_CHOICES, value='backups', allow_blank=False, id='f-style')
                        yield Static('sudo', classes='key-short')
                        yield Select(SUDO_CHOICES, value='no', allow_blank=False, id='f-sudo')
            with Vertical(id='right'):
                with Vertical(id='preview-panel', classes='panel'):
                    with VerticalScroll(id='preview-scroll'):
                        yield Static(Text('Выберите цель, чтобы увидеть последний отчёт.', style=DIM),
                                     id='preview')
                with Vertical(id='log-panel', classes='panel'):
                    yield RichLog(id='log', wrap=True, markup=False, highlight=False)
        yield Static('ctrl+r проверить   ctrl+k и отправить   ctrl+p пути   ctrl+s сохранить   '
                     'ctrl+n новая   ctrl+d удалить   ctrl+l статус   ctrl+q выход', id='hints')

    @staticmethod
    def _header_text() -> Text:
        text = Text()
        text.append('* ', style=f'bold {ACCENT}')
        text.append('monitor-hub', style='bold')
        text.append('   мониторинг серверов через PAM jump-хост', style=DIM)
        return text

    def on_mount(self) -> None:
        self._write_terminal('\x1b[22;0t')
        self.set_window_title()
        self.register_theme(THEME)
        self.theme = 'claude-report'
        self.query_one('#targets-panel').border_title = 'цели'
        self.query_one('#params-panel').border_title = 'параметры'
        self.query_one('#preview-panel').border_title = 'предпросмотр'
        self.query_one('#log-panel').border_title = 'журнал'
        table = self.query_one('#targets', DataTable)
        table.add_columns(' ', 'имя', 'название', 'вм', 'расписание', 'итог', 'когда')
        self._setup_engine()
        self.reload_targets()
        table.focus()

    def on_unmount(self) -> None:
        self._write_terminal('\x1b[23;0t')
        logging.getLogger('monitor_hub').removeHandler(self._journal)

    # заголовок окна
    def _write_terminal(self, data: str) -> None:
        driver = getattr(self, '_driver', None)
        if driver is None:
            return
        try:
            driver.write(data)
            driver.flush()
        except Exception:
            pass

    def set_window_title(self, text: str = '') -> None:
        clean = ''.join(ch for ch in str(text) if ch.isprintable())[:80]
        title = f'{clean} - {self.APP_NAME}' if clean else self.APP_NAME
        self._write_terminal(f'\x1b]0;{title}\x07')
        self._window_title = title

    # журнал
    def say(self, text: str, color: str | None = None, bullet: str = '-') -> None:
        line = Text()
        line.append(f'{bullet} ', style=color or '')
        line.append(text, style=color or '')
        self.query_one('#log', RichLog).write(line)

    def headline(self, text: str, color: str) -> None:
        line = Text()
        line.append('* ', style=f'bold {color}')
        line.append(text, style='bold')
        self.query_one('#log', RichLog).write(line)

    def say_threadsafe(self, text: str, color: str | None = None) -> None:
        if threading.get_ident() == self._main_thread:
            self.say(text, color)
        else:
            self.call_from_thread(self.say, text, color)

    # настройки
    def _setup_engine(self) -> None:
        loaded = _app.load_env(str(self.root))
        if self.env_path is None:
            self.env_path = Path(loaded) if loaded else self.root / '.env'
        self.settings = Settings.from_env()
        log, console = _app.setup_logging('monitor_hub', str(Path(self.settings.state_dir) / 'monitor-hub.log'))
        _app.detach_console_logging(log, console)   # консоль занята интерфейсом
        self._journal = JournalHandler(self)
        log.addHandler(self._journal)
        self.log_ = log

    def _make_engine(self, extra_env: dict[str, str] | None = None) -> Engine:
        env = {**os.environ, **(extra_env or {})}
        return Engine(self.settings, env, self.log_, self.targets)

    def reload_targets(self, select: str | None = None) -> None:
        self.targets, self.load_problems = load_targets(self.config_path)
        self.defaults = read_defaults(self.config_path)
        missing = not self.config_path.exists()
        if missing:
            self.load_problems = []
            self.say(f'{self.config_path.name} ещё нет: ctrl+n - первая цель, ctrl+s - сохранить', DIM)
        elif self.load_problems:
            self.headline(f'Ошибки в {self.config_path.name} - сохранение отключено', ERR)
            for problem in self.load_problems:
                self.say(problem, ERR)
        else:
            self.say(f'целей: {len(self.targets)} ({_short(self.config_path)})', DIM)
            env_problems = validate_environment(self.settings, self.targets, os.environ, need_chat=False)
            for problem in env_problems:
                self.say(problem, WARN)
        self._fill_table(select)

    def _fill_table(self, select: str | None = None, show: bool = True) -> None:
        table = self.query_one('#targets', DataTable)
        table.clear()
        last = runlog.read(self.settings.state_dir) if self.settings.state_dir else {}
        for target in self.targets.values():
            table.add_row(*self._row(target, last.get(target.name)), key=target.name)
        if not self.targets:
            self.action_new()
            return
        names = list(self.targets)
        index = names.index(select) if select in names else 0
        table.move_cursor(row=index)
        if show or self.current != names[index]:
            self._show_target(names[index])

    @staticmethod
    def _row(target: Target, last: dict | None) -> tuple:
        when = ', '.join(str(s) for s in target.schedule) or 'вручную'
        if last is None:
            mark, result, age = Text('-', style=DIM), Text('нет', style=DIM), ''
        elif last.get('ok'):
            mark, result = Text('+', style=OK), Text('ok', style=OK)
            age = runlog.age_text(time.time() - float(last.get('at', 0)))
        else:
            mark, result = Text('!', style=ERR), Text('ошибка', style=ERR)
            age = runlog.age_text(time.time() - float(last.get('at', 0)))
        return mark, target.name, target.title, target.target_host, when, result, age

    # форма
    def _field(self, name: str) -> Input:
        return self.query_one(f'#f-{name}', Input)

    def _show_target(self, name: str) -> None:
        target = self.targets[name]
        self.current = name
        values = {
            'name': target.name, 'title': target.title, 'pam_user': target.pam_user,
            'target_host': target.target_host, 'target_sel': target.target_sel,
            'paths': ', '.join(target.paths), 'schedule': ', '.join(str(s) for s in target.schedule),
            'command': target.command, 'peers': ', '.join(target.peers),
        }
        for key, value in values.items():
            self._field(key).value = value
        self._field('password').clear()
        self._field('name').disabled = True
        self._field('password').placeholder = f'не менять ({target.password_env} в .env)'
        self.query_one('#f-style', Select).value = target.style
        self.query_one('#f-sudo', Select).value = 'yes' if target.sudo else 'no'
        self._mark_password(target)
        self.query_one('#params-panel').border_title = f'параметры: {name}'
        self._show_preview(target)
        self._delete_armed = None

    def _mark_password(self, target: Target | None) -> None:
        key = self.query_one('#k-password', Static)
        if target is None:
            key.update(Text('пароль', style=DIM))
        elif os.environ.get(target.password_env):
            key.update(Text('пароль +', style=OK))
        else:
            key.update(Text('пароль !', style=ERR))

    def form_target(self) -> tuple[Target | None, list[str], str]:
        """Цель из формы. Возвращает (цель, проблемы, введённый пароль)."""
        name = self._field('name').value.strip()
        if not name:
            return None, ['укажите имя цели (латиница, цифры, - и _)'], ''
        existing = self.targets.get(self.current or '')
        raw: dict[str, object] = {
            'title': self._field('title').value.strip() or name,
            'pam_user': self._field('pam_user').value.strip(),
            'password_env': existing.password_env if existing else default_password_env(name or 'x'),
            'target_host': self._field('target_host').value.strip(),
            'target_sel': self._field('target_sel').value.strip() or '1',
            'paths': split_list(self._field('paths').value),
            'schedule': split_list(self._field('schedule').value),
            'style': str(self.query_one('#f-style', Select).value),
            'sudo': self.query_one('#f-sudo', Select).value == 'yes',
            'peers': split_list(self._field('peers').value),
        }
        command = self._field('command').value.strip()
        if command:
            raw['command'] = command
        if existing:
            keep = target_to_dict(existing, self.defaults)
            for key in ('stale_days', 'prompt_timeout', 'connect_retries', 'connect_retry_delay', 'run_timeout'):
                if key in keep:
                    raw[key] = keep[key]
        others = {n: target_to_dict(t, self.defaults) for n, t in self.targets.items() if n != self.current}
        if not self.current and name in others:
            return None, [f'цель {name} уже есть'], ''
        parsed, problems = parse_targets({'defaults': self.defaults, 'targets': {**others, name: raw}})
        return parsed.get(name), problems, self._field('password').value

    # события
    @on(DataTable.RowHighlighted, '#targets')
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        name = event.row_key.value if event.row_key is not None else None
        if name in self.targets and name != self.current:
            self._show_target(name)

    @on(Input.Submitted, '#prompt-input')
    def _prompt(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ''
        if not text:
            return
        parts = text.split()
        if parts[0] == '/new':
            self.action_new()
            if len(parts) > 1:
                self._field('name').value = parts[1]
            return
        if parts[0] == '/run' and len(parts) == 2:
            name = parts[1]
        else:
            name = parts[0].lstrip('/')
            by_command = {t.command: n for n, t in self.targets.items() if t.command}
            name = by_command.get(parts[0], name)
        if name not in self.targets:
            self.headline(f'Нет такой цели: {name}', ERR)
            return
        self._select(name)
        if parts[0] == '/run' or parts[0] in {t.command for t in self.targets.values()}:
            self.action_check()

    def _select(self, name: str) -> None:
        table = self.query_one('#targets', DataTable)
        table.move_cursor(row=list(self.targets).index(name))
        self._show_target(name)

    # действия
    def action_focus_targets(self) -> None:
        self.query_one('#targets', DataTable).focus()

    def action_new(self) -> None:
        self.current = None
        for field_id, _, _ in FIELDS:
            self._field(field_id).value = ''
        self._field('name').disabled = False
        self._field('password').placeholder = 'пароль PAM (сохранится только в .env)'
        self.query_one('#f-style', Select).value = 'backups'
        self.query_one('#f-sudo', Select).value = 'no'
        self._mark_password(None)
        self.query_one('#params-panel').border_title = 'параметры: новая цель'
        self.query_one('#preview', Static).update(Text('Новая цель: заполните поля и нажмите ctrl+s.', style=DIM))
        self._field('name').focus()

    def action_save(self) -> None:
        if self.load_problems:
            self.headline(f'{self.config_path.name} с ошибками: исправьте его вручную', ERR)
            return
        target, problems, password = self.form_target()
        if target is None or problems:
            self.headline('Не сохранено', ERR)
            for problem in problems:
                self.say(problem, ERR)
            return
        if not self.current and not password and not os.environ.get(target.password_env):
            self.headline('Не сохранено: введите пароль PAM', ERR)
            return
        targets = dict(self.targets)
        targets[target.name] = target
        try:
            backup = save_targets(self.config_path, targets, self.defaults)
            if password:
                set_env_value(self.env_path or self.root / '.env', target.password_env, password)
                os.environ[target.password_env] = password
        except (OSError, ValueError) as exc:
            self.headline(f'Ошибка записи: {exc}', ERR)
            return
        self._field('password').value = ''
        self.headline(f'Сохранено: {target.name}', OK)
        if backup:
            self.say(f'прошлая версия: {backup.name}', DIM)
        if password:
            self.say(f'пароль записан в .env ({target.password_env})', DIM)
        self.say('демон подхватит изменения после: ./monitor-hub restart', DIM)
        self.current = target.name
        self.reload_targets(select=target.name)

    def action_delete(self) -> None:
        name = self.current
        if not name:
            return
        now = time.monotonic()
        if not self._delete_armed or self._delete_armed[0] != name or now - self._delete_armed[1] > 4:
            self._delete_armed = (name, now)
            self.headline(f'Удалить {name}? Нажмите ctrl+d ещё раз', WARN)
            return
        targets = {n: t for n, t in self.targets.items() if n != name}
        try:
            if targets:
                save_targets(self.config_path, targets, self.defaults)
            else:
                from monitor.envfile import atomic_write
                atomic_write(self.config_path, '', mode=0o640)
        except (OSError, ValueError) as exc:
            self.headline(f'Ошибка записи: {exc}', ERR)
            return
        self._delete_armed = None
        self.current = None
        self.headline(f'Удалено: {name} (прошлая версия: {self.config_path.name}.bak)', OK)
        self.reload_targets()

    def _target_for_run(self) -> tuple[Target | None, str]:
        target, problems, password = self.form_target()
        if target is None or problems:
            self.headline('Цель заполнена с ошибками', ERR)
            for problem in problems:
                self.say(problem, ERR)
            return None, ''
        return target, password

    def action_check(self, send: bool = False) -> None:
        target, password = self._target_for_run()
        if target is None:
            return
        extra = {target.password_env: password} if password else {}
        need = validate_environment(self.settings, {target.name: target}, {**os.environ, **extra}, need_chat=send)
        if need:
            self.headline('Не хватает настроек', ERR)
            for problem in need:
                self.say(problem, ERR)
            return
        self.headline(f'Проверяю {target.name}{" и отправляю" if send else ""}...', ACCENT)
        self.set_window_title(f'[..] проверяю: {target.name}')
        self._check_worker(target, extra, send)

    def action_check_send(self) -> None:
        self.action_check(send=True)

    @work(thread=True, exclusive=True, group='net')
    def _check_worker(self, target: Target, extra: dict[str, str], send: bool) -> None:
        engine = self._make_engine(extra)
        try:
            saved = engine.check(target, send=send)
        except Exception as exc:
            self.call_from_thread(self._check_failed, target, engine.mask(engine.user_facing(exc), target))
            return
        self.call_from_thread(self._check_done, target, saved, engine.last_report, send)

    def _check_failed(self, target: Target, message: str) -> None:
        self.set_window_title(f'[!] ошибка: {target.name}')
        self.headline(f'Ошибка: {message}', ERR)
        self._fill_table(select=self.current or target.name, show=False)

    def _check_done(self, target: Target, saved: list[str], report, send: bool) -> None:
        self.set_window_title(f'[ok] готово: {target.name}')
        if report is not None:
            self.query_one('#preview', Static).update(
                listing_preview(report.listings, target.style, self._preview_width()))
            self.query_one('#preview-panel').border_title = f'предпросмотр: {target.name}, только что'
        for path in saved:
            self.say(f'картинка: {_short(path)}', DIM)
        self.headline('Готово, отправлено в беседу' if send else 'Готово (в беседу не отправлялось)', OK)
        self._fill_table(select=self.current or target.name, show=False)

    def action_check_paths(self) -> None:
        target, password = self._target_for_run()
        if target is None:
            return
        extra = {target.password_env: password} if password else {}
        need = validate_environment(self.settings, {target.name: target}, {**os.environ, **extra}, need_chat=False)
        if need:
            self.headline('Не хватает настроек', ERR)
            for problem in need:
                self.say(problem, ERR)
            return
        self.headline(f'Проверяю пути {target.name}...', ACCENT)
        self.set_window_title(f'[..] пути: {target.name}')
        self._paths_worker(target, extra)

    @work(thread=True, exclusive=True, group='net')
    def _paths_worker(self, target: Target, extra: dict[str, str]) -> None:
        engine = self._make_engine(extra)
        try:
            verdicts = engine.check_paths(target)
        except Exception as exc:
            self.call_from_thread(self._check_failed, target, engine.mask(engine.user_facing(exc), target))
            return
        self.call_from_thread(self._paths_done, target, verdicts)

    def _paths_done(self, target: Target, verdicts) -> None:
        bad = [v for v in verdicts if not v.ok]
        for v in verdicts:
            self.say(f'{"+" if v.ok else "!"} {v.path}: {v}', OK if v.ok else ERR, bullet=' ')
        if bad:
            self.set_window_title(f'[!] пути: {target.name}')
            self.headline(f'Проблемных путей: {len(bad)} из {len(verdicts)}', ERR)
        else:
            self.set_window_title(f'[ok] пути: {target.name}')
            self.headline(f'Все пути в порядке ({len(verdicts)})', OK)

    def action_status(self) -> None:
        from monitor.daemon import Daemon
        from monitor.scheduler import build_scheduler, next_runs
        state = Path(self.settings.state_dir)
        daemon = Daemon(log=self.log_, script_name='monitor-hub',
                        pid_file=str(state / 'monitor-hub.pid'), log_file=str(state / 'monitor-hub.log'))
        pid, _ = daemon.read_pid()
        if daemon.alive(pid):
            self.headline(f'Демон запущен (PID {pid})', OK)
        else:
            self.headline('Демон не запущен: ./monitor-hub start или служба systemd', WARN)
        for name, when in next_runs(build_scheduler(self.targets, lambda t: None)):
            self.say(f'следующий запуск {name}: {when:%d.%m %H:%M}', DIM)
        for line in daemon.tail(str(state / 'monitor-hub.log'), 8):
            self.say(line.rstrip(), DIM, bullet=' ')

    def action_open_image(self) -> None:
        if not self.current:
            return
        path = latest_screenshot(self.settings.save_dir, self.current)
        if path is None:
            self.headline('Картинок этой цели ещё нет: ctrl+r', DIM)
            return
        from monitor.ls_report import open_screenshot
        open_screenshot(str(path))
        self.say(f'открыто: {_short(path)}', DIM)

    def _preview_width(self) -> int:
        width = self.query_one('#preview-scroll').size.width - 2
        return max(40, min(width, 120)) if width > 0 else 72

    def _show_preview(self, target: Target) -> None:
        preview = self.query_one('#preview', Static)
        panel = self.query_one('#preview-panel')
        saved = runlog.read_listing(self.settings.state_dir, target.name) if self.settings.state_dir else None
        if saved is not None:
            style, at, listings = saved
            panel.border_title = f'предпросмотр: {target.name}, {runlog.age_text(time.time() - at)} назад'
            preview.update(listing_preview(listings, style if style in REPORT_STYLES else target.style,
                                           self._preview_width()))
            return
        path = latest_screenshot(self.settings.save_dir, target.name) if self.settings.save_dir else None
        if path is None:
            panel.border_title = 'предпросмотр'
            preview.update(Text('Отчётов ещё нет: ctrl+r - проверить сейчас.', style=DIM))
            return
        panel.border_title = f'предпросмотр: {path.name}'
        try:
            preview.update(image_preview(path, self._preview_width()))
        except Exception as exc:
            preview.update(Text(f'Не удаётся показать {path.name}: {exc}', style=WARN))


def _short(path: str | Path) -> str:
    home = str(Path.home())
    text = str(path)
    return '~' + text[len(home):] if text.startswith(home) else text


def main(config_path: str | Path, root: str | Path) -> int:
    HubApp(config_path, root).run()
    return 0

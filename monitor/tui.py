"""Терминальный интерфейс monitor-hub (Textual).

    ./monitor-hub tui

Слева список целей и параметры выбранной, справа предпросмотр отчёта и журнал.
Всё настраивается здесь: поля цели (они зависят от типа и способа входа), общие
настройки (ctrl+e) и команды в строке ">" (/help). Интерфейс правит только
targets.toml и .env и проверяет цели вручную; мониторинг по расписанию остаётся за демоном.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import DataTable, Input, RichLog, Select, Static

from monitor import app as _app
from monitor import mailcheck, runlog, titles
from monitor.config import (
    Settings,
    Target,
    load_targets,
    parse_targets,
    path_problem,
    read_defaults,
    save_targets,
    target_to_dict,
    validate_environment,
)
from monitor.engine import STYLES as REPORT_STYLES
from monitor.engine import Engine
from monitor.envfile import set_env_value
from monitor.formspec import (
    ENV_FIELDS,
    FIELD_BY_ID,
    FIELDS,
    HELP,
    blank_values,
    check_env_value,
    default_password_env,
    parse_prompt,
    raw_from_values,
    values_from_target,
    visible,
)
from monitor.formspec import apply_set as _apply_set

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


class SettingsScreen(ModalScreen[list]):
    """Общие настройки из .env: мессенджер, шлюз, токены. Закрывается списком изменённых ключей."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding('ctrl+s', 'save', 'сохранить', priority=True),
        Binding('escape', 'cancel', 'закрыть', priority=True),
    ]
    CSS = f"""
    SettingsScreen {{ align: center middle; background: #000000 60%; }}
    #settings-box {{ width: 84; max-width: 96%; height: 90%; border: round {ACCENT}; background: $surface;
                     padding: 0 1; }}
    #settings-title {{ height: 1; text-style: bold; color: {ACCENT}; margin-bottom: 1; }}
    #settings-fields {{ height: 1fr; scrollbar-size-vertical: 1; }}
    SettingsScreen .row {{ height: 1; }}
    SettingsScreen .key {{ width: 14; color: {DIM}; background: transparent; }}
    SettingsScreen Input {{ border: none; height: 1; padding: 0 1; background: $surface-lighten-1; width: 1fr; }}
    SettingsScreen Input:focus {{ background: $surface-lighten-2; }}
    SettingsScreen Input > .input--placeholder {{ color: {DIM}; }}
    SettingsScreen Select {{ width: 26; height: 1; }}
    SettingsScreen Select > SelectCurrent {{ border: none; height: 1; padding: 0 1;
                                             background: $surface-lighten-1; }}
    #settings-problems {{ height: auto; color: {ERR}; }}
    #settings-hints {{ height: 1; color: {DIM}; }}
    """

    def __init__(self, env_path: Path) -> None:
        super().__init__()
        self.env_path = env_path

    def compose(self) -> ComposeResult:
        with Vertical(id='settings-box'):
            yield Static(f'общие настройки ({self.env_path.name})', id='settings-title')
            with VerticalScroll(id='settings-fields'):
                for field in ENV_FIELDS:
                    with Horizontal(classes='row'):
                        yield Static(field.label, classes='key')
                        if field.choices:
                            yield Select(list(field.choices), value=self._current_choice(field),
                                         allow_blank=False, id=f'e-{field.key}')
                        else:
                            yield Input(value='' if field.secret else os.environ.get(field.key, ''),
                                        placeholder=self._placeholder(field), password=field.secret,
                                        id=f'e-{field.key}')
            yield Static('', id='settings-problems')
            yield Static('ctrl+s сохранить   escape закрыть   пустой секрет - не менять', id='settings-hints')

    @staticmethod
    def _current_choice(field) -> str:
        value = os.environ.get(field.key, '')
        if field.key == 'MESSENGER':
            return value.lower() or 'vk'
        if field.key == 'SSH_STRICT_HOST_KEY':
            return value or 'accept-new'
        if field.key == 'ALLOW_ANY_USER':
            return '1' if value.lower() in ('1', 'true', 'yes', 'on') else ''
        return value

    @staticmethod
    def _placeholder(field) -> str:
        if field.secret:
            return 'задан (не показывается)' if os.environ.get(field.key) else 'не задан'
        return field.hint

    def action_cancel(self) -> None:
        self.dismiss([])

    def action_save(self) -> None:
        wanted: dict[str, str] = {}
        problems: list[str] = []
        for field in ENV_FIELDS:
            widget = self.query_one(f'#e-{field.key}')
            if isinstance(widget, Select):
                wanted[field.key] = str(widget.value)
                continue
            text = widget.value.strip() if isinstance(widget, Input) else ''
            if field.secret and not text:
                continue                     # секрет не показываем, пустое поле - не менять
            error = check_env_value(field, text)
            if error:
                problems.append(f'{field.label}: {error}')
            wanted[field.key] = text
        box = self.query_one('#settings-problems', Static)
        if problems:
            box.update('\n'.join(problems))
            return
        changed: list[str] = []
        try:
            for key, value in wanted.items():
                if value == os.environ.get(key, ''):
                    continue
                set_env_value(self.env_path, key, value)
                if value:
                    os.environ[key] = value
                else:
                    os.environ.pop(key, None)
                changed.append(key)
        except (OSError, ValueError) as exc:
            box.update(f'Ошибка записи: {exc}')
            return
        self.dismiss(changed)


class HubApp(App):
    TITLE = 'monitor-hub'
    APP_NAME = titles.APP_NAME
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding('ctrl+r', 'check', 'проверить', priority=True),
        Binding('ctrl+k', 'check_send', 'проверить и отправить', priority=True),
        Binding('ctrl+p', 'check_paths', 'пути', priority=True),
        Binding('ctrl+s', 'save', 'сохранить', priority=True),
        Binding('ctrl+n', 'new', 'новая', priority=True),
        Binding('ctrl+d', 'delete', 'удалить', priority=True),
        Binding('ctrl+e', 'settings', 'настройки', priority=True),
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
    #targets-panel {{ height: auto; max-height: 40%; }}
    #params-panel {{ height: 1fr; scrollbar-size-vertical: 1; }}
    #preview-panel {{ height: 1fr; }}
    #log-panel {{ height: 12; }}

    DataTable {{ background: transparent; height: auto; max-height: 100%; scrollbar-size-vertical: 1; }}
    DataTable > .datatable--header {{ background: transparent; color: {DIM}; text-style: none; }}
    DataTable > .datatable--cursor {{ background: {ACCENT} 30%; }}
    DataTable:focus > .datatable--cursor {{ background: {ACCENT} 45%; text-style: bold; }}

    .row {{ height: 1; }}
    .key {{ width: 13; color: {DIM}; background: transparent; }}
    Input {{ border: none; height: 1; padding: 0 1; background: $surface-lighten-1; }}
    Input:focus {{ border: none; background: $surface-lighten-2; }}
    Input > .input--placeholder {{ color: {DIM}; }}
    .row Input {{ width: 1fr; }}
    #prompt Input {{ background: transparent; }}
    #prompt Input:focus {{ background: transparent; }}
    .row Select {{ width: 30; height: 1; }}
    Select > SelectCurrent {{ border: none; height: 1; padding: 0 1;
                              background: $surface-lighten-1; }}
    Select:focus > SelectCurrent {{ border: none; background: $surface-lighten-2; }}
    Select > SelectOverlay {{ width: 34; }}

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
            yield Input(placeholder='имя цели, /run, /ls <путь>, /set <поле> <значение>, /help', id='prompt-input')
        with Horizontal(id='columns'):
            with Vertical(id='left'):
                with Vertical(id='targets-panel', classes='panel'):
                    yield DataTable(id='targets', cursor_type='row', zebra_stripes=False)
                with VerticalScroll(id='params-panel', classes='panel'):
                    for field in FIELDS:
                        with Horizontal(classes='row', id=f'row-{field.id}'):
                            yield Static(field.label, id=f'k-{field.id}', classes='key')
                            if field.widget == 'select':
                                yield Select(list(field.choices), value=field.choices[0][1],
                                             allow_blank=False, id=f'f-{field.id}')
                            else:
                                yield Input(placeholder=field.hint, id=f'f-{field.id}',
                                            password=field.widget == 'password')
            with Vertical(id='right'):
                with Vertical(id='preview-panel', classes='panel'):
                    with VerticalScroll(id='preview-scroll'):
                        yield Static(Text('Выберите цель, чтобы увидеть последний отчёт.', style=DIM),
                                     id='preview')
                with Vertical(id='log-panel', classes='panel'):
                    yield RichLog(id='log', wrap=True, markup=False, highlight=False)
        yield Static('ctrl+r проверить  ctrl+k и отправить  ctrl+p пути  ctrl+s сохранить  ctrl+n новая  '
                     'ctrl+d удалить  ctrl+e настройки  ctrl+l статус  /help  ctrl+q выход', id='hints')

    @staticmethod
    def _header_text() -> Text:
        text = Text()
        text.append('* ', style=f'bold {ACCENT}')
        text.append('monitor-hub', style='bold')
        text.append('   мониторинг серверов и бэкапов: ssh, PAM, почта', style=DIM)
        return text

    def on_mount(self) -> None:
        self._write_terminal(titles.SAVE)
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
        self._write_terminal(titles.RESTORE)
        logging.getLogger('monitor_hub').removeHandler(self._journal)

    # заголовок окна (вкладки терминала)
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
        title = titles.format_title(text)
        self._write_terminal(titles.osc(title))
        self._window_title = title

    def _idle_title(self) -> None:
        """Пока ничего не идёт: сколько целей и есть ли среди последних проверок ошибки."""
        last = runlog.read(self.settings.state_dir) if self.settings.state_dir else {}
        bad = sum(1 for name in self.targets if name in last and not last[name].get('ok'))
        text = f'целей: {len(self.targets)}'
        self.set_window_title(f'[!] {text}, с ошибкой: {bad}' if bad else text)

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
            self.say(f'целей: {len(self.targets)} ({_short(self.config_path)}), '
                     f'мессенджер: {self.settings.messenger}', DIM)
            env_problems = validate_environment(self.settings, self.targets, os.environ, need_chat=False)
            for problem in env_problems:
                self.say(problem, WARN)
        self._fill_table(select)
        self._idle_title()

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
        where = target.imap_host if target.type == 'mail' else target.target_host
        return mark, target.name, target.title, where, when, result, age

    # форма
    def _widget(self, name: str) -> Input | Select:
        widget = self.query_one(f'#f-{name}')
        assert isinstance(widget, (Input, Select))
        return widget

    def _input(self, name: str) -> Input:
        widget = self.query_one(f'#f-{name}')
        assert isinstance(widget, Input)
        return widget

    def _get(self, name: str) -> str:
        widget = self._widget(name)
        return str(widget.value)

    def _set(self, name: str, value: str) -> None:
        widget = self._widget(name)
        if isinstance(widget, Select):
            options = {v for _, v in FIELD_BY_ID[name].choices}
            widget.value = value if value in options else FIELD_BY_ID[name].choices[0][1]
        else:
            widget.value = value

    def _kind(self) -> str:
        return self._get('type') or 'ls'

    def _via(self) -> str:
        return self._get('via') or 'pam'

    def _apply_visibility(self) -> None:
        kind, via = self._kind(), self._via()
        for field in FIELDS:
            self.query_one(f'#row-{field.id}').display = visible(field, kind, via)
        label = 'пароль почты' if kind == 'mail' else 'пароль ssh' if via == 'ssh' else 'пароль PAM'
        self._mark_password(self.targets.get(self.current or ''), label)

    def _fill_form(self, values: dict[str, str]) -> None:
        for field in FIELDS:
            if field.id == 'password':
                continue
            self._set(field.id, values.get(field.id, ''))
        self._input('password').value = ''
        self._apply_visibility()

    def _values(self) -> dict[str, str]:
        return {field.id: self._get(field.id) for field in FIELDS}

    def _show_target(self, name: str) -> None:
        target = self.targets[name]
        self.current = name
        self._fill_form(values_from_target(target))
        self._input('name').disabled = True
        self._input('password').placeholder = f'не менять ({target.password_env} в .env)'
        self.query_one('#params-panel').border_title = f'параметры: {name}'
        self._show_preview(target)
        self._delete_armed = None

    def _mark_password(self, target: Target | None, label: str = 'пароль') -> None:
        key = self.query_one('#k-password', Static)
        if target is None:
            key.update(Text(label, style=DIM))
        elif os.environ.get(target.password_env):
            key.update(Text(f'{label} +', style=OK))
        else:
            key.update(Text(f'{label} !', style=ERR))

    def form_target(self) -> tuple[Target | None, list[str], str]:
        """Цель из формы. Возвращает (цель, проблемы, введённый пароль)."""
        values = self._values()
        name = self._get('name').strip()
        if not name:
            return None, ['укажите имя цели (латиница, цифры, - и _)'], ''
        kind, via = self._kind(), self._via()
        existing = self.targets.get(self.current or '')
        password_env = existing.password_env if existing else default_password_env(name, kind, via)
        raw, problems = raw_from_values(values, kind=kind, via=via, password_env=password_env)
        others = {n: target_to_dict(t, self.defaults) for n, t in self.targets.items() if n != self.current}
        if not self.current and name in others:
            return None, [f'цель {name} уже есть'], ''
        parsed, more = parse_targets({'defaults': self.defaults, 'targets': {**others, name: raw}})
        return parsed.get(name), problems + more, self._get('password')

    # события
    @on(DataTable.RowHighlighted, '#targets')
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        name = event.row_key.value if event.row_key is not None else None
        if name in self.targets and name != self.current:
            self._show_target(name)

    @on(Select.Changed, '#f-type')
    @on(Select.Changed, '#f-via')
    def _kind_changed(self, event: Select.Changed) -> None:
        self._apply_visibility()

    @on(Input.Submitted, '#prompt-input')
    def _prompt(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ''
        command = parse_prompt(text)
        if command is None:
            return
        handler = getattr(self, f'_cmd_{command.name}', None) if text.startswith('/') else None
        if handler is not None:
            handler(command)
            return
        by_command = {t.command: n for n, t in self.targets.items() if t.command}
        head = text.split()[0]
        name = by_command.get(head, head.lstrip('/'))
        if name not in self.targets:
            self.headline(f'Нет такой цели или команды: {head}. /help - список команд', ERR)
            return
        self._select(name)
        if head in by_command:
            self.action_check(arg=' '.join(text.split()[1:]))

    def _select(self, name: str) -> None:
        table = self.query_one('#targets', DataTable)
        table.move_cursor(row=list(self.targets).index(name))
        self._show_target(name)

    # команды строки ">"
    def _target_and_rest(self, args: tuple[str, ...]) -> tuple[str | None, tuple[str, ...]]:
        """Первое слово - имя цели? Тогда выбираем её. Иначе работаем с выбранной."""
        if args and args[0] in self.targets:
            self._select(args[0])
            return args[0], args[1:]
        return self.current, args

    def _cmd_new(self, command) -> None:
        name = next((a for a in command.args if a not in ('mail', 'ls')), '')
        self.action_new('mail' if 'mail' in command.args else 'ls', name)

    def _cmd_run(self, command) -> None:
        _, rest = self._target_and_rest(command.args)
        self.action_check(arg=' '.join(rest))

    def _cmd_send(self, command) -> None:
        _, rest = self._target_and_rest(command.args)
        self.action_check(send=True, arg=' '.join(rest))

    def _cmd_paths(self, command) -> None:
        self._target_and_rest(command.args)
        self.action_check_paths()

    def _cmd_ls(self, command) -> None:
        _, rest = self._target_and_rest(command.args)
        send = bool(rest) and rest[-1].lower() in ('send', 'отправить')
        words = rest[:-1] if send else rest
        if not words:
            self.headline('Укажите папку: /ls /путь/к/папке  (в конце send - отправить картинку)', WARN)
            return
        self.action_ls(' '.join(words), send)

    def _cmd_set(self, command) -> None:
        field_id, _, value = command.rest.partition(' ')
        if not field_id:
            self.headline('Вид: /set <поле> <значение>, например /set command /pg', WARN)
            return
        values = self._values()
        error = _apply_set(values, field_id, value, kind=self._kind(), via=self._via())
        if error:
            self.headline(error, ERR)
            return
        for key in ('type', 'via'):
            if key == field_id:
                self._set(key, values[key])
        self._apply_visibility()
        if field_id not in ('type', 'via'):
            self._set(field_id, values[field_id])
        self.say(f'{field_id} = {values[field_id]}  (не забудьте /save)', DIM)

    def _cmd_save(self, command) -> None:
        self.action_save()

    def _cmd_delete(self, command) -> None:
        self.action_delete()

    def _cmd_settings(self, command) -> None:
        self.action_settings()

    def _cmd_status(self, command) -> None:
        self.action_status()

    def _cmd_open(self, command) -> None:
        self.action_open_image()

    def _cmd_help(self, command) -> None:
        self.headline('Команды строки ">"', ACCENT)
        for usage, text in HELP:
            self.say(f'{usage:<28} {text}', DIM, bullet=' ')

    # действия
    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Пока открыто окно настроек, горячие клавиши главного экрана не работают
        (иначе ctrl+s сохранил бы цель вместо настроек)."""
        if isinstance(self.screen, SettingsScreen) and action != 'quit':
            return False
        return True

    def action_focus_targets(self) -> None:
        self.query_one('#targets', DataTable).focus()

    def action_new(self, kind: str = 'ls', name: str = '') -> None:
        self.current = None
        self._fill_form(blank_values(kind))
        self._set('name', name)
        self._input('name').disabled = False
        self._input('password').placeholder = 'сохранится только в .env'
        self._apply_visibility()
        self.query_one('#params-panel').border_title = 'параметры: новая цель'
        self.query_one('#preview', Static).update(Text('Новая цель: заполните поля и нажмите ctrl+s.', style=DIM))
        self._input('name').focus()

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
            self.headline(f'Не сохранено: введите пароль ({self._password_label()})', ERR)
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
        self._input('password').value = ''
        self.headline(f'Сохранено: {target.name}', OK)
        if backup:
            self.say(f'прошлая версия: {backup.name}', DIM)
        if password:
            self.say(f'пароль записан в .env ({target.password_env})', DIM)
        self.say('демон подхватит изменения после: ./monitor-hub restart', DIM)
        self.current = target.name
        self.reload_targets(select=target.name)

    def _password_label(self) -> str:
        return 'почты' if self._kind() == 'mail' else 'ssh' if self._via() == 'ssh' else 'PAM'

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

    def action_settings(self) -> None:
        self.push_screen(SettingsScreen(self.env_path or self.root / '.env'), self._settings_closed)

    def _settings_closed(self, changed: list | None) -> None:
        if not changed:
            return
        self.settings = Settings.from_env()
        self.headline('Настройки сохранены: ' + ', '.join(changed), OK)
        for problem in validate_environment(self.settings, self.targets, os.environ, need_chat=True):
            self.say(problem, WARN)
        self.say('демон подхватит изменения после: ./monitor-hub restart', DIM)
        self._idle_title()

    def _target_for_run(self) -> tuple[Target | None, str]:
        target, problems, password = self.form_target()
        if target is None or problems:
            self.headline('Цель заполнена с ошибками', ERR)
            for problem in problems:
                self.say(problem, ERR)
            return None, ''
        return target, password

    def _env_ready(self, target: Target, extra: dict[str, str], need_chat: bool) -> bool:
        need = validate_environment(self.settings, {target.name: target}, {**os.environ, **extra},
                                    need_chat=need_chat)
        if need:
            self.headline('Не хватает настроек', ERR)
            for problem in need:
                self.say(problem, ERR)
        return not need

    def action_check(self, send: bool = False, arg: str = '') -> None:
        target, password = self._target_for_run()
        if target is None:
            return
        extra = {target.password_env: password} if password else {}
        if not self._env_ready(target, extra, send):
            return
        self.headline(f'Проверяю {target.name}{" и отправляю" if send else ""}...', ACCENT)
        self.set_window_title(f'[..] проверяю: {target.name}')
        self._check_worker(target, extra, send, arg)

    def action_check_send(self) -> None:
        self.action_check(send=True)

    @work(thread=True, exclusive=True, group='net')
    def _check_worker(self, target: Target, extra: dict[str, str], send: bool, arg: str = '') -> None:
        engine = self._make_engine(extra)
        try:
            if target.type == 'mail':
                text = engine.check_mail(target, send=send, check_date=mailcheck.parse_date_arg(arg))
            else:
                saved = engine.check(target, send=send)
        except Exception as exc:
            self.call_from_thread(self._check_failed, target, engine.mask(engine.user_facing(exc), target))
            return
        if target.type == 'mail':
            self.call_from_thread(self._mail_done, target, text, send)
        else:
            self.call_from_thread(self._check_done, target, saved, engine.last_report, send)

    def _check_failed(self, target: Target, message: str) -> None:
        self.set_window_title(f'[!] ошибка: {target.name}')
        self.headline(f'Ошибка: {message}', ERR)
        self._fill_table(select=self.current or target.name, show=False)

    def _sent_text(self, send: bool) -> str:
        label = {'vk': 'ВКонтакте', 'telegram': 'Telegram'}.get(self.settings.messenger, 'беседу')
        return f'Готово, отправлено: {label}' if send else 'Готово (ничего не отправлялось)'

    def _check_done(self, target: Target, saved: list[str], report, send: bool) -> None:
        self.set_window_title(f'[ok] готово: {target.name}')
        if report is not None:
            self.query_one('#preview', Static).update(
                listing_preview(report.listings, target.style, self._preview_width()))
            self.query_one('#preview-panel').border_title = f'предпросмотр: {target.name}, только что'
        for path in saved:
            self.say(f'картинка: {_short(path)}', DIM)
        self.headline(self._sent_text(send), OK)
        self._fill_table(select=self.current or target.name, show=False)

    def _mail_done(self, target: Target, text: str, send: bool) -> None:
        self.set_window_title(f'[ok] готово: {target.name}')
        self.query_one('#preview', Static).update(_report_text(text))
        self.query_one('#preview-panel').border_title = f'предпросмотр: {target.name}, только что'
        self.headline(self._sent_text(send), OK)
        self._fill_table(select=self.current or target.name, show=False)

    # папка сразу
    def action_ls(self, path: str, send: bool = False) -> None:
        if self._kind() != 'ls':
            self.headline('/ls работает для целей типа ls (папки по ssh)', ERR)
            return
        target, password = self._target_for_run()
        if target is None:
            return
        problem = path_problem(path)
        if problem:
            self.headline(f'Путь {path!r} не подходит: {problem}', ERR)
            return
        extra = {target.password_env: password} if password else {}
        if not self._env_ready(target, extra, send):
            return
        self.headline(f'Смотрю {path} на {target.name}...', ACCENT)
        self.set_window_title(f'[..] ls {path}')
        self._ls_worker(target, path, extra, send)

    @work(thread=True, exclusive=True, group='net')
    def _ls_worker(self, target: Target, path: str, extra: dict[str, str], send: bool) -> None:
        engine = self._make_engine(extra)
        one = replace(target, paths=(path,))
        try:
            report = engine.build_report(one, engine.fetch_listings(one, (path,)))
            if send:
                saved = engine.save_screenshots(one, report.buffers)
                engine.chat.send_photos(saved, report.caption(), engine.peers_for(one)[0])
        except Exception as exc:
            self.call_from_thread(self._check_failed, target, engine.mask(engine.user_facing(exc), target))
            return
        self.call_from_thread(self._ls_done, target, path, report, send)

    def _ls_done(self, target: Target, path: str, report, send: bool) -> None:
        self.set_window_title(f'[ok] ls {path}')
        self.query_one('#preview', Static).update(
            listing_preview(report.listings, target.style, self._preview_width()))
        self.query_one('#preview-panel').border_title = f'предпросмотр: {target.name} {path}'
        self.headline(self._sent_text(send) if send else f'Готово: {path}', OK)

    def action_check_paths(self) -> None:
        target, password = self._target_for_run()
        if target is None:
            return
        if target.type != 'ls':
            self.headline('Проверка папок работает для целей типа ls', ERR)
            return
        extra = {target.password_env: password} if password else {}
        if not self._env_ready(target, extra, False):
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
        if target.type == 'mail':
            saved_report = runlog.read_report(self.settings.state_dir, target.name) if self.settings.state_dir else None
            if saved_report is None:
                panel.border_title = 'предпросмотр'
                preview.update(Text('Отчётов ещё нет: ctrl+r - проверить сейчас.', style=DIM))
            else:
                at, text = saved_report
                panel.border_title = f'предпросмотр: {target.name}, {runlog.age_text(time.time() - at)} назад'
                preview.update(_report_text(text))
            return
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


def _report_text(text: str) -> Text:
    """Текстовый отчёт mail: [ok] зелёным, [!] красным, [?] жёлтым."""
    out = Text()
    for line in text.split('\n'):
        style = OK if line.startswith('[ok]') else ERR if line.startswith('[!]') else \
            WARN if line.startswith('[?]') else ''
        out.append(line + '\n', style=style)
    return out


def _short(path: str | Path) -> str:
    home = str(Path.home())
    text = str(path)
    return '~' + text[len(home):] if text.startswith(home) else text


def main(config_path: str | Path, root: str | Path) -> int:
    HubApp(config_path, root).run()
    return 0

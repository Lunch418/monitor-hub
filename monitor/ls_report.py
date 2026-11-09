"""
ls_report.py - разбор вывода `ls -la` и отрисовка листинга в стиле Midnight Commander.
Используется движком (engine.py) для всех целей.

Различия между стилями отчёта (формат размера, звёздочка у исполняемых, цвета строк,
тексты предупреждений о шрифте) передаются параметрами, а не зашиты в код.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Разбор ls -la

# Вырезаются и OSC-последовательности (\x1b]0;заголовок окна\x07), которые
# многие bastion-хосты и .bashrc выставляют при входе: иначе хвост такой
# последовательности приклеивается к первой строке ls, и первый файл теряется.
_ANSI_RE = re.compile(
    r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)'   # OSC ... BEL | ST
    r'|\x1b\[[0-9;?]*[ -/]*[@-~]'          # CSI
    r'|\x1b[@-Z\\-_]'                      # прочие двухсимвольные ESC
)

# 11-й символ прав: '.' (контекст SELinux) или '+' (ACL). На RHEL/Rocky он
# есть почти у каждой строки, без него листинг разбирался бы в пустоту.
_PERMS = r'([-dlbcps])([rwxsStTl-]{9})[.+]?'

# owner и group собраны в одну нежадную группу: имя группы вида
# 'domain users' (частое в схемах с интеграцией в AD) ломало жёсткое \S+\s+\S+,
# и строка тихо выпадала из отчёта.
_LS_LINE_RE = re.compile(_PERMS + r'\s+\d+\s')

_LS_FULL_RE = re.compile(
    _PERMS
    + r'\s+\d+'                       # число ссылок
    + r'\s+(.+?)'                     # owner [+ group, возможно с пробелами]
    + r'\s+(\d+)'                     # размер
    + r'\s+([A-Za-z]{3})'             # месяц
    + r'\s+(\d{1,2})'                 # день
    + r'\s+(\d{2}:\d{2}|\d{4})'       # время или год
    + r'\s+(.+)$'                     # имя
)

_MONTHS = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}


def resolve_mtime(month: str, day: str, time_or_year: str) -> datetime | None:
    """Дата из ls -la.

    Для свежих файлов ls печатает HH:MM вместо года. Текущий год берётся,
    только если дата не получается в будущем, иначе предыдущий: в январе
    файл от 25 декабря - это прошлый год."""
    mon = _MONTHS.get(month.lower())
    if mon is None:
        return None
    try:
        day_num = int(day)
    except ValueError:
        return None

    if ':' in time_or_year:
        hour, minute = time_or_year.split(':')
        now = datetime.now()
        for year in (now.year, now.year - 1):
            try:
                candidate = datetime(year, mon, day_num, int(hour), int(minute))
            except ValueError:
                continue
            if candidate <= now + timedelta(days=1):
                return candidate
        return None
    try:
        return datetime(int(time_or_year), mon, day_num)
    except ValueError:
        return None


def size_display_listing(size: int, is_dir: bool) -> str:
    """Стиль listing: байты до 10 МБ, дальше K, от 10 ГБ - M. Папки - DIR."""
    if is_dir:
        return 'DIR'
    if size >= 10_000_000_000:
        return f'{size // (1024 * 1024)}M'
    if size >= 10_000_000:
        return f'{size // 1024}K'
    return str(size)


def size_display_backups(size: int, is_dir: bool) -> str:
    """Стиль backups: файлы обычно крупные, K уже с 1 МБ (с округлением). Папки - DIR."""
    if is_dir:
        return 'DIR'
    return size_display_backups_sizes(size, is_dir)


def size_display_backups_sizes(size: int, is_dir: bool) -> str:
    """Стиль backups-sizes: как backups, но у папок настоящий размер в байтах.
    Ступень M нужна для архивов в сотни гигабайт: иначе число не влезает в колонку."""
    if size >= 10_000_000_000:
        return f'{size // (1024 * 1024)}M'
    if size >= 1_000_000:
        return f'{(size + 512) // 1024}K'
    return str(size)


def parse_ls_block(raw_bytes: bytes, log, *, path: str | None = None,
                   mask: Callable[[str], str] = lambda text: text) -> list[str]:
    """Строки вывода ls, похожие на листинг (права, число ссылок).

    path=None - в лог пишется только число строк. path задан -
    режим с путём: при нуле строк в лог идёт сырой вывод (через
    mask, чтобы не светить секреты) - причину 'Permission denied' иначе не найти."""
    try:
        raw = raw_bytes.decode('utf-8')
    except UnicodeDecodeError:
        raw = raw_bytes.decode('windows-1251', errors='replace')
    clean = _ANSI_RE.sub('', raw)
    lines = [ln.rstrip() for ln in clean.replace('\r', '').split('\n')]
    result = [ln for ln in lines if _LS_LINE_RE.match(ln.strip())]
    if path is None:
        log.info('Строк ls: %d', len(result))
        return result

    if not result:
        # Ноль строк - почти всегда это не "каталог пуст", а сообщение ls:
        # 'Permission denied' (каталоги бэкапов бывают drwx------ postgres),
        # 'No such file or directory' и т.п. Без этого лога оставалось лишь
        # "Все пути дали пустой список", по которому причину не найти.
        snippet = mask(' | '.join(ln for ln in lines if ln.strip())[:300])
        log.warning('Путь %s: ни одной строки листинга. Сырой вывод: %s',
                    path or '?', snippet or '<пусто>')
    return result


def parse_ls_line(line: str, size_display: Callable[[int, bool], str],
                  mark_exec: bool = False) -> dict | None:
    """Одна строка ls -la -> словарь для отрисовки (None, если это не листинг).

    mark_exec=True добавляет '*' к имени исполняемого файла прямо в 'name'
    (стиль listing); остальные стили рисуют префикс при отрисовке."""
    m = _LS_FULL_RE.match(line.strip())
    if not m:
        return None

    kind, mode, _owner, size_s, month, day, time_or_year, name = m.groups()
    size = int(size_s)

    is_dir = kind == 'd'
    is_link = kind == 'l'
    # Исполняемым (зелёным, со звёздочкой) считается только обычный файл:
    # у символьной ссылки права всегда lrwxrwxrwx.
    is_exec = kind == '-' and 'x' in mode

    if is_link:
        # ' -> ' обрезается только у символьной ссылки: у обычного файла
        # такое сочетание может быть частью имени.
        name = name.split(' -> ', 1)[0]
    name = name.strip()

    mtime = resolve_mtime(month, day, time_or_year)
    is_recent = ':' in time_or_year
    day_padded = day.rjust(2)
    date_str = (
        f'{month} {day_padded} {time_or_year}' if is_recent
        else f'{month} {day_padded}  {time_or_year}'
    )

    return {
        'name': ('*' + name) if (mark_exec and is_exec) else name,
        'raw_name': name,
        'size': size,
        'size_disp': size_display(size, is_dir),
        'date_str': date_str,
        'is_recent': is_recent,
        'is_exec': is_exec,
        'is_dir': is_dir,
        'is_link': is_link,
        'mtime': mtime,
    }


def stale_entries(entries: Iterable[dict], days: int) -> list[dict]:
    """Файлы старше `days` дней. Пустой список, если проверка выключена."""
    if days <= 0:
        return []
    threshold = datetime.now() - timedelta(days=days)
    return [
        e for e in entries
        if not e['is_dir'] and e['mtime'] is not None and e['mtime'] < threshold
    ]


def split_entries(lines: list[str], parse_line: Callable[[str], dict | None]) -> tuple[list[dict], str]:
    """Строки ls -> (записи без '.' и '..', дата родительского каталога)."""
    entries: list[dict] = []
    parent_date = ''
    for line in lines:
        entry = parse_line(line)
        if entry is None:
            continue
        if entry['raw_name'] == '..':
            parent_date = entry['date_str']
        elif entry['raw_name'] != '.':
            entries.append(entry)
    return entries, parent_date


def sort_dirs_then_files_desc(entries: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Сортировка бэкапов: папки первыми по алфавиту, затем файлы по убыванию
    имени (свежие даты обычно в начале имени). Возвращает (всё, папки, файлы)."""
    dirs = sorted((e for e in entries if e['is_dir']), key=lambda e: e['raw_name'].lower())
    files = sorted((e for e in entries if not e['is_dir']),
                   key=lambda e: e['raw_name'].lower(), reverse=True)
    return dirs + files, dirs, files


# Рендеринг (вид Midnight Commander)

_BG_DARK = (19, 72, 140)
_BG_TOP = (14, 72, 140)
_BG_PARENT = (42, 162, 179)
_PATH_BOX = (208, 205, 208)
_PATH_TEXT = (30, 30, 40)
_PATH_BORDER = (100, 100, 120)
_C_GREEN = (0, 255, 100)
_C_ORANGE = (255, 165, 0)
_C_PAR_TXT = (0, 0, 0)
_C_DIV = (180, 212, 250)
C_GREEN = _C_GREEN

_FS = 14
_ROW_H = 22
_TOP_H = 24
_HDR_H = 22
_PAD = 6
_WIDTH = 940

_CNX = _PAD
_CNW = 718
_CSX = 726
_CSW = 82
_CDX = 810
_CDW = 128

# Корень проекта: monitor/ls_report.py -> на уровень выше.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT_DIRS = (
    os.path.join(_ROOT, 'fonts'),
    '/usr/share/fonts/truetype/dejavu',
    '/usr/share/fonts/truetype/liberation',
    '/usr/share/fonts/dejavu',
    '/usr/share/fonts/liberation',
    '/usr/share/fonts/TTF',
    '/Library/Fonts',
)
_FONT_NAMES = {
    False: ('DejaVuSansMono.ttf', 'LiberationMono-Regular.ttf', 'DejaVuSansMono-Regular.ttf'),
    True: ('DejaVuSansMono-Bold.ttf', 'LiberationMono-Bold.ttf'),
}


class FontProvider:
    """Моноширинный шрифт для отчёта (обычный и жирный) с кэшем.

    bold_fallback=True: если жирный не найден, а обычный уже загружен, жирным
    считается обычный (с одним предупреждением), а не встроенный растровый.
    missing_message/missing_args - предупреждение, если шрифта нет совсем."""

    def __init__(self, log, font_dirs, *, bold_fallback: bool, missing_message: str,
                 missing_args: tuple = ()) -> None:
        self.log = log
        self.font_dirs = font_dirs
        self.bold_fallback = bold_fallback
        self.missing_message = missing_message
        self.missing_args = missing_args
        self.cache: dict = {}
        self.warned = False

    def get(self, bold: bool = False):
        log = self.log
        if bold in self.cache:
            return self.cache[bold]
        for directory in self.font_dirs:
            for filename in _FONT_NAMES[bold]:
                path = os.path.join(directory, filename)
                if os.path.exists(path):
                    try:
                        self.cache[bold] = ImageFont.truetype(path, _FS)
                        return self.cache[bold]
                    except OSError:
                        continue
        # bold не нашли - как fallback берём regular (не встроенный растровый).
        if self.bold_fallback and bold and False in self.cache:
            self.cache[True] = self.cache[False]
            if not self.warned:
                log.warning('Bold-начертание моноширинного шрифта не найдено, использую regular.')
                self.warned = True
            return self.cache[True]
        if not self.warned:
            log.warning(self.missing_message, *self.missing_args)
            self.warned = True
        self.cache[bold] = ImageFont.load_default()
        return self.cache[bold]


def _tw(text: str, font) -> int:
    try:
        return int(font.getlength(text))
    except Exception:
        return len(text) * 8


def _cx(text: str, font, col_x: int, col_w: int) -> int:
    return col_x + max(0, (col_w - _tw(text, font)) // 2)


def _ra(text: str, font, col_x: int, col_w: int) -> int:
    return col_x + max(0, col_w - _tw(text, font) - 3)


def _fit(text: str, font, width: int) -> str:
    """Обрезка имени под ширину колонки бинарным поиском.

    Бинарный поиск по длине: измерять строку после каждого отрезанного
    символа - квадратично по длине имени."""
    if _tw(text, font) <= width:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _tw(text[:mid] + '~', font) <= width:
            lo = mid
        else:
            hi = mid - 1
    return (text[:lo] + '~') if lo > 0 else '~'


def _dividers(draw, y: int, height: int) -> None:
    for x in (_CSX - 2, _CDX - 2):
        draw.line([(x, y), (x, y + height - 1)], fill=_C_DIV, width=2)


# Стиль строки: (запись, устаревшая ли) -> (текст имени, цвет имени и размера, цвет даты)
RowStyle = Callable[[dict, bool], 'tuple[str, tuple, tuple]']


class McRenderer:
    """Рисует листинг в стиле Midnight Commander.

    header_marker - значок в левом углу строки заголовка колонок;
    row_style - как красить и подписывать каждую запись (см. RowStyle)."""

    def __init__(self, fonts: FontProvider, *, header_marker: str, row_style: RowStyle) -> None:
        self.fonts = fonts
        self.header_marker = header_marker
        self.row_style = row_style

    def render_mc(self, path: str, entries: list[dict], parent_date: str = '',
                  stale: set | None = None, page: int = 1, pages: int = 1) -> BytesIO:
        stale = stale or set()
        height = _TOP_H + _HDR_H + _ROW_H + _ROW_H * len(entries) + 4
        img = Image.new('RGB', (_WIDTH, height), _BG_DARK)
        draw = ImageDraw.Draw(img)
        font = self.fonts.get(False)
        font_b = self.fonts.get(True)

        draw.rectangle([0, 0, _WIDTH - 1, _TOP_H - 1], fill=_BG_TOP)
        draw.text((_PAD, 4), '<', fill=_C_DIV, font=font_b)
        path_x = _PAD + _tw('<', font_b) + 4
        path_w = _tw(path, font_b) + 8
        draw.rectangle([path_x - 2, 2, path_x + path_w, _TOP_H - 2],
                       fill=_PATH_BOX, outline=_PATH_BORDER, width=1)
        draw.text((path_x + 2, 4), path, fill=_PATH_TEXT, font=font_b)
        corner = f'{page}/{pages}' if pages > 1 else '.[^]'
        draw.text((_WIDTH - _tw(corner, font) - _PAD, 4), corner, fill=_C_DIV, font=font)
        y = _TOP_H

        draw.rectangle([0, y, _WIDTH - 1, y + _HDR_H - 1], fill=_BG_DARK)
        draw.text((_PAD, y + 3), self.header_marker, fill=_C_ORANGE, font=font)
        draw.text((_cx('Name', font_b, _CNX, _CNW), y + 3), 'Name', fill=_C_ORANGE, font=font_b)
        draw.text((_cx('Size', font_b, _CSX, _CSW), y + 4), 'Size', fill=_C_ORANGE, font=font_b)
        draw.text((_cx('Modify time', font_b, _CDX, _CDW), y + 4), 'Modify time',
                  fill=_C_ORANGE, font=font_b)
        y += _HDR_H

        draw.rectangle([0, y, _WIDTH - 1, y + _ROW_H - 1], fill=_BG_PARENT)
        draw.text((_CNX, y + 3), '/..', fill=_C_PAR_TXT, font=font_b)
        draw.text((_ra('UP--DIR', font, _CSX, _CSW), y + 3), 'UP--DIR', fill=_C_PAR_TXT, font=font)
        if parent_date:
            draw.text((_CDX + 2, y + 3), parent_date, fill=_C_PAR_TXT, font=font)
        _dividers(draw, y, _ROW_H)
        y += _ROW_H

        for entry in entries:
            draw.rectangle([0, y, _WIDTH - 1, y + _ROW_H - 1], fill=_BG_DARK)
            is_stale = entry['raw_name'] in stale
            display_name, name_color, date_color = self.row_style(entry, is_stale)
            draw.text((_CNX, y + 3), _fit(display_name, font, _CNW - 4),
                      fill=name_color, font=font)
            draw.text((_ra(entry['size_disp'], font, _CSX, _CSW), y + 3),
                      entry['size_disp'], fill=name_color, font=font)
            draw.text((_CDX + 2, y + 3), entry['date_str'], fill=date_color, font=font)
            _dividers(draw, y, _ROW_H)
            y += _ROW_H

        draw.rectangle([0, 0, _WIDTH - 1, height - 1], outline=_C_DIV, width=1)

        buf = BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return buf

    def render_pages(self, path: str, entries: list[dict], parent_date: str = '',
                     stale: set | None = None, max_rows: int = 400) -> list[BytesIO]:
        """Один PNG, пока листинг помещается в max_rows строк - то есть в
        подавляющем большинстве случаев поведение не меняется.

        Выше порога картинка резалась бы на несколько тысяч пикселей в высоту
        (при 600 файлах ~1:14, при 1000 ~1:23,5) - ВК такое отклоняет, а
        ретраи отправки три раза натыкались на тот же отказ и слали в беседу
        "попробуйте позже" - сообщение, которое никогда не станет правдой."""
        if not entries:
            return [self.render_mc(path, [], parent_date, stale)]
        chunks = [entries[i:i + max_rows] for i in range(0, len(entries), max_rows)]
        total = len(chunks)
        return [
            self.render_mc(path, chunk, parent_date, stale, page=i, pages=total)
            for i, chunk in enumerate(chunks, start=1)
        ]


# Сохранение

def save_screenshots(buffers: list[BytesIO], *, save_dir: str, prefix: str,
                     keep: int, log) -> list[str]:
    os.makedirs(save_dir, exist_ok=True)
    stamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    paths: list[str] = []
    for i, buf in enumerate(buffers, start=1):
        suffix = '' if len(buffers) == 1 else f'_p{i:02d}'
        path = os.path.join(save_dir, f'{prefix}_{stamp}{suffix}.png')
        with open(path, 'wb') as fh:
            fh.write(buf.read())
        buf.seek(0)
        paths.append(path)
    home = os.path.expanduser('~')
    shown = '~' + save_dir[len(home):] if save_dir.startswith(home + os.sep) else save_dir
    log.info('Скриншотов сохранено: %d (%s)', len(paths), shown)

    files = sorted(Path(save_dir).glob(f'{prefix}_*.png'))
    for old in files[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass
    return paths


def open_screenshot(path: str) -> None:
    """Открывает картинку в просмотрщике (check --open, ctrl+o в интерфейсе).
    NO_OPEN_SCREENSHOT=1 отключает: нужно тестам и серверам без экрана."""
    if os.environ.get('NO_OPEN_SCREENSHOT') == '1':
        return
    import subprocess
    for viewer in ('eog', 'xviewer', 'eom', 'feh', 'xdg-open'):
        try:
            subprocess.Popen(
                [viewer, path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return
        except (FileNotFoundError, OSError):
            continue

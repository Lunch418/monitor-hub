"""ls_report: разбор ls -la, размеры, шрифты, отрисовка и сохранение скриншотов."""
import logging
from pathlib import Path

import pytest

from monitor import ls_report as lr

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize('size,expected', [
    (0, '0'), (999_999, '999999'), (1_000_000, '977K'), (1_000_511, '977K'),
    (9_999_999, '9766K'), (10_000_000, '9766K'), (10**10 - 1, '9765625K'), (10**10, '9536M'),
])
def test_size_display_backups_thresholds(size, expected):
    # из этих двух функций только обработка каталогов различается
    assert lr.size_display_backups(size, False) == expected
    assert lr.size_display_backups_sizes(size, False) == expected


def test_size_display_listing_uses_ten_megabyte_threshold():
    assert lr.size_display_listing(9_999_999, False) == '9999999'
    assert lr.size_display_listing(10_000_000, False) == '9765K'
    assert lr.size_display_listing(10**10, False) == '9536M'


def test_directories_differ_between_monitors():
    assert lr.size_display_listing(4096, True) == 'DIR'
    assert lr.size_display_backups(4096, True) == 'DIR'
    assert lr.size_display_backups_sizes(4096, True) == '4096'     # папки реальными байтами


def test_parse_mark_exec_flag():
    line = '-rwxr-xr-x 1 root root 100 Oct 18 09:00 run.sh'
    assert lr.parse_ls_line(line, lr.size_display_listing, mark_exec=True)['name'] == '*run.sh'
    assert lr.parse_ls_line(line, lr.size_display_listing)['name'] == 'run.sh'


def test_parse_handles_selinux_dot_and_group_with_spaces():
    entry = lr.parse_ls_line('drwx------. 2 postgres domain users 16384 Mar  3 12:00 pgdata',
                             lr.size_display_backups_sizes)
    assert entry['is_dir'] and entry['raw_name'] == 'pgdata' and entry['size'] == 16384


def test_symlink_target_is_cut_only_for_real_links():
    link = lr.parse_ls_line('lrwxrwxrwx 1 root root 8 Oct 18 09:00 cur -> target', lr.size_display_listing)
    plain = lr.parse_ls_line('-rw-r--r-- 1 root root 8 Oct 18 09:00 a -> b', lr.size_display_listing)
    assert link['raw_name'] == 'cur' and plain['raw_name'] == 'a -> b'


def test_resolve_mtime_never_in_future():
    from datetime import datetime, timedelta
    now = datetime.now()
    future_day = now + timedelta(days=40)
    value = lr.resolve_mtime(future_day.strftime('%b'), str(future_day.day), '12:00')
    assert value is not None and value <= now + timedelta(days=1)
    assert lr.resolve_mtime('Foo', '1', '12:00') is None
    assert lr.resolve_mtime('Feb', '30', '2020') is None


def test_parse_ls_block_crl_mode_logs_count(caplog):
    log = logging.getLogger('t_ls1')
    raw = b'\x1b]0;t\x07-rw-r--r-- 1 a b 1 Jan 1 2020 x\r\nprompt$ '
    with caplog.at_level(logging.INFO, logger='t_ls1'):
        assert len(lr.parse_ls_block(raw, log)) == 1
    assert 'Строк ls: 1' in caplog.text


def test_parse_ls_block_path_mode_warns_with_masked_output(caplog):
    log = logging.getLogger('t_ls2')
    with caplog.at_level(logging.WARNING, logger='t_ls2'):
        result = lr.parse_ls_block(b'ls: Permission denied secret', log, path='/b',
                                   mask=lambda text: text.replace('secret', '***'))
    assert result == []
    assert 'Permission denied ***' in caplog.text and 'secret' not in caplog.text


def test_parse_ls_block_decodes_cp1251():
    raw = 'drwxr-xr-x 2 root root 4096 Oct 18 09:00 папка\n'.encode('cp1251')
    (line,) = lr.parse_ls_block(raw, logging.getLogger('t_ls3'))
    assert 'папка' in line


def test_stale_entries():
    from datetime import datetime, timedelta
    old = {'is_dir': False, 'mtime': datetime.now() - timedelta(days=10), 'raw_name': 'old'}
    new = {'is_dir': False, 'mtime': datetime.now(), 'raw_name': 'new'}
    folder = {'is_dir': True, 'mtime': datetime.now() - timedelta(days=99), 'raw_name': 'dir'}
    assert lr.stale_entries([old, new, folder], 0) == []
    assert lr.stale_entries([old, new, folder], 5) == [old]


def make_fonts(dirs, log, bold_fallback=True):
    return lr.FontProvider(log, dirs, bold_fallback=bold_fallback,
                           missing_message='нет шрифта в %s', missing_args=('X',))


def test_font_found_in_dirs(caplog):
    fonts = make_fonts((str(ROOT / 'fonts'),), logging.getLogger('t_f1'))
    regular, bold = fonts.get(False), fonts.get(True)
    assert isinstance(regular, lr.ImageFont.FreeTypeFont) and bold is not regular
    assert fonts.get(False) is regular                      # кэш


def test_font_missing_warns_once(caplog):
    fonts = make_fonts((), logging.getLogger('t_f2'))
    with caplog.at_level(logging.WARNING, logger='t_f2'):
        fonts.get(False)
        fonts.get(True)
    assert caplog.text.count('нет шрифта в X') == 1


@pytest.mark.parametrize('fallback,same', [(True, True), (False, False)])
def test_bold_fallback_to_regular(tmp_path, fallback, same):
    import shutil
    shutil.copy(ROOT / 'fonts' / 'DejaVuSansMono.ttf', tmp_path)   # жирного нет
    fonts = make_fonts((str(tmp_path),), logging.getLogger('t_f3'), bold_fallback=fallback)
    regular = fonts.get(False)
    assert (fonts.get(True) is regular) is same


def row_style(entry, is_stale):
    return entry['name'], (255, 255, 255), (200, 200, 200)


def make_renderer(marker='.n'):
    fonts = make_fonts((str(ROOT / 'fonts'),), logging.getLogger('t_r'))
    return lr.McRenderer(fonts, header_marker=marker, row_style=row_style)


def entries(count):
    line = '-rw-r--r-- 1 root root 2048 Oct 18 09:00 test.crl'
    return [lr.parse_ls_line(line, lr.size_display_listing)] * count


def test_render_is_deterministic_png():
    first = make_renderer().render_mc('/p', entries(3)).getvalue()
    second = make_renderer().render_mc('/p', entries(3)).getvalue()
    assert first == second and first.startswith(b'\x89PNG')


def test_render_depends_on_marker_and_row_count():
    base = make_renderer('.n').render_mc('/p', entries(3)).getvalue()
    assert make_renderer("'n").render_mc('/p', entries(3)).getvalue() != base
    assert make_renderer('.n').render_mc('/p', entries(4)).getvalue() != base


def test_render_pages_splits_and_handles_empty():
    renderer = make_renderer()
    assert len(renderer.render_pages('/p', entries(25), max_rows=10)) == 3
    assert len(renderer.render_pages('/p', [])) == 1


def test_save_screenshots_names_and_rotation(tmp_path):
    log = logging.getLogger('t_s')
    buffers = [make_renderer().render_mc('/p', entries(1)) for _ in range(3)]
    paths = lr.save_screenshots(buffers, save_dir=str(tmp_path), prefix='crl', keep=2, log=log)
    assert [Path(p).name[-8:] for p in paths] == ['_p01.png', '_p02.png', '_p03.png']
    assert all(Path(p).name.startswith('crl_') for p in paths)
    left = sorted(p.name for p in tmp_path.iterdir())
    assert len(left) == 2 and left == sorted(Path(p).name for p in paths[1:])   # старейший удалён


def test_open_screenshot_can_be_disabled(monkeypatch):
    import subprocess
    started = []
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: started.append(a))
    monkeypatch.setenv('NO_OPEN_SCREENSHOT', '1')
    lr.open_screenshot('/x.png')
    assert started == []
    monkeypatch.delenv('NO_OPEN_SCREENSHOT')
    lr.open_screenshot('/x.png')
    assert started and started[0][0][0] == 'eog'

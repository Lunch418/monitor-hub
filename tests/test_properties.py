"""Свойства на случайных данных (hypothesis): разбор ls, .env, argv ssh, маскирование."""
import logging
import subprocess

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from monitor import app
from monitor import common as mc
from monitor import ls_report as lr
from monitor import pam_session as pam

FAST = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])

MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
names = st.text(alphabet=st.characters(blacklist_categories=('Cc', 'Cs'), blacklist_characters='\n\r'),
                min_size=1, max_size=60).filter(lambda s: s.strip() and s == s.strip() and ' -> ' not in s)


@FAST
@given(st.text(max_size=300))
def test_parse_ls_line_never_crashes(text):
    result = lr.parse_ls_line(text, lr.size_display_backups)
    assert result is None or isinstance(result['size'], int)


@FAST
@given(kind=st.sampled_from(['-', 'd', 'l']), mode=st.from_regex(r'[r-][w-][x-][r-][w-][x-][r-][w-][x-]', fullmatch=True),
       size=st.integers(0, 10**13), month=st.sampled_from(MONTHS), day=st.integers(1, 28),
       when=st.one_of(st.from_regex(r'([01]\d|2[0-3]):[0-5]\d', fullmatch=True), st.integers(1990, 2030).map(str)),
       name=names)
def test_parse_ls_line_roundtrip(kind, mode, size, month, day, when, name):
    line = f'{kind}{mode} 1 owner group {size} {month} {day:>2} {when} {name}'
    entry = lr.parse_ls_line(line, lr.size_display_backups, mark_exec=True)
    assert entry is not None
    assert entry['raw_name'] == name and entry['size'] == size
    assert entry['is_dir'] == (kind == 'd') and entry['is_link'] == (kind == 'l')
    assert entry['is_exec'] == (kind == '-' and 'x' in mode)
    assert entry['is_recent'] == (':' in when)
    assert entry['name'] == ('*' + name if entry['is_exec'] else name)
    assert entry['mtime'] is None or entry['mtime'].month == MONTHS.index(month) + 1


@FAST
@given(st.integers(0, 10**14), st.booleans())
def test_size_display_is_always_short_enough_for_the_column(size, is_dir):
    for fn in (lr.size_display_listing, lr.size_display_backups, lr.size_display_backups_sizes):
        assert len(fn(size, is_dir)) <= 12


@FAST
@given(st.text(max_size=120), st.integers(5, 400))
def test_fit_never_exceeds_width_and_keeps_short_text(text, width):
    from PIL import ImageFont
    font = ImageFont.load_default()
    fitted = lr._fit(text, font, width)
    if lr._tw(text, font) <= width:
        assert fitted == text
    else:
        assert fitted.endswith('~') and lr._tw(fitted, font) <= max(width, lr._tw('~', font))


@FAST
@given(st.text(max_size=100))
def test_unquote_inverts_quoting(value):
    assert app.unquote(f'"{value}"') == value
    assert app.unquote(f"'{value}'") == value


@FAST
@given(st.text(max_size=100))
def test_unquote_is_stable_without_quotes(value):
    stripped = value.strip()
    if not (len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in '"\''):
        assert app.unquote(value) == stripped


@FAST
@given(st.text(max_size=200), st.lists(st.text(min_size=4, max_size=12).filter(lambda s: '*' not in s), max_size=3))
def test_mask_secrets_removes_every_secret(text, secrets):
    masked = app.mask_secrets(text, secrets)
    for secret in secrets:
        assert secret not in masked or any(secret in other and secret != other for other in secrets)


@FAST
@given(st.text(max_size=40), st.text(max_size=40))
def test_ssh_argv_keeps_hostile_values_inside_single_arguments(user, host):
    cfg = pam.PamConfig(host=host, port=22, user=user, password='p', target_host='t', target_sel='1',
                        strict_host_key='no', known_hosts='/dev/null', kex='k', hostkey_algs='h',
                        strict_algs=False, prompt_timeout=60, prompt_nudge=0, connect_retries=1,
                        connect_retry_delay=0)
    argv = pam.ssh_argv(cfg)
    assert argv[-1] == f'{user}@{host}' and argv.count('-o') == 10
    assert all(isinstance(item, str) for item in argv)


@FAST
@given(st.text(alphabet=st.characters(blacklist_categories=('Cs',), blacklist_characters='\x00'), max_size=20),
       st.integers(-5, 5))
def test_env_int_always_returns_int(raw, default):
    import os
    os.environ['HYP_T'] = raw
    try:
        assert isinstance(mc._env_int('HYP_T', default), int)
    finally:
        del os.environ['HYP_T']


@given(st.integers(2, 6))
def test_resolve_mtime_never_in_the_future_for_recent_files(month_offset):
    from datetime import datetime, timedelta
    when = datetime.now() + timedelta(days=30 * month_offset)
    value = lr.resolve_mtime(when.strftime('%b'), str(when.day), '12:00')
    assert value is None or value <= datetime.now() + timedelta(days=1)


def test_resolve_mtime_invalid_input():
    assert lr.resolve_mtime('Jan', 'xx', '12:00') is None
    assert lr.resolve_mtime('Feb', '30', '12:00') is None
    assert lr.resolve_mtime('Feb', '30', '2020') is None
    assert lr.resolve_mtime('Nop', '1', '12:00') is None


def test_open_screenshot_tries_viewers_until_one_exists(monkeypatch):
    monkeypatch.delenv('NO_OPEN_SCREENSHOT', raising=False)
    tried = []

    def popen(argv, **kwargs):
        tried.append(argv[0])
        if argv[0] != 'feh':
            raise FileNotFoundError(argv[0])

    monkeypatch.setattr(subprocess, 'Popen', popen)
    lr.open_screenshot('/x.png')
    assert tried == ['eog', 'xviewer', 'eom', 'feh']
    tried.clear()
    monkeypatch.setattr(subprocess, 'Popen', lambda argv, **kw: (_ for _ in ()).throw(OSError('no display')))
    lr.open_screenshot('/x.png')                                   # без единого просмотрщика - тихо


def test_render_with_parent_date_and_long_name_is_valid_png():
    import io

    from PIL import Image
    fonts = lr.FontProvider(logging.getLogger('t_prop'), lr.FONT_DIRS, bold_fallback=True,
                            missing_message='x', missing_args=())
    renderer = lr.McRenderer(fonts, header_marker='.n',
                             row_style=lambda e, stale: (e['name'], (255, 255, 255), (200, 200, 200)))
    entry = lr.parse_ls_line('-rw-r--r-- 1 root root 5 Oct 18 09:00 ' + 'x' * 400, lr.size_display_listing)
    png = renderer.render_mc('/p', [entry], parent_date='Sep  5  2025').getvalue()
    image = Image.open(io.BytesIO(png))
    assert image.size[0] == 940 and image.format == 'PNG'

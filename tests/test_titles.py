import io

from monitor import titles


class Tty(io.StringIO):
    def isatty(self):
        return True


def test_format_and_clean():
    assert titles.format_title() == 'monitor-hub'
    assert titles.format_title('[ok] готово: web') == '[ok] готово: web - monitor-hub'
    assert titles.clean('a\x1b]0;evil\x07b') == 'a]0;evilb'          # управляющие символы вырезаны
    assert len(titles.clean('x' * 200)) == 80
    assert titles.osc('t') == '\x1b]0;t\x07'


def test_nothing_written_to_a_pipe(monkeypatch):
    monkeypatch.setenv('TERM', 'xterm')
    out = io.StringIO()
    titles.set_title('x', out)
    with titles.tab('a', 'b', 'c', out) as done:
        done(True)
    assert out.getvalue() == ''


def test_dumb_terminal_is_silent(monkeypatch):
    monkeypatch.setenv('TERM', 'dumb')
    out = Tty()
    titles.set_title('x', out)
    assert out.getvalue() == ''


def test_tab_success_and_failure(monkeypatch):
    monkeypatch.setenv('TERM', 'xterm')
    out = Tty()
    with titles.tab('проверяю: web', 'готово: web', 'ошибка: web', out) as done:
        assert '[..] проверяю: web - monitor-hub' in out.getvalue()
        done(True)
    assert out.getvalue().endswith('[ok] готово: web - monitor-hub\x07')
    assert out.getvalue().startswith(titles.SAVE)
    bad = Tty()
    with titles.tab('x', 'ok', 'сбой', bad) as done:
        done(False)
    assert '[!] сбой - monitor-hub' in bad.getvalue()


def test_tab_restores_when_nothing_reported(monkeypatch):
    monkeypatch.setenv('TERM', 'xterm')
    out = Tty()
    try:
        with titles.tab('x', 'ok', 'bad', out):
            raise KeyboardInterrupt
    except KeyboardInterrupt:
        pass
    assert out.getvalue().endswith(titles.RESTORE)

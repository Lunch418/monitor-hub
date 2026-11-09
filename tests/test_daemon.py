"""monitor.daemon: PID-файл, остановка, статус, планировщик и VK long poll."""
import logging
import os
import signal
import subprocess
import sys
import time
import types

import pytest

from monitor import daemon as md


# --- Daemon: PID-файл и процессы ---
@pytest.fixture
def make_daemon(tmp_path):
    def _make(script_name='unique_test_marker'):
        return md.Daemon(
            log=logging.getLogger('t_daemon'), script_name=script_name,
            pid_file=str(tmp_path / 'x.pid'), log_file=str(tmp_path / 'x.log'))
    return _make


@pytest.fixture
def sleeper():
    """Живой процесс, в командной строке которого есть маркер unique_test_marker."""
    proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', 'unique_test_marker'])
    yield proc
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def test_read_pid_ignores_missing_and_garbage(make_daemon, tmp_path):
    d = make_daemon()
    assert d.read_pid() == (None, None)
    (tmp_path / 'x.pid').write_text('garbage')
    assert d.read_pid() == (None, None)
    (tmp_path / 'x.pid').write_text('123\n')
    assert d.read_pid() == (123, str(tmp_path / 'x.pid'))


def test_alive_checks_that_pid_belongs_to_the_script(make_daemon, sleeper):
    assert make_daemon('unique_test_marker').alive(sleeper.pid) is True
    assert make_daemon('some_other_script').alive(sleeper.pid) is False   # PID чужого процесса
    assert make_daemon().alive(None) is False and make_daemon().alive(0) is False
    sleeper.kill()
    sleeper.wait()
    assert make_daemon('unique_test_marker').alive(sleeper.pid) is False


def test_write_pid_file_is_private_and_replaces_symlink(make_daemon, tmp_path):
    d = make_daemon()
    victim = tmp_path / 'victim'
    victim.write_text('precious')
    os.symlink(victim, tmp_path / 'x.pid')                      # подложенная ссылка
    d.write_pid_file()
    assert victim.read_text() == 'precious'                      # цель не затёрта
    pid_path = tmp_path / 'x.pid'
    assert not pid_path.is_symlink() and pid_path.read_text() == str(os.getpid())
    assert pid_path.stat().st_mode & 0o777 == 0o600
    d.remove_pid_file()
    d.remove_pid_file()                                          # повторное удаление не падает
    assert not pid_path.exists()


def test_stop_terminates_process_and_removes_pid_file(make_daemon, sleeper, tmp_path, capsys):
    d = make_daemon('unique_test_marker')
    (tmp_path / 'x.pid').write_text(str(sleeper.pid))
    d.stop()
    assert sleeper.wait(timeout=10) == -signal.SIGTERM
    assert 'остановлен' in capsys.readouterr().out and not (tmp_path / 'x.pid').exists()


def test_stop_handles_missing_and_stale_pid_file(make_daemon, tmp_path, capsys):
    d = make_daemon()
    d.stop()
    assert 'Демон не запущен' in capsys.readouterr().out
    (tmp_path / 'x.pid').write_text('99999999')
    d.stop()
    assert 'устарел' in capsys.readouterr().out and not (tmp_path / 'x.pid').exists()


def test_tail_and_log_path(make_daemon, tmp_path):
    d = make_daemon()
    assert d.log_path_for_reading() is None and d.tail(str(tmp_path / 'none'), 5) == []
    (tmp_path / 'x.log').write_text(''.join(f'line{i}\n' for i in range(30)))
    assert d.log_path_for_reading() == str(tmp_path / 'x.log')
    assert d.tail(str(tmp_path / 'x.log'), 3) == ['line27\n', 'line28\n', 'line29\n']


def test_status_not_running_and_running(make_daemon, sleeper, tmp_path, capsys):
    d = make_daemon('unique_test_marker')
    (tmp_path / 'x.log').write_text('some log line\n')
    d.status(running_lines=lambda: ['   Цель: t'], validate_config=lambda: ['PAM_HOST не задан'])
    out = capsys.readouterr().out
    assert 'Не запущен' in out and 'Проблемы конфигурации' in out and 'some log line' in out and 'Цель' not in out
    (tmp_path / 'x.pid').write_text(str(sleeper.pid))
    d.status(running_lines=lambda: ['   Цель: t'], validate_config=lambda: [])
    out = capsys.readouterr().out
    assert f'Запущен (PID {sleeper.pid})' in out and '   Цель: t' in out and 'PID-файл' in out


def test_logs_command(make_daemon, tmp_path, capsys):
    d = make_daemon()
    d.logs()
    assert 'Лог пуст' in capsys.readouterr().out
    (tmp_path / 'x.log').write_text('a\nb\nc\n')
    d.logs(2)
    out = capsys.readouterr().out
    assert 'последние 2 строк' in out and out.rstrip().endswith('b\nc')


def test_signal_handler_raises_system_exit(make_daemon):
    old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        make_daemon().install_signal_handlers()
        with pytest.raises(SystemExit) as info:
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        assert info.value.code == 0
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)


def test_foreground_refuses_when_preflight_fails_or_already_running(make_daemon, sleeper, tmp_path, capsys):
    d = make_daemon('unique_test_marker')
    with pytest.raises(SystemExit) as info:
        d.foreground(preflight=lambda: False, scheduler_loop=lambda: None, message='m')
    assert info.value.code == 2
    (tmp_path / 'x.pid').write_text(str(sleeper.pid))
    with pytest.raises(SystemExit) as info:
        d.foreground(preflight=lambda: True, scheduler_loop=lambda: None, message='m')
    assert info.value.code == 2 and 'Уже запущен в фоне' in capsys.readouterr().err


def test_foreground_runs_loop_and_cleans_up(make_daemon, tmp_path, caplog):
    d = make_daemon()
    seen = {}

    def loop():
        seen['pid_file'] = (tmp_path / 'x.pid').read_text()
        raise SystemExit(0)

    with caplog.at_level(logging.INFO, logger='t_daemon'):
        d.foreground(preflight=lambda: True, scheduler_loop=loop, message='Foreground. Отправка')
    assert seen['pid_file'] == str(os.getpid())
    assert not (tmp_path / 'x.pid').exists() and 'Foreground. Отправка' in caplog.text


# --- poll_messages ---
class FatalError(Exception):
    code = 5


class FakeEvent:
    def __init__(self, text='/get', peer_id=10, from_id=5, kind='new'):
        self.type = 'NEW' if kind == 'new' else 'OTHER'
        self.obj = types.SimpleNamespace(message={'peer_id': peer_id, 'from_id': from_id, 'text': text})


@pytest.fixture
def longpoll_env(monkeypatch):
    """Подменяет vk_api.bot_longpoll: каждый listen() отдаёт следующий сценарий."""
    state = types.SimpleNamespace(scenarios=[], created=0, group_ids=[])
    module = types.ModuleType('vk_api.bot_longpoll')

    class VkBotEventType:
        MESSAGE_NEW = 'NEW'

    class VkBotLongPoll:
        """Сценарий - список событий для listen() либо исключение прямо при создании."""

        def __init__(self, session, group_id):
            state.created += 1
            state.group_ids.append(group_id)
            self.scenario = state.scenarios.pop(0)
            if isinstance(self.scenario, Exception):
                raise self.scenario

        def listen(self):
            for item in self.scenario:
                if isinstance(item, Exception):
                    raise item
                yield item

    module.VkBotLongPoll, module.VkBotEventType = VkBotLongPoll, VkBotEventType
    parent = types.ModuleType('vk_api')
    parent.bot_longpoll = module
    monkeypatch.setitem(sys.modules, 'vk_api', parent)
    monkeypatch.setitem(sys.modules, 'vk_api.bot_longpoll', module)
    return state


class FakeVk:
    def __init__(self, fail=None):
        self.cache, self.fail = {'session': 'SESSION'}, fail

    def connect(self):
        if self.fail:
            raise self.fail


def run_poll(vk=None, enabled=True, group_id=2):
    """poll_messages с поддельным ВК; возвращает (вызовы startup, полученные сообщения)."""
    started, received = [], []
    md.poll_messages(
        log=logging.getLogger('t_longpoll'), vk=vk or FakeVk(), group_id=group_id,
        on_message=lambda peer, from_id, text: received.append((peer, from_id, text)), label='команд',
        is_fatal_vk_error=lambda exc: isinstance(exc, FatalError),
        startup=lambda: started.append('startup'), enabled=enabled, disabled_message='long poll off')
    return started, received


@pytest.fixture
def poll_sleeps(monkeypatch):
    sleeps = []
    monkeypatch.setattr(time, 'sleep', lambda s: sleeps.append(s))
    return sleeps


def test_longpoll_disabled_and_no_group(caplog):
    with caplog.at_level(logging.INFO, logger='t_longpoll'):
        run_poll(enabled=False)
        run_poll(group_id=0)
    assert 'long poll off' in caplog.text and 'VK_GROUP_ID не задан' in caplog.text


def test_longpoll_connect_failure_is_logged_not_raised(caplog):
    with caplog.at_level(logging.ERROR, logger='t_longpoll'):
        started, _ = run_poll(vk=FakeVk(fail=RuntimeError('no token')))
    assert 'VK long poll не запущен' in caplog.text and started == []


def test_longpoll_passes_new_messages_only(longpoll_env, poll_sleeps):
    longpoll_env.scenarios = [[FakeEvent('/get', peer_id=10), FakeEvent('hello', peer_id=11, from_id=7),
                               FakeEvent('/get', kind='other'), FatalError('stop')]]
    started, received = run_poll()
    assert started == ['startup']
    assert received == [(10, '5', '/get'), (11, '7', 'hello')]   # разбор команд - дело Router
    assert longpoll_env.group_ids == [2]


def test_longpoll_backoff_grows_while_connecting_fails_then_stops_on_fatal(longpoll_env, poll_sleeps, caplog):
    longpoll_env.scenarios = [OSError('net'), OSError('net'), OSError('net'), [FatalError('bad token')]]
    with caplog.at_level(logging.WARNING, logger='t_longpoll'):
        run_poll()
    assert poll_sleeps == [10, 20, 40] and longpoll_env.created == 4
    assert 'неустранимая ошибка (code=5)' in caplog.text


def test_longpoll_backoff_is_capped(longpoll_env, poll_sleeps):
    longpoll_env.scenarios = [OSError('x') for _ in range(8)] + [[FatalError('x')]]
    run_poll()
    assert poll_sleeps == [10, 20, 40, 80, 160, 300, 300, 300]


def test_longpoll_reconnects_quickly_when_an_established_connection_drops(longpoll_env, poll_sleeps):
    # соединение было установлено (объект создан) - пауза остаётся минимальной
    longpoll_env.scenarios = [[OSError('drop')], [OSError('drop')], [OSError('drop')], [FatalError('x')]]
    run_poll()
    assert poll_sleeps == [10, 10, 10]

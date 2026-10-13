import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.supervisor import supervise


def test_optional_watchdog_checks_before_launch_and_on_timeout(tmp_path):
    checks, launches = [], []

    class Child:
        pid = 456

        def __init__(self):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired('backend', timeout)
            return 0

    def launch(*args, **kwargs):
        launches.append((args, kwargs))
        return Child()

    assert supervise(['fake-backend'], tmp_path, launch=launch, max_runs=1,
                     watchdog_check=lambda: checks.append('checked')) == 1
    assert len(launches) == 1
    assert launches[0][1]['creationflags'] == getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    assert launches[0][1]['stderr'] is subprocess.STDOUT
    assert launches[0][1]['env']['PYTHONIOENCODING'] == 'utf-8'
    assert checks == ['checked', 'checked']


def test_watchdog_check_error_is_fixed_code_and_does_not_restart_backend(tmp_path):
    launches, terminated = [], []

    class Child:
        pid = 789

        def __init__(self):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired('backend', timeout)
            return 0

    def launch(*args, **kwargs):
        launches.append(args)
        return Child()

    def bad_check():
        raise RuntimeError('private token must not enter logs')

    assert supervise(['fake-backend'], tmp_path, launch=launch, max_runs=1,
                     watchdog_check=bad_check, terminate=terminated.append) == 1
    assert len(launches) == 1 and terminated == []
    logged = (tmp_path / 'startup.jsonl').read_text(encoding='utf-8')
    events = [json.loads(line) for line in logged.splitlines()]
    assert [event['event'] for event in events] == ['deepseek_watchdog_check_failed'] * 2
    assert 'private token' not in logged


def _watchdog_fixture(tmp_path):
    script = tmp_path / '.runtime' / 'startup' / 'deepseek_watchdog.py'
    script.parent.mkdir(parents=True)
    script.write_text('pass\n', encoding='utf-8')
    script.with_name('deepseek-watchdog.json').write_text(
        json.dumps({'pid': 123, 'started': 101.0}), encoding='utf-8')
    return script


def test_owned_watchdog_is_left_running(tmp_path):
    from app.local_proxy_watchdog import ensure_deepseek_watchdog

    script = _watchdog_fixture(tmp_path)
    process = SimpleNamespace(create_time=lambda: 100.0,
                              cmdline=lambda: [sys.executable, str(script)],
                              name=lambda: 'python.exe')

    def spawn(*args, **kwargs):
        raise AssertionError('owned watchdog must not be restarted')

    assert ensure_deepseek_watchdog(script, process_factory=lambda pid: process,
                                    spawn=spawn) == 'running'


def test_dead_watchdog_is_started_hidden_without_starting_go(tmp_path):
    import psutil
    from app.local_proxy_watchdog import ensure_deepseek_watchdog

    script = _watchdog_fixture(tmp_path)
    launched = []

    def missing(pid):
        raise psutil.NoSuchProcess(pid)

    def spawn(args, **kwargs):
        launched.append((args, kwargs))

    assert ensure_deepseek_watchdog(script, process_factory=missing, spawn=spawn,
                                    python_executable='python.exe') == 'started'
    assert len(launched) == 1
    assert launched[0][0] == ['python.exe', str(script.resolve())]
    assert launched[0][1]['creationflags'] == getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    assert 'doubao' not in ' '.join(launched[0][0]).lower()


@pytest.mark.parametrize(('name', 'birth', 'argv_kind'), [
    ('node.exe', 100.0, 'expected'),
    ('python.exe', 102.0, 'expected'),
    ('python.exe', 100.0, 'different'),
])
def test_foreign_or_reused_pid_cannot_impersonate_watchdog(tmp_path, name, birth, argv_kind):
    from app.local_proxy_watchdog import ensure_deepseek_watchdog

    script = _watchdog_fixture(tmp_path)
    argv_script = script if argv_kind == 'expected' else script.with_name('other.py')
    process = SimpleNamespace(create_time=lambda: birth,
                              cmdline=lambda: [sys.executable, str(argv_script)],
                              name=lambda: name)
    launches = []
    assert ensure_deepseek_watchdog(script, process_factory=lambda pid: process,
                                    spawn=lambda *args, **kwargs: launches.append(args)) == 'started'
    assert len(launches) == 1


def test_crash_restarts_with_backoff_and_normal_stop_stays_stopped(tmp_path):
    codes = iter([3, 7, 0])
    calls, delays = [], []
    class Child:
        pid = 123
        def wait(self):
            return next(codes)
    def launch(command, **kwargs):
        calls.append(command)
        return Child()
    assert supervise(['fake-backend'], tmp_path, launch=launch, wait=delays.append) == 3
    assert len(calls) == 3
    assert delays == [10, 20]


def test_explicit_supervisor_stop_prevents_launch(tmp_path):
    (tmp_path / 'STOP_SUPERVISOR').touch()
    def launch(*args, **kwargs):
        raise AssertionError('must stay stopped')
    assert supervise(['fake-backend'], tmp_path, launch=launch) == 0


def test_real_child_crash_is_restarted_without_touching_wechat(tmp_path):
    import sys
    script = tmp_path / 'isolated_child.py'
    marker = tmp_path / 'starts.txt'
    script.write_text("from pathlib import Path\nimport sys\np=Path(sys.argv[1])\nn=int(p.read_text())+1 if p.exists() else 1\np.write_text(str(n))\nsys.exit(3 if n==1 else 0)\n")
    assert supervise([sys.executable, str(script), str(marker)], tmp_path,
                     wait=lambda seconds: None, max_runs=3) == 2
    assert marker.read_text() == '2'
    events = [json.loads(line) for line in (tmp_path / 'restarts.jsonl').read_text().splitlines()]
    assert [event['exit_code'] for event in events] == [3, 0]


def test_stale_owned_child_is_terminated_and_recovered(monkeypatch, tmp_path):
    import subprocess
    import app.supervisor as module
    times = iter([0, 601, 602, 603])
    monkeypatch.setattr(module.time, 'monotonic', lambda: next(times))
    children = []
    class Child:
        pid = 456
        terminated = False
        def wait(self, timeout=None):
            if len(children) == 1 and not self.terminated:
                raise subprocess.TimeoutExpired('owned-child', timeout)
            return 0
        def terminate(self):
            self.terminated = True
    def launch(*args, **kwargs):
        child = Child()
        children.append(child)
        return child
    assert supervise(['fake'], tmp_path, launch=launch, wait=lambda _: None,
                     healthy=lambda pid: False, max_runs=2) == 2
    assert children[0].terminated


def test_windows_launcher_runtime_is_recognized_as_owned(monkeypatch):
    from types import SimpleNamespace
    import psutil
    from app.supervisor import owns_process
    monkeypatch.setattr(psutil, 'Process', lambda pid: SimpleNamespace(parents=lambda: [SimpleNamespace(pid=12)]))
    assert owns_process(12, 34)
    assert not owns_process(99, 34)


@pytest.mark.parametrize(('reason', 'expected_code'), [
    ('locked', 0),
    ('legacy_owner_active', 0),
    ('lock_io_error', 2),
])
def test_rejected_supervisor_lock_leaves_startup_diagnostic(
        monkeypatch, tmp_path, capsys, reason, expected_code):
    from types import SimpleNamespace
    import app.config
    import app.instance_lock
    import app.supervisor

    class RejectedLock:
        failure_reason = reason
        diagnostic = 'lock diagnostic'

        def __init__(self, path):
            assert path == tmp_path / 'supervisor.lock'

        def acquire(self):
            return False

    monkeypatch.setattr(app.config, 'load_config',
                        lambda path: SimpleNamespace(resolve=lambda ignored: tmp_path))
    monkeypatch.setattr(app.instance_lock, 'SingleInstanceLock', RejectedLock)
    monkeypatch.setattr(app.supervisor.sys, 'argv', ['supervisor', '--config', 'test.yaml'])

    assert app.supervisor.main() == expected_code
    event = json.loads((tmp_path / 'startup.jsonl').read_text(encoding='utf-8'))
    assert event['event'] == 'supervisor_lock_rejected'
    assert event['reason'] == reason
    assert event['diagnostic'] == 'lock diagnostic'
    assert event['pid'] > 0
    assert 'lock diagnostic' in capsys.readouterr().err

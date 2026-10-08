from app.supervisor import supervise


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
    import json
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

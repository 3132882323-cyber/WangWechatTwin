"""Locks only in pytest's temporary directories; no backend or sender starts."""
import json
import os
from pathlib import Path
import subprocess
import sys

import psutil
import pytest

from app import instance_lock as module
from app.instance_lock import SingleInstanceLock


def _owned_metadata(lock):
    return json.loads(lock.path.read_text(encoding="ascii"))


def _child(code, path, **kwargs):
    return subprocess.run(
        [sys.executable, "-c", code, str(path)], cwd=module._PROJECT_ROOT,
        capture_output=True, text=True, timeout=15, **kwargs,
    )


def test_failed_contender_release_does_not_remove_owner(tmp_path):
    path = tmp_path / "RUNNING.lock"
    owner, loser, third = (SingleInstanceLock(path) for _ in range(3))
    assert owner.acquire()
    try:
        original = _owned_metadata(owner)
        assert not loser.acquire()
        assert loser.failure_reason == "locked"
        assert loser.fd is None
        loser.release()
        loser.release()
        assert path.exists()
        assert _owned_metadata(owner) == original
        assert not third.acquire()
    finally:
        owner.release()
    assert third.acquire()
    third.release()


def test_metadata_contains_birth_time_but_kernel_lock_is_authoritative(tmp_path):
    path = tmp_path / "RUNNING.lock"
    first = SingleInstanceLock(path)
    assert first.acquire()
    record = _owned_metadata(first)
    assert record["pid"] == os.getpid()
    assert record["create_time"] == psutil.Process().create_time()
    assert record["lock_protocol"] == module._PROTOCOL
    assert record["role"] == "backend"
    assert record["kernel_file"] == first.kernel_path.name
    assert not os.get_inheritable(first.fd)
    first.release()
    assert path.exists()
    assert first.kernel_path.exists()
    second = SingleInstanceLock(path)
    # The recorded PID is still alive, but there is no OS lock after release.
    assert second.acquire()
    assert second.failure_reason == second.diagnostic == ""
    second.release()


def test_repeated_acquire_keeps_same_descriptor_and_release_is_idempotent(tmp_path):
    lock = SingleInstanceLock(tmp_path / "RUNNING.lock")
    assert lock.acquire()
    fd = lock.fd
    assert lock.acquire() and lock.fd == fd
    lock.release()
    lock.release()
    assert lock.fd is None and lock.path.exists()


def test_real_second_process_is_excluded_then_can_acquire(tmp_path):
    path = tmp_path / "RUNNING.lock"
    owner = SingleInstanceLock(path)
    code = "from pathlib import Path; import sys; from app.instance_lock import SingleInstanceLock; lock=SingleInstanceLock(Path(sys.argv[1])); ok=lock.acquire(); lock.release(); sys.exit(0 if ok else 7)"
    assert owner.acquire()
    try:
        blocked = _child(code, path)
        assert blocked.returncode == 7, blocked.stderr
        assert owner.fd is not None and path.exists()
    finally:
        owner.release()
    free = _child(code, path)
    assert free.returncode == 0, free.stderr


def test_os_releases_lock_after_holder_exits_without_release(tmp_path):
    path = tmp_path / "RUNNING.lock"
    crashed = _child("from pathlib import Path; import os,sys; from app.instance_lock import SingleInstanceLock; lock=SingleInstanceLock(Path(sys.argv[1])); assert lock.acquire(); os._exit(17)", path)
    assert crashed.returncode == 17, crashed.stderr
    assert path.exists()
    recovered = SingleInstanceLock(path)
    assert recovered.acquire()
    recovered.release()


@pytest.mark.parametrize("stage,exit_code", [
    ("after_create", 21),
    ("before_metadata", 22),
    ("during_metadata", 23),
])
def test_crash_before_complete_metadata_never_blocks_recovery(tmp_path, stage, exit_code):
    path = tmp_path / "RUNNING.lock"
    code = """
from pathlib import Path
import os, sys
from app import instance_lock as module
lock = module.SingleInstanceLock(Path(sys.argv[1]))
stage = sys.argv[2]
if stage == 'after_create':
    original_open = module.os.open
    def crash_open(path, *args, **kwargs):
        fd = original_open(path, *args, **kwargs)
        if Path(path) == lock.kernel_path:
            os._exit(21)
        return fd
    module.os.open = crash_open
elif stage == 'before_metadata':
    module._publish_metadata = lambda *args: os._exit(22)
else:
    original_write = module.os.write
    def crash_write(fd, data):
        original_write(fd, data[:max(1, len(data) // 2)])
        os._exit(23)
    module.os.write = crash_write
assert lock.acquire()
raise AssertionError('crash stage was not exercised')
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(path), stage], cwd=module._PROJECT_ROOT,
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == exit_code, result.stderr
    recovered = SingleInstanceLock(path)
    assert recovered.kernel_path.exists()
    assert recovered.kernel_path.stat().st_size == 0
    assert not path.exists()
    assert recovered.acquire(), recovered.diagnostic
    recovered.release()


def test_crash_during_metadata_refresh_preserves_previous_complete_json(tmp_path):
    path = tmp_path / "RUNNING.lock"
    previous = SingleInstanceLock(path)
    assert previous.acquire()
    previous.release()
    before = path.read_bytes()
    result = _child("""
from pathlib import Path
import os, sys
from app import instance_lock as module
original_write = module.os.write
def crash_write(fd, data):
    original_write(fd, data[:len(data) // 2])
    os._exit(24)
module.os.write = crash_write
lock = module.SingleInstanceLock(Path(sys.argv[1]))
lock.acquire()
""", path)
    assert result.returncode == 24, result.stderr
    assert path.read_bytes() == before
    recovered = SingleInstanceLock(path)
    assert recovered.acquire(), recovered.diagnostic
    recovered.release()


def test_unheld_kernel_file_contents_are_never_authoritative(tmp_path):
    lock = SingleInstanceLock(tmp_path / "RUNNING.lock")
    lock.kernel_path.write_bytes(b"12345 / incomplete metadata / arbitrary stale bytes")
    assert lock.acquire(), lock.diagnostic
    lock.release()


def test_first_start_process_race_has_one_owner(tmp_path):
    path = tmp_path / "RUNNING.lock"
    code = """
from pathlib import Path
import sys
from app.instance_lock import SingleInstanceLock
print('ready', flush=True)
sys.stdin.readline()
lock = SingleInstanceLock(Path(sys.argv[1]))
ok = lock.acquire()
print('1' if ok else '0', flush=True)
sys.stdin.readline()
lock.release()
"""
    children = [subprocess.Popen(
        [sys.executable, "-c", code, str(path)], cwd=module._PROJECT_ROOT,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    ) for _ in range(4)]
    try:
        for child in children:
            assert child.stdout.readline().strip() == "ready"
        for child in children:
            child.stdin.write("start\n")
            child.stdin.flush()
        results = [child.stdout.readline().strip() for child in children]
        assert results.count("1") == 1, results
        contender = SingleInstanceLock(path)
        assert not contender.acquire()
        contender.release()
    finally:
        for child in children:
            _, error = child.communicate("release\n", timeout=15)
            assert child.returncode == 0, error
    recovered = SingleInstanceLock(path)
    assert recovered.acquire()
    recovered.release()


class _Process:
    def __init__(self, command=None, cwd=None, created=100.0, executable=None):
        self.command = command or [sys.executable, "-u", "-m", "app", "--config", "config.yaml", "run"]
        self.directory = cwd or str(module._PROJECT_ROOT)
        self.created = created
        self.executable = executable or sys.executable

    def create_time(self):
        return self.created

    def cmdline(self):
        return self.command

    def cwd(self):
        return self.directory

    def exe(self):
        return self.executable

    def status(self):
        return psutil.STATUS_RUNNING


def _fake_pid(monkeypatch, process, pid=12345):
    real_process = psutil.Process
    monkeypatch.setattr(module.psutil, "Process", lambda value=None: process if value == pid else real_process(value))
    return pid


@pytest.mark.parametrize("name,command", [
    ("RUNNING.lock", [sys.executable, "-u", "-m", "app", "--config", "config.yaml", "run"]),
    ("supervisor.lock", [sys.executable, "-m", "app.supervisor", "--config", "config.yaml"]),
    ("RUNNING.lock", [sys.executable, "-m", "app", "--config=config.yaml", "run"]),
    ("supervisor.lock", [sys.executable, str(module._PROJECT_ROOT / "app" / "supervisor.py")]),
])
def test_active_legacy_owner_is_never_overwritten_or_released(tmp_path, monkeypatch, name, command):
    path = tmp_path / name
    pid = _fake_pid(monkeypatch, _Process(command))
    path.write_text(str(pid), encoding="ascii")
    contender = SingleInstanceLock(path)
    assert not contender.acquire()
    assert contender.failure_reason == "legacy_owner_active"
    assert str(pid) in contender.diagnostic
    contender.release()
    assert path.read_text(encoding="ascii") == str(pid)
    assert contender.fd is None


@pytest.mark.parametrize("process", [
    _Process([sys.executable, "-c", "print('-m app run')"]),
    _Process([sys.executable, "other.py", "-m", "app", "run"]),
    _Process([sys.executable, "-m", "app", "doctor"]),
    _Process(executable="other-program.exe"),
    _Process([sys.executable, "-m", "app.supervisor"]),
])
def test_recycled_pid_for_unrelated_process_does_not_block(tmp_path, monkeypatch, process):
    path = tmp_path / "RUNNING.lock"
    pid = _fake_pid(monkeypatch, process)
    path.write_text(str(pid), encoding="ascii")
    recovered = SingleInstanceLock(path)
    assert recovered.acquire()
    assert _owned_metadata(recovered)["pid"] == os.getpid()
    recovered.release()


def test_dead_legacy_pid_is_migrated(tmp_path, monkeypatch):
    real_process = psutil.Process
    def inspect(pid=None):
        if pid == 12345:
            raise psutil.NoSuchProcess(pid)
        return real_process(pid)
    monkeypatch.setattr(module.psutil, "Process", inspect)
    lock = SingleInstanceLock(tmp_path / "RUNNING.lock")
    lock.path.write_text("12345", encoding="ascii")
    assert lock.acquire()
    lock.release()


@pytest.mark.parametrize("invalid_pid", ["0", "-1"])
def test_nonpositive_legacy_pid_is_stale(tmp_path, invalid_pid):
    lock = SingleInstanceLock(tmp_path / "RUNNING.lock")
    lock.path.write_text(invalid_pid, encoding="ascii")
    assert lock.acquire()
    lock.release()


@pytest.mark.skipif(not hasattr(sys, "set_int_max_str_digits"), reason="requires configurable integer digit limit")
def test_large_legacy_pid_respects_python_digit_limit_without_escaping_diagnostic(tmp_path):
    lock = SingleInstanceLock(tmp_path / "RUNNING.lock")
    content = "9" * 641
    lock.path.write_text(content, encoding="ascii")
    previous = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(640)
        assert not lock.acquire()
        assert lock.failure_reason == "lock_metadata_unknown"
        assert "ValueError" in lock.diagnostic
        assert lock.fd is None
    finally:
        sys.set_int_max_str_digits(previous)
        lock.release()
    assert lock.path.read_text(encoding="ascii") == content


@pytest.mark.parametrize("error_type", [OverflowError, ValueError])
def test_legacy_process_query_range_exception_is_diagnosed(tmp_path, monkeypatch, error_type):
    real_process = psutil.Process
    def inspect(pid=None):
        if pid == 12345:
            raise error_type("test process PID conversion failure")
        return real_process(pid)
    monkeypatch.setattr(module.psutil, "Process", inspect)
    lock = SingleInstanceLock(tmp_path / "RUNNING.lock")
    lock.path.write_text("12345", encoding="ascii")
    assert not lock.acquire()
    assert lock.failure_reason == "lock_metadata_unknown"
    assert error_type.__name__ in lock.diagnostic
    assert lock.fd is None
    lock.release()
    assert lock.path.read_text(encoding="ascii") == "12345"


def test_uninspectable_legacy_pid_fails_closed_with_diagnostic(tmp_path, monkeypatch):
    class Denied(_Process):
        def cmdline(self):
            raise psutil.AccessDenied(12345)
    pid = _fake_pid(monkeypatch, Denied())
    lock = SingleInstanceLock(tmp_path / "RUNNING.lock")
    lock.path.write_text(str(pid), encoding="ascii")
    assert not lock.acquire()
    assert lock.failure_reason == "legacy_owner_unknown"
    assert "AccessDenied" in lock.diagnostic
    lock.release()
    assert lock.path.read_text(encoding="ascii") == str(pid)


def test_pid_changes_during_legacy_inspection_fail_closed(tmp_path, monkeypatch):
    real_process = psutil.Process
    snapshots = iter([_Process(), _Process(created=200.0)])
    monkeypatch.setattr(module.psutil, "Process", lambda pid=None: next(snapshots) if pid == 12345 else real_process(pid))
    lock = SingleInstanceLock(tmp_path / "RUNNING.lock")
    lock.path.write_text("12345", encoding="ascii")
    assert not lock.acquire()
    assert lock.failure_reason == "legacy_owner_unknown"
    lock.release()
    assert lock.path.read_text(encoding="ascii") == "12345"


def test_matching_module_from_unknown_project_cannot_be_assumed_stale(tmp_path, monkeypatch):
    pid = _fake_pid(monkeypatch, _Process(cwd=str(tmp_path)))
    lock = SingleInstanceLock(tmp_path / "RUNNING.lock")
    lock.path.write_text(str(pid), encoding="ascii")
    assert not lock.acquire()
    assert lock.failure_reason == "legacy_owner_unknown"


@pytest.mark.parametrize("content", ["", "garbled", "--12345", '{"pid": 12345}', "x" * 4097])
def test_unknown_legacy_contents_are_preserved(tmp_path, content):
    lock = SingleInstanceLock(tmp_path / "RUNNING.lock")
    lock.path.write_text(content, encoding="ascii")
    assert not lock.acquire()
    assert lock.failure_reason == "lock_metadata_unknown"
    lock.release()
    assert lock.path.read_text(encoding="ascii") == content


def test_open_permission_failure_returns_diagnostic_and_release_is_harmless(tmp_path, monkeypatch):
    path = tmp_path / "RUNNING.lock"
    path.write_text("12345", encoding="ascii")
    def denied(*args, **kwargs):
        raise PermissionError("test access denied")
    monkeypatch.setattr(module.os, "open", denied)
    contender = SingleInstanceLock(path)
    assert not contender.acquire()
    assert contender.failure_reason == "lock_io_error"
    assert "PermissionError" in contender.diagnostic
    contender.release()
    assert path.read_text(encoding="ascii") == "12345"


def test_failed_metadata_write_closes_own_descriptor(tmp_path, monkeypatch):
    original_write = module.os.write
    def fail_write(fd, data):
        raise OSError("test write failure")
    lock = SingleInstanceLock(tmp_path / "RUNNING.lock")
    monkeypatch.setattr(module.os, "write", fail_write)
    assert not lock.acquire()
    assert lock.fd is None and lock.failure_reason == "lock_io_error"
    assert not lock.path.exists() and lock.kernel_path.exists()
    # No kernel lock leaked, and a failed write did not publish partial JSON.
    monkeypatch.setattr(module.os, "write", original_write)
    assert lock.acquire()
    lock.release()


def test_unlock_error_still_closes_owned_fd_without_deleting_path(tmp_path, monkeypatch):
    lock = SingleInstanceLock(tmp_path / "RUNNING.lock")
    assert lock.acquire()
    original_unlock = module._unlock
    def fail_unlock(fd):
        raise OSError("test unlock failure")
    monkeypatch.setattr(module, "_unlock", fail_unlock)
    lock.release()
    assert lock.fd is None and lock.path.exists()
    assert lock.failure_reason == "lock_release_error"
    monkeypatch.setattr(module, "_unlock", original_unlock)
    recovered = SingleInstanceLock(lock.path)
    assert recovered.acquire()
    recovered.release()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_forked_child_release_cannot_unlock_parent(tmp_path):
    lock = SingleInstanceLock(tmp_path / "RUNNING.lock")
    assert lock.acquire()
    pid = os.fork()
    if pid == 0:
        lock.release()
        os._exit(0)
    try:
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        contender = SingleInstanceLock(lock.path)
        assert not contender.acquire()
    finally:
        lock.release()

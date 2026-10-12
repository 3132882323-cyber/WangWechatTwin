"""GLM monitor contract using fake processes, fake health and fake launches."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import psutil
import pytest

from app.local_proxy_watchdog import ensure_glm_watchdog, process_record_matches

spec = importlib.util.spec_from_file_location("glm_watchdog_test_module", Path(__file__).resolve().parents[1] / "scripts" / "glm_watchdog.py")
watchdog = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = watchdog
spec.loader.exec_module(watchdog)


class Process:
    def __init__(self, command, birth=100):
        self.command, self.birth, self.dead = command, birth, False
        self.terminated = self.killed = 0

    def create_time(self):
        if self.dead:
            raise psutil.NoSuchProcess(123)
        return self.birth

    def cmdline(self): return self.command
    def exe(self): return sys.executable
    def terminate(self): self.terminated += 1; self.dead = True
    def kill(self): self.killed += 1; self.dead = True
    def wait(self, timeout): return 0


def fixture(tmp_path):
    runtime = tmp_path / ".runtime" / "glm_proxy"
    (runtime / "auth").mkdir(parents=True)
    (runtime / "auth" / "refresh.json").write_text("mock, never read", encoding="utf-8")
    script = tmp_path / "experiments" / "glm_web_trial" / "gateway.py"
    script.parent.mkdir(parents=True)
    script.write_text("# mock source", encoding="utf-8")
    args = ["--runtime-dir", str(runtime), "--mode", "production"]
    process = Process([sys.executable, str(script), *args])
    record = {"pid": 123, "create_time": 100}
    (runtime / "gateway-process.json").write_text(json.dumps(record), encoding="utf-8")
    return runtime, script, process, record


def listeners(process, *, ip="127.0.0.1"):
    return [] if process.dead else [SimpleNamespace(status=psutil.CONN_LISTEN,
                                                   pid=123, laddr=SimpleNamespace(ip=ip, port=19659))]


def idle_health(record, overdue=False):
    return {"runtime_ok": True, "mode": "production", "instance": record,
            "ok": False, "inference": {"verified": False}, "maintenance": {"overdue_pending": overdue}}


def test_idle_inference_does_not_restart_owned_service(tmp_path):
    runtime, script, process, record = fixture(tmp_path)
    launched = []
    monitor = watchdog.Monitor(tmp_path, runtime, process_factory=lambda pid: process,
                               connections=lambda: listeners(process), spawn=lambda *a, **k: launched.append(a),
                               health_reader=lambda root: idle_health(record), now=lambda: 1000)
    assert [monitor.check_once() for _ in range(5)] == ["running"] * 5
    assert not launched and process.terminated == 0


def test_overdue_request_only_restarts_verified_service_without_replay(tmp_path):
    runtime, script, process, record = fixture(tmp_path)
    launched, clock = [], [1000]
    monitor = watchdog.Monitor(tmp_path, runtime, process_factory=lambda pid: process,
                               connections=lambda: listeners(process), spawn=lambda *a, **k: launched.append((a, k)),
                               health_reader=lambda root: idle_health(record, True), now=lambda: clock[0])
    assert [monitor.check_once() for _ in range(3)] == ["unhealthy_observed", "unhealthy_observed", "stopped_unresponsive"]
    assert process.terminated == 1 and process.killed == 0
    assert monitor.check_once() == "restart_backoff"
    clock[0] += 10
    assert monitor.check_once() == "started"
    assert launched[0][0][0] == [sys.executable, str(script), "--runtime-dir", str(runtime), "--mode", "production"]
    assert launched[0][1]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert "request_id" not in str(launched)
    assert monitor.check_once() == "starting"


@pytest.mark.parametrize("changed", ["birth", "command", "external_listener"])
def test_reused_pid_or_foreign_listener_is_never_terminated_or_replaced(tmp_path, changed):
    runtime, script, process, record = fixture(tmp_path)
    if changed == "birth": process.birth = 101
    if changed == "command": process.command[1] = str(script.with_name("other.py"))
    launched = []
    monitor = watchdog.Monitor(tmp_path, runtime, process_factory=lambda pid: process,
                               connections=lambda: listeners(process, ip="0.0.0.0" if changed == "external_listener" else "127.0.0.1"),
                               spawn=lambda *a, **k: launched.append(a), now=lambda: 1000)
    assert monitor.check_once() == "listener_conflict"
    assert not launched and process.terminated == 0


def test_uninspectable_process_never_authorizes_second_launch(tmp_path):
    runtime, _, _, _ = fixture(tmp_path)
    def denied(pid): raise psutil.AccessDenied(pid)
    launched = []
    monitor = watchdog.Monitor(tmp_path, runtime, process_factory=denied,
                               connections=lambda: [], spawn=lambda *a, **k: launched.append(a))
    with pytest.raises(psutil.AccessDenied): monitor.check_once()
    assert not launched


def test_windows_venv_base_python_child_requires_exact_birth_and_argv(tmp_path, monkeypatch):
    runtime, script, process, record = fixture(tmp_path)
    launcher = tmp_path / ".venv" / "Scripts" / "python.exe"
    base_python = tmp_path / "Python" / "python.exe"
    monkeypatch.setattr(sys, "_base_executable", str(base_python))
    process.command[0] = str(launcher)
    process.exe = lambda: str(base_python)
    arguments = ["--runtime-dir", str(runtime), "--mode", "production"]
    matches = lambda: process_record_matches(record, script, arguments,
        process_factory=lambda pid: process, python_executable=str(launcher))
    assert matches()
    process.birth = 100.000001
    assert not matches()
    process.birth = 100
    process.command[-1] = "trial"
    assert not matches()
    process.command[-1] = "production"
    process.command.append("--unexpected")
    assert not matches()
    process.command.pop()
    process.exe = lambda: str(tmp_path / "other" / "python.exe")
    assert not matches()


def test_process_identity_must_remain_equal_during_inspection(tmp_path):
    runtime, script, process, record = fixture(tmp_path)
    replacement = Process(process.command, birth=101)
    observed = iter([process, replacement])
    assert not process_record_matches(record, script,
        ["--runtime-dir", str(runtime), "--mode", "production"],
        process_factory=lambda pid: next(observed))


def watcher_fixture(tmp_path, seen=1000):
    runtime, _, _, _ = fixture(tmp_path)
    script = tmp_path / "scripts" / "glm_watchdog.py"
    script.parent.mkdir()
    script.write_text("# fake", encoding="utf-8")
    record = {"pid": 123, "create_time": 100, "seen_at": seen}
    (runtime / "watchdog-process.json").write_text(json.dumps(record), encoding="utf-8")
    process = Process([sys.executable, str(script), "--runtime-dir", str(runtime)])
    return runtime, script, process


def test_supervisor_recovers_hung_watchdog_with_birth_and_exact_argv_checks(tmp_path):
    runtime, script, process = watcher_fixture(tmp_path, seen=100)
    launched = []
    result = ensure_glm_watchdog(script, runtime, process_factory=lambda pid: process,
                                 spawn=lambda *a, **k: launched.append((a, k)), now=lambda: 1000)
    assert result == "restarted" and process.terminated == 1
    assert launched[0][0][0] == [sys.executable, str(script), "--runtime-dir", str(runtime)]
    assert launched[0][1]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


@pytest.mark.parametrize("seen", [None, "invalid", float("nan"), 2000])
def test_invalid_heartbeat_cannot_hide_a_hung_owned_watchdog(tmp_path, seen):
    runtime, script, process = watcher_fixture(tmp_path, seen=seen)
    launched = []
    result = ensure_glm_watchdog(script, runtime, process_factory=lambda pid: process,
                                 spawn=lambda *a, **k: launched.append(a), now=lambda: 1000)
    assert result == "restarted" and process.terminated == 1 and len(launched) == 1


def test_explicit_glm_stop_prevents_watchdog_start(tmp_path):
    runtime, script, process = watcher_fixture(tmp_path)
    startup = tmp_path / ".runtime" / "startup"
    startup.mkdir()
    (startup / "STOP_GLM_PROXY").touch()
    assert ensure_glm_watchdog(script, runtime, spawn=lambda *a, **k: pytest.fail("must stay stopped")) == "stopped"


def test_supervisor_glm_hook_does_not_start_disabled_deepseek_or_doubao(monkeypatch, tmp_path):
    import app.local_proxy_watchdog as hooks
    from app.supervisor import provider_watchdog_checks
    runtime, script, process = watcher_fixture(tmp_path)
    startup = tmp_path / ".runtime" / "startup"
    startup.mkdir()
    (startup / "deepseek_watchdog.py").write_text("pass", encoding="utf-8")
    config = SimpleNamespace(openai=SimpleNamespace(provider="hybrid_web", disabled_lanes=["deepseek"],
        local_proxies={name: SimpleNamespace(enabled=True) for name in ("deepseek", "glm", "doubao")}))
    calls = []
    monkeypatch.setattr(hooks, "ensure_glm_watchdog", lambda source, state: calls.append((source, state)))
    checks = provider_watchdog_checks(config, startup, platform_name="nt")
    assert set(checks) == {"glm"}
    checks["glm"]()
    assert calls == [(script, runtime)]


def test_one_watchdog_failure_does_not_skip_glm_or_restart_backend(monkeypatch, tmp_path):
    from app.supervisor import supervise
    import app.resource_health
    monkeypatch.setattr(app.resource_health, "under_pressure", lambda value: False)
    calls = []
    class Child:
        pid = 77
        def wait(self, timeout=None): return 0
    def fail(): raise RuntimeError("private token must never be logged")
    assert supervise(["fake"], tmp_path, launch=lambda *a, **k: Child(),
                     watchdog_checks={"deepseek": fail, "glm": lambda: calls.append("glm")}) == 1
    assert calls == ["glm"]
    logged = (tmp_path / "startup.jsonl").read_text(encoding="utf-8")
    assert "deepseek_watchdog_check_failed" in logged and "private token" not in logged

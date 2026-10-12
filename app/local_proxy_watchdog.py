"""Check and start only explicitly enabled project Python watchdogs on Windows.

The watchdog's own OS lock prevents duplicate owners. Its diagnostic PID is
accepted only when the process birth time and exact script argv still match.
This module never starts the DeepSeek server directly or touches Doubao.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import sys


def _owned_watchdog(script: Path, *, process_factory) -> bool:
    state_path = script.with_name("deepseek-watchdog.json")
    try:
        with state_path.open("rb") as state_file:
            raw = state_file.read(4097)
    except FileNotFoundError:
        return False
    if len(raw) > 4096:
        raise ValueError("watchdog state exceeds size limit")
    try:
        state = json.loads(raw)
        pid, started = state["pid"], state["started"]
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return False
        if isinstance(started, bool) or not isinstance(started, (int, float)) or not math.isfinite(started):
            return False
    except (KeyError, TypeError, ValueError):
        return False

    import psutil
    try:
        process = process_factory(pid)
        born = process.create_time()
        argv = process.cmdline()
        name = process.name().lower()
    except (psutil.NoSuchProcess, psutil.ZombieProcess, ValueError, OverflowError):
        return False
    # AccessDenied remains an error: uncertainty must not authorize a second
    # launch based solely on a stale PID file.
    if not isinstance(born, (int, float)) or not math.isfinite(born):
        return False
    if not 0 <= started - born <= 120 or name not in {"python.exe", "pythonw.exe"}:
        return False
    if not isinstance(argv, (list, tuple)) or len(argv) < 2 or not isinstance(argv[1], str):
        return False
    return os.path.normcase(str(Path(argv[1]).resolve())).casefold() == os.path.normcase(str(script)).casefold()


def ensure_deepseek_watchdog(script: Path, *, process_factory=None, spawn=None,
                             python_executable: str | None = None) -> str:
    """Return running/started; never infer ownership from PID liveness alone."""
    import psutil
    script = Path(script).resolve(strict=True)
    if (script.name != "deepseek_watchdog.py" or script.parent.name != "startup"
            or script.parent.parent.name != ".runtime"):
        raise ValueError("watchdog script must stay in this project's runtime startup directory")
    process_factory = process_factory or psutil.Process
    spawn = spawn or subprocess.Popen
    if _owned_watchdog(script, process_factory=process_factory):
        return "running"
    python_executable = python_executable or sys.executable
    spawn([python_executable, str(script)], cwd=str(script.parents[2]),
          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), close_fds=True)
    return "started"


def read_process_record(path: Path) -> dict | None:
    """A bounded record contains diagnostic identity, never authority by itself."""
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(8193)
    except FileNotFoundError:
        return None
    if len(raw) > 8192:
        raise ValueError("process_record_too_large")
    try:
        value = json.loads(raw)
        if (not isinstance(value, dict) or type(value.get("pid")) is not int or value["pid"] <= 0
                or type(value.get("create_time")) not in (int, float)
                or not math.isfinite(value["create_time"])):
            return None
        return value
    except (ValueError, TypeError):
        return None


def process_record_matches(record: dict | None, script: Path, arguments: list[str], *, process_factory=None,
                           python_executable: str | None = None) -> bool:
    """Require birth time, Python executable, script and every argument to match."""
    if record is None:
        return False
    import psutil
    process_factory = process_factory or psutil.Process
    try:
        process = process_factory(record["pid"])
        created = process.create_time()
        command = process.cmdline()
        executable = Path(process.exe()).resolve()
        allowed = {Path(python_executable or sys.executable).resolve(),
                   Path(getattr(sys, "_base_executable", sys.executable)).resolve()}
        if (type(created) not in (int, float) or not math.isfinite(created)
                or created != record["create_time"] or executable not in allowed
                or not isinstance(command, (list, tuple)) or len(command) != 2 + len(arguments)
                or not isinstance(command[1], str) or Path(command[1]).resolve() != Path(script).resolve()
                or list(command[2:]) != arguments):
            return False
        return process_factory(record["pid"]).create_time() == created
    except (psutil.NoSuchProcess, psutil.ZombieProcess, KeyError, TypeError, ValueError, OverflowError):
        return False
    # AccessDenied is deliberately propagated: it is not permission to launch
    # a duplicate or terminate a process whose identity cannot be verified.


def terminate_recorded_process(record, script, arguments, *, process_factory=None, python_executable=None):
    """Stop a previously identified process; recheck identity before escalation."""
    import psutil
    process_factory = process_factory or psutil.Process
    if not process_record_matches(record, script, arguments, process_factory=process_factory,
                                  python_executable=python_executable):
        return False
    process = process_factory(record["pid"])
    process.terminate()
    try:
        process.wait(timeout=10)
    except psutil.TimeoutExpired:
        if not process_record_matches(record, script, arguments, process_factory=process_factory,
                                      python_executable=python_executable):
            return False
        process_factory(record["pid"]).kill()
        process_factory(record["pid"]).wait(timeout=5)
    return True


def ensure_glm_watchdog(script: Path, runtime_dir: Path, *, process_factory=None, spawn=None,
                        python_executable: str | None = None, now=None) -> str:
    """Recover only this project's GLM monitor; never touch another listener."""
    import time
    script = Path(script).resolve(strict=True)
    project = script.parent.parent
    runtime_dir = Path(runtime_dir).resolve()
    if script.name != "glm_watchdog.py" or script.parent.name != "scripts" or runtime_dir != project / ".runtime" / "glm_proxy":
        raise ValueError("invalid_glm_watchdog_paths")
    if (project / ".runtime" / "startup" / "STOP_GLM_PROXY").exists():
        return "stopped"
    if not (runtime_dir / "auth" / "refresh.json").is_file():
        return "missing_auth"
    now = now or time.time
    python_executable = python_executable or sys.executable
    arguments = ["--runtime-dir", str(runtime_dir)]
    record = read_process_record(runtime_dir / "watchdog-process.json")
    owned = process_record_matches(record, script, arguments, process_factory=process_factory,
                                   python_executable=python_executable)
    if owned:
        seen = record.get("seen_at")
        current = now()
        # An absent/corrupt heartbeat must not keep a hung owner healthy
        # forever. Its verified creation time still grants a startup window.
        if type(seen) not in (int, float) or not math.isfinite(seen) or seen > current + 5:
            seen = record["create_time"]
        if current - seen <= 120:
            return "running"
        if not terminate_recorded_process(record, script, arguments, process_factory=process_factory,
                                          python_executable=python_executable):
            raise ValueError("watchdog_identity_changed")
    (spawn or subprocess.Popen)([python_executable, str(script), *arguments], cwd=str(project),
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), close_fds=True)
    return "restarted" if owned else "started"

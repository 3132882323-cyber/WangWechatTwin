"""Check and start only this project's DeepSeek watchdog on Windows.

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

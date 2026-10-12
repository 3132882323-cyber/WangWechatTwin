"""Own only the local GLM Python gateway. No generation, replay or WeChat I/O."""
from __future__ import annotations

import argparse
import http.client
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

import psutil

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))
from app.instance_lock import SingleInstanceLock
from app.local_proxy_watchdog import read_process_record, process_record_matches, terminate_recorded_process

PORT = 19659


def publish(path: Path, value: dict):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def local_health(runtime_dir: Path):
    """A bounded local health read; never creates an upstream model request."""
    path = runtime_dir / "local-key.txt"
    if path.is_symlink():
        raise ValueError("invalid_key_path")
    key = path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", key):
        raise ValueError("invalid_local_key")
    connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=3)
    try:
        connection.request("GET", "/health", headers={"Authorization": "Bearer " + key, "Connection": "close"})
        response = connection.getresponse()
        if response.status != 200:
            return None
        body = response.read(65537)
        if len(body) > 65536:
            return None
        value = json.loads(body)
        return value if isinstance(value, dict) else None
    finally:
        connection.close()


class Monitor:
    def __init__(self, project: Path, runtime_dir: Path, *, process_factory=psutil.Process,
                 connections=lambda: psutil.net_connections(kind="tcp"), spawn=subprocess.Popen,
                 health_reader=local_health, now=time.time, python_executable=None):
        self.project, self.runtime = Path(project).resolve(), Path(runtime_dir).resolve()
        if self.runtime != self.project / ".runtime" / "glm_proxy":
            raise ValueError("invalid_runtime_directory")
        self.script = self.project / "experiments" / "glm_web_trial" / "gateway.py"
        self.arguments = ["--runtime-dir", str(self.runtime), "--mode", "production"]
        self.process_factory, self.connections, self.spawn = process_factory, connections, spawn
        self.health_reader, self.now = health_reader, now
        self.python = python_executable or sys.executable
        self.failures, self.next_start, self.backoff = 0, 0.0, 5.0
        self.healthy_since = None
        self.starting_until = 0.0

    def _matches(self, record):
        return process_record_matches(record, self.script, self.arguments,
                                      process_factory=self.process_factory, python_executable=self.python)

    def _terminate(self, record):
        return terminate_recorded_process(record, self.script, self.arguments,
                                          process_factory=self.process_factory, python_executable=self.python)

    def stop_owned(self):
        record = read_process_record(self.runtime / "gateway-process.json")
        return self._terminate(record) if self._matches(record) else False

    def check_once(self) -> str:
        record = read_process_record(self.runtime / "gateway-process.json")
        owned = self._matches(record)
        listeners = [row for row in self.connections() if row.status == psutil.CONN_LISTEN and row.laddr.port == PORT]
        if listeners and (not owned or len(listeners) != 1 or listeners[0].pid != record["pid"]
                          or listeners[0].laddr.ip != "127.0.0.1"):
            # A live port and matching basename are insufficient ownership.
            return "listener_conflict"
        if not owned:
            self.healthy_since = None
            if self.now() < self.starting_until:
                return "starting"
            if self.now() < self.next_start:
                return "restart_backoff"
            if not self.script.is_file() or not (self.runtime / "auth" / "refresh.json").is_file():
                return "service_files_missing"
            self.spawn([self.python, str(self.script), *self.arguments], cwd=str(self.project),
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), close_fds=True)
            self.next_start = self.now() + self.backoff
            self.starting_until = self.now() + 30
            self.backoff = min(self.backoff * 2, 120)
            self.failures = 0
            return "started"
        if not listeners and self.now() - record["create_time"] <= 30:
            return "starting"
        try:
            health = self.health_reader(self.runtime) if listeners else None
        except Exception:
            health = None
        instance = health.get("instance") if isinstance(health, dict) else None
        maintenance = health.get("maintenance") if isinstance(health, dict) else None
        healthy = bool(isinstance(health, dict) and health.get("runtime_ok") is True
                       and health.get("mode") == "production" and isinstance(instance, dict)
                       and instance.get("pid") == record["pid"]
                       and type(instance.get("create_time")) in (int, float)
                       and math.isfinite(instance["create_time"])
                       and instance["create_time"] == record["create_time"]
                       and isinstance(maintenance, dict) and maintenance.get("overdue_pending") is False)
        if healthy:
            self.failures = 0
            if self.healthy_since is None:
                self.healthy_since = self.now()
            if self.now() - self.healthy_since >= 120:
                self.backoff = 5
            # No dependency on inference.verified or overall ok: idle, blocked
            # auth and upstream failures are not process-health failures.
            return "running"
        self.healthy_since = None
        self.failures += 1
        if self.failures < 3 or self.now() - record["create_time"] <= 30:
            return "unhealthy_observed"
        if not self._terminate(record):
            return "identity_changed"
        self.next_start = self.now() + self.backoff
        self.backoff = min(self.backoff * 2, 120)
        self.failures = 0
        return "stopped_unresponsive"


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", required=True)
    args = parser.parse_args(argv)
    runtime = Path(args.runtime_dir).resolve()
    if runtime != PROJECT / ".runtime" / "glm_proxy":
        return 2
    runtime.mkdir(parents=True, exist_ok=True)
    lock = SingleInstanceLock(runtime / "watchdog.lock")
    if not lock.acquire():
        return 0 if lock.failure_reason == "locked" else 2
    stop = PROJECT / ".runtime" / "startup" / "STOP_GLM_PROXY"
    try:
        identity = {"pid": os.getpid(), "create_time": psutil.Process().create_time()}
        monitor = Monitor(PROJECT, runtime)
        while not stop.exists():
            publish(runtime / "watchdog-process.json", {**identity, "seen_at": time.time()})
            try:
                code = monitor.check_once()
            except Exception:
                code = "watchdog_check_failed"
            publish(runtime / "watchdog-status.json", {"at": time.time(), "status": code})
            time.sleep(10)
        try:
            monitor.stop_owned()
        except Exception:
            publish(runtime / "watchdog-status.json", {"at": time.time(), "status": "stop_identity_unconfirmed"})
        return 0
    except Exception:
        return 2
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())

"""Process-owned instance locks; the persistent kernel file is never unlinked.

The OS lock on a fixed ``.oslock`` sidecar is authoritative, even when that
file is empty or damaged. JSON at the original path is published atomically
and is diagnostic metadata, not a PID-based lease. Older versions used a plain
PID there, so their owner must be identified before overwriting that format.
An uninspectable legacy owner blocks migration. Do not delete a live kernel
file or start an older implementation alongside this one: POSIX advisory
locks require all starters to cooperate.
"""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import sys
import tempfile
import threading

import psutil


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PROTOCOL = "os-file-lock-v2"
_MAX_METADATA_BYTES = 4096


def _try_lock(fd: int) -> bool:
    if sys.platform == "win32":
        import msvcrt

        # CRT byte locks use the current position, including during unlock.
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
    else:
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                return False
            raise
    return True


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _publish_metadata(path: Path, metadata: bytes) -> None:
    """Never expose a partially written JSON file at the legacy lock path."""
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.set_inheritable(fd, False)
        remaining = memoryview(metadata)
        while remaining:
            count = os.write(fd, remaining)
            if count <= 0:
                raise OSError("instance lock metadata write made no progress")
            remaining = remaining[count:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(name, path)
    finally:
        if fd is not None:
            os.close(fd)
        # This unique temporary file belongs to this attempt. Neither the
        # diagnostic file nor the stable kernel lock file is ever unlinked.
        try:
            Path(name).unlink(missing_ok=True)
        except OSError:
            pass


def _entrypoint(command: list[str]) -> tuple[str, str, list[str]]:
    """Identify Python's actual entry point, not tokens passed to a script."""
    index = 1
    while index < len(command):
        value = command[index]
        if value == "-m" and index + 1 < len(command):
            return "module", command[index + 1], command[index + 2:]
        if value == "-c":
            return "code", "", []
        if value in {"-X", "-W"}:
            index += 2
        elif value == "--":
            index += 1
            break
        elif value.startswith("-"):
            index += 1
        else:
            break
    if index < len(command):
        return "script", command[index], command[index + 1:]
    return "", "", []


def _backend_command(arguments: list[str]) -> bool:
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == "--config":
            index += 2
        elif value.startswith("--config="):
            index += 1
        else:
            return value == "run"
    return False


def _role(path: Path) -> str:
    name = path.name.casefold()
    return "supervisor" if name == "supervisor.lock" else "backend" if name == "running.lock" else "instance"


def _legacy_owner(pid: int, path: Path) -> tuple[str, str]:
    """Return active, stale, or unknown for a pre-OS-lock PID file.

    A recycled PID belonging to an unrelated program is stale. A matching app
    command must also resolve to this project, with a stable process birth time.
    Matching module commands from another cwd may use PYTHONPATH, so are unknown
    rather than proof that the lock is stale.
    """
    try:
        process = psutil.Process(pid)
        created = process.create_time()
        executable = process.exe()
        role = ""
        kind = ""
        if Path(executable).name.casefold().startswith(("python", "pypy")):
            command = process.cmdline()
            if not command:
                return "unknown", "无法读取进程命令行"
            kind, entry, arguments = _entrypoint(command)
            if kind == "module":
                if entry == "app.supervisor":
                    role = "supervisor"
                elif entry in {"app", "app.cli"} and _backend_command(arguments):
                    role = "backend"
            elif kind == "script":
                cwd = Path(process.cwd()).resolve()
                script = Path(entry)
                script = (cwd / script).resolve() if not script.is_absolute() else script.resolve()
                if script == _PROJECT_ROOT / "app" / "supervisor.py":
                    role = "supervisor"
                elif script in {_PROJECT_ROOT / "app" / "__main__.py", _PROJECT_ROOT / "app" / "cli.py"} and _backend_command(arguments):
                    role = "backend"
        expected = _role(path)
        matches = bool(role) and expected in {role, "instance"}
        if matches and kind == "module" and Path(process.cwd()).resolve() != _PROJECT_ROOT:
            return "unknown", "相同模块命令的工作目录无法确认属于本项目"
        current = psutil.Process(pid)
        if current.create_time() != created:
            return "unknown", "检查期间 PID 对应的进程已变化"
        if current.status() == psutil.STATUS_ZOMBIE:
            return "stale", "原进程已退出"
        if not matches:
            return "stale", "PID 已属于其他命令"
        return "active", f"原版{role}进程仍在运行（PID {pid}）"
    except psutil.NoSuchProcess:
        return "stale", "原进程已退出"
    except (psutil.AccessDenied, OSError) as exc:
        return "unknown", f"无法确认原进程身份（PID {pid}，{type(exc).__name__}）"


class SingleInstanceLock:
    """A nonblocking, process-owned lock on a stable file identity.

    ``failure_reason`` is a stable code and ``diagnostic`` is suitable for a
    local startup log. A failed contender's ``release`` changes no files.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.kernel_path = self.path.with_name(self.path.name + ".oslock")
        self.fd: int | None = None
        self.failure_reason = ""
        self.diagnostic = ""
        self._owner_pid: int | None = None
        self._mutex = threading.Lock()

    def _fail(self, reason: str, detail: str) -> bool:
        self.failure_reason = reason
        self.diagnostic = f"{reason}: {detail}"
        return False

    def acquire(self) -> bool:
        with self._mutex:
            if self.fd is not None:
                if self._owner_pid == os.getpid():
                    return True
                # A fork copied our descriptor. Closing its copy must not
                # explicitly unlock the parent's shared open file description.
                os.close(self.fd)
                self.fd = None
                self._owner_pid = None
            self.failure_reason = self.diagnostic = ""
            fd = None
            locked = False
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
                fd = os.open(self.kernel_path, flags | os.O_CREAT, 0o600)
                os.set_inheritable(fd, False)
                if not _try_lock(fd):
                    return self._fail("locked", "已有进程持有操作系统实例锁")
                locked = True
                held = os.fstat(fd)
                named = self.kernel_path.stat()
                if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
                    return self._fail("lock_path_changed", "检查期间锁文件已被替换；未接管实例")
                try:
                    with self.path.open("rb") as legacy:
                        content = legacy.read(_MAX_METADATA_BYTES + 1)
                except FileNotFoundError:
                    content = None
                if content is not None:
                    if len(content) > _MAX_METADATA_BYTES:
                        return self._fail("lock_metadata_unknown", "原锁文件内容过大；无法安全迁移")
                    content = content.strip()
                    if content.isdigit() or (content[:1] in {b"+", b"-"} and content[1:].isdigit()):
                        pid = int(content)
                        state, detail = _legacy_owner(pid, self.path) if pid > 0 else ("stale", "无效 PID")
                        if state != "stale":
                            return self._fail("legacy_owner_active" if state == "active" else "legacy_owner_unknown", detail)
                    else:
                        try:
                            record = json.loads(content)
                        except (ValueError, UnicodeDecodeError):
                            record = None
                        if not isinstance(record, dict) or record.get("lock_protocol") != _PROTOCOL:
                            return self._fail("lock_metadata_unknown", "无法识别锁文件；确认原版进程退出后再迁移")
                metadata = json.dumps({
                    "lock_protocol": _PROTOCOL,
                    "pid": os.getpid(),
                    "create_time": psutil.Process().create_time(),
                    "role": _role(self.path),
                    "kernel_file": self.kernel_path.name,
                }, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n"
                _publish_metadata(self.path, metadata)
                self.fd, self._owner_pid = fd, os.getpid()
                fd = None
                locked = False
                return True
            except (OSError, psutil.Error) as exc:
                return self._fail("lock_io_error", f"实例锁不可用（{type(exc).__name__}，{exc}）")
            finally:
                if fd is not None:
                    try:
                        if locked:
                            try:
                                _unlock(fd)
                            except OSError:
                                pass  # Closing still releases this descriptor.
                    finally:
                        os.close(fd)

    def release(self) -> None:
        with self._mutex:
            if self.fd is None:
                return
            fd, owner = self.fd, self._owner_pid
            self.fd = self._owner_pid = None
            try:
                if owner == os.getpid():
                    _unlock(fd)
            except OSError as exc:
                self._fail("lock_release_error", f"显式解锁失败，将关闭本进程句柄（{type(exc).__name__}）")
            finally:
                # Closing also releases this handle's OS lock if explicit
                # unlock failed. Both persistent pathnames remain in place.
                os.close(fd)

#!/usr/bin/env python3
"""macOS draft-only setup and explicit user LaunchAgent management.

This file intentionally uses only the Python standard library. It can create the
project venv before importing any application dependencies. No command connects
to a native WeChat client or installs a LaunchAgent implicitly.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser


ROOT = Path(__file__).resolve().parents[1]
LABEL = "io.github.wangwechattwin.drafts"
CONFIG_NAME = "config.macos.local.yaml"
PYTHON_RANGE = "Python 3.10–3.12"


class MacSetupError(RuntimeError):
    pass


def require_macos() -> None:
    if sys.platform != "darwin":
        raise MacSetupError("本工具只在 macOS 运行；没有安装或修改登录自启。")


def config_path(root: Path, value: str | Path | None = None) -> Path:
    value = Path(value or CONFIG_NAME).expanduser()
    return (value if value.is_absolute() else root / value).resolve()


def initialize_config(root: Path, destination: Path) -> bool:
    """Copy the sample once, including when another setup races this process."""
    sample = (root / "config.macos.example.yaml").read_bytes()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "wb") as stream:
        stream.write(sample)
    return True


def project_python(root: Path) -> Path:
    # Resolving the symlink would select the base interpreter and lose the venv.
    executable = root.absolute() / ".venv" / "bin" / "python"
    if not executable.is_file():
        raise MacSetupError("尚无 Mac 虚拟环境。请先运行 START_HERE.command setup。")
    return executable


def check_python(executable: str | Path, root: Path) -> None:
    result = subprocess.run(
        [str(executable), "-c", "import json, sys; print(json.dumps(list(sys.version_info[:2])))"],
        cwd=root, capture_output=True, text=True, check=True,
    )
    try:
        version = tuple(json.loads(result.stdout))
    except (TypeError, ValueError):
        raise MacSetupError("无法核对 Python 版本。") from None
    if not (3, 10) <= version < (3, 13):
        raise MacSetupError(f"需要 {PYTHON_RANGE}；现有环境不会被覆盖。")


def ensure_venv(root: Path, executable: str | Path) -> Path:
    environment = root / ".venv"
    if environment.exists():
        python = project_python(root)
    else:
        check_python(executable, root)
        subprocess.run([str(executable), "-m", "venv", str(environment)], cwd=root, check=True)
        python = project_python(root)
    check_python(python, root)
    return python


def dependencies_ready(python: Path, root: Path) -> bool:
    result = subprocess.run(
        [str(python), "-c", "import app.config, fastapi, uvicorn, openai, httpx, websockets, zstandard, PIL, psutil"],
        cwd=root, capture_output=True, text=True,
    )
    return result.returncode == 0


def runtime_settings(python: Path, root: Path, config: Path) -> dict:
    # Use the installed venv, never a system-Python import of application modules.
    code = (
        "import json, sys; from app.config import load_config; "
        "c=load_config(sys.argv[1]); "
        "print(json.dumps({'adapter':c.adapter,'mode':c.mode,'host':c.web.host,"
        "'port':c.web.port,'web_enabled':c.web.enabled}))"
    )
    result = subprocess.run(
        [str(python), "-c", code, str(config)], cwd=root,
        capture_output=True, text=True,
    )
    if result.returncode:
        raise MacSetupError("配置无法加载，请运行 setup 检查依赖及 YAML 配置。")
    try:
        settings = json.loads(result.stdout)
    except (TypeError, ValueError):
        raise MacSetupError("无法读取本地配置。") from None
    if not isinstance(settings, dict):
        raise MacSetupError("无法读取本地配置。")
    if settings.get("adapter") != "mock":
        raise MacSetupError("Mac 草稿版只支持 adapter: mock；本机微信读取、发送尚未支持。")
    if settings.get("mode") != "shadow":
        raise MacSetupError("Mac 草稿版需要 mode: shadow。请保留审核模式后再启动。")
    if not settings.get("web_enabled") or settings.get("host") != "127.0.0.1":
        raise MacSetupError("审核台需要 web.enabled: true 和 web.host: 127.0.0.1。")
    port = settings.get("port")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise MacSetupError("审核台端口需要在 1–65535 之间。")
    return settings


def setup(root: Path, config: Path, *, reinstall: bool = False) -> Path:
    require_macos()
    created = initialize_config(root, config)
    print(f"{'已创建' if created else '保留已有'}配置：{config}")
    python = ensure_venv(root, sys.executable)
    if reinstall or not dependencies_ready(python, root):
        # Core dependencies only: do not request the Windows extra or wxauto.
        subprocess.run([str(python), "-m", "pip", "install", "-e", "."], cwd=root, check=True)
    runtime_settings(python, root, config)
    print("Mac 草稿环境已就绪。网页模型仍需你自己的登录及扩展配对。")
    return python


def app_command(python: Path, config: Path, command: str, *extra: str) -> list[str]:
    result = [str(python), "-u", "-m", "app", "--config", str(config), command]
    result.extend(extra)
    return result


def make_launch_agent(root: Path, python: Path, config: Path) -> dict:
    root = root.absolute()
    logs = root / ".runtime" / "macos"
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(python.absolute()), "-u", str(root / "scripts" / "macos.py"),
            "--config", str(config.absolute()), "serve",
        ],
        "WorkingDirectory": str(root),
        "RunAtLoad": True,
        # A normal stop stays stopped; crashes are restarted by launchd.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
        "StandardOutPath": str(logs / "launchd.stdout.log"),
        "StandardErrorPath": str(logs / "launchd.stderr.log"),
    }


def agent_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def owned_agent(path: Path, root: Path) -> dict | None:
    if path.is_symlink():
        raise MacSetupError("登录自启文件是符号链接，已停止；不会覆盖或删除。")
    if not path.exists():
        return None
    try:
        with path.open("rb") as stream:
            value = plistlib.load(stream)
        arguments = value.get("ProgramArguments", [])
        expected = [
            str(root.absolute() / ".venv" / "bin" / "python"),
            "-u", str(root.absolute() / "scripts" / "macos.py"), "--config",
        ]
        valid = (
            value.get("Label") == LABEL
            and value.get("WorkingDirectory") == str(root.absolute())
            and len(arguments) == 6 and arguments[:4] == expected
            and isinstance(arguments[4], str) and bool(arguments[4])
            and arguments[5] == "serve"
        )
    except (OSError, ValueError, TypeError, AttributeError, plistlib.InvalidFileException):
        valid = False
    if not valid:
        raise MacSetupError("已有登录自启不属于此目录，已保留。请从原目录卸载后再安装。")
    return value


def launchctl(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/launchctl", *arguments], capture_output=True, text=True, timeout=20,
    )


def service_target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def install_agent(root: Path, config: Path, *, home: Path | None = None) -> Path:
    # Guard before every filesystem and launchctl mutation on non-Mac hosts.
    require_macos()
    python = project_python(root)
    check_python(python, root)
    runtime_settings(python, root, config)
    path = agent_path(home)
    existing = owned_agent(path, root)
    planned = make_launch_agent(root, python, config)
    if existing is not None and existing != planned:
        raise MacSetupError("已有自启使用另一份配置或设置，未覆盖；请先 uninstall 再 install。")
    target = service_target()
    if launchctl("print", target).returncode == 0:
        if existing is None:
            # A loaded job retains its original arguments after its plist is
            # removed. Do not create a new checkout's ownership file over it.
            raise MacSetupError("检测到已加载的同名服务，但缺少本目录自启文件；未写入或接管。请先从原目录处理旧服务。")
        print("此目录的登录自启已加载，未重复启动。")
        return path
    if existing is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        (root / ".runtime" / "macos").mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise MacSetupError("登录自启文件刚被其他进程创建，未覆盖；请重新核对。") from None
        with os.fdopen(fd, "wb") as stream:
            plistlib.dump(planned, stream)
    result = launchctl("enable", target)
    if result.returncode:
        raise MacSetupError(f"launchctl enable 失败：{result.stderr.strip()}")
    result = launchctl("bootstrap", f"gui/{os.getuid()}", str(path))
    if result.returncode:
        raise MacSetupError(f"launchctl bootstrap 失败；自启文件已保留便于排查：{result.stderr.strip()}")
    print(f"已安装并加载登录自启：{path}")
    print("这只启动草稿后台；网页模型登录、Mac 微信连接没有自动完成。")
    return path


def stop_agent(root: Path, *, home: Path | None = None, uninstall: bool = False) -> None:
    require_macos()
    path = agent_path(home)
    if owned_agent(path, root) is None:
        print("此用户没有安装本目录的登录自启。")
        return
    target = service_target()
    if launchctl("print", target).returncode == 0:
        result = launchctl("bootout", target)
        if result.returncode:
            raise MacSetupError(f"停止自启失败，文件仍保留：{result.stderr.strip()}")
    if uninstall:
        path.unlink()
        print("已卸载登录自启；配置、密钥、数据库和草稿均保留。")
    else:
        print("已停止登录自启后台。自启文件保留，下次登录时仍会启动。")


def show_status(root: Path) -> int:
    require_macos()
    if owned_agent(agent_path(), root) is None:
        print("登录自启：未安装。")
        return 0
    result = launchctl("print", service_target())
    print("登录自启：已加载。" if result.returncode == 0 else "登录自启：已安装，当前未加载。")
    print("后台 /health 与网页生成结果需分别核对；已加载不代表生成成功。")
    return 0


def open_when_ready(child: subprocess.Popen, url: str, stop: threading.Event) -> None:
    for _ in range(60):
        if stop.wait(0.5) or child.poll() is not None:
            return
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                ready = response.status == 200
        except (OSError, urllib.error.URLError):
            continue
        if ready and child.poll() is None:
            webbrowser.open(url)
            return


def start_foreground(root: Path, python: Path, config: Path, settings: dict) -> int:
    command = app_command(python, config, "run", "--mode", "shadow")
    url = f"http://127.0.0.1:{settings['port']}/"
    print("只生成审核草稿。保留此终端窗口，按 Ctrl+C 停止。", flush=True)
    child = subprocess.Popen(command, cwd=root)
    stop = threading.Event()
    threading.Thread(target=open_when_ready, args=(child, url, stop), daemon=True).start()
    try:
        return child.wait()
    except KeyboardInterrupt:
        # The foreground terminal also sends SIGINT to our owned child.
        try:
            return child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.terminate()
            child.wait(timeout=15)
            return 130
    finally:
        stop.set()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mac 微信分身草稿版（未连接本机微信）")
    parser.add_argument("--config", help=f"默认保留/创建 {CONFIG_NAME}")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("launch", help="准备环境并启动审核台（默认）")
    commands.add_parser("setup", help="安装核心依赖，保留已有配置")
    commands.add_parser("start", help="前台启动草稿后台并打开审核台")
    commands.add_parser("serve", help="供登录自启调用的草稿后台")
    commands.add_parser("doctor", help="检查配置及模型桥接")
    commands.add_parser("dashboard", help="打开审核台")
    commands.add_parser("pause", help="暂停回复处理")
    commands.add_parser("resume", help="显式恢复回复处理")
    draft = commands.add_parser("draft", help="导入消息 JSON，生成审核草稿")
    draft.add_argument("--input", required=True, help="消息 JSON 文件")
    commands.add_parser("install", help="显式安装并加载当前用户登录自启")
    commands.add_parser("status", help="查看登录自启状态")
    commands.add_parser("stop", help="停止自启后台，保留自启设置")
    commands.add_parser("uninstall", help="停止并卸载登录自启，保留用户数据")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "launch"
    try:
        require_macos()
        config = config_path(ROOT, args.config)
        if command in {"launch", "setup"}:
            python = setup(ROOT, config, reinstall=command == "setup")
            if command == "setup":
                return 0
        elif command == "install":
            install_agent(ROOT, config)
            return 0
        elif command in {"stop", "uninstall"}:
            stop_agent(ROOT, uninstall=command == "uninstall")
            return 0
        elif command == "status":
            return show_status(ROOT)
        else:
            python = project_python(ROOT)
            check_python(python, ROOT)
        settings = runtime_settings(python, ROOT, config)
        if command in {"launch", "start"}:
            return start_foreground(ROOT, python, config, settings)
        if command == "serve":
            # launchd owns this process and restarts abnormal exits. Revalidate
            # the configuration at every login/restart before loading adapters.
            os.chdir(ROOT)
            os.execv(str(python), app_command(python, config, "run", "--mode", "shadow"))
        extra = ["--input", str(config_path(ROOT, args.input))] if command == "draft" else []
        return subprocess.call(app_command(python, config, command, *extra), cwd=ROOT)
    except (MacSetupError, OSError, subprocess.SubprocessError) as exc:
        print(f"Mac 草稿版：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

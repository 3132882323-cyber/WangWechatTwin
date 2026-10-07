from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from app.adapters import MockAdapter, WxAutoAdapter
from app.adapters.wxauto_adapter import WxAutoUnavailable
from app.config import AppConfig, load_config, openai_credentials
from app.db import Database
from app.models import IncomingMessage
from app.pipeline import ReplyPipeline
from app.webui import start_webui




class SingleInstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    @staticmethod
    def _alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if sys.platform == "win32":
            # os.kill(pid, 0) terminates processes on Windows; query instead.
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return ctypes.get_last_error() == 5  # Access denied: assume alive.
            try:
                code = wintypes.DWORD()
                if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return True
                return code.value == 259  # STILL_ACTIVE
            finally:
                kernel.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                old_pid = int(self.path.read_text(encoding="utf-8").strip())
            except Exception:
                old_pid = -1
            if self._alive(old_pid):
                return False
            self.path.unlink(missing_ok=True)
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self.fd, str(os.getpid()).encode("ascii"))
            return True
        except FileExistsError:
            return False

    def release(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        self.path.unlink(missing_ok=True)

def merge_incoming_messages(messages: list[IncomingMessage], *, separate_media: bool=False) -> list[IncomingMessage]:
    """Merge a burst from the same conversation into one coherent turn."""
    groups: dict[tuple[str, str], list[IncomingMessage]] = {}
    order: list[tuple[str, str]] = []
    for message in messages:
        kind='text' if message.message_type=='text' else 'visual' if message.message_type in {'image','sticker'} else message.message_type
        key = (message.contact, message.chat_type,kind) if separate_media else (message.contact,message.chat_type)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(message)

    merged: list[IncomingMessage] = []
    for key in order:
        items = groups[key]
        if len(items) == 1:
            merged.append(items[0])
            continue
        identity = "|".join(item.external_id for item in items)
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        origins=[]
        for item in items:
            try:origin=json.loads(item.raw_summary or '{}')
            except ValueError:origin={}
            origins.append(origin if isinstance(origin,dict) else {})
        latest_index=max(range(len(items)),key=lambda index:items[index].received_at)
        summary=dict(origins[latest_index])
        members=[]
        for item,origin in zip(items,origins):members.extend([item.external_id]+origin.get('member_external_ids',[]))
        summary.update(merged_count=len(items),member_external_ids=list(dict.fromkeys(members)),
                       asr_uncertain=any(origin.get('asr_uncertain') for origin in origins))
        if any(origin.get('original_type')=='voice' for origin in origins):
            summary.update(original_type='voice',transcription_source='offline_whisper')
        all_paths=list(dict.fromkeys(path for item in items for path in item.media_paths))
        summary['image_partial']=len(all_paths)>3 or any(origin.get('image_partial') for origin in origins)
        types={item.message_type for item in items}
        merged_type='text' if types=={'text'} else 'image' if types<= {'text','image','sticker'} and 'image' in types and all_paths else 'sticker' if types<= {'text','sticker'} and all_paths else 'mixed'
        merged.append(
            IncomingMessage(
                external_id=f"merged:{digest}",
                contact=items[-1].contact,
                sender='群中多位成员' if items[-1].chat_type=='group' and len({m.sender_key or m.sender for m in items})>1 else items[-1].sender,
                sender_key=items[-1].sender_key if len({m.sender_key or m.sender for m in items})==1 else '',
                display_name=items[-1].display_name,
                content="\n".join((item.sender+'：'+item.content) if item.chat_type=='group' else item.content for item in items if item.content.strip()),
                media_paths=all_paths[:3],
                message_type=merged_type,
                chat_type=items[-1].chat_type,
                received_at=max(item.received_at for item in items),
                raw_summary=json.dumps(summary),
            )
        )
    return merged

def _load(args: argparse.Namespace) -> tuple[AppConfig, Database]:
    config = load_config(args.config)
    if getattr(args, "mode", None):
        config.mode = args.mode
    db = Database(config.resolve(config.paths.database))
    return config, db


def _adapter(config: AppConfig, *, initialize: bool = True):
    if config.adapter == "history_http_sender":
        from app.adapters.http_sender import HistoryHTTPSender
        return HistoryHTTPSender(config)
    if config.adapter == "history_verified_sender":
        from app.adapters.verified_sender import HistoryVerifiedSender
        return HistoryVerifiedSender(config)
    if config.adapter == "history_readonly":
        from app.adapters.history_reader import HistoryReadOnlyAdapter
        return HistoryReadOnlyAdapter(config)
    if config.adapter == "mock":
        return MockAdapter()
    return WxAutoAdapter(config, initialize=initialize)


def command_doctor(args: argparse.Namespace) -> int:
    config, db = _load(args)
    key, base = openai_credentials()
    uses_account = config.openai.provider == "account" or (config.openai.provider == "auto" and not key)
    account_ready = False
    if uses_account and shutil.which("codex"):
        try:
            account_ready = subprocess.run(
                [shutil.which("codex"), "login", "status"], capture_output=True,
                timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    model_ready = account_ready if uses_account else bool(key)
    python_supported = (3, 10) <= sys.version_info[:2] < (3, 13)
    report = {
        "ok": model_ready and python_supported,
        "python": sys.version,
        "python_supported": python_supported,
        "platform": platform.platform(),
        "config": str(Path(args.config).resolve()),
        "mode": config.mode,
        "database": str(config.resolve(config.paths.database)),
        "database_writable": True,
        "openai_key_present": bool(key),
        "inference_provider": "account" if uses_account else "api",
        "model_credentials_available": model_ready,
        "model_inference_verified": False,
        "openai_base_url": base or "official",
        "contacts": [c.name for c in config.contacts],
        "warnings": [],
    }
    if not model_ready:
        report["warnings"].append("AI 尚未连接，请先登录 Codex 或配置模型密钥")
    if not python_supported:
        report["warnings"].append("建议使用 Python 3.11 或 3.12")
    try:
        adapter = _adapter(config, initialize=True)
        adapter_report = adapter.doctor()
        report["adapter"] = adapter_report
        if not adapter_report.get("ok", False):
            report["ok"] = False
        if adapter_report.get("warning"):
            report["warnings"].append(adapter_report["warning"])
    except Exception as exc:
        report["ok"] = False
        report["adapter_error"] = f"{type(exc).__name__}: {exc}"
    db.add_event("doctor", json.dumps(report, ensure_ascii=False))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


def command_pause(args: argparse.Namespace) -> int:
    config, db = _load(args)
    config.resolve(config.paths.pause_file).write_text("paused", encoding="utf-8")
    db.add_event("paused", "通过命令暂停")
    print("已暂停。正在进行中的一次界面操作可能无法撤回。")
    return 0


def command_resume(args: argparse.Namespace) -> int:
    config, db = _load(args)
    config.resolve(config.paths.pause_file).unlink(missing_ok=True)
    db.add_event("resumed", "通过命令恢复")
    print("已恢复。")
    return 0


def command_dashboard(args: argparse.Namespace) -> int:
    config, _ = _load(args)
    url = f"http://{config.web.host}:{config.web.port}/"
    webbrowser.open(url)
    print(url)
    return 0


def command_run(args: argparse.Namespace) -> int:
    config, db = _load(args)
    db.cleanup(config.memory.retain_message_days)
    lock = SingleInstanceLock(config.resolve(config.paths.database).parent / "RUNNING.lock")
    if not lock.acquire():
        print("检测到另一个数字分身进程正在运行。请先关闭原运行窗口。", file=sys.stderr)
        return 2

    adapter = None
    scheduler = None
    candidates = None
    started = False
    try:
        try:
            adapter = _adapter(config, initialize=True)
        except Exception as exc:
            print(f"启动微信适配器失败：{exc}", file=sys.stderr)
            return 2
        pipeline = ReplyPipeline(config, db)
        from app.reply_scheduler import ReplyScheduler
        scheduler=ReplyScheduler(config,db,pipeline,adapter)
        if config.openai.deepseek_drafts:
            from app.deepseek_candidates import DeepseekCandidates
            candidates=DeepseekCandidates(config,db)
        from app.proactive import ProactivePlanner
        proactive_planner = ProactivePlanner(config, db, adapter)
        start_webui(config, db)
        db.set_state("runtime", {"status": "running", "mode": config.mode, "pid": os.getpid()})
        db.add_event("runtime_started", f"mode={config.mode}; adapter={config.adapter}")
        started = True
        print(f"微信聊天分身已启动，模式：{config.mode}")
        if config.web.enabled:
            print(f"本地控制台：http://{config.web.host}:{config.web.port}/")
        print("按 Ctrl+C 停止。")

        try:
            while True:
                if config.resolve(config.paths.pause_file).exists():
                    time.sleep(max(config.wechat.poll_seconds, 1.0))
                try:
                    messages = merge_incoming_messages(adapter.poll(),separate_media=True)
                    scheduler.submit(messages)
                    if candidates:candidates.submit(messages)
                    proactive_planner.tick()
                except WxAutoUnavailable as exc:
                    db.add_event("adapter_error", str(exc), level="error")
                    print(f"微信适配器错误：{exc}", file=sys.stderr)
                    time.sleep(5)
                except Exception as exc:
                    db.add_event("runtime_error", f"{type(exc).__name__}: {exc}", level="error")
                    print(f"运行错误：{type(exc).__name__}: {exc}", file=sys.stderr)
                    time.sleep(3)
                time.sleep(config.wechat.poll_seconds)
        except KeyboardInterrupt:
            print("\n已停止。")
        return 0
    finally:
        if scheduler:scheduler.close()
        if candidates:candidates.close()
        if adapter is not None:
            try:
                adapter.close()
            except Exception:
                pass
        if started:
            db.set_state("runtime", {"status": "stopped", "mode": config.mode})
            db.add_event("runtime_stopped", "用户停止程序")
        lock.release()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="微信聊天分身")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="检查环境和微信连接")
    doctor.set_defaults(func=command_doctor)

    run = sub.add_parser("run", help="启动消息处理")
    run.add_argument("--mode", choices=["shadow", "low_risk_auto", "full_auto", "off"])
    run.set_defaults(func=command_run)

    pause = sub.add_parser("pause", help="暂停")
    pause.set_defaults(func=command_pause)

    resume = sub.add_parser("resume", help="恢复")
    resume.set_defaults(func=command_resume)

    dashboard = sub.add_parser("dashboard", help="打开本地控制台")
    dashboard.set_defaults(func=command_dashboard)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))

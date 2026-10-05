"""A local read bridge with explicit recipient gating and verified GUI sending."""
from __future__ import annotations
from contextlib import closing

import ctypes
import hashlib
import json
import re
import sqlite3
import struct
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

import psutil
import win32gui
import win32process
import zstandard

from app.adapters.base import MessageAdapter
from app.adapters.history_reader import HistoryReadOnlyAdapter, authenticated_snapshot
from app.models import RiskLevel
from app.risk import assess_risk


def ensure_client_accessibility(runtime_root):
    from app.adapters.accessibility_bridge import (
        WeChatUIA, QACCESSIBLE_GATE_PATTERN, IMAGE_SCN_MEM_EXECUTE, IMAGE_SCN_MEM_WRITE)
    windows = []
    def observe(hwnd, _):
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            title = win32gui.GetWindowText(hwnd)
            if (win32gui.IsWindowVisible(hwnd) and win32gui.GetClassName(hwnd).startswith("Qt")
                    and psutil.Process(pid).name().lower() == "weixin.exe"
                    and (re.fullmatch(r"微信(?:\(\d+\))?", title) or title == "Weixin")):
                windows.append((hwnd, pid))
        except (OSError, psutil.Error):
            pass
    win32gui.EnumWindows(observe, None)
    if len(windows) != 1:
        raise RuntimeError("微信主窗口不唯一，停止发送")
    hwnd, pid = windows[0]
    if WeChatUIA._mmui_present(hwnd, timeout=.2):
        return
    module = WeChatUIA._weixin_dll_module(pid)
    if not module or Path(module[2]).parent.name != "4.1.15.13":
        raise RuntimeError("当前微信版本尚未通过发送兼容验证")
    base, _, path = module
    data = Path(path).read_bytes()
    sections = WeChatUIA._pe_sections(data)
    expected, matched = 0x0B135C38, False
    for match in QACCESSIBLE_GATE_PATTERN.finditer(data):
        offset = WeChatUIA._offset_to_rva(sections, match.start("disp"))
        if offset is None:
            continue
        rva = offset + 5 + struct.unpack("<i", match.group("disp"))[0]
        section = WeChatUIA._section_for_rva(sections, rva)
        if (rva == expected and section and section["chars"] & IMAGE_SCN_MEM_WRITE
                and not section["chars"] & IMAGE_SCN_MEM_EXECUTE):
            matched = True
            break
    if not matched:
        raise RuntimeError("界面识别开关校验失败，停止发送")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x438, False, pid)
    if not handle:
        raise PermissionError("无法启用微信界面识别")
    try:
        address = base + expected
        original = WeChatUIA._read_process_byte(handle, address)
        if original not in (0, 1):
            raise RuntimeError("界面状态不符合已验证格式")
        (runtime_root / "accessibility_rollback.json").write_text(json.dumps({
            "pid": pid, "address": address, "original": original,
            "dll_sha256": hashlib.sha256(data).hexdigest(),
        }), encoding="utf-8")
        if original == 0 and not WeChatUIA._write_process_byte(handle, address, 1):
            raise PermissionError("界面识别开关更新失败")
        if not WeChatUIA._mmui_present(hwnd, timeout=2):
            WeChatUIA._write_process_byte(handle, address, original)
            raise RuntimeError("界面控件未出现，已恢复原值")
    finally:
        kernel.CloseHandle(handle)


class HistoryVerifiedSender(MessageAdapter):
    def __init__(self, config):
        self.config = config
        self.reader = HistoryReadOnlyAdapter(config)
        self.allowed = set(config.wechat.sender_allowed_contacts)
        self.context = {}
        self.context_origin = {}
        self.wx = None

    def doctor(self):
        report = self.reader.doctor()
        report.update(adapter="history_verified_sender", sender_allowlist_count=len(self.allowed),
                      automatic_sending_armed=self.config.mode == "low_risk_auto")
        return report

    def check_sender_connection(self):
        ensure_client_accessibility(self.reader.root)
        from wxauto4 import WeChat, WxParam
        WxParam.TELEMETRY_ENABLED = False
        WxParam.ENABLE_FILE_LOGGER = False
        self.wx = WeChat(debug=False, resize=False, ads=False)
        identity = self.wx.GetMyInfo() or {}
        if not self.reader.self_alias or identity.get("id") != self.reader.self_alias:
            self.wx = None
            raise RuntimeError("当前登录账号与聊天数据库不一致，停止发送")
        return True

    def poll(self):
        messages = self.reader.poll()
        if self.config.wechat.sender_all_existing_chats:
            self.allowed.update(self._eligible_contacts())
        for message in messages:
            self.context[message.contact] = message.external_id
            if message.raw_summary:
                self.context_origin[message.contact] = json.loads(message.raw_summary)
            if message.message_type == 'voice':
                try:
                    self._transcribe_voice(message)
                except Exception as exc:
                    message.content = '对方发来语音，自动转写未完成，需本人查看。'
                    origin = json.loads(message.raw_summary)
                    origin['voice_error'] = type(exc).__name__
                    message.raw_summary = json.dumps(origin)
        return messages

    def _transcribe_voice(self, message):
        from app.voice import align_voice
        if message.chat_type != 'friend' or self.config.resolve(self.config.paths.pause_file).exists():
            raise ValueError('当前不允许语音转写')
        name = self.reader.names.get(message.contact)
        if not name or list(self.reader.names.values()).count(name) != 1:
            raise ValueError('语音联系人不唯一')
        origin = json.loads(message.raw_summary)
        info = self.reader.files[origin['source']]
        table = 'Msg_' + hashlib.md5(message.contact.encode()).hexdigest()
        with tempfile.TemporaryDirectory(dir=self.reader.root) as tmp:
            snapshot, _ = authenticated_snapshot(info, Path(tmp))
            with closing(sqlite3.connect(snapshot.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
                rows = db.execute(f'SELECT local_id,local_type,message_content,WCDB_CT_message_content,real_sender_id FROM "{table}" ORDER BY local_id DESC LIMIT 12').fetchall()[::-1]
                own = db.execute('SELECT rowid FROM Name2Id WHERE user_name=?', (self.reader.self_username,)).fetchone()
        expected, target_index = [], None
        for local_id, typ, content, compression, sender_id in rows:
            kind = {1:'text',34:'voice'}.get(typ & 0xffffffff)
            if kind is None:
                raise ValueError('语音上下文包含暂不能核验的消息')
            if kind == 'text' and isinstance(content, bytes):
                content = (zstandard.ZstdDecompressor().decompress(content, max_output_size=8*1024*1024) if compression else content).decode('utf-8')
            if local_id == origin['local_id']:
                target_index = len(expected)
                origin['transcription_direction'] = 'out' if own and sender_id == own[0] else 'in'
            expected.append((kind, content if kind == 'text' else ''))
        if target_index is None:
            raise ValueError('语音已离开最新消息范围')
        self.check_sender_connection()
        selected = self.wx.ChatWith(name, exact=True)
        current_info = {}
        for _ in range(8):
            current_info = self.wx.ChatInfo() or {}
            if current_info.get('chat_name') == name:
                break
            time.sleep(.25)
        if current_info.get('chat_name') != name:
            status = selected.get('status','unknown') if isinstance(selected, dict) else type(selected).__name__
            raise ValueError('微信当前会话名称未通过核验；切换状态=' + str(status) + '；字段=' + ','.join(current_info))
        if self._has_saved_human_draft(message.contact):
            raise ValueError('该会话已有本人未发送的草稿，保留编辑现场')
        gui = [m for m in self.wx.GetAllMessage() if getattr(m, 'type', '') in {'text','voice'}]
        visible = [(getattr(m,'type',''), str(getattr(m,'content','')) if getattr(m,'type','') == 'text' else '') for m in gui]
        # WeChat may load fewer than twelve items in a small window. Keep the
        # longest available anchored tail, while retaining the target voice.
        trim = max(0, len(expected)-len(visible))
        if trim > target_index:
            raise ValueError('目标语音尚未出现在窗口中')
        expected = expected[trim:]
        target_index -= trim
        index = align_voice(expected, visible, target_index)
        if self.config.resolve(self.config.paths.pause_file).exists():
            raise ValueError('已暂停')
        try:
            transcript = gui[index].to_text()
        except NameError:
            from app.voice import native_transcription
            transcript = native_transcription(gui[index])
        if not isinstance(transcript, str) or not transcript.strip() or transcript.strip() in {'[语音]', '转换失败', '无法识别'}:
            raise ValueError('微信没有返回有效转写')
        cache = self.reader.root / 'voice_transcripts'
        cache.mkdir(exist_ok=True)
        (cache / (hashlib.sha256(message.external_id.encode()).hexdigest()+'.json')).write_text(json.dumps({'external_id':message.external_id,'contact':message.contact,'transcript':transcript,'source':'wechat_builtin','direction':origin['transcription_direction']}, ensure_ascii=False),encoding='utf-8')
        from app.chat_memory import remember
        remember(self.config.resolve(self.config.paths.chat_memory), [('voice:'+message.external_id, message.contact, origin['transcription_direction'], origin['created_at'], transcript)])
        message.content = transcript.strip()
        message.message_type = 'text'
        origin['original_type'] = 'voice'
        origin['transcription_source'] = 'wechat_builtin'
        message.raw_summary = json.dumps(origin)

    def _eligible_contacts(self):
        system = {"filehelper", "weixin", "qqmail", "fmessage", "medianote", "newsapp", "notification_messages"}
        return {uid for uid in self.reader.existing_conversations
                if uid in self.reader.names and uid != self.reader.self_username
                and uid not in system and not uid.startswith("gh_")}

    def _outgoing_state(self, contact, text):
        table = "Msg_" + hashlib.md5(contact.encode()).hexdigest()
        watermarks, records, own_watermarks = {}, [], {}
        for rel, info in self.reader.files.items():
            with tempfile.TemporaryDirectory(dir=self.reader.root) as tmp:
                snapshot, _ = authenticated_snapshot(info, Path(tmp))
                conn = sqlite3.connect(snapshot.as_uri() + "?mode=ro&immutable=1", uri=True)
                try:
                    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                        continue
                    owner = rel.split("/")[0].rsplit("_", 1)[0]
                    own_id = conn.execute("SELECT rowid FROM Name2Id WHERE user_name=?", (owner,)).fetchone()[0]
                    watermarks[rel] = conn.execute(f'SELECT max(local_id) FROM "{table}"').fetchone()[0] or 0
                    own = conn.execute(f'SELECT local_id,create_time FROM "{table}" WHERE real_sender_id=? ORDER BY local_id DESC LIMIT 1', (own_id,)).fetchone()
                    if own:
                        own_watermarks[rel] = own
                    for local_id, server_id, content, compression in conn.execute(
                        f'SELECT local_id,server_id,message_content,WCDB_CT_message_content FROM "{table}" '
                        'WHERE real_sender_id=? ORDER BY local_id DESC LIMIT 12', (own_id,)):
                        if isinstance(content, bytes):
                            content = (zstandard.ZstdDecompressor().decompress(content, max_output_size=8*1024*1024)
                                       if compression else content).decode("utf-8")
                        if content == text:
                            records.append((rel, local_id, bool(server_id)))
                finally:
                    conn.close()
        return watermarks, records, own_watermarks

    def _has_saved_human_draft(self, contact):
        for rel, info in self.reader.keys.items():
            if not rel.replace("\\", "/").endswith("/session/session.db"):
                continue
            with tempfile.TemporaryDirectory(dir=self.reader.root) as tmp:
                snapshot, _ = authenticated_snapshot(info, Path(tmp))
                conn = sqlite3.connect(snapshot.as_uri() + "?mode=ro&immutable=1", uri=True)
                try:
                    row = conn.execute("SELECT draft FROM SessionTable WHERE username=?", (contact,)).fetchone()
                    if row and row[0] and str(row[0]).strip():
                        return True
                finally:
                    conn.close()
        return False

    def send_text(self, contact, text):
        if self.config.wechat.sender_all_existing_chats:
            self.allowed.update(self._eligible_contacts())
        if self.config.mode in {"shadow", "off"} or contact not in self.allowed:
            return False
        if self.config.resolve(self.config.paths.pause_file).exists():
            return False
        manual = getattr(self, "manual_approval_in_progress", False)
        if not text.strip() or (not manual and assess_risk(text).level != RiskLevel.low):
            return False
        if not manual and self._has_saved_human_draft(contact):
            return False
        context = self.context.get(contact, "manual")
        token = hashlib.sha256((contact + "|" + context + "|" + text).encode()).hexdigest()
        claims = self.reader.root / "send_claims"
        claims.mkdir(exist_ok=True)
        marker = claims / (token + ".json")
        if marker.exists():
            return False
        before, _, own_watermarks = self._outgoing_state(contact, text)
        origin = getattr(self, "context_origin", {}).get(contact)
        if not manual and origin and any(
                local_id > origin["local_id"] if rel == origin["source"] else ts > origin["created_at"]
                for rel, (local_id, ts) in own_watermarks.items()):
            return False  # The owner answered while the model was thinking.
        self.reader._load_names()
        name = self.reader.names.get(contact)
        if not name or list(self.reader.names.values()).count(name) != 1:
            raise RuntimeError("发送联系人名称不唯一，停止发送")
        self.check_sender_connection()
        self.wx.ChatWith(name, exact=True)
        current_name = None
        for _ in range(8):
            current_name = (self.wx.ChatInfo() or {}).get('chat_name')
            if current_name == name:
                break
            time.sleep(.25)
        if current_name != name:
            raise RuntimeError("当前会话与授权联系人不一致，停止发送")
        if self.config.resolve(self.config.paths.pause_file).exists():
            return False
        if not manual and self._has_saved_human_draft(contact):
            return False
        # Claim exclusively before GUI action: unknown outcomes must not be retried.
        with marker.open("x", encoding="utf-8") as claim:
            json.dump({"status": "attempting"}, claim)
        try:
            self.wx.SendMsg(msg=text, who=name, clear=True, exact=True)
            for _ in range(12):
                _, records, _ = self._outgoing_state(contact, text)
                if any(local_id > before.get(rel, 0) and acknowledged for rel, local_id, acknowledged in records):
                    marker.write_text('{"status":"verified_sent"}', encoding="utf-8")
                    return True
                time.sleep(1)
        except Exception:
            marker.write_text('{"status":"unknown_do_not_retry"}', encoding="utf-8")
            raise
        marker.write_text('{"status":"unknown_do_not_retry"}', encoding="utf-8")
        return False

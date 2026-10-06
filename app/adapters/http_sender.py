"""Authenticated local HTTP sending, with database receipts and no GUI fallback.

Inbound messages come from the existing authenticated database reader. This
does not claim to implement a native receive Hook or support new DLL offsets.
"""
from __future__ import annotations

from contextlib import closing

import hashlib
import http.client
import json
import sqlite3
import tempfile
import time
from pathlib import Path

import zstandard

from app.adapters.base import MessageAdapter
from app.adapters.history_reader import HistoryReadOnlyAdapter, authenticated_snapshot
from app.models import RiskLevel
from app.risk import assess_risk


class LocalAPIError(RuntimeError):
    pass


class LocalHookClient:
    def __init__(self, settings, token_path):
        self.settings, self.token_path = settings, token_path

    def post(self, route, payload):
        try:
            token = self.token_path.read_text(encoding="utf-8").strip()
        except OSError:
            raise LocalAPIError("尚未配置本机微信接口令牌") from None
        if not 32 <= len(token) < 512 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise LocalAPIError("本机接口令牌格式无效")
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.settings.port, timeout=self.settings.timeout_seconds)
        try:
            connection.request("POST", route, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                               {"Content-Type": "application/json", "Authorization": "Bearer " + token})
            response = connection.getresponse()
            if response.status != 200:
                raise LocalAPIError(f"本机微信接口拒绝请求（HTTP {response.status}）")
            data = response.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise LocalAPIError("本机微信接口响应超出限制")
            parsed = json.loads(data)
            if not isinstance(parsed, dict):
                raise LocalAPIError("本机微信接口响应格式无效")
            return parsed
        except (OSError, http.client.HTTPException, ValueError):
            # Do not leak payloads, account identifiers or authentication values.
            raise LocalAPIError("本机微信接口未连接或响应无效") from None
        finally:
            connection.close()

    def verify_owner(self, expected):
        profile = self.post("/GetSelfProfile", {})
        if not expected or profile.get("wxid") != expected:
            raise LocalAPIError("接口登录账号与已验证的聊天数据库不一致，停止发送")

    def submit(self, contact, text):
        result = self.post("/SendTextMsg", {"wxidorgid": contact, "msg": text})
        if type(result.get("ret")) is not int or result["ret"] != 0:
            raise LocalAPIError("本机微信接口未接受发送请求")


class HistoryHTTPSender(MessageAdapter):
    def __init__(self, config):
        self.config = config
        self.reader = HistoryReadOnlyAdapter(config)
        self.client = LocalHookClient(config.local_api, config.resolve(config.local_api.token_file))
        self.allowed = set(config.wechat.sender_allowed_contacts)
        self.context, self.context_origin = {}, {}
        self.last_send_skip_reason = "本机微信接口尚未验证"

    def doctor(self):
        report = self.reader.doctor()
        report.update(adapter="history_http_sender", gui_sending=False,
                      inbound_transport="authenticated_database_poll", outbound_transport="localhost_http",
                      sending_supported=False, automatic_sending_armed=False)
        try:
            self.client.verify_owner(self.reader.self_username)
            report.update(sending_supported=True, connection_verified=True,
                          automatic_sending_armed=self.config.mode in {"low_risk_auto", "full_auto"})
        except LocalAPIError as exc:
            report.update(ok=False, connection_verified=False, warning=str(exc))
        return report

    def poll(self):
        messages = self.reader.poll()
        for message in messages:
            self.context[message.contact] = message.external_id
            if message.raw_summary:
                self.context_origin[message.contact] = json.loads(message.raw_summary)
        return messages

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
                with closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
                    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                        continue
                    owner = conn.execute("SELECT rowid FROM Name2Id WHERE user_name=?", (self.reader.self_username,)).fetchone()
                    if not owner:
                        raise LocalAPIError("消息数据库未找到本人账号")
                    own_id = owner[0]
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
        return watermarks, records, own_watermarks

    def _has_saved_human_draft(self, contact):
        for rel, info in self.reader.keys.items():
            if not rel.replace("\\", "/").endswith("/session/session.db"):
                continue
            with tempfile.TemporaryDirectory(dir=self.reader.root) as tmp:
                snapshot, _ = authenticated_snapshot(info, Path(tmp))
                with closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
                    row = conn.execute("SELECT draft FROM SessionTable WHERE username=?", (contact,)).fetchone()
                    if row and row[0] and str(row[0]).strip():
                        return True
        return False

    def _skip(self, reason):
        self.last_send_skip_reason = reason
        return False

    def send_text(self, contact, text):
        if self.config.wechat.sender_all_existing_chats:
            self.allowed.update(self._eligible_contacts())
        if self.config.mode in {"shadow", "off"} or contact not in self.allowed:
            return self._skip("当前模式或联系人范围不允许发送")
        if contact.endswith("@chatroom") and not self.config.wechat.allow_groups:
            return self._skip("群聊自动发送未开启")
        if self.config.resolve(self.config.paths.pause_file).exists():
            return self._skip("后台已暂停")
        manual = getattr(self, "manual_approval_in_progress", False)
        if not text.strip() or len(text) > self.config.wechat.max_reply_chars:
            return self._skip("回复为空或过长")
        if not manual and assess_risk(text).level != RiskLevel.low:
            return self._skip("回复内容需要审核")
        origin = self.context_origin.get(contact)
        context = self.context.get(contact)
        if not manual and (not origin or not context):
            return self._skip("没有已读取的新消息上下文，禁止主动发送")
        if not manual and self._has_saved_human_draft(contact):
            return self._skip("该会话已有本人输入草稿，保留编辑现场")
        token = hashlib.sha256((contact + "|" + (context or "manual") + "|" + text).encode()).hexdigest()
        claims = self.reader.root / "send_claims"
        claims.mkdir(exist_ok=True)
        marker = claims / (token + ".json")
        if marker.exists():
            return self._skip("已有发送尝试记录，不重复发送")
        before, _, own_watermarks = self._outgoing_state(contact, text)
        if not manual and any(local_id > origin["local_id"] if rel == origin["source"]
                              else ts > origin["created_at"]
                              for rel, (local_id, ts) in own_watermarks.items()):
            return self._skip("生成期间本人已回复，取消旧回复")
        self.client.verify_owner(self.reader.self_username)
        if self.config.resolve(self.config.paths.pause_file).exists():
            return self._skip("后台已暂停")
        if not manual and self._has_saved_human_draft(contact):
            return self._skip("该会话已有本人输入草稿，保留编辑现场")
        try:
            with marker.open("x", encoding="utf-8") as claim:
                json.dump({"status": "attempting", "transport": "http"}, claim)
        except FileExistsError:
            return self._skip("另一进程已申请发送，不重复发送")
        try:
            self.client.submit(contact, text)
            deadline = time.monotonic() + self.config.local_api.receipt_timeout_seconds
            while time.monotonic() < deadline:
                _, records, _ = self._outgoing_state(contact, text)
                if any(local_id > before.get(rel, 0) and acknowledged for rel, local_id, acknowledged in records):
                    marker.write_text('{"status":"verified_sent","transport":"http"}', encoding="utf-8")
                    self.last_send_skip_reason = ""
                    return True
                time.sleep(.5)
        except Exception:
            marker.write_text('{"status":"unknown_do_not_retry","transport":"http"}', encoding="utf-8")
            raise
        marker.write_text('{"status":"unknown_do_not_retry","transport":"http"}', encoding="utf-8")
        return self._skip("接口已接受，但未核对到微信服务器确认；不自动重试")

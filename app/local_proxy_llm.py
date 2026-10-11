"""One-attempt local proxy draft requests with durable conversation ownership.

No browser history, account credentials, redirects, retries, or WeChat sender is
used here. A pending/unknown turn requires human review before its conversation
can accept another request, including after a process restart.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from pydantic import ValidationError

from app.llm import LLMError
from app.models import ReplyDecision, RiskLevel


_ENDPOINTS = {"deepseek": (19655, "deepseek-chat"), "doubao": (19656, "doubao")}
_MAX_IMAGE_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_IMAGE_BYTES = 4 * 1024 * 1024
_MAX_REQUEST_BYTES = 8 * 1024 * 1024
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_PAYLOAD_BYTES = 256 * 1024
_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _fail(code: str) -> LLMError:
    # Never include a credential, model response, URL, path or contact in errors.
    return LLMError(code, fallback_safe=False)


class LocalProxyReplyLLM:
    def __init__(self, config, lane: str):
        self.config = config
        self.lane = lane
        if lane not in _ENDPOINTS:
            raise _fail("local_proxy_invalid_lane")
        self.port, self.model = _ENDPOINTS[lane]
        settings = config.openai.local_proxies.get(lane)
        if settings is None or settings.enabled is not True:
            raise _fail("local_proxy_disabled")
        if settings.base_url != f"http://127.0.0.1:{self.port}/v1":
            raise _fail("local_proxy_invalid_endpoint")
        self.timeout = float(settings.timeout_seconds)
        if not 1 <= self.timeout <= 120:
            raise _fail("local_proxy_invalid_timeout")
        self.key_file = config.resolve(settings.api_key_file)
        self.session_db = config.resolve(settings.session_db)

    @contextmanager
    def _connect(self):
        self.session_db.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.session_db, timeout=3)
        connection.row_factory = sqlite3.Row
        try:
            connection.executescript("""
            CREATE TABLE IF NOT EXISTS local_proxy_sessions(
                lane TEXT NOT NULL, conversation_key TEXT NOT NULL,
                session_key TEXT NOT NULL, remote_id TEXT,
                state TEXT NOT NULL DEFAULT 'ready',
                PRIMARY KEY(lane,conversation_key), UNIQUE(lane,remote_id));
            CREATE TABLE IF NOT EXISTS local_proxy_turns(
                lane TEXT NOT NULL, conversation_key TEXT NOT NULL,
                request_key TEXT NOT NULL, status TEXT NOT NULL,
                error_code TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
                PRIMARY KEY(lane,conversation_key,request_key));
            CREATE INDEX IF NOT EXISTS idx_local_proxy_turn_identity
                ON local_proxy_turns(conversation_key,request_key);
            """)
            with connection:
                yield connection
        finally:
            connection.close()

    def _reserve(self, conversation_key, request_key):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("""SELECT status FROM local_proxy_turns
                WHERE conversation_key=? AND request_key=?""",
                (conversation_key, request_key)).fetchone()
            if previous:
                raise _fail("local_proxy_unknown_result" if previous[0] != "done"
                            else "local_proxy_duplicate_turn")
            # A shared DB also prevents a later local lane from continuing an
            # unresolved contact turn under a different model.
            blocked = db.execute("""SELECT 1 FROM local_proxy_sessions
                WHERE conversation_key=? AND state!='ready' LIMIT 1""",
                (conversation_key,)).fetchone()
            if blocked:
                raise _fail("local_proxy_unknown_session")
            db.execute("""INSERT OR IGNORE INTO local_proxy_sessions
                (lane,conversation_key,session_key) VALUES(?,?,?)""",
                (self.lane, conversation_key, "wechat-proxy-" + uuid.uuid4().hex))
            session = dict(db.execute("""SELECT session_key,remote_id FROM local_proxy_sessions
                WHERE lane=? AND conversation_key=?""", (self.lane, conversation_key)).fetchone())
            if not isinstance(session["session_key"], str) or not re.fullmatch(r"wechat-proxy-[a-f0-9]{32}", session["session_key"]):
                raise _fail("local_proxy_state_error")
            if session["remote_id"] is not None and (not isinstance(session["remote_id"], str) or not _ID.fullmatch(session["remote_id"])):
                raise _fail("local_proxy_state_error")
            db.execute("""INSERT INTO local_proxy_turns
                (lane,conversation_key,request_key,status,created) VALUES(?,?,?,'pending',?)""",
                (self.lane, conversation_key, request_key, time.time()))
            db.execute("UPDATE local_proxy_sessions SET state='pending' WHERE lane=? AND conversation_key=?",
                       (self.lane, conversation_key))
            return session

    def _unknown(self, conversation_key, request_key, code):
        with self._connect() as db:
            db.execute("""UPDATE local_proxy_turns SET status='unknown',error_code=?
                WHERE lane=? AND conversation_key=? AND request_key=? AND status='pending'""",
                (code, self.lane, conversation_key, request_key))
            db.execute("UPDATE local_proxy_sessions SET state='unknown' WHERE lane=? AND conversation_key=?",
                       (self.lane, conversation_key))

    def _complete(self, conversation_key, request_key, remote_id):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if remote_id:
                owner = db.execute("""SELECT conversation_key FROM local_proxy_sessions
                    WHERE lane=? AND remote_id=?""", (self.lane, remote_id)).fetchone()
                if owner and owner[0] != conversation_key:
                    raise _fail("local_proxy_conversation_collision")
            updated = db.execute("""UPDATE local_proxy_turns SET status='done'
                WHERE lane=? AND conversation_key=? AND request_key=? AND status='pending'""",
                (self.lane, conversation_key, request_key)).rowcount
            if updated != 1:
                raise _fail("local_proxy_state_conflict")
            db.execute("""UPDATE local_proxy_sessions SET state='ready',remote_id=?
                WHERE lane=? AND conversation_key=?""", (remote_id, self.lane, conversation_key))

    def _read_key(self):
        try:
            with self.key_file.open("rb") as file:
                raw = file.read(4097)
            key = raw.decode("utf-8-sig").strip()
            if len(raw) > 4096 or not key or not re.fullmatch(r"[!-~]{1,512}", key):
                raise ValueError
            return key
        except (OSError, UnicodeError, ValueError):
            raise _fail("local_proxy_key_unavailable") from None

    def _images(self, raw_paths):
        if not isinstance(raw_paths, list) or len(raw_paths) > 3:
            raise _fail("local_proxy_invalid_images")
        if raw_paths and self.lane != "doubao":
            raise _fail("local_proxy_wrong_media_lane")
        roots = [(self.config.resolve(self.config.paths.history_reader) / name).resolve()
                 for name in ("sticker_assets", "image_assets")]
        if raw_paths:
            from PIL import Image
        parts = []
        total = 0
        for raw_path in raw_paths:
            try:
                if not isinstance(raw_path, str):
                    raise ValueError
                path = self.config.resolve(raw_path).resolve(strict=True)
                if path.suffix.lower() != ".png" or not any(path.is_relative_to(root) for root in roots):
                    raise ValueError
                with path.open("rb") as file:
                    data = file.read(_MAX_IMAGE_BYTES + 1)
                if not data or len(data) > _MAX_IMAGE_BYTES:
                    raise ValueError
                total += len(data)
                if total > _MAX_TOTAL_IMAGE_BYTES:
                    raise ValueError
                with Image.open(io.BytesIO(data)) as image:
                    if image.format != "PNG" or image.width * image.height > 16_777_216:
                        raise ValueError
                    image.verify()
                parts.append({"type": "image_url", "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(data).decode("ascii")}})
            except (OSError, ValueError, TypeError, SyntaxError, Image.DecompressionBombError):
                raise _fail("local_proxy_invalid_images") from None
        return parts

    def _post(self, body, key):
        # HTTPConnection has no proxy-environment handling, redirect following,
        # SDK retries or account failover. Only one POST is ever issued.
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        deadline = time.monotonic() + self.timeout
        try:
            connection.request("POST", "/v1/chat/completions", body=body, headers={
                "Authorization": "Bearer " + key, "Content-Type": "application/json",
                "Content-Length": str(len(body)), "Connection": "close"})
            if connection.sock:
                connection.sock.settimeout(max(.01, deadline - time.monotonic()))
            active_socket = connection.sock
            response = connection.getresponse()
            if response.status != 200:
                raise _fail("local_proxy_http_error")
            chunks, total = [], 0
            while total <= _MAX_RESPONSE_BYTES:
                if response.isclosed():
                    break
                if time.monotonic() >= deadline:
                    raise _fail("local_proxy_timeout")
                # Connection: close detaches the connection's socket after
                # headers; its file reference remains owned by the response.
                if active_socket and active_socket.fileno() >= 0:
                    active_socket.settimeout(max(.01, deadline - time.monotonic()))
                chunk = response.read1(min(65536, _MAX_RESPONSE_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk); total += len(chunk)
            if total > _MAX_RESPONSE_BYTES:
                raise _fail("local_proxy_response_too_large")
            return json.loads(b"".join(chunks))
        finally:
            connection.close()

    @staticmethod
    def _decision(response, mode):
        try:
            choices = response["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError
            choice = choices[0]
            if choice.get("finish_reason") != "stop" or choice["message"].get("role") != "assistant" or choice["message"].get("tool_calls"):
                raise ValueError
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise ValueError
            raw = json.loads(content)
            if not isinstance(raw, dict):
                raise ValueError
            if mode == "direct":
                raw.pop("candidates", None); raw.pop("selected_candidate", None)
            decision = ReplyDecision.model_validate_json(json.dumps(raw), strict=True)
            if mode == "abc" and decision.action != "ignore":
                candidates = decision.candidates or []
                normalized = {re.sub(r"[\s，,。.!！?？…~～]+", "", item.reply).casefold() for item in candidates}
                if len(candidates) != 3 or {item.label for item in candidates} != {"A", "B", "C"} or len(normalized) != 3:
                    raise ValueError
            return decision
        except (KeyError, IndexError, TypeError, ValueError, ValidationError):
            raise _fail("local_proxy_invalid_decision") from None

    def decide(self, system_prompt: str, user_payload: str, risk: RiskLevel) -> ReplyDecision:
        try:
            if not isinstance(user_payload, str) or len(user_payload.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
                raise ValueError
            payload = json.loads(user_payload)
            conversation_key = payload["conversation_key"]
            external_id = payload.get("external_id") or payload["incoming"].get("external_id")
            mode = payload.get("reply_selection", {}).get("mode", "abc")
            if not isinstance(conversation_key, str) or not re.fullmatch(r"[a-f0-9]{64}", conversation_key):
                raise ValueError
            if not isinstance(external_id, str) or not external_id.strip() or len(external_id) > 512 or mode not in {"abc", "direct"}:
                raise ValueError
            images = self._images(payload.pop("__media_paths", []))
            payload.pop("__is_local_test", None)
            request_key = hashlib.sha256(external_id.encode("utf-8")).hexdigest()
            schema = json.dumps(ReplyDecision.model_json_schema(), ensure_ascii=False)
            messages = [{"role": "system", "content": system_prompt + "\n仅依据本轮payload核对事实；旧网页对话和模型草稿不能当作本人事实。只返回符合以下完整结构的JSON，不要普通文本或代码围栏：\n" + schema},
                        {"role": "user", "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}, *images] if images else json.dumps(payload, ensure_ascii=False)}]
            base_body = {"model": self.model, "stream": False, "messages": messages}
            # Validate serialization and the largest possible routing metadata
            # before a durable pending claim is made.
            preflight_body = {**base_body, **({"user": "wechat-proxy-" + "f" * 32}
                if self.lane == "deepseek" else {"conversation_id": "x" * 128})}
            if len(json.dumps(preflight_body, ensure_ascii=False).encode("utf-8")) > _MAX_REQUEST_BYTES:
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            raise _fail("local_proxy_invalid_payload") from None
        key = self._read_key()
        try:
            session = self._reserve(conversation_key, request_key)
        except (OSError, sqlite3.Error):
            raise _fail("local_proxy_state_error") from None
        try:
            body = dict(base_body)
            if self.lane == "deepseek":
                body["user"] = session["session_key"]
            else:
                body["conversation_id"] = session["remote_id"] or ""
            response = self._post(json.dumps(body, ensure_ascii=False).encode("utf-8"), key)
            decision = self._decision(response, mode)
            remote_id = None
            if self.lane == "doubao":
                remote_id = response.get("conversation_id")
                if not isinstance(remote_id, str) or not _ID.fullmatch(remote_id):
                    raise _fail("local_proxy_missing_conversation")
                if session["remote_id"] and remote_id != session["remote_id"]:
                    raise _fail("local_proxy_conversation_changed")
            self._complete(conversation_key, request_key, remote_id)
            return decision
        except Exception as exc:
            code = str(exc) if isinstance(exc, LLMError) and re.fullmatch(r"local_proxy_[a-z_]+", str(exc)) else "local_proxy_request_unknown"
            try:
                self._unknown(conversation_key, request_key, code)
            except (OSError, sqlite3.Error):
                pass  # The committed pending claim still blocks later turns.
            raise _fail(code) from None

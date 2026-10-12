"""One-attempt local proxy draft requests with durable conversation ownership.

No browser history, account credentials, redirects, retries, or WeChat sender is
used here. Pending turns block their contact. Terminal unknown model drafts are
never retried; a later message may rebuild an isolated session from verified
local context while preserving the old turn and conversation bindings.
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


_ENDPOINTS = {"deepseek": (19655, "deepseek-chat"), "doubao": (19656, "doubao"),
              "glm": (19659, "chatglm-web")}
_MAX_IMAGE_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_IMAGE_BYTES = 4 * 1024 * 1024
_MAX_REQUEST_BYTES = 8 * 1024 * 1024
_MAX_GLM_REQUEST_BYTES = 256 * 1024
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_PAYLOAD_BYTES = 256 * 1024
_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_RECOVERY_BACKOFF_SECONDS = 30.0
_MAX_RECOVERY_BACKOFF_SECONDS = 300.0
_TOOL_REVIEW_REASON = "上游返回了工具元数据，本轮草稿须先核对本轮本机事实后再决定是否发送。"


def _fail(code: str) -> LLMError:
    # Never include a credential, model response, URL, path or contact in errors.
    return LLMError(code, fallback_safe=False)


class _NoSubmission(Exception):
    """The TCP connection failed before HTTP request bytes could be sent."""


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
            CREATE TABLE IF NOT EXISTS local_proxy_session_history(
                lane TEXT NOT NULL, conversation_key TEXT NOT NULL,
                generation INTEGER NOT NULL, session_key TEXT NOT NULL,
                remote_id TEXT, state TEXT NOT NULL, quarantined_at REAL NOT NULL,
                PRIMARY KEY(lane,conversation_key,generation), UNIQUE(lane,session_key),
                UNIQUE(lane,remote_id));
            CREATE TABLE IF NOT EXISTS local_proxy_backoff(
                lane TEXT NOT NULL, conversation_key TEXT NOT NULL, failures INTEGER NOT NULL,
                retry_after REAL NOT NULL, last_failure REAL NOT NULL,
                PRIMARY KEY(lane,conversation_key));
            CREATE TABLE IF NOT EXISTS local_proxy_quarantined_ids(
                lane TEXT NOT NULL, remote_id TEXT NOT NULL, conversation_key TEXT NOT NULL,
                generation INTEGER NOT NULL, request_key TEXT NOT NULL, quarantined_at REAL NOT NULL,
                PRIMARY KEY(lane,remote_id));
            """)
            # Existing production rows are generation zero. Serialize additive
            # migration so concurrent first requests cannot race ALTER TABLE.
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                for table, additions in (
                    ("local_proxy_sessions", {"generation": "INTEGER NOT NULL DEFAULT 0",
                                              "rebuild_required": "INTEGER NOT NULL DEFAULT 0"}),
                    ("local_proxy_turns", {"generation": "INTEGER NOT NULL DEFAULT 0"}),
                ):
                    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                    for name, definition in additions.items():
                        if name not in columns:
                            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            with connection:
                yield connection
        finally:
            connection.close()

    def _reserve(self, conversation_key, request_key, recovery_context):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("""SELECT status FROM local_proxy_turns
                WHERE conversation_key=? AND request_key=?""",
                (conversation_key, request_key)).fetchone()
            if previous:
                code = ("local_proxy_duplicate_turn" if previous[0] == "done" else
                        "local_proxy_not_submitted" if previous[0] == "not_submitted" else
                        "local_proxy_unknown_result")
                raise _fail(code)
            # Even a stale pending claim has no confirmed terminal outcome.
            # Recovery never expires it or races an in-flight request in a lane.
            blocked = db.execute("""SELECT 1 FROM local_proxy_turns
                WHERE conversation_key=? AND status='pending' LIMIT 1""",
                (conversation_key,)).fetchone()
            sessions = [dict(row) for row in db.execute("""SELECT * FROM local_proxy_sessions
                WHERE conversation_key=?""", (conversation_key,))]
            if blocked or any(row["state"] == "pending" for row in sessions):
                raise _fail("local_proxy_unknown_session")
            if any(row["state"] not in {"ready", "unknown"} for row in sessions):
                raise _fail("local_proxy_state_error")
            if any(type(row["generation"]) is not int or row["generation"] < 0 for row in sessions):
                raise _fail("local_proxy_state_error")
            generation = max((row["generation"] for row in sessions), default=0)
            for row in sessions:
                if row["state"] == "unknown" and not db.execute("""SELECT 1 FROM local_proxy_turns
                    WHERE lane=? AND conversation_key=? AND generation=? AND status='unknown' LIMIT 1""",
                    (row["lane"], conversation_key, row["generation"])).fetchone():
                    raise _fail("local_proxy_state_error")
            target = next((row for row in sessions if row["lane"] == self.lane), None)
            quarantine = any(row["state"] == "unknown" for row in sessions)
            needs_context = quarantine or bool(target and target["rebuild_required"]) or (not target and generation > 0)
            if needs_context and recovery_context != "ready":
                raise _fail(recovery_context)
            now = time.time()
            backoff = db.execute("""SELECT retry_after FROM local_proxy_backoff
                WHERE lane=? AND conversation_key=?""", (self.lane, conversation_key)).fetchone()
            if backoff and backoff[0] > now:
                raise _fail("local_proxy_cooldown")
            if quarantine:
                # All old lane contexts are sealed together. An unknown draft
                # is not a WeChat send, and is not replayed into the replacement.
                generation += 1
                for row in sessions:
                    db.execute("""INSERT INTO local_proxy_session_history
                        (lane,conversation_key,generation,session_key,remote_id,state,quarantined_at)
                        VALUES(?,?,?,?,?,?,?)""", (row["lane"], conversation_key, row["generation"],
                        row["session_key"], row["remote_id"], row["state"], now))
                    db.execute("""UPDATE local_proxy_sessions
                        SET session_key=?,remote_id=NULL,state='ready',generation=?,rebuild_required=1
                        WHERE lane=? AND conversation_key=?""",
                        ("wechat-proxy-" + uuid.uuid4().hex, generation, row["lane"], conversation_key))
            db.execute("""INSERT OR IGNORE INTO local_proxy_sessions
                (lane,conversation_key,session_key,generation,rebuild_required) VALUES(?,?,?,?,?)""",
                (self.lane, conversation_key, "wechat-proxy-" + uuid.uuid4().hex, generation, int(generation > 0)))
            session = dict(db.execute("""SELECT session_key,remote_id,generation,rebuild_required FROM local_proxy_sessions
                WHERE lane=? AND conversation_key=?""", (self.lane, conversation_key)).fetchone())
            if not isinstance(session["session_key"], str) or not re.fullmatch(r"wechat-proxy-[a-f0-9]{32}", session["session_key"]):
                raise _fail("local_proxy_state_error")
            if session["remote_id"] is not None and (not isinstance(session["remote_id"], str) or not _ID.fullmatch(session["remote_id"])):
                raise _fail("local_proxy_state_error")
            db.execute("""INSERT INTO local_proxy_turns
                (lane,conversation_key,request_key,status,created,generation) VALUES(?,?,?,'pending',?,?)""",
                (self.lane, conversation_key, request_key, now, session["generation"]))
            db.execute("UPDATE local_proxy_sessions SET state='pending' WHERE lane=? AND conversation_key=?",
                       (self.lane, conversation_key))
            return {**session, "started": now}

    def _not_submitted(self, conversation_key, request_key):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute("""UPDATE local_proxy_turns
                SET status='not_submitted',error_code='local_proxy_not_submitted'
                WHERE lane=? AND conversation_key=? AND request_key=? AND status='pending'""",
                (self.lane, conversation_key, request_key)).rowcount
            if updated != 1:
                raise _fail("local_proxy_state_conflict")
            updated = db.execute("""UPDATE local_proxy_sessions SET state='ready'
                WHERE lane=? AND conversation_key=? AND state='pending'""",
                (self.lane, conversation_key)).rowcount
            if updated != 1:
                raise _fail("local_proxy_state_conflict")

    def _unknown(self, conversation_key, request_key, code, observed_remote_id=None):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute("""UPDATE local_proxy_turns SET status='unknown',error_code=?
                WHERE lane=? AND conversation_key=? AND request_key=? AND status='pending'""",
                (code, self.lane, conversation_key, request_key)).rowcount
            if updated != 1:
                raise _fail("local_proxy_state_conflict")
            session = db.execute("""SELECT generation,remote_id FROM local_proxy_sessions
                WHERE lane=? AND conversation_key=? AND state='pending'""",
                (self.lane, conversation_key)).fetchone()
            if session is None:
                raise _fail("local_proxy_state_conflict")
            now = time.time()
            # An unexpected new ID and the prior known ID can both refer to
            # untrusted upstream history. Seal every observed unowned ID, without
            # replacing the current binding or claiming another contact's ID.
            for remote_id in {session["remote_id"], observed_remote_id} - {None}:
                owner = db.execute("""SELECT conversation_key FROM local_proxy_sessions
                    WHERE lane=? AND remote_id=?""", (self.lane, remote_id)).fetchone()
                archived = db.execute("""SELECT conversation_key FROM local_proxy_session_history
                    WHERE lane=? AND remote_id=?""", (self.lane, remote_id)).fetchone()
                quarantined = db.execute("""SELECT conversation_key FROM local_proxy_quarantined_ids
                    WHERE lane=? AND remote_id=?""", (self.lane, remote_id)).fetchone()
                if any(row and row[0] != conversation_key for row in (owner, archived, quarantined)):
                    continue
                db.execute("""INSERT OR IGNORE INTO local_proxy_quarantined_ids
                    (lane,remote_id,conversation_key,generation,request_key,quarantined_at)
                    VALUES(?,?,?,?,?,?)""", (self.lane, remote_id, conversation_key,
                    session["generation"], request_key, now))
                if remote_id == observed_remote_id and not archived and not quarantined:
                    # A malformed first decision can still establish the new
                    # session's observed ID, which will be archived on recovery.
                    db.execute("""UPDATE local_proxy_sessions SET remote_id=?
                        WHERE lane=? AND conversation_key=? AND state='pending'
                        AND (remote_id IS NULL OR remote_id=?)""",
                        (remote_id, self.lane, conversation_key, remote_id))
            updated = db.execute("""UPDATE local_proxy_sessions SET state='unknown'
                WHERE lane=? AND conversation_key=? AND state='pending'""",
                (self.lane, conversation_key)).rowcount
            if updated != 1:
                raise _fail("local_proxy_state_conflict")
            previous = db.execute("""SELECT failures FROM local_proxy_backoff
                WHERE lane=? AND conversation_key=?""", (self.lane, conversation_key)).fetchone()
            failures = min(32, (previous[0] if previous else 0) + 1)
            delay = min(_MAX_RECOVERY_BACKOFF_SECONDS, _RECOVERY_BACKOFF_SECONDS * 2 ** min(failures - 1, 4))
            db.execute("""INSERT OR REPLACE INTO local_proxy_backoff
                (lane,conversation_key,failures,retry_after,last_failure) VALUES(?,?,?,?,?)""",
                (self.lane, conversation_key, failures, now + delay, now))

    def _complete(self, conversation_key, request_key, remote_id, started):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if remote_id:
                owner = db.execute("""SELECT conversation_key FROM local_proxy_sessions
                    WHERE lane=? AND remote_id=?""", (self.lane, remote_id)).fetchone()
                if owner and owner[0] != conversation_key:
                    raise _fail("local_proxy_conversation_collision")
                archived = db.execute("""SELECT conversation_key FROM local_proxy_session_history
                    WHERE lane=? AND remote_id=? UNION ALL
                    SELECT conversation_key FROM local_proxy_quarantined_ids
                    WHERE lane=? AND remote_id=?""", (self.lane, remote_id, self.lane, remote_id)).fetchone()
                if archived:
                    raise _fail("local_proxy_quarantined_conversation" if archived[0] == conversation_key
                                else "local_proxy_conversation_collision")
            updated = db.execute("""UPDATE local_proxy_turns SET status='done'
                WHERE lane=? AND conversation_key=? AND request_key=? AND status='pending'""",
                (self.lane, conversation_key, request_key)).rowcount
            if updated != 1:
                raise _fail("local_proxy_state_conflict")
            updated = db.execute("""UPDATE local_proxy_sessions SET state='ready',remote_id=?,rebuild_required=0
                WHERE lane=? AND conversation_key=? AND state='pending'""", (remote_id, self.lane, conversation_key)).rowcount
            if updated != 1:
                raise _fail("local_proxy_state_conflict")
            # A request started before a later failure must not clear its delay.
            db.execute("""DELETE FROM local_proxy_backoff
                WHERE lane=? AND conversation_key=? AND last_failure<=?""", (self.lane, conversation_key, started))

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
            try:
                connection.connect()
            except Exception:
                # HTTPConnection.connect only opens the local TCP socket. No
                # request bytes have been handed to the transport yet.
                raise _NoSubmission from None
            if connection.sock:
                connection.sock.settimeout(max(.01, deadline - time.monotonic()))
            connection.request("POST", "/v1/chat/completions", body=body, headers={
                "Authorization": "Bearer " + key, "Content-Type": "application/json",
                "Content-Length": str(len(body)), "Connection": "close"})
            if connection.sock:
                connection.sock.settimeout(max(.01, deadline - time.monotonic()))
            active_socket = connection.sock
            response = connection.getresponse()
            if response.status != 200:
                # Status is sufficient for auth/rate-limit/upstream diagnosis;
                # do not read or echo an error body, URL or authentication data.
                code = f"local_proxy_http_{response.status}" if 100 <= response.status <= 599 else "local_proxy_http_error"
                raise _fail(code)
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
            fenced = re.fullmatch(r"\s*```json[ \t]*\r?\n(.*?)\r?\n```\s*", content, re.DOTALL)
            if fenced:
                content = fenced[1]
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

    @staticmethod
    def _recovery_context(payload, evidence, external_id, has_images):
        """Check the private marker made by the local PromptBuilder, not the model.

        The caller must establish completeness and media dependencies from local
        WeChat records. Lists or a model's own assertion alone never opt in.
        """
        required = "local_proxy_recovery_context_required"
        if (not isinstance(evidence, dict) or type(evidence.get("version")) is not int
                or evidence["version"] != 1 or evidence.get("source") != "local_wechat"
                or evidence.get("conversation_key") != payload["conversation_key"]
                or evidence.get("external_id") != external_id or evidence.get("complete") is not True):
            return required
        if evidence.get("unresolved_media") is not False:
            return "local_proxy_recovery_media_unresolved"
        incoming = payload.get("incoming", {})
        kind = incoming.get("message_type")
        if kind not in {"text", "image", "sticker"} or not isinstance(incoming.get("content"), str) or not incoming["content"].strip():
            return required
        if kind != "text" and not has_images:
            return "local_proxy_recovery_media_unresolved"
        recent = payload.get("recent_messages")
        personal = payload.get("current_personal_conversation")
        if not isinstance(recent, list) or not recent or not isinstance(personal, list):
            return required
        for rows, type_field in ((recent, "message_type"), (personal, "kind")):
            if any(not isinstance(row, dict) or row.get("direction") not in {"in", "out"}
                   or not isinstance(row.get("content"), str) or not isinstance(row.get(type_field), str)
                   for row in rows):
                return required
        if not any(row["direction"] == "in" and row["content"] == incoming["content"]
                   and row["message_type"] == kind for row in recent):
            return required
        return "ready"

    def decide(self, system_prompt: str, user_payload: str, risk: RiskLevel) -> ReplyDecision:
        try:
            if not isinstance(user_payload, str) or len(user_payload.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
                raise ValueError
            payload = json.loads(user_payload)
            conversation_key = payload["conversation_key"]
            external_id = payload.get("external_id") or payload["incoming"].get("external_id")
            incoming_external_id = payload.get("incoming", {}).get("external_id")
            if incoming_external_id is not None and incoming_external_id != external_id:
                raise ValueError
            mode = payload.get("reply_selection", {}).get("mode", "abc")
            if not isinstance(conversation_key, str) or not re.fullmatch(r"[a-f0-9]{64}", conversation_key):
                raise ValueError
            if not isinstance(external_id, str) or not external_id.strip() or len(external_id) > 512 or mode not in {"abc", "direct"}:
                raise ValueError
            if self.lane == "glm" and (mode != "abc" or payload.get("incoming", {}).get("message_type", "text") != "text"):
                raise ValueError
            images = self._images(payload.pop("__media_paths", []))
            payload.pop("__is_local_test", None)
            evidence = payload.pop("__local_proxy_recovery_context", None)
            recovery_context = self._recovery_context(payload, evidence, external_id, bool(images))
            request_key = hashlib.sha256(external_id.encode("utf-8")).hexdigest()
            schema = json.dumps(ReplyDecision.model_json_schema(), ensure_ascii=False)
            def make_body(value, recovery=False):
                prompt = system_prompt + "\n仅依据本轮payload核对事实；旧网页对话和模型草稿不能当作本人事实。"
                if recovery:
                    prompt += "本轮是隔离旧模型会话后建立的新会话，只根据本轮本机微信记录重建语境；旧模型草稿不表示已向微信发送。缺少必需事实、指代不明或依赖未识别媒体时必须review，不猜测、不补答旧消息。"
                prompt += "只返回符合以下完整结构的JSON，不要普通文本或代码围栏：\n" + schema
                text = json.dumps(value, ensure_ascii=False)
                return {"model": self.model, "stream": False, "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": [{"type": "text", "text": text}, *images] if images else text}]}
            base_body = make_body(payload)
            # Unsent suggestions and old model summaries are not authoritative
            # WeChat history and must not reconstruct a quarantined conversation.
            rebuilt_payload = {key: value for key, value in payload.items()
                               if key not in {"optional_model_candidates", "contact_memories",
                                              "historical_conversation_memory", "style_examples", "owner_role_profile"}}
            recovery_body = make_body(rebuilt_payload, recovery=True)
            # Validate serialization and the largest possible routing metadata
            # before a durable pending claim is made.
            for candidate_body in (base_body, recovery_body):
                metadata = ({"user": "f" * 64, "request_id": request_key} if self.lane == "glm" else
                            {"user": "wechat-proxy-" + "f" * 32} if self.lane == "deepseek" else
                            {"conversation_id": "x" * 128})
                preflight_body = {**candidate_body, **metadata}
                request_limit = _MAX_GLM_REQUEST_BYTES if self.lane == "glm" else _MAX_REQUEST_BYTES
                if len(json.dumps(preflight_body, ensure_ascii=False).encode("utf-8")) > request_limit:
                    raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            raise _fail("local_proxy_invalid_payload") from None
        key = self._read_key()
        try:
            session = self._reserve(conversation_key, request_key, recovery_context)
        except (OSError, sqlite3.Error):
            raise _fail("local_proxy_state_error") from None
        observed_remote_id = None
        try:
            body = dict(recovery_body if session["rebuild_required"] else base_body)
            if self.lane == "deepseek":
                body["user"] = session["session_key"]
            elif self.lane == "glm":
                # Stable across ordinary turns; a verified new-message recovery
                # changes the generation while preserving old IDs and claims.
                body["user"] = hashlib.sha256(session["session_key"].encode()).hexdigest()
                body["request_id"] = request_key
            else:
                body["conversation_id"] = session["remote_id"] or ""
            response = self._post(json.dumps(body, ensure_ascii=False).encode("utf-8"), key)
            remote_id = None
            if self.lane in {"doubao", "glm"}:
                remote_id = response.get("conversation_id")
                if not isinstance(remote_id, str) or not _ID.fullmatch(remote_id):
                    raise _fail("local_proxy_missing_conversation")
                observed_remote_id = remote_id
                if session["remote_id"] and remote_id != session["remote_id"]:
                    raise _fail("local_proxy_conversation_changed")
            decision = self._decision(response, mode)
            if self.lane == "glm":
                tool_metadata_seen = response.get("tool_metadata_seen")
                if type(tool_metadata_seen) is not bool:
                    raise _fail("local_proxy_invalid_tool_metadata")
                # Even ignore must contain ABC in this text-only GLM contract.
                candidates = decision.candidates or []
                normalized = {re.sub(r"[\s，,。.!！?？…~～]+", "", item.reply).casefold() for item in candidates}
                if len(candidates) != 3 or {item.label for item in candidates} != {"A", "B", "C"} or len(normalized) != 3:
                    raise _fail("local_proxy_invalid_decision")
                if tool_metadata_seen:
                    decision = decision.model_copy(update={"action": "review", "confidence": 0.0,
                        "holding_reply": None, "reason": _TOOL_REVIEW_REASON})
                    decision._tool_metadata_seen = True
                if session["rebuild_required"]:
                    decision = decision.model_copy(update={"action": "review", "confidence": 0.0,
                        "holding_reply": None,
                        "reason": (decision.reason + "；GLM 未知会话已用当前本机记录建立新代次，首次恢复草稿须审核").strip("；")})
                    decision._manual_review_required = True
            self._complete(conversation_key, request_key, remote_id, session["started"])
            return decision
        except _NoSubmission:
            try:
                self._not_submitted(conversation_key, request_key)
            except (OSError, sqlite3.Error, LLMError):
                # A pending claim remains durable if the release cannot be
                # confirmed. Never retry this same external message.
                raise _fail("local_proxy_state_error") from None
            raise _fail("local_proxy_not_submitted") from None
        except Exception as exc:
            code = str(exc) if isinstance(exc, LLMError) and re.fullmatch(r"local_proxy_(?:[a-z_]+|http_[1-5][0-9]{2})", str(exc)) else "local_proxy_request_unknown"
            try:
                self._unknown(conversation_key, request_key, code, observed_remote_id)
            except (OSError, sqlite3.Error, LLMError):
                pass  # The committed pending claim still blocks later turns.
            raise _fail(code) from None

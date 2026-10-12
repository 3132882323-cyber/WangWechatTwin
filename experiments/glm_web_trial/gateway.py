"""Isolated normal-account GLM draft gateway; never connected to a sender.

Protocol reference only: forsakenkraken/glm2api@4dd06ee61b107d6990ae60e41d445f84afcde6c6.
This implementation does not import or execute that package. Importing this
module does not read credentials, create keys, start a server, or call a model.
"""
from __future__ import annotations

import asyncio
import codecs
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import time
from typing import Callable
import uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


TRIAL = Path(__file__).resolve().parent
AUTH_PATH = TRIAL / "auth" / "refresh.json"
KEY_PATH = TRIAL / "local-key.txt"
LEDGER_PATH = TRIAL / "state" / "gateway.sqlite3"
PUBLIC = TRIAL.parent.parent
if str(PUBLIC) not in sys.path:
    sys.path.insert(0, str(PUBLIC))
from app.local_proxy_llm import LocalProxyReplyLLM
from app.models import ReplyDecision


HOST, PORT = "127.0.0.1", 19659
MODEL = "web_assistant_unverified"
REFRESH_URL = "https://chatglm.cn/chatglm/user-api/user/refresh"
GENERATE_URL = "https://chatglm.cn/chatglm/backend-api/assistant/stream"
AUTH_SOURCE = "authorized_single_glm_tab"
MAX_BODY = 256 * 1024
MAX_STREAM = 1024 * 1024
MAX_SECONDS = 90
_CONTACT = re.compile(r"[a-f0-9]{64}\Z")
_REQUEST = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_REMOTE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_OUTPUT_KINDS = {
    "code": "code", "execution_output": "code", "markdown": "markdown", "md": "markdown",
    "tool": "tool", "tool_call": "tool", "tool_calls": "tool", "function_call": "tool",
    "search": "search", "search_result": "search", "search_results": "search", "web_search": "search",
    "image": "image", "image_url": "image", "audio": "audio", "audio_url": "audio", "voice": "audio",
}
_DIAGNOSTIC_FIELDS = ("type", "text", "code", "markdown", "content", "tool_calls", "image", "audio", "language", "value")
_DIAGNOSTIC_CATEGORIES = frozenset({"code", "markdown", "tool", "search", "image", "audio", "unknown"})
TOOL_REVIEW_REASON = "上游返回了工具元数据，本轮草稿须先核对本轮本机事实后再决定是否发送。"


class GatewayFailure(Exception):
    """Only fixed diagnostic codes may leave the gateway."""

    def __init__(self, code: str, status: int = 502, diagnostic: dict | None = None):
        self.code, self.status = code, status
        self.diagnostic = _safe_diagnostic(diagnostic)
        super().__init__(code)


def _safe_diagnostic(value):
    """Allow only fixed categories and Boolean shapes, never upstream values."""
    if (not isinstance(value, dict) or not isinstance(value.get("category"), str)
            or value["category"] not in _DIAGNOSTIC_CATEGORIES):
        return None
    shape = value.get("shape")
    if not isinstance(shape, dict):
        return None
    allowed = {prefix + name for name in _DIAGNOSTIC_FIELDS for prefix in ("has_",)}
    allowed |= {name + suffix for name in _DIAGNOSTIC_FIELDS for suffix in ("_is_string", "_is_list", "_is_dict")}
    return {"category": value["category"], "shape": {key: item for key, item in shape.items()
                                                        if key in allowed and type(item) is bool}}


def _unsupported_output(item: dict):
    raw_kind = item.get("type")
    category = _OUTPUT_KINDS.get(raw_kind, "unknown") if isinstance(raw_kind, str) else "unknown"
    shape = {}
    for name in _DIAGNOSTIC_FIELDS:
        shape["has_" + name] = name in item
        shape[name + "_is_string"] = isinstance(item.get(name), str)
        shape[name + "_is_list"] = isinstance(item.get(name), list)
        shape[name + "_is_dict"] = isinstance(item.get(name), dict)
    return GatewayFailure("non_text_output_" + category, diagnostic={"category": category, "shape": shape})


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    conversation_key TEXT PRIMARY KEY,
                    remote_id TEXT UNIQUE,
                    state TEXT NOT NULL CHECK(state IN ('ready','pending','unknown'))
                );
                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY,
                    conversation_key TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','done','unknown')),
                    claimed_at REAL NOT NULL,
                    completed_at REAL,
                    error_code TEXT,
                    tool_metadata_seen INTEGER NOT NULL DEFAULT 0 CHECK(tool_metadata_seen IN (0,1))
                );
                CREATE TABLE IF NOT EXISTS remote_ids (
                    remote_id TEXT PRIMARY KEY,
                    conversation_key TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            db.execute("BEGIN IMMEDIATE")
            if "tool_metadata_seen" not in {row[1] for row in db.execute("PRAGMA table_info(requests)")}:
                db.execute("ALTER TABLE requests ADD COLUMN tool_metadata_seen INTEGER NOT NULL DEFAULT 0 CHECK(tool_metadata_seen IN (0,1))")
            db.commit()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=2, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def claim(self, contact: str, request_id: str, now: float) -> str:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT status FROM requests WHERE request_id=?", (request_id,)).fetchone()
            if old:
                raise GatewayFailure("duplicate_request_" + old["status"], 409)
            session = db.execute("SELECT * FROM sessions WHERE conversation_key=?", (contact,)).fetchone()
            if session and session["state"] != "ready":
                raise GatewayFailure("conversation_" + session["state"], 409)
            remote = session["remote_id"] if session else ""
            db.execute("INSERT INTO requests(request_id,conversation_key,status,claimed_at) VALUES(?,?,'pending',?)",
                       (request_id, contact, now))
            if session:
                db.execute("UPDATE sessions SET state='pending' WHERE conversation_key=?", (contact,))
            else:
                db.execute("INSERT INTO sessions(conversation_key,state) VALUES(?,'pending')", (contact,))
            db.commit()
            return remote or ""

    def observe_remote(self, contact: str, remote: str):
        # Reserve every observed ID, including an unexpected or malformed turn's
        # ID, before accepting any text. Never transfer ownership from a contact.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT conversation_key FROM remote_ids WHERE remote_id=?", (remote,)).fetchone()
            if row and row[0] != contact:
                raise GatewayFailure("remote_id_owned_by_other_contact")
            db.execute("INSERT OR IGNORE INTO remote_ids(remote_id,conversation_key) VALUES(?,?)", (remote, contact))
            session = db.execute("SELECT remote_id FROM sessions WHERE conversation_key=?", (contact,)).fetchone()
            if not session:
                raise GatewayFailure("gateway_state_error", 503)
            changed = bool(session[0] and session[0] != remote)
            if not session[0]:
                db.execute("UPDATE sessions SET remote_id=? WHERE conversation_key=?", (remote, contact))
            db.commit()
            if changed:
                raise GatewayFailure("conversation_changed")

    def finish(self, contact: str, request_id: str, status: str, now: float, code: str | None = None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE requests SET status=?,completed_at=?,error_code=? "
                                 "WHERE request_id=? AND conversation_key=? AND status='pending'",
                                 (status, now, code, request_id, contact)).rowcount
            if changed != 1:
                raise GatewayFailure("gateway_state_error", 503)
            db.execute("UPDATE sessions SET state=? WHERE conversation_key=?",
                       ("ready" if status == "done" else "unknown", contact))
            db.commit()

    def auth_event(self, now: float, code: str = ""):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not code:
                db.execute("INSERT OR REPLACE INTO metadata VALUES('auth_last_success',?)", (str(now),))
            db.execute("INSERT OR REPLACE INTO metadata VALUES('auth_last_error',?)", (code,))
            db.commit()

    def output_diagnostic(self, now: float, value: dict):
        safe = _safe_diagnostic(value)
        if safe is None:
            return
        safe["captured_at"] = now
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO metadata VALUES('last_unsupported_output',?)",
                       (json.dumps(safe, separators=(",", ":")),))

    def mark_tool_metadata(self, contact: str, request_id: str):
        # Only a Boolean is retained; tool names, arguments and results are not.
        with self.connect() as db:
            changed = db.execute("UPDATE requests SET tool_metadata_seen=1 "
                                 "WHERE request_id=? AND conversation_key=? AND status='pending'",
                                 (request_id, contact)).rowcount
            if changed != 1:
                raise GatewayFailure("gateway_state_error", 503)

    def health(self, now: float, configured: bool):
        with self.connect() as db:
            counts = dict(db.execute("SELECT status,COUNT(*) FROM requests GROUP BY status").fetchall())
            last_done = db.execute("SELECT MAX(completed_at) FROM requests WHERE status='done'").fetchone()[0]
            tool_seen_count = db.execute("SELECT COUNT(*) FROM requests WHERE tool_metadata_seen=1").fetchone()[0]
            latest = db.execute("SELECT status FROM requests ORDER BY rowid DESC LIMIT 1").fetchone()
            meta = dict(db.execute("SELECT key,value FROM metadata").fetchall())
        verified = bool(latest and latest[0] == "done" and last_done is not None and 0 <= now - last_done <= 120)
        auth_success = float(meta["auth_last_success"]) if meta.get("auth_last_success") else None
        auth_error = meta.get("auth_last_error") or None
        diagnostic = None
        if meta.get("last_unsupported_output"):
            try:
                saved = json.loads(meta["last_unsupported_output"])
                diagnostic = _safe_diagnostic(saved)
                if diagnostic is not None and type(saved.get("captured_at")) in (int, float):
                    diagnostic["captured_at"] = saved["captured_at"]
            except (ValueError, TypeError):
                diagnostic = None
        auth_status = ("missing" if not configured else "error" if auth_error else
                       "recently_refreshed" if auth_success is not None and 0 <= now - auth_success <= 120 else "unverified")
        return {
            "service": "glm-normal-trial", "runtime_ok": True,
            "model_identity": MODEL, "synthetic_only": True,
            "ok": bool(configured and not auth_error and verified),
            "auth": {"configured": configured, "status": auth_status,
                     "last_success_at": auth_success, "last_error_code": auth_error},
            "diagnostic": {"last_unsupported_output": diagnostic},
            "inference": {"verified": verified, "status": "verified_recently" if verified else "unverified",
                          "last_done_at": last_done,
                          "tool_metadata_seen_count": tool_seen_count,
                          "done_count": counts.get("done", 0), "pending_count": counts.get("pending", 0),
                          "unknown_count": counts.get("unknown", 0)},
        }


def _headers(token: str, device: str) -> dict[str, str]:
    # Public webpage protocol metadata; no forged forwarding address or guest mode.
    raw = str(time.time_ns() // 1_000_000)
    check_digit = (sum(map(int, raw)) - int(raw[-2])) % 10
    timestamp = raw[:-2] + str(check_digit) + raw[-1]
    nonce = uuid.uuid4().hex
    signature = hashlib.md5(f"{timestamp}-{nonce}-8a1317a7468aa3ad86e997d08f3f31cb".encode()).hexdigest()
    return {"Authorization": "Bearer " + token, "X-Timestamp": timestamp, "X-Nonce": nonce,
            "X-Sign": signature, "X-Device-Id": device, "X-Request-Id": uuid.uuid4().hex,
            "X-App-Fr": "default", "X-App-Platform": "pc", "X-App-Version": "0.0.1", "X-Lang": "zh",
            "Content-Type": "application/json", "User-Agent": "WangWechatTwin-IsolatedTrial/1.0"}


def _validate_body(body):
    if not isinstance(body, dict) or set(body) - {"user", "request_id", "messages", "model", "stream"}:
        raise GatewayFailure("invalid_request", 400)
    if body.get("model", MODEL) not in (MODEL, "chatglm-web") or body.get("stream", False) is not False:
        raise GatewayFailure("unsupported_model_or_stream", 400)
    contact, request_id = body.get("user"), body.get("request_id")
    if not isinstance(contact, str) or not _CONTACT.fullmatch(contact):
        raise GatewayFailure("invalid_contact_hash", 400)
    if not isinstance(request_id, str) or not _REQUEST.fullmatch(request_id):
        raise GatewayFailure("invalid_request_id", 400)
    messages = body.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 24:
        raise GatewayFailure("invalid_messages", 400)
    for message in messages:
        if (not isinstance(message, dict) or set(message) != {"role", "content"}
                or not isinstance(message["role"], str)
                or message["role"] not in {"system", "user", "assistant"}
                or not isinstance(message["content"], str) or not message["content"].strip()
                or len(message["content"].encode("utf-8")) > 128 * 1024):
            raise GatewayFailure("text_messages_only", 400)
    if messages[-1]["role"] != "user":
        raise GatewayFailure("last_message_must_be_user", 400)
    return contact, request_id, messages


def _prompt(messages: list[dict]) -> str:
    return ("本轮仅为隔离的虚构文字草稿验收，不发送微信，不执行工具，不读取文件或网页。"
            "只根据下面调用方本轮提供的消息回答；此前模型草稿不能当作本人经历或已发送消息。"
            "必须输出完整ReplyDecision JSON，给出内容不同的A/B/C三个候选。不要代码围栏。\n"
            + json.dumps(ReplyDecision.model_json_schema(), ensure_ascii=False)
            + "\n本轮消息：\n" + json.dumps(messages, ensure_ascii=False))


def _upstream_body(prompt: str, remote: str):
    return {"assistant_id": "65940acff94777010aa6b796", "conversation_id": remote,
            "project_id": "", "chat_type": "user_chat",
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            "meta_data": {"channel": "", "chat_mode": "", "draft_id": "", "if_plus_model": False,
                          "input_question_type": "xxxx", "is_networking": False, "is_test": False,
                          "platform": "pc", "quote_log_id": "", "cogview": {"rm_label_watermark": False}}}


def _status(response: httpx.Response, phase: str):
    if response.status_code != 200:
        raise GatewayFailure(phase + "_http_" + str(response.status_code))
    if "text/html" in response.headers.get("content-type", "").lower():
        raise GatewayFailure(phase + "_html_or_challenge")


async def _stream_text(response: httpx.Response, expected: str, observe: Callable[[str], None],
                       record_tool_metadata: Callable[[], None] | None = None):
    _status(response, "generate")
    if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/event-stream":
        raise GatewayFailure("generate_not_event_stream")
    snapshots, remote, size = {}, expected, 0
    seen_remote = tool_metadata_seen = False

    def consume(value: str):
        nonlocal remote, seen_remote, tool_metadata_seen
        if not value or value == "[DONE]":
            return False
        try:
            event = json.loads(value)
        except (ValueError, TypeError):
            raise GatewayFailure("invalid_sse_json") from None
        if not isinstance(event, dict):
            raise GatewayFailure("invalid_sse_shape")
        observed = event.get("conversation_id")
        if observed is not None and observed != "":
            if not isinstance(observed, str) or not _REMOTE.fullmatch(observed):
                raise GatewayFailure("invalid_conversation_id")
            observe(observed)
            if remote and observed != remote:
                raise GatewayFailure("conversation_changed")
            remote = observed
            seen_remote = True
        if event.get("last_error") or event.get("error") or event.get("status") in {"error", "intervene"}:
            raise GatewayFailure("upstream_error_or_intervention")
        parts = event.get("parts", [])
        if not isinstance(parts, list):
            raise GatewayFailure("invalid_sse_parts")
        for part in parts:
            if not isinstance(part, dict) or part.get("error") or part.get("status") == "error":
                raise GatewayFailure("invalid_or_error_part")
            content = part.get("content", [])
            if not isinstance(content, list):
                raise GatewayFailure("invalid_part_content")
            texts, part_has_tool_metadata = [], False
            for item in content:
                if not isinstance(item, dict):
                    raise GatewayFailure("invalid_content_item")
                if item.get("type") == "text":
                    if not isinstance(item.get("text"), str):
                        raise GatewayFailure("invalid_text_content")
                    texts.append(item["text"])
                elif item.get("type") == "tool_calls" and isinstance(item.get("tool_calls"), dict):
                    # This is only an observation in the already-running upstream
                    # stream. Never dispatch, replay, serialize or forward the
                    # metadata or treat its arguments/results as answer text.
                    if not tool_metadata_seen and record_tool_metadata:
                        record_tool_metadata()
                    tool_metadata_seen = part_has_tool_metadata = True
                elif item.get("type") != "think":
                    raise _unsupported_output(item)
            if part_has_tool_metadata and not texts:
                continue
            logic = part.get("logic_id")
            if not isinstance(logic, (str, int)) or isinstance(logic, bool) or not str(logic):
                raise GatewayFailure("missing_logic_id")
            snapshots[str(logic)] = "".join(texts)
        return event.get("status") == "finish"

    def completed():
        text = "\n\n".join(snapshots[key] for key in sorted(snapshots) if snapshots[key]).strip()
        if not seen_remote or not remote or not text:
            raise GatewayFailure("missing_explicit_finish_or_text")
        return text, remote, tool_metadata_seen

    decoder, buffered, data_lines = codecs.getincrementaldecoder("utf-8")("strict"), "", []
    try:
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_STREAM:
                raise GatewayFailure("stream_too_large")
            buffered += decoder.decode(chunk)
            while "\n" in buffered:
                line, buffered = buffered.split("\n", 1)
                line = line.removesuffix("\r")
                if not line:
                    if data_lines and consume("\n".join(data_lines)):
                        return completed()
                    data_lines = []
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip(" "))
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        raise GatewayFailure("invalid_stream_encoding") from None
    # A partial terminal event at EOF is not a dispatched SSE completion.
    raise GatewayFailure("missing_explicit_finish_or_text")


def _default_client():
    return httpx.AsyncClient(timeout=httpx.Timeout(50, connect=10), follow_redirects=False,
                             trust_env=False, transport=httpx.AsyncHTTPTransport(retries=0))


def create_app(*, ledger_path: Path, api_key: str, credential_reader: Callable[[], dict],
               client_factory: Callable[[], httpx.AsyncClient] = _default_client,
               now: Callable[[], float] = time.time, credential_writer: Callable[[dict], None] | None = None,
               credential_available: Callable[[], bool] | None = None) -> FastAPI:
    """Inject a temporary ledger and MockTransport client for all offline tests."""
    if not isinstance(api_key, str) or not api_key:
        raise ValueError("gateway_key_required")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    ledger, lock, device = Ledger(ledger_path), asyncio.Lock(), uuid.uuid4().hex
    app.state.ledger = ledger

    def authorize(request: Request):
        if not hmac.compare_digest(request.headers.get("authorization", "").encode("utf-8"), ("Bearer " + api_key).encode("utf-8")):
            raise GatewayFailure("unauthorized", 401)

    @app.exception_handler(GatewayFailure)
    async def known_error(request, exc):
        error = {"code": exc.code, "message": exc.code}
        if exc.diagnostic is not None:
            error["diagnostic"] = exc.diagnostic
        return JSONResponse({"error": error}, status_code=exc.status)

    @app.exception_handler(Exception)
    async def unknown_error(request, exc):
        return JSONResponse({"error": {"code": "gateway_internal_error", "message": "gateway_internal_error"}}, status_code=503)

    @app.get("/health")
    async def health(request: Request):
        authorize(request)
        try:
            configured = bool(credential_available()) if credential_available else True
            return ledger.health(now(), configured)
        except Exception:
            raise GatewayFailure("gateway_state_error", 503) from None

    @app.post("/v1/chat/completions")
    async def completion(request: Request):
        authorize(request)
        if lock.locked():
            raise GatewayFailure("gateway_busy", 429)
        await lock.acquire()
        contact = request_id = None
        claimed = False
        try:
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > MAX_BODY:
                    raise GatewayFailure("request_too_large", 413)
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise GatewayFailure("invalid_json", 400) from None
            contact, request_id, messages = _validate_body(body)
            prompt = _prompt(messages)
            expected = ledger.claim(contact, request_id, now())
            claimed = True
            try:
                credentials = credential_reader()
                token = credentials.get("refresh_token") if isinstance(credentials, dict) else None
                if (not isinstance(credentials, dict) or credentials.get("source") != AUTH_SOURCE
                        or not isinstance(token, str) or not token.strip() or len(token) > 32768):
                    raise GatewayFailure("authorized_login_required", 503)
                async with client_factory() as client:
                    async def one_attempt():
                        try:
                            response = await client.post(REFRESH_URL, json={}, headers=_headers(token, device))
                            _status(response, "refresh")
                            if len(response.content) > 128 * 1024:
                                raise GatewayFailure("refresh_response_too_large")
                            value = response.json()
                            result = value.get("result") if isinstance(value, dict) else None
                            if (not isinstance(result, dict) or type(value.get("code", value.get("status"))) is not int
                                    or value.get("code", value.get("status")) != 0
                                    or not isinstance(result.get("access_token"), str) or not result["access_token"].strip()):
                                raise GatewayFailure("normal_login_refresh_rejected")
                            if result.get("refresh_token") and credential_writer:
                                if not isinstance(result["refresh_token"], str):
                                    raise GatewayFailure("invalid_refreshed_token")
                                credential_writer({**credentials, "refresh_token": result["refresh_token"]})
                            ledger.auth_event(now())
                        except Exception as exc:
                            code = exc.code if isinstance(exc, GatewayFailure) else "normal_login_refresh_failed"
                            ledger.auth_event(now(), code)
                            raise GatewayFailure(code) from None
                        # Exactly one generation POST; no fallback, loop, refresh retry,
                        # tool execution, upload, delete, or request replay exists here.
                        async with client.stream("POST", GENERATE_URL, json=_upstream_body(prompt, expected),
                                                 headers={**_headers(result["access_token"], device), "Accept": "text/event-stream"}) as stream:
                            return await _stream_text(stream, expected, lambda rid: ledger.observe_remote(contact, rid),
                                                      lambda: ledger.mark_tool_metadata(contact, request_id))

                    text, remote, tool_metadata_seen = await asyncio.wait_for(one_attempt(), timeout=MAX_SECONDS)
                choice = {"finish_reason": "stop", "message": {"role": "assistant", "content": text}}
                try:
                    decision = LocalProxyReplyLLM._decision({"choices": [choice]}, "abc")
                    if tool_metadata_seen:
                        decision = decision.model_copy(update={"action": "review", "holding_reply": None,
                                                               "confidence": 0.0, "reason": TOOL_REVIEW_REASON})
                        choice["message"]["content"] = decision.model_dump_json()
                        # Review requires ABC even if the original model decision
                        # was ignore (whose validator otherwise permits no ABC).
                        decision = LocalProxyReplyLLM._decision({"choices": [choice]}, "abc")
                except Exception:
                    raise GatewayFailure("invalid_reply_decision") from None
                choice["message"]["content"] = decision.model_dump_json()
                choice["index"] = 0
                ledger.finish(contact, request_id, "done", now())
                return {"id": "glm-trial-" + hashlib.sha256(request_id.encode()).hexdigest()[:24],
                        "object": "chat.completion", "created": int(now()), "model": MODEL,
                        "conversation_id": remote, "tool_metadata_seen": tool_metadata_seen, "choices": [choice]}
            except BaseException as exc:
                code = exc.code if isinstance(exc, GatewayFailure) else "upstream_request_unknown"
                diagnostic = exc.diagnostic if isinstance(exc, GatewayFailure) else None
                if diagnostic is not None:
                    try:
                        ledger.output_diagnostic(now(), diagnostic)
                    except Exception:
                        pass  # Diagnostics never weaken the existing pending barrier.
                try:
                    ledger.finish(contact, request_id, "unknown", now(), code)
                except Exception:
                    pass  # A committed pending record remains a permanent barrier.
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                raise GatewayFailure(code, exc.status if isinstance(exc, GatewayFailure) else 502, diagnostic) from None
        except GatewayFailure:
            raise
        except Exception:
            raise GatewayFailure("gateway_state_error" if not claimed else "upstream_request_unknown", 503) from None
        finally:
            lock.release()

    return app


def _read_authorized_credentials():
    if AUTH_PATH.is_symlink() or AUTH_PATH.parent.resolve() != TRIAL / "auth":
        raise GatewayFailure("invalid_auth_path", 503)
    return json.loads(AUTH_PATH.read_text(encoding="utf-8"))


def _write_authorized_credentials(value: dict):
    if (value.get("source") != AUTH_SOURCE or AUTH_PATH.is_symlink()
            or AUTH_PATH.parent.resolve() != TRIAL / "auth"):
        raise GatewayFailure("invalid_auth_source", 503)
    pending = AUTH_PATH.with_name("refresh.update-" + uuid.uuid4().hex + ".json")
    try:
        with pending.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, AUTH_PATH)
    finally:
        pending.unlink(missing_ok=True)


def _runtime_key():
    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if KEY_PATH.is_symlink():
        raise ValueError("invalid_gateway_key_path")
    if not KEY_PATH.exists():
        try:
            with KEY_PATH.open("x", encoding="utf-8") as handle:
                handle.write(secrets.token_urlsafe(48))
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            pass
    key = KEY_PATH.read_text(encoding="utf-8").strip()
    if len(key) < 32 or len(key) > 256 or not re.fullmatch(r"[A-Za-z0-9_-]+", key):
        raise ValueError("invalid_gateway_key")
    return key


def main():
    import logging
    import uvicorn
    logging.disable(logging.CRITICAL)
    try:
        app = create_app(ledger_path=LEDGER_PATH, api_key=_runtime_key(),
                         credential_reader=_read_authorized_credentials,
                         credential_writer=_write_authorized_credentials,
                         credential_available=lambda: AUTH_PATH.is_file())
        uvicorn.run(app, host=HOST, port=PORT, workers=1, access_log=False, log_config=None)
    except Exception:
        print("glm_gateway_start_failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

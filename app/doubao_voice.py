"""One-shot local preview of Doubao web TTS with an approved QR session.

This module has no WeChat sender, voice cloning, account login, or retry path.
It uses a built-in Doubao speaker; a generated preview is not the owner's voice.
The caller must supply a stable private output directory and a unique request ID.
An existing pending, unknown, or done claim permanently blocks that ID there.

Protocol references only (no source copied from these unlicensed projects):
https://github.com/ljheee/tts-extension/tree/ea64a4a183cd077088bd445e2a9888e59689a7a7
https://github.com/callmerio/doubao-tts/tree/a525dd283ca52f4ab92685c9641dd24e6eec7ec2
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import math
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from websockets.asyncio.client import connect as WebSocketConnect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus


WS_BASE = "wss://ws-samantha.doubao.com/samantha/audio/tts"
ORIGIN = "https://www.doubao.com"
SPEAKER = "zh_female_taozi_conversation_v4_wvae_bigtts"
USER_AGENT = "WangWechatTwin-VoicePreview/0.1 (Python websockets client)"
COOKIE_NAMES = ("sessionid", "sid_guard", "uid_tt", "sessionid_ss")
MAX_STATE_BYTES = 2 * 1024 * 1024
MAX_TEXT_BYTES = 1200
MAX_TEXT_CHARS = 160
MAX_AUDIO_BYTES = 2 * 1024 * 1024
MAX_EVENT_BYTES = 16 * 1024
MAX_EVENTS = 64
_ID = re.compile(r"[0-9]{8,24}\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_SENTENCE_TERMINATORS = ".。!?！？;；…"


class VoiceFailure(Exception):
    """A fixed printable code and tightly bounded, non-secret diagnostics."""

    def __init__(self, code: str, *, phase: str | None = None,
                 category: str | None = None, http_status: int | None = None,
                 content_type: str | None = None, body_kind: str | None = None):
        super().__init__(code)
        self.diagnostic: dict[str, str | int] = {}
        if phase in {"handshake", "send_text", "send_finish", "receive", "close"}:
            self.diagnostic["phase"] = phase
        if category in {"http_status", "dns_error", "tls_verification_error",
                        "tls_error", "connection_refused", "connection_reset",
                        "connection_closed", "invalid_handshake", "network_os_error",
                        "client_api_error", "unexpected_error", "timeout"}:
            self.diagnostic["category"] = category
        if type(http_status) is int and 100 <= http_status <= 599:
            self.diagnostic["http_status"] = http_status
        if content_type in {"html", "json", "plain_text", "other", "missing"}:
            self.diagnostic["content_type"] = content_type
        if body_kind in {"html_like", "json_like", "other", "empty"}:
            self.diagnostic["body_kind"] = body_kind


class NoRedirectConnect(WebSocketConnect):
    """Never carry the session Cookie to a redirected host."""

    def process_redirect(self, exc):
        return exc


@dataclass(frozen=True, repr=False)
class QRSession:
    cookie_header: str
    device_id: str
    web_id: str


def _private_path(path: Path, *, code: str) -> Path:
    resolved = path.expanduser().resolve()
    public_root = Path(__file__).resolve().parents[1]
    if resolved == public_root or public_root in resolved.parents:
        raise VoiceFailure(code)
    return resolved


def load_qr_session(path: Path) -> QRSession:
    """Use four named cookies from one already-approved isolated QR account."""
    path = _private_path(path, code="session_path_must_be_private")
    try:
        if path.stat().st_size > MAX_STATE_BYTES:
            raise VoiceFailure("session_file_too_large")
        state = json.loads(path.read_text(encoding="utf-8-sig"))
        accounts = state.get("accounts") if isinstance(state, dict) else None
        if not isinstance(accounts, list) or len(accounts) != 1:
            raise VoiceFailure("single_qr_account_required")
        account = accounts[0]
        if (not isinstance(account, dict) or account.get("source") != "qr"
                or account.get("enabled") is not True or account.get("status") != "ok"):
            raise VoiceFailure("single_qr_account_required")
        device_id, web_id = account.get("device_id"), account.get("web_id")
        if (not isinstance(device_id, str) or not _ID.fullmatch(device_id)
                or not isinstance(web_id, str) or not _ID.fullmatch(web_id)):
            raise VoiceFailure("qr_device_identity_required")
        cookies = account.get("cookies")
        if not isinstance(cookies, dict):
            raise VoiceFailure("qr_session_cookies_required")
        selected = []
        for name in COOKIE_NAMES:
            value = cookies.get(name)
            if (not isinstance(value, str) or not 1 <= len(value) <= 4096
                    or any(char in value for char in "\r\n;")):
                raise VoiceFailure("qr_session_cookies_required")
            selected.append(name + "=" + value)
        return QRSession("; ".join(selected), device_id, web_id)
    except VoiceFailure:
        raise
    except (OSError, UnicodeError, ValueError, TypeError):
        raise VoiceFailure("session_file_unavailable") from None


def _validate_preview_text(text: str) -> str:
    if (not isinstance(text, str) or not text or len(text) > MAX_TEXT_CHARS
            or "\n" in text or "\r" in text
            or not all(character.isprintable() for character in text)):
        raise VoiceFailure("invalid_preview_text")
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise VoiceFailure("preview_text_too_large")
    # A sentence_end is the only terminal evidence established for this
    # protocol. Never accept multiple sentences and publish only the first.
    # Compatibility punctuation such as halfwidth/fullwidth full stops must
    # not allow an additional sentence through the same preflight.
    core = unicodedata.normalize("NFKC", text).strip().rstrip(_SENTENCE_TERMINATORS).strip()
    if not core or any(character in _SENTENCE_TERMINATORS for character in core):
        raise VoiceFailure("single_sentence_required")
    return text


def _readable_text_key(text: str) -> str:
    # The service may omit terminal punctuation or alter whitespace. Preserve
    # every other character; never accept a prefix or invent missing speech.
    value = unicodedata.normalize("NFC", text)
    return "".join(value.split()).rstrip(_SENTENCE_TERMINATORS)


def load_preview_text(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_TEXT_BYTES:
            raise VoiceFailure("preview_text_too_large")
        text = path.read_text(encoding="utf-8-sig").strip()
        return _validate_preview_text(text)
    except VoiceFailure:
        raise
    except (OSError, UnicodeError, ValueError):
        raise VoiceFailure("preview_text_unavailable") from None


def _voice_url(session: QRSession) -> str:
    params = {
        "speaker": SPEAKER, "format": "aac", "speech_rate": "0", "pitch": "0",
        "version_code": "20800", "language": "zh", "device_platform": "web",
        "aid": "497858", "real_aid": "497858", "pkg_type": "release_version",
        "device_id": session.device_id, "pc_version": "3.15.1",
        "web_id": session.web_id, "tea_uuid": session.web_id,
        "region": "", "sys_region": "", "samantha_web": "1",
        "use-olympus-account": "1", "web_tab_id": str(uuid.uuid4()),
    }
    url = WS_BASE + "?" + urlencode(params)
    parsed = urlsplit(url)
    if (parsed.scheme, parsed.hostname, parsed.path, parsed.port) != (
            "wss", "ws-samantha.doubao.com", "/samantha/audio/tts", None):
        raise VoiceFailure("upstream_not_allowlisted")
    return url


def _connection_options(session: QRSession) -> dict:
    options = {
        "origin": ORIGIN,
        "additional_headers": {"Cookie": session.cookie_header},
        "user_agent_header": USER_AGENT,
        "compression": None,
        "open_timeout": 8,
        "close_timeout": 2,
        "max_size": MAX_AUDIO_BYTES,
    }
    # Automatic proxy handling was added after the earliest supported
    # websockets release. Older releases have no proxy argument.
    if "proxy" in inspect.signature(WebSocketConnect).parameters:
        options["proxy"] = None
    return options


def _connection_diagnostic(exc: Exception) -> dict:
    if isinstance(exc, InvalidStatus):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        result = {"category": "http_status"}
        if type(status) is int and 100 <= status <= 599:
            result["http_status"] = status
        headers = getattr(response, "headers", None)
        try:
            content_type = headers.get("Content-Type", "") if headers is not None else ""
        except Exception:
            content_type = ""
        media_type = content_type.lower().split(";", 1)[0].strip() if isinstance(content_type, str) else ""
        if media_type in {"text/html", "application/xhtml+xml"}:
            result["content_type"] = "html"
        elif media_type in {"application/json", "application/problem+json"}:
            result["content_type"] = "json"
        elif media_type == "text/plain":
            result["content_type"] = "plain_text"
        else:
            result["content_type"] = "other" if media_type else "missing"
        body = getattr(response, "body", b"")
        prefix = bytes(body[:64]).lstrip().lower() if isinstance(body, (bytes, bytearray)) else b""
        result["body_kind"] = ("html_like" if prefix.startswith((b"<!doctype html", b"<html"))
                               else "json_like" if prefix.startswith((b"{", b"["))
                               else "empty" if not prefix else "other")
        return result
    if isinstance(exc, socket.gaierror):
        return {"category": "dns_error"}
    if isinstance(exc, ssl.SSLCertVerificationError):
        return {"category": "tls_verification_error"}
    if isinstance(exc, ssl.SSLError):
        return {"category": "tls_error"}
    if isinstance(exc, ConnectionRefusedError):
        return {"category": "connection_refused"}
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return {"category": "connection_reset"}
    if isinstance(exc, ConnectionClosed):
        return {"category": "connection_closed"}
    if isinstance(exc, InvalidHandshake):
        return {"category": "invalid_handshake"}
    if isinstance(exc, OSError):
        return {"category": "network_os_error"}
    if isinstance(exc, (TypeError, ValueError)):
        return {"category": "client_api_error"}
    return {"category": "unexpected_error"}


async def collect_aac(session: QRSession, text: str, *, connect=NoRedirectConnect) -> bytes:
    """Require a complete single-sentence event sequence before returning bytes."""
    text = _validate_preview_text(text)
    expected_text = _readable_text_key(text)
    url = _voice_url(session)
    chunks: list[bytes] = []
    total = 0
    opened = started = ended = False
    phase = "handshake"

    async def _exchange() -> None:
        nonlocal total, opened, started, ended, phase
        async with connect(url, **_connection_options(session)) as ws:
            phase = "send_text"
            await ws.send(json.dumps({"event": "text", "text": text}, ensure_ascii=False))
            phase = "send_finish"
            await ws.send('{"event":"finish"}')
            phase = "receive"
            for _ in range(MAX_EVENTS):
                message = await asyncio.wait_for(ws.recv(), timeout=10)
                if isinstance(message, bytes):
                    if not opened or not started or ended:
                        raise VoiceFailure("unexpected_audio_frame")
                    total += len(message)
                    if total > MAX_AUDIO_BYTES:
                        raise VoiceFailure("audio_too_large")
                    chunks.append(message)
                    continue
                if not isinstance(message, str) or len(message.encode("utf-8")) > MAX_EVENT_BYTES:
                    raise VoiceFailure("invalid_tts_event")
                try:
                    event = json.loads(message)
                except (TypeError, ValueError):
                    raise VoiceFailure("invalid_tts_event") from None
                if not isinstance(event, dict):
                    raise VoiceFailure("invalid_tts_event")
                if "code" in event and (type(event["code"]) is not int or event["code"] != 0):
                    raise VoiceFailure("tts_upstream_error")
                kind = event.get("event")
                if kind == "open_success":
                    if opened or type(event.get("code")) is not int or event["code"] != 0:
                        raise VoiceFailure("tts_open_rejected")
                    opened = True
                elif kind == "sentence_start":
                    detail = event.get("sentence_start_result")
                    if (not opened or started or not isinstance(detail, dict)
                            or not isinstance(detail.get("readable_text"), str)
                            or not detail["readable_text"].strip()):
                        raise VoiceFailure("invalid_sentence_start")
                    if _readable_text_key(detail["readable_text"]) != expected_text:
                        raise VoiceFailure("tts_sentence_text_mismatch")
                    started = True
                elif kind == "sentence_end":
                    if (not started or ended or type(event.get("code")) is not int
                            or event["code"] != 0 or not chunks):
                        raise VoiceFailure("incomplete_sentence")
                    ended = True
                    break
                elif kind == "error":
                    raise VoiceFailure("tts_upstream_error")
            if not (opened and started and ended and chunks):
                raise VoiceFailure("tts_incomplete")
            phase = "close"

    try:
        await asyncio.wait_for(_exchange(), timeout=35)
    except VoiceFailure:
        raise
    except (asyncio.TimeoutError, TimeoutError):
        raise VoiceFailure("tts_timeout", phase=phase, category="timeout") from None
    except Exception as exc:
        raise VoiceFailure("tts_connection_unknown", phase=phase,
                           **_connection_diagnostic(exc)) from None
    return b"".join(chunks)


def _probe_duration(path: Path, *, expected_format: str, expected_codec: str) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise VoiceFailure("ffprobe_unavailable")
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries",
             "format=format_name,duration:stream=codec_name,codec_type",
             "-of", "json", str(path)], capture_output=True, text=True, timeout=8,
            check=False,
        )
        if result.returncode != 0 or len(result.stdout) > 8192:
            raise VoiceFailure("invalid_" + expected_format)
        info = json.loads(result.stdout)
        media = info.get("format", {})
        streams = info.get("streams", [])
        duration = float(media.get("duration"))
        if (expected_format not in str(media.get("format_name", "")).split(",")
                or not isinstance(streams, list)
                or not any(isinstance(stream, dict) and stream.get("codec_type") == "audio"
                           and stream.get("codec_name") == expected_codec for stream in streams)
                or not math.isfinite(duration) or not .2 <= duration <= 30):
            raise VoiceFailure("invalid_" + expected_format)
        return duration
    except VoiceFailure:
        raise
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        raise VoiceFailure("invalid_" + expected_format) from None


def validate_aac(path: Path) -> float:
    return _probe_duration(path, expected_format="aac", expected_codec="aac")


def validate_mp3(path: Path) -> float:
    return _probe_duration(path, expected_format="mp3", expected_codec="mp3")


def convert_aac_to_mp3(source: Path, destination: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise VoiceFailure("ffmpeg_unavailable")
    try:
        result = subprocess.run(
            [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-n",
             "-i", str(source), "-map", "0:a:0", "-vn", "-c:a", "libmp3lame",
             "-q:a", "4", "-f", "mp3", str(destination)],
            capture_output=True, timeout=15, check=False,
        )
        if result.returncode != 0 or not destination.is_file():
            raise VoiceFailure("aac_to_mp3_failed")
    except VoiceFailure:
        raise
    except (OSError, subprocess.TimeoutExpired):
        raise VoiceFailure("aac_to_mp3_failed") from None


def _write_claim(path: Path, request_id: str, status: str, *, audio_sha256: str = "") -> None:
    data = {"request_id": request_id, "status": status,
            "audio_sha256": audio_sha256}
    temporary = path.with_name(path.name + ".next")
    with temporary.open("x", encoding="utf-8") as file:
        file.write(json.dumps(data, separators=(",", ":")))
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def _failure_report(request_id: str, failure: VoiceFailure) -> dict:
    result = {"ok": False, "request_id": request_id, "error_code": str(failure),
              "voice_identity": "preset_not_owner_clone", "wechat_sent": False}
    if failure.diagnostic:
        result["diagnostic"] = failure.diagnostic
    return result


async def generate_preview(*, request_id: str, text_file: Path, session_file: Path,
                           output_dir: Path, connect=NoRedirectConnect,
                           validate_aac_audio=validate_aac,
                           convert_audio=convert_aac_to_mp3,
                           validate_mp3_audio=validate_mp3) -> dict:
    """Generate one local MP3; never replay an ID after a claim is created."""
    if not _REQUEST_ID.fullmatch(request_id):
        raise VoiceFailure("invalid_request_id")
    output_dir = _private_path(output_dir, code="output_dir_must_be_private")
    if ((validate_aac_audio is validate_aac or validate_mp3_audio is validate_mp3)
            and not shutil.which("ffprobe")):
        raise VoiceFailure("ffprobe_unavailable")
    if convert_audio is convert_aac_to_mp3 and not shutil.which("ffmpeg"):
        raise VoiceFailure("ffmpeg_unavailable")
    requests_dir = output_dir / "requests"
    claim = requests_dir / (request_id + ".json")
    report = output_dir / (request_id + ".report.json")
    output = output_dir / (request_id + ".mp3")
    if claim.exists() or report.exists() or output.exists():
        raise VoiceFailure("request_already_recorded")
    text = load_preview_text(text_file)
    session = load_qr_session(session_file)
    try:
        requests_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise VoiceFailure("output_dir_unavailable") from None
    try:
        with claim.open("x", encoding="utf-8") as file:
            file.write(json.dumps({"request_id": request_id, "status": "pending"},
                                  separators=(",", ":")))
            file.flush()
            os.fsync(file.fileno())
    except FileExistsError:
        raise VoiceFailure("request_already_recorded") from None
    except OSError:
        raise VoiceFailure("claim_unavailable") from None
    source = output_dir / (request_id + ".aac.part")
    target = output_dir / (request_id + ".mp3.part")
    try:
        audio = await collect_aac(session, text, connect=connect)
        if not audio or len(audio) > MAX_AUDIO_BYTES:
            raise VoiceFailure("invalid_aac")
        with source.open("xb") as file:
            file.write(audio)
            file.flush()
            os.fsync(file.fileno())
        source_duration = validate_aac_audio(source)
        convert_audio(source, target)
        duration = validate_mp3_audio(target)
        converted = target.read_bytes()
        if not converted or len(converted) > MAX_AUDIO_BYTES:
            raise VoiceFailure("invalid_mp3")
        digest = hashlib.sha256(converted).hexdigest()
        with output.open("xb") as file:
            file.write(converted)
            file.flush()
            os.fsync(file.fileno())
        result = {"ok": True, "request_id": request_id, "audio_path": str(output),
                  "audio_sha256": digest, "audio_bytes": len(converted),
                  "source_format": "aac", "output_format": "mp3",
                  "source_duration_seconds": round(source_duration, 3),
                  "duration_seconds": round(duration, 3),
                  "voice_identity": "preset_not_owner_clone", "wechat_sent": False}
        report.write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
        _write_claim(claim, request_id, "done", audio_sha256=digest)
        return result
    except BaseException as exc:
        try:
            _write_claim(claim, request_id, "unknown")
        except OSError:
            pass
        if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
            raise
        failure = exc if isinstance(exc, VoiceFailure) else VoiceFailure("preview_unknown")
        try:
            report.write_text(json.dumps(_failure_report(request_id, failure),
                                         separators=(",", ":")), encoding="utf-8")
        except OSError:
            pass
        raise failure from None
    finally:
        for temporary in (source, target):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One approved local Doubao voice preview")
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--text-file", type=Path, required=True)
    parser.add_argument("--session-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        result = asyncio.run(generate_preview(request_id=args.request_id,
                                              text_file=args.text_file,
                                              session_file=args.session_file,
                                              output_dir=args.output_dir))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except VoiceFailure as failure:
        print(json.dumps(_failure_report(args.request_id, failure), ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Offline checks for the isolated Doubao preset-voice preview CLI."""

import asyncio
import json
import shutil
import subprocess
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from app import doubao_voice as voice


SECRET = "SYNTHETIC_SESSION_SECRET_NEVER_PRINT_572"


@pytest.mark.parametrize(("name", "codec"), [("aac", "aac"), ("mp3", "mp3")])
def test_audio_probe_subprocess_is_windowless_without_changing_capture(tmp_path, monkeypatch, name, codec):
    observed = []
    monkeypatch.setattr(voice.shutil, "which", lambda tool: "ffprobe.exe" if tool == "ffprobe" else None)
    def fake_run(command, **kwargs):
        observed.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "format": {"format_name": name, "duration": "1.2"},
            "streams": [{"codec_type": "audio", "codec_name": codec}]}))
    monkeypatch.setattr(voice, "subprocess", SimpleNamespace(
        run=fake_run, CREATE_NO_WINDOW=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        TimeoutExpired=subprocess.TimeoutExpired))
    assert voice._probe_duration(tmp_path / "synthetic.aac", expected_format=name, expected_codec=codec) == 1.2
    command, kwargs = observed[0]
    assert command[0] == "ffprobe.exe"
    assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert kwargs["capture_output"] is True and kwargs["text"] is True
    assert kwargs["timeout"] == 8 and kwargs["check"] is False


def test_aac_conversion_subprocess_is_windowless_without_changing_capture(tmp_path, monkeypatch):
    observed = []
    monkeypatch.setattr(voice.shutil, "which", lambda tool: "ffmpeg.exe" if tool == "ffmpeg" else None)
    destination = tmp_path / "preview.mp3"
    def fake_run(command, **kwargs):
        observed.append((command, kwargs))
        destination.write_bytes(b"synthetic-mp3")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(voice, "subprocess", SimpleNamespace(
        run=fake_run, CREATE_NO_WINDOW=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        TimeoutExpired=subprocess.TimeoutExpired))
    voice.convert_aac_to_mp3(tmp_path / "synthetic.aac", destination)
    command, kwargs = observed[0]
    assert command[0] == "ffmpeg.exe" and "-nostdin" in command
    assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert kwargs["capture_output"] is True
    assert kwargs["timeout"] == 15 and kwargs["check"] is False


def session_file(tmp_path, *, missing=None, source="qr", count=1):
    cookies = {"sessionid": SECRET, "sid_guard": "synthetic-guard",
               "uid_tt": "synthetic-uid", "sessionid_ss": "synthetic-secure-session",
               "other_secret": "never-forward"}
    if missing:
        cookies.pop(missing)
    account = {"source": source, "enabled": True, "status": "ok",
               "device_id": "123456789012345", "web_id": "1234567890123456789",
               "cookies": cookies}
    path = tmp_path / "qr-state.json"
    path.write_text(json.dumps({"accounts": [account] * count,
                                "keys": [{"key": "never-read"}]}), encoding="utf-8")
    return path


def text_file(tmp_path, text="你好，这是一次本地语音试听。"):
    path = tmp_path / "preview-text.txt"
    path.write_text(text, encoding="utf-8")
    return path


def event(kind, **extra):
    return json.dumps({"event": kind, **extra}, ensure_ascii=False)


def complete_frames():
    return [event("open_success", code=0),
            event("sentence_start", sentence_start_result={"readable_text": "你好，这是一次本地语音试听。"}),
            b"synthetic-aac-bytes", event("sentence_end", code=0)]


class FakeSocket:
    def __init__(self, frames):
        self.frames = list(frames)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def send(self, message):
        self.sent.append(message)

    async def recv(self):
        if not self.frames:
            raise RuntimeError("synthetic interrupted stream " + SECRET)
        item = self.frames.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def fake_connection(frames):
    socket = FakeSocket(frames)
    calls = []

    def connect(url, **options):
        calls.append((url, options))
        return socket

    return socket, calls, connect


def synthetic_converter(source, destination):
    assert source.read_bytes() == b"synthetic-aac-bytes"
    destination.write_bytes(b"synthetic-converted-mp3")


def generate(tmp_path, *, request_id="voice_001", connect,
             text=None, session=None, validate_aac_audio=None,
             convert_audio=synthetic_converter, validate_mp3_audio=None):
    return asyncio.run(voice.generate_preview(
        request_id=request_id,
        text_file=text or text_file(tmp_path),
        session_file=session or session_file(tmp_path),
        output_dir=tmp_path / "private-output",
        connect=connect,
        validate_aac_audio=validate_aac_audio or (lambda path: 1.1),
        convert_audio=convert_audio,
        validate_mp3_audio=validate_mp3_audio or (lambda path: 1.12),
    ))


def test_one_complete_preview_has_private_claim_no_sender_or_secret(tmp_path):
    socket, calls, connect = fake_connection(complete_frames())
    result = generate(tmp_path, connect=connect)
    assert result["ok"] is True and result["wechat_sent"] is False
    assert result["voice_identity"] == "preset_not_owner_clone"
    assert result["source_format"] == "aac" and result["output_format"] == "mp3"
    assert len(calls) == 1
    url, options = calls[0]
    uri = urlsplit(url)
    query = parse_qs(uri.query, keep_blank_values=True)
    assert (uri.scheme, uri.hostname, uri.path) == (
        "wss", "ws-samantha.doubao.com", "/samantha/audio/tts")
    assert query["format"] == ["aac"] and query["pc_version"] == ["3.15.1"]
    assert query["device_id"] == ["123456789012345"]
    assert query["web_id"] == ["1234567890123456789"]
    assert query["region"] == query["sys_region"] == [""]
    assert options["origin"] == voice.ORIGIN and "Chrome" not in options["user_agent_header"]
    assert options["additional_headers"]["Cookie"] == (
        "sessionid=" + SECRET + "; sid_guard=synthetic-guard; "
        "uid_tt=synthetic-uid; sessionid_ss=synthetic-secure-session")
    assert "other_secret" not in options["additional_headers"]["Cookie"]
    assert [json.loads(item) for item in socket.sent] == [
        {"event": "text", "text": "你好，这是一次本地语音试听。"}, {"event": "finish"}]
    output = tmp_path / "private-output"
    assert (output / "voice_001.mp3").read_bytes() == b"synthetic-converted-mp3"
    claim = output / "requests" / "voice_001.json"
    report = output / "voice_001.report.json"
    assert json.loads(claim.read_text(encoding="utf-8"))["status"] == "done"
    assert json.loads(report.read_text(encoding="utf-8")) == result
    assert SECRET not in claim.read_text(encoding="utf-8") + report.read_text(encoding="utf-8")
    assert not (output / "voice_001.aac.part").exists()
    with pytest.raises(voice.VoiceFailure, match="request_already_recorded"):
        generate(tmp_path, connect=connect)
    assert len(calls) == 1


@pytest.mark.parametrize("missing", list(voice.COOKIE_NAMES))
def test_missing_qr_cookie_stops_before_claim_or_network(tmp_path, missing):
    session = session_file(tmp_path, missing=missing)
    with pytest.raises(voice.VoiceFailure, match="qr_session_cookies_required"):
        generate(tmp_path, session=session,
                 connect=lambda *a, **k: pytest.fail("network must not start"))
    assert not (tmp_path / "private-output" / "requests" / "voice_001.json").exists()


@pytest.mark.parametrize("bad_id", ["", "..", "../other", "id:one", "汉字", "x" * 65])
def test_request_id_cannot_escape_ledger(tmp_path, bad_id):
    with pytest.raises(voice.VoiceFailure, match="invalid_request_id"):
        generate(tmp_path, request_id=bad_id,
                 connect=lambda *a, **k: pytest.fail("network must not start"))


def test_text_bounds_prevent_network_and_leave_id_free(tmp_path):
    invalid = text_file(tmp_path, "第一句\n第二句")
    with pytest.raises(voice.VoiceFailure, match="invalid_preview_text"):
        generate(tmp_path, text=invalid,
                 connect=lambda *a, **k: pytest.fail("network must not start"))
    assert not (tmp_path / "private-output" / "requests" / "voice_001.json").exists()


@pytest.mark.parametrize("text", [
    "第一句。第二句。", "先休息！明天再说。", "First sentence. Second sentence.",
    "想一想；再决定", "等一下…然后呢", "只是第一句。 后面还有一句",
    "第一句｡第二句｡", "第一句．第二句．", "第一句﹒第二句﹒",
])
def test_multiple_sentences_are_rejected_before_claim_or_connection(tmp_path, text):
    with pytest.raises(voice.VoiceFailure, match="single_sentence_required"):
        generate(tmp_path, text=text_file(tmp_path, text),
                 connect=lambda *a, **k: pytest.fail("multi-sentence network must not start"))
    assert not (tmp_path / "private-output" / "requests" / "voice_001.json").exists()


def test_direct_collector_cannot_skip_single_sentence_preflight():
    session = voice.QRSession("sessionid=synthetic", "123456789012", "1234567890123")
    with pytest.raises(voice.VoiceFailure, match="single_sentence_required"):
        asyncio.run(voice.collect_aac(session, "First sentence. Second sentence.",
                    connect=lambda *a, **k: pytest.fail("direct collector must not connect")))


@pytest.mark.parametrize("readable", ["你好", "完全不同的句子", "你好，这是一次本地语音试听。额外内容", SECRET])
def test_partial_or_foreign_readable_text_is_unknown_and_never_published(tmp_path, readable):
    frames = complete_frames()
    frames[1] = event("sentence_start", sentence_start_result={"readable_text": readable})
    _, calls, connect = fake_connection(frames)
    with pytest.raises(voice.VoiceFailure, match="tts_sentence_text_mismatch"):
        generate(tmp_path, connect=connect)
    output = tmp_path / "private-output"
    assert len(calls) == 1 and not (output / "voice_001.mp3").exists()
    assert json.loads((output / "requests" / "voice_001.json").read_text())["status"] == "unknown"
    report = (output / "voice_001.report.json").read_text(encoding="utf-8")
    assert SECRET not in report and readable not in report
    with pytest.raises(voice.VoiceFailure, match="request_already_recorded"):
        generate(tmp_path, connect=connect)
    assert len(calls) == 1


@pytest.mark.parametrize("bad_code", [403, 1, "0", False])
def test_sentence_start_error_code_cannot_be_accepted_as_success(tmp_path, bad_code):
    frames = complete_frames()
    frames[1] = event("sentence_start", code=bad_code,
                      sentence_start_result={"readable_text": "你好，这是一次本地语音试听。"})
    _, calls, connect = fake_connection(frames)
    with pytest.raises(voice.VoiceFailure, match="tts_upstream_error"):
        generate(tmp_path, connect=connect)
    output = tmp_path / "private-output"
    assert len(calls) == 1 and not (output / "voice_001.mp3").exists()
    assert json.loads((output / "requests" / "voice_001.json").read_text())["status"] == "unknown"


def test_full_readable_text_allows_only_whitespace_and_terminal_punctuation_normalization(tmp_path):
    frames = complete_frames()
    frames[1] = event("sentence_start", code=0,
                      sentence_start_result={"readable_text": " 你好， 这是一次本地语音试听 "})
    _, calls, connect = fake_connection(frames)
    result = generate(tmp_path, connect=connect)
    assert result["ok"] is True and len(calls) == 1


def test_existing_pending_claim_survives_and_blocks_restart(tmp_path):
    requests = tmp_path / "private-output" / "requests"
    requests.mkdir(parents=True)
    claim = requests / "voice_001.json"
    claim.write_text('{"request_id":"voice_001","status":"pending"}', encoding="utf-8")
    with pytest.raises(voice.VoiceFailure, match="request_already_recorded"):
        generate(tmp_path, connect=lambda *a, **k: pytest.fail("network must not start"))
    assert json.loads(claim.read_text(encoding="utf-8"))["status"] == "pending"


def test_http200_html_fails_once_with_only_shape_diagnostic(tmp_path):
    response = Response(200, SECRET,
                        Headers([("Content-Type", "text/html; charset=utf-8"),
                                 ("Set-Cookie", SECRET)]),
                        ("<html>" + SECRET).encode())
    calls = []

    def connect(url, **options):
        calls.append(1)
        raise InvalidStatus(response)

    with pytest.raises(voice.VoiceFailure, match="tts_connection_unknown") as captured:
        generate(tmp_path, connect=connect)
    assert captured.value.diagnostic == {
        "phase": "handshake", "category": "http_status", "http_status": 200,
        "content_type": "html", "body_kind": "html_like"}
    output = tmp_path / "private-output"
    assert json.loads((output / "requests" / "voice_001.json").read_text())["status"] == "unknown"
    report = (output / "voice_001.report.json").read_text(encoding="utf-8")
    assert SECRET not in report and not (output / "voice_001.mp3").exists()
    with pytest.raises(voice.VoiceFailure, match="request_already_recorded"):
        generate(tmp_path, connect=connect)
    assert len(calls) == 1


@pytest.mark.parametrize("frames,error", [
    ([event("open_success", code=0), event("sentence_start", sentence_start_result={"readable_text": "你好，这是一次本地语音试听。"}),
      b"incomplete"], "tts_connection_unknown"),
    ([event("open_success", code=0), b"audio-before-start"], "unexpected_audio_frame"),
    ([event("open_success", code=0), event("error", code=403, message=SECRET)],
     "tts_upstream_error"),
])
def test_incomplete_or_error_events_never_publish_mp3(tmp_path, frames, error):
    _, calls, connect = fake_connection(frames)
    with pytest.raises(voice.VoiceFailure, match=error):
        generate(tmp_path, connect=connect)
    output = tmp_path / "private-output"
    assert len(calls) == 1 and not (output / "voice_001.mp3").exists()
    assert json.loads((output / "requests" / "voice_001.json").read_text())["status"] == "unknown"
    assert SECRET not in (output / "voice_001.report.json").read_text(encoding="utf-8")


def test_failed_conversion_preserves_unknown_and_never_publishes(tmp_path):
    _, calls, connect = fake_connection(complete_frames())

    def broken_converter(source, destination):
        raise voice.VoiceFailure("aac_to_mp3_failed")

    with pytest.raises(voice.VoiceFailure, match="aac_to_mp3_failed"):
        generate(tmp_path, connect=connect, convert_audio=broken_converter)
    output = tmp_path / "private-output"
    assert len(calls) == 1 and not (output / "voice_001.mp3").exists()
    assert json.loads((output / "requests" / "voice_001.json").read_text())["status"] == "unknown"
    assert not (output / "voice_001.aac.part").exists()


def test_local_aac_to_mp3_pipeline_uses_real_ffmpeg(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required")
    aac = tmp_path / "sine.aac"
    mp3 = tmp_path / "sine.mp3"
    created = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                              "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
                              "-c:a", "aac", "-f", "adts", str(aac)],
                             capture_output=True, timeout=10, check=False)
    assert created.returncode == 0
    assert .2 <= voice.validate_aac(aac) <= 1
    voice.convert_aac_to_mp3(aac, mp3)
    assert mp3.is_file() and .2 <= voice.validate_mp3(mp3) <= 1
    with pytest.raises(voice.VoiceFailure, match="invalid_aac"):
        voice.validate_aac(mp3)

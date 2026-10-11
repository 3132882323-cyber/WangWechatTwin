import hashlib
import http.client
import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image

from app.config import AppConfig
from app.llm import LLMError
from app.local_proxy_llm import LocalProxyReplyLLM
from app.models import RiskLevel


def decision():
    return {"action": "send", "risk": "low", "reply": "说吧，我在。", "confidence": .9,
            "reason": "具体回应", "facts_to_confirm": [], "memory_updates": [],
            "media_description": "测试图片", "media_confidence": .8,
            "candidates": [{"label": label, "reply": reply, "style_match": 4, "context_fit": 4,
                            "continuation": 4, "boundary_respect": 5, "invented_facts": False, "risk": "low"}
                           for label, reply in zip("ABC", ["说吧，我在。", "在，什么事？", "我看到了，你接着说。"])],
            "selected_candidate": "A"}


def payload(contact="fictional-a", external_id="turn-1", **extra):
    return json.dumps({"conversation_key": hashlib.sha256(contact.encode()).hexdigest(),
                       "incoming": {"external_id": external_id, "content": "在吗", "message_type": "text"},
                       "contact_profile": {"name": contact, "notes": "仅本轮虚构测试参考"},
                       "reply_selection": {"mode": "abc"}, **extra}, ensure_ascii=False)


@pytest.fixture
def proxy(tmp_path, monkeypatch):
    state = {"requests": [], "response": decision(), "status": 200, "conv_count": 0,
             "force_conv": None, "missing_conv": False, "gate": None, "started": threading.Event()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            body = json.loads(raw)
            state["requests"].append({"path": self.path, "body": body,
                                      "authorization": self.headers.get("Authorization")})
            state["started"].set()
            if state["gate"]:
                state["gate"].wait(3)
            time.sleep(state.get("header_delay", 0))
            self.send_response(state["status"])
            if state["status"] == 302:
                self.send_header("Location", "http://127.0.0.1:19656/forbidden-redirect")
            response = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant",
                        "content": json.dumps(state["response"], ensure_ascii=False)}}]}
            if body["model"] == "doubao" and not state["missing_conv"]:
                if not body.get("conversation_id"):
                    state["conv_count"] += 1
                response["conversation_id"] = state["force_conv"] or body.get("conversation_id") or f"mock-conv-{state['conv_count']}"
            raw_response = json.dumps(response, ensure_ascii=False).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw_response)))
            self.send_header("Connection", "close")
            self.end_headers()
            time.sleep(state.get("body_delay", 0))
            try:
                self.wfile.write(raw_response)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    actual_connection = http.client.HTTPConnection

    def mock_connection(host, port, **kwargs):
        assert host == "127.0.0.1" and port in {19655, 19656}
        return actual_connection(host, server.server_port, **kwargs)

    monkeypatch.setattr("app.local_proxy_llm.http.client.HTTPConnection", mock_connection)
    key = tmp_path / "private-key.txt"
    key.write_text("mock-private-key", encoding="utf-8")
    key.chmod(0o600)
    config = AppConfig(project_root=tmp_path, openai={"provider": "hybrid_web", "local_proxies": {
        lane: {"enabled": True, "base_url": f"http://127.0.0.1:{port}/v1", "api_key_file": str(key), "timeout_seconds": 1}
        for lane, port in [("deepseek", 19655), ("doubao", 19656)]}})
    try:
        yield config, state
    finally:
        if state["gate"]:
            state["gate"].set()
        server.shutdown(); server.server_close(); thread.join(2)


def call(adapter, value=None):
    return adapter.decide("本轮虚构草稿", value or payload(), RiskLevel.low)


def test_deepseek_sessions_persist_and_do_not_mix_contacts(proxy):
    config, state = proxy
    adapter = LocalProxyReplyLLM(config, "deepseek")
    assert call(adapter).reply == "说吧，我在。"
    call(adapter, payload("fictional-b"))
    call(LocalProxyReplyLLM(config, "deepseek"), payload(external_id="turn-2"))
    a, b, a2 = [row["body"] for row in state["requests"]]
    assert a["user"] == a2["user"] != b["user"]
    assert a["user"] not in {"dev-agent", "default"}
    assert a["model"] == "deepseek-chat" and a["stream"] is False
    assert state["requests"][0]["authorization"] == "Bearer mock-private-key"
    schema = a["messages"][0]["content"]
    assert all(field in schema for field in ["candidates", "boundary_respect", "invented_facts", "media_description", "media_confidence"])


def test_doubao_remote_conversation_is_persistent_and_distinct(proxy):
    config, state = proxy
    call(LocalProxyReplyLLM(config, "doubao"))
    call(LocalProxyReplyLLM(config, "doubao"), payload("fictional-b"))
    call(LocalProxyReplyLLM(config, "doubao"), payload(external_id="turn-2"))
    a, b, a2 = [row["body"] for row in state["requests"]]
    assert a["conversation_id"] == b["conversation_id"] == ""
    assert a2["conversation_id"] == "mock-conv-1"
    with sqlite3.connect(config.resolve(config.openai.local_proxies["doubao"].session_db)) as db:
        assert db.execute("SELECT count(DISTINCT remote_id) FROM local_proxy_sessions").fetchone()[0] == 2


def test_same_contact_lanes_use_separate_persistent_rows(proxy):
    config, _ = proxy
    call(LocalProxyReplyLLM(config, "deepseek"))
    call(LocalProxyReplyLLM(config, "doubao"), payload(external_id="turn-2"))
    with sqlite3.connect(config.resolve(config.openai.local_proxies["deepseek"].session_db)) as db:
        assert db.execute("SELECT count(*),count(DISTINCT session_key) FROM local_proxy_sessions").fetchone() == (2, 2)


def test_completed_external_id_is_never_automatically_resubmitted(proxy):
    config, state = proxy
    call(LocalProxyReplyLLM(config, "deepseek"))
    with pytest.raises(LLMError, match="local_proxy_duplicate_turn") as error:
        call(LocalProxyReplyLLM(config, "deepseek"))
    assert error.value.fallback_safe is False
    assert len(state["requests"]) == 1


def test_completed_external_id_is_never_resubmitted_in_another_lane(proxy):
    config, state = proxy
    call(LocalProxyReplyLLM(config, "deepseek"))
    with pytest.raises(LLMError, match="local_proxy_duplicate_turn"):
        call(LocalProxyReplyLLM(config, "doubao"))
    assert len(state["requests"]) == 1


@pytest.mark.parametrize("status", [302, 401, 429, 503])
def test_http_failures_never_follow_redirect_retry_or_continue_unknown_contact(proxy, status):
    config, state = proxy; state["status"] = status
    adapter = LocalProxyReplyLLM(config, "deepseek")
    with pytest.raises(LLMError, match="local_proxy_http_error"):
        call(adapter)
    for subsequent in [payload(), payload(external_id="turn-2")]:
        with pytest.raises(LLMError, match="local_proxy_unknown"):
            call(LocalProxyReplyLLM(config, "deepseek"), subsequent)
    with pytest.raises(LLMError, match="local_proxy_unknown_session"):
        call(LocalProxyReplyLLM(config, "doubao"), payload(external_id="turn-3"))
    assert len(state["requests"]) == 1
    state["status"] = 200
    assert call(adapter, payload("fictional-b")).action == "send"


def test_pending_claim_blocks_same_contact_before_any_second_post(proxy):
    config, state = proxy; state["gate"] = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(call, LocalProxyReplyLLM(config, "deepseek"))
        assert state["started"].wait(2)
        with pytest.raises(LLMError, match="local_proxy_unknown_session"):
            call(LocalProxyReplyLLM(config, "deepseek"), payload(external_id="turn-2"))
        state["gate"].set()
        assert pending.result(3).action == "send"
    assert len(state["requests"]) == 1


def test_timeout_survives_restart_as_unknown_and_does_not_retry(proxy):
    config, state = proxy; state["gate"] = threading.Event()
    with pytest.raises(LLMError, match="local_proxy_request_unknown"):
        call(LocalProxyReplyLLM(config, "deepseek"))
    state["gate"].set()
    with pytest.raises(LLMError, match="local_proxy_unknown_session"):
        call(LocalProxyReplyLLM(config, "deepseek"), payload(external_id="turn-2"))
    assert len(state["requests"]) == 1


def test_headers_and_closing_response_body_share_one_timeout_budget(proxy):
    config, state = proxy; state.update(header_delay=.6, body_delay=.6)
    with pytest.raises(LLMError, match="local_proxy_(request_unknown|timeout)"):
        call(LocalProxyReplyLLM(config, "deepseek"))
    assert len(state["requests"]) == 1


@pytest.mark.parametrize("bad", ["plain text", {"action": "send", "risk": "low", "reply": "no ABC"},
                                  {**decision(), "media_confidence": 1.1}])
def test_invalid_structured_output_is_unknown_and_never_plain_text_reply(proxy, bad):
    config, state = proxy; state["response"] = bad
    with pytest.raises(LLMError, match="local_proxy_invalid_decision"):
        call(LocalProxyReplyLLM(config, "deepseek"))
    assert len(state["requests"]) == 1


@pytest.mark.parametrize("field,value", [("style_match", True), ("invented_facts", "false")])
def test_candidate_scores_and_booleans_are_strict(proxy, field, value):
    config, state = proxy; state["response"]["candidates"][0][field] = value
    with pytest.raises(LLMError, match="local_proxy_invalid_decision"):
        call(LocalProxyReplyLLM(config, "deepseek"))


def test_direct_mode_discards_unused_invalid_candidates_like_existing_web_contract(proxy):
    config, state = proxy; state["response"].update(candidates="unused", selected_candidate="Z")
    result = call(LocalProxyReplyLLM(config, "deepseek"), payload(reply_selection={"mode": "direct"}))
    assert result.candidates is None and result.selected_candidate is None


@pytest.mark.parametrize("changed", [False, True])
def test_doubao_missing_or_changed_conversation_is_not_reused(proxy, changed):
    config, state = proxy; adapter = LocalProxyReplyLLM(config, "doubao")
    if changed:
        call(adapter); state["force_conv"] = "foreign-conv"
    else:
        state["missing_conv"] = True
    with pytest.raises(LLMError, match="local_proxy_(conversation_changed|missing_conversation)"):
        call(adapter, payload(external_id="turn-2"))
    with pytest.raises(LLMError, match="local_proxy_unknown_session"):
        call(adapter, payload(external_id="turn-3"))


def test_foreign_doubao_conversation_collision_is_rejected(proxy):
    config, state = proxy; adapter = LocalProxyReplyLLM(config, "doubao")
    call(adapter); state["force_conv"] = "mock-conv-1"
    with pytest.raises(LLMError, match="local_proxy_conversation_collision"):
        call(adapter, payload("fictional-b"))


def test_png_images_are_bounded_inline_data_from_owned_asset_roots(proxy):
    config, state = proxy
    root = config.resolve(config.paths.history_reader) / "image_assets"; root.mkdir(parents=True)
    image = root / "fixture.png"; Image.new("RGB", (2, 2), "red").save(image)
    call(LocalProxyReplyLLM(config, "doubao"), payload(__media_paths=[str(image)]))
    user_parts = state["requests"][0]["body"]["messages"][1]["content"]
    assert user_parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert str(image) not in user_parts[0]["text"]


@pytest.mark.parametrize("variant", ["outside", "large", "four", "deepseek"])
def test_invalid_image_scope_size_count_or_lane_makes_no_request(proxy, variant):
    config, state = proxy
    root = config.resolve(config.paths.history_reader) / "sticker_assets"; root.mkdir(parents=True)
    image = (config.project_root if variant == "outside" else root) / "fixture.png"
    Image.new("RGB", (2, 2), "red").save(image)
    if variant == "large": image.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    lane = "deepseek" if variant == "deepseek" else "doubao"
    with pytest.raises(LLMError, match="local_proxy_(invalid_images|wrong_media_lane)"):
        call(LocalProxyReplyLLM(config, lane), payload(__media_paths=[str(image)] * (4 if variant == "four" else 1)))
    assert not state["requests"]


def test_missing_external_id_or_key_fails_before_submission(proxy):
    config, state = proxy; adapter = LocalProxyReplyLLM(config, "deepseek")
    value = json.loads(payload()); value["incoming"].pop("external_id")
    with pytest.raises(LLMError, match="local_proxy_invalid_payload"):
        call(adapter, json.dumps(value))
    adapter.key_file.unlink()
    with pytest.raises(LLMError, match="local_proxy_key_unavailable"):
        call(adapter)
    assert not state["requests"]


def test_utf8_bom_in_private_key_is_accepted(proxy):
    config, state = proxy; adapter = LocalProxyReplyLLM(config, "deepseek")
    adapter.key_file.write_text("mock-private-key", encoding="utf-8-sig")
    call(adapter)
    assert state["requests"][0]["authorization"] == "Bearer mock-private-key"


def test_combined_image_limit_is_four_mib_even_when_each_png_is_under_two(proxy):
    config, state = proxy
    root = config.resolve(config.paths.history_reader) / "image_assets"; root.mkdir(parents=True)
    image = root / "noise.png"
    Image.frombytes("RGB", (750, 750), os.urandom(750 * 750 * 3)).save(image)
    assert 4 * 1024 * 1024 / 3 < image.stat().st_size <= 2 * 1024 * 1024
    with pytest.raises(LLMError, match="local_proxy_invalid_images"):
        call(LocalProxyReplyLLM(config, "doubao"), payload(__media_paths=[str(image)] * 3))
    assert not state["requests"]
    assert not config.resolve(config.openai.local_proxies["doubao"].session_db).exists()


@pytest.mark.parametrize("system", ["x" * (8 * 1024 * 1024), "bad-surrogate-\ud800"],
                         ids=["oversized_body", "invalid_unicode"])
def test_request_size_and_serialization_are_validated_before_claim(proxy, system):
    config, state = proxy
    with pytest.raises(LLMError, match="local_proxy_invalid_payload"):
        LocalProxyReplyLLM(config, "doubao").decide(system, payload(), RiskLevel.low)
    assert not state["requests"]
    assert not config.resolve(config.openai.local_proxies["doubao"].session_db).exists()


def test_unknown_exception_details_and_credentials_never_escape(proxy, monkeypatch):
    config, _ = proxy; adapter = LocalProxyReplyLLM(config, "deepseek")
    def fail(*args):
        raise RuntimeError("SECRET-CREDENTIAL http://private.invalid/?token=SECRET")
    monkeypatch.setattr(adapter, "_post", fail)
    with pytest.raises(LLMError) as error:
        call(adapter)
    assert str(error.value) == "local_proxy_request_unknown"
    assert error.value.fallback_safe is False

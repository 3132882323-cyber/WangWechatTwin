import hashlib
import http.client
import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

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


def recovery_payload(contact="fictional-a", external_id="turn-2", **extra):
    value = json.loads(payload(contact, external_id, **extra))
    incoming = value["incoming"]
    value.setdefault("recent_messages", [{"direction": "in", "sender": contact,
                     "content": incoming["content"], "message_type": incoming["message_type"]}])
    value.setdefault("current_personal_conversation", [{"direction": "in", "content": incoming["content"],
                     "kind": incoming["message_type"], "provenance": "wechat_original"}])
    value.setdefault("__local_proxy_recovery_context", {"version": 1, "source": "local_wechat",
                     "conversation_key": value["conversation_key"], "external_id": external_id,
                     "complete": True, "unresolved_media": False})
    return json.dumps(value, ensure_ascii=False)


@pytest.fixture
def proxy(tmp_path, monkeypatch):
    state = {"requests": [], "response": decision(), "status": 200, "conv_count": 0,
             "force_conv": None, "missing_conv": False, "gate": None, "started": threading.Event(),
             "now": 1_700_000_000.0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            body = json.loads(raw)
            state["requests"].append({"path": self.path, "body": body,
                                      "authorization": self.headers.get("Authorization")})
            state["started"].set()
            if state.get("drop_after_read"):
                # The full request was received, but its outcome is unknown.
                self.close_connection = True
                return
            if state["gate"]:
                state["gate"].wait(3)
            time.sleep(state.get("header_delay", 0))
            self.send_response(state["status"])
            if state["status"] == 302:
                self.send_header("Location", "http://127.0.0.1:19656/forbidden-redirect")
            response = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant",
                        "content": state.get("raw_assistant", json.dumps(state["response"], ensure_ascii=False))}}]}
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
    monkeypatch.setattr("app.local_proxy_llm.time", SimpleNamespace(time=lambda: state["now"], monotonic=time.monotonic))
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


@pytest.mark.parametrize("lane", ["deepseek", "doubao"])
def test_refused_before_request_keeps_old_turn_but_allows_new_message(proxy, monkeypatch, lane):
    config, state = proxy
    actual_factory = http.client.HTTPConnection

    class RefusedConnection:
        def connect(self):
            raise ConnectionRefusedError("mock local listener is down")

        def close(self):
            pass

    monkeypatch.setattr("app.local_proxy_llm.http.client.HTTPConnection",
                        lambda *args, **kwargs: RefusedConnection())
    adapter = LocalProxyReplyLLM(config, lane)
    with pytest.raises(LLMError, match="local_proxy_not_submitted") as error:
        call(adapter)
    assert error.value.fallback_safe is False
    assert not state["requests"]
    with sqlite3.connect(adapter.session_db) as db:
        assert db.execute("SELECT status FROM local_proxy_turns WHERE lane=?", (lane,)).fetchone()[0] == "not_submitted"
        assert db.execute("SELECT state FROM local_proxy_sessions WHERE lane=?", (lane,)).fetchone()[0] == "ready"

    monkeypatch.setattr("app.local_proxy_llm.http.client.HTTPConnection", actual_factory)
    with pytest.raises(LLMError, match="local_proxy_not_submitted"):
        call(LocalProxyReplyLLM(config, lane))
    assert not state["requests"]
    assert call(LocalProxyReplyLLM(config, lane), payload(external_id="turn-2")).action == "send"
    assert len(state["requests"]) == 1


def test_disconnect_requires_verified_local_context_before_a_new_turn(proxy):
    config, state = proxy
    state["drop_after_read"] = True
    adapter = LocalProxyReplyLLM(config, "deepseek")
    with pytest.raises(LLMError, match="local_proxy_request_unknown"):
        call(adapter)
    assert len(state["requests"]) == 1
    state["drop_after_read"] = False
    with pytest.raises(LLMError, match="local_proxy_recovery_context_required"):
        call(adapter, payload(external_id="turn-2"))
    assert len(state["requests"]) == 1


@pytest.mark.parametrize("status", [302, 401, 429, 503])
def test_http_failures_have_fixed_status_and_never_redirect_retry_or_reuse_context(proxy, status):
    config, state = proxy; state["status"] = status
    adapter = LocalProxyReplyLLM(config, "deepseek")
    with pytest.raises(LLMError, match=f"local_proxy_http_{status}$"):
        call(adapter)
    with pytest.raises(LLMError, match="local_proxy_unknown_result"):
        call(LocalProxyReplyLLM(config, "deepseek"), payload())
    with pytest.raises(LLMError, match="local_proxy_recovery_context_required"):
        call(LocalProxyReplyLLM(config, "deepseek"), payload(external_id="turn-2"))
    with pytest.raises(LLMError, match="local_proxy_recovery_context_required"):
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
    with pytest.raises(LLMError, match="local_proxy_recovery_context_required"):
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
    with pytest.raises(LLMError, match="local_proxy_recovery_context_required"):
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


def test_conflicting_external_id_aliases_are_rejected_before_claim(proxy):
    config, state = proxy
    value = json.loads(payload())
    value["external_id"] = "different-turn"
    with pytest.raises(LLMError, match="local_proxy_invalid_payload"):
        call(LocalProxyReplyLLM(config, "doubao"), json.dumps(value))
    assert not state["requests"]
    assert not config.resolve(config.openai.local_proxies["doubao"].session_db).exists()


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


def test_unknown_draft_can_rebuild_only_for_a_new_message_after_cooldown(proxy):
    config, state = proxy
    adapter = LocalProxyReplyLLM(config, "deepseek")
    state["drop_after_read"] = True
    with pytest.raises(LLMError, match="local_proxy_request_unknown"):
        call(adapter)
    original = state["requests"][0]["body"]["user"]
    state["drop_after_read"] = False
    clean = recovery_payload(optional_model_candidates=[{"content": "UNSENT-DRAFT"}],
                             contact_memories=["OLD-SUMMARY"], historical_conversation_memory=[{"content": "OLD-AI-MEDIA"}],
                             style_examples=[{"preferred_reply": "OLD-STYLE"}], owner_role_profile={"summary": "OLD-PROFILE"})
    with pytest.raises(LLMError, match="local_proxy_cooldown"):
        call(LocalProxyReplyLLM(config, "deepseek"), clean)
    assert len(state["requests"]) == 1
    state["now"] += 31
    assert call(LocalProxyReplyLLM(config, "deepseek"), clean).action == "send"
    replacement = state["requests"][1]["body"]
    assert replacement["user"] != original
    assert len(replacement["messages"]) == 2
    wire = json.dumps(replacement, ensure_ascii=False)
    assert all(token not in wire for token in ["UNSENT-DRAFT", "OLD-SUMMARY", "OLD-AI-MEDIA", "OLD-STYLE", "OLD-PROFILE",
                                               "__local_proxy_recovery_context", "turn-1"])
    assert "turn-2" in wire and "旧模型草稿不表示已向微信发送" in wire
    with sqlite3.connect(adapter.session_db) as db:
        assert db.execute("SELECT status,generation FROM local_proxy_turns ORDER BY created").fetchall() == [("unknown", 0), ("done", 1)]
        assert db.execute("SELECT session_key,state,generation FROM local_proxy_session_history").fetchone() == (original, "unknown", 0)
        assert db.execute("SELECT state,generation,rebuild_required FROM local_proxy_sessions").fetchone() == ("ready", 1, 0)
    for lane in ("deepseek", "doubao"):
        with pytest.raises(LLMError, match="local_proxy_unknown_result"):
            call(LocalProxyReplyLLM(config, lane), recovery_payload(external_id="turn-1"))
        with pytest.raises(LLMError, match="local_proxy_duplicate_turn"):
            call(LocalProxyReplyLLM(config, lane), clean)
    assert len(state["requests"]) == 2
    call(LocalProxyReplyLLM(config, "deepseek"), payload(external_id="turn-3"))
    assert state["requests"][2]["body"]["user"] == replacement["user"]


def test_other_contact_is_not_frozen_by_a_malformed_reply(proxy):
    config, state = proxy
    state["response"] = "not JSON"
    with pytest.raises(LLMError, match="local_proxy_invalid_decision"):
        call(LocalProxyReplyLLM(config, "deepseek"))
    state["response"] = decision()
    assert call(LocalProxyReplyLLM(config, "deepseek"), payload("fictional-new-friend")).action == "send"
    assert len(state["requests"]) == 2
    with pytest.raises(LLMError, match="local_proxy_cooldown"):
        call(LocalProxyReplyLLM(config, "deepseek"), recovery_payload())


def test_backoff_is_persistent_capped_and_only_actual_failed_requests_increase_it(proxy):
    config, state = proxy
    state["status"] = 503
    adapter = LocalProxyReplyLLM(config, "deepseek")
    for number, delay in enumerate([30, 60, 120, 240, 300, 300], 1):
        value = recovery_payload(external_id=f"failure-{number}")
        with pytest.raises(LLMError, match="local_proxy_http_503"):
            call(LocalProxyReplyLLM(config, "deepseek"), value)
        with sqlite3.connect(adapter.session_db) as db:
            assert db.execute("SELECT failures,retry_after-last_failure FROM local_proxy_backoff").fetchone() == (number, delay)
        for repeat in range(2):
            with pytest.raises(LLMError, match="local_proxy_cooldown"):
                call(LocalProxyReplyLLM(config, "deepseek"), recovery_payload(external_id=f"blocked-{number}-{repeat}"))
        assert len(state["requests"]) == number
        state["now"] += delay
    state["status"] = 200
    call(LocalProxyReplyLLM(config, "deepseek"), recovery_payload(external_id="success-after-backoff"))
    with sqlite3.connect(adapter.session_db) as db:
        assert db.execute("SELECT count(*) FROM local_proxy_backoff").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM local_proxy_turns WHERE status='unknown'").fetchone()[0] == 6
        assert db.execute("SELECT count(*) FROM local_proxy_session_history").fetchone()[0] == 6


def test_cross_lane_recovery_quarantines_both_old_contexts_and_keeps_lane_cooldown(proxy):
    config, state = proxy
    call(LocalProxyReplyLLM(config, "deepseek"))
    call(LocalProxyReplyLLM(config, "doubao"), payload(external_id="image-1"))
    first_ds = state["requests"][0]["body"]["user"]
    state["status"] = 503
    with pytest.raises(LLMError, match="local_proxy_http_503"):
        call(LocalProxyReplyLLM(config, "doubao"), payload(external_id="image-2"))
    state["status"] = 200
    assert call(LocalProxyReplyLLM(config, "deepseek"), recovery_payload(external_id="new-text")).action == "send"
    assert state["requests"][-1]["body"]["user"] != first_ds
    adapter = LocalProxyReplyLLM(config, "doubao")
    with sqlite3.connect(adapter.session_db) as db:
        assert db.execute("SELECT count(*) FROM local_proxy_session_history").fetchone()[0] == 2
        assert db.execute("SELECT generation,rebuild_required,remote_id FROM local_proxy_sessions WHERE lane='doubao'").fetchone() == (1, 1, None)
    with pytest.raises(LLMError, match="local_proxy_recovery_context_required"):
        call(adapter, payload(external_id="new-image"))
    with pytest.raises(LLMError, match="local_proxy_cooldown"):
        call(adapter, recovery_payload(external_id="new-image"))
    state["now"] += 31
    call(adapter, recovery_payload(external_id="new-image"))
    assert state["requests"][-1]["body"]["conversation_id"] == ""
    with sqlite3.connect(adapter.session_db) as db:
        assert db.execute("SELECT generation,remote_id FROM local_proxy_sessions WHERE lane='doubao'").fetchone() == (1, "mock-conv-2")


@pytest.mark.parametrize("other_lane", ["deepseek", "doubao"])
def test_pending_request_blocks_recovery_in_every_lane_even_with_context(proxy, other_lane):
    config, state = proxy
    state["gate"] = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(call, LocalProxyReplyLLM(config, "deepseek"))
        assert state["started"].wait(2)
        with pytest.raises(LLMError, match="local_proxy_unknown_session"):
            call(LocalProxyReplyLLM(config, other_lane), recovery_payload())
        state["gate"].set()
        assert pending.result(3).action == "send"
    assert len(state["requests"]) == 1


def test_concurrent_recovery_claims_only_one_new_generation(proxy):
    config, state = proxy
    state["status"] = 503
    with pytest.raises(LLMError, match="local_proxy_http_503"):
        call(LocalProxyReplyLLM(config, "deepseek"))
    state.update(status=200, now=state["now"] + 31, gate=threading.Event())
    state["started"].clear()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(call, LocalProxyReplyLLM(config, "deepseek"), recovery_payload())
        assert state["started"].wait(2)
        for lane in ("deepseek", "doubao"):
            with pytest.raises(LLMError, match="local_proxy_unknown_session"):
                call(LocalProxyReplyLLM(config, lane), recovery_payload(external_id="turn-3"))
            with pytest.raises(LLMError, match="local_proxy_unknown_result"):
                call(LocalProxyReplyLLM(config, lane), recovery_payload())
        state["gate"].set()
        assert pending.result(3).action == "send"
    adapter = LocalProxyReplyLLM(config, "deepseek")
    with sqlite3.connect(adapter.session_db) as db:
        assert db.execute("SELECT count(*) FROM local_proxy_session_history").fetchone()[0] == 1
        assert db.execute("SELECT generation FROM local_proxy_sessions").fetchone()[0] == 1
    assert len(state["requests"]) == 2


@pytest.mark.parametrize("problem", ["no_marker", "wrong_source", "wrong_contact", "old_external", "incomplete", "bool_version",
                                     "missing_recent", "missing_personal", "malformed_history", "different_incoming", "media_dependency"])
def test_recovery_rejects_missing_stale_or_incomplete_local_evidence(proxy, problem):
    config, state = proxy
    state["status"] = 503
    adapter = LocalProxyReplyLLM(config, "deepseek")
    with pytest.raises(LLMError, match="local_proxy_http_503"):
        call(adapter)
    state.update(status=200, now=state["now"] + 31)
    value = json.loads(recovery_payload())
    evidence = value["__local_proxy_recovery_context"]
    if problem == "no_marker": value.pop("__local_proxy_recovery_context")
    elif problem == "wrong_source": evidence["source"] = "model_asserted"
    elif problem == "wrong_contact": evidence["conversation_key"] = "f" * 64
    elif problem == "old_external": evidence["external_id"] = "turn-1"
    elif problem == "incomplete": evidence["complete"] = False
    elif problem == "bool_version": evidence["version"] = True
    elif problem == "missing_recent": value.pop("recent_messages")
    elif problem == "missing_personal": value.pop("current_personal_conversation")
    elif problem == "malformed_history": value["recent_messages"][0]["direction"] = "unknown"
    elif problem == "different_incoming": value["incoming"]["content"] = "这个图多少钱"
    elif problem == "media_dependency": evidence["unresolved_media"] = True
    with pytest.raises(LLMError, match="local_proxy_recovery_(context_required|media_unresolved)"):
        call(adapter, json.dumps(value, ensure_ascii=False))
    with sqlite3.connect(adapter.session_db) as db:
        assert db.execute("SELECT count(*) FROM local_proxy_session_history").fetchone()[0] == 0
        assert db.execute("SELECT state,generation FROM local_proxy_sessions").fetchone() == ("unknown", 0)
        assert db.execute("SELECT count(*) FROM local_proxy_turns").fetchone()[0] == 1
    assert len(state["requests"]) == 1


@pytest.mark.parametrize("same_contact", [True, False])
def test_quarantined_remote_id_is_never_accepted_again_even_for_its_original_contact(proxy, same_contact):
    config, state = proxy
    adapter = LocalProxyReplyLLM(config, "doubao")
    call(adapter)
    state["status"] = 503
    with pytest.raises(LLMError, match="local_proxy_http_503"):
        call(adapter, payload(external_id="turn-2"))
    state.update(status=200, now=state["now"] + 31)
    call(adapter, recovery_payload(external_id="turn-3"))
    state["force_conv"] = "mock-conv-1"
    if same_contact:
        # Fail the replacement, then try to attach its next generation to the
        # original remote. Normal same-generation changed-ID checks also remain.
        state["status"] = 503
        with pytest.raises(LLMError, match="local_proxy_http_503"):
            call(adapter, payload(external_id="turn-4"))
        state.update(status=200, now=state["now"] + 31)
        value = recovery_payload(external_id="turn-5")
        expected = "local_proxy_quarantined_conversation"
    else:
        value = payload("fictional-b")
        expected = "local_proxy_conversation_collision"
    with pytest.raises(LLMError, match=expected):
        call(LocalProxyReplyLLM(config, "doubao"), value)
    with sqlite3.connect(adapter.session_db) as db:
        assert db.execute("SELECT count(*) FROM local_proxy_session_history WHERE remote_id='mock-conv-1'").fetchone()[0] == 1


@pytest.mark.parametrize("same_contact", [True, False])
def test_malformed_first_doubao_reply_keeps_remote_binding_for_quarantine(proxy, same_contact):
    config, state = proxy
    adapter = LocalProxyReplyLLM(config, "doubao")
    state["response"] = "not a decision"
    with pytest.raises(LLMError, match="local_proxy_invalid_decision"):
        call(adapter)
    with sqlite3.connect(adapter.session_db) as db:
        assert db.execute("SELECT state,remote_id FROM local_proxy_sessions").fetchone() == ("unknown", "mock-conv-1")
    state.update(response=decision(), now=state["now"] + 31)
    if same_contact:
        value = recovery_payload()
        expected = "local_proxy_quarantined_conversation"
    else:
        call(adapter, recovery_payload())
        value = payload("fictional-b")
        expected = "local_proxy_conversation_collision"
    state["force_conv"] = "mock-conv-1"
    with pytest.raises(LLMError, match=expected):
        call(adapter, value)
    with sqlite3.connect(adapter.session_db) as db:
        assert db.execute("SELECT remote_id,state FROM local_proxy_session_history").fetchone() == ("mock-conv-1", "unknown")
        assert db.execute("SELECT count(*) FROM local_proxy_turns WHERE status='unknown'").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM local_proxy_sessions WHERE remote_id='mock-conv-1'").fetchone()[0] == 0


def test_malformed_doubao_reply_never_steals_another_contacts_remote_binding(proxy):
    config, state = proxy
    adapter = LocalProxyReplyLLM(config, "doubao")
    call(adapter)
    state.update(response="not a decision", force_conv="mock-conv-1")
    with pytest.raises(LLMError, match="local_proxy_invalid_decision"):
        call(adapter, payload("fictional-b"))
    with sqlite3.connect(adapter.session_db) as db:
        rows = dict(db.execute("SELECT state,remote_id FROM local_proxy_sessions"))
        assert rows == {"ready": "mock-conv-1", "unknown": None}
        assert db.execute("SELECT status FROM local_proxy_turns ORDER BY rowid").fetchall() == [("done",), ("unknown",)]


@pytest.mark.parametrize("same_contact", [True, False])
@pytest.mark.parametrize("returned_id", ["mock-conv-1", "unexpected-old-context"])
def test_changed_doubao_reply_quarantines_both_ids_across_restart(proxy, same_contact, returned_id):
    config, state = proxy
    adapter = LocalProxyReplyLLM(config, "doubao")
    call(adapter)
    state["force_conv"] = "unexpected-old-context"
    with pytest.raises(LLMError, match="local_proxy_conversation_changed"):
        call(adapter, payload(external_id="turn-2"))
    with sqlite3.connect(adapter.session_db) as db:
        assert db.execute("SELECT remote_id,state FROM local_proxy_sessions").fetchone() == ("mock-conv-1", "unknown")
        assert set(db.execute("SELECT remote_id,generation FROM local_proxy_quarantined_ids")) == {
            ("mock-conv-1", 0), ("unexpected-old-context", 0)}
    state.update(force_conv=returned_id, now=state["now"] + 31)
    adapter = LocalProxyReplyLLM(config, "doubao")
    value = recovery_payload(external_id="turn-3") if same_contact else payload("fictional-b")
    expected = "local_proxy_quarantined_conversation" if same_contact else "local_proxy_conversation_collision"
    with pytest.raises(LLMError, match=expected):
        call(adapter, value)
    with sqlite3.connect(adapter.session_db) as db:
        assert db.execute("SELECT count(*) FROM local_proxy_turns WHERE status='unknown'").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM local_proxy_quarantined_ids").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM local_proxy_sessions WHERE remote_id='unexpected-old-context'").fetchone()[0] == 0


def test_changed_doubao_reply_does_not_quarantine_another_contacts_live_id(proxy):
    config, state = proxy
    adapter = LocalProxyReplyLLM(config, "doubao")
    call(adapter)
    call(adapter, payload("fictional-b"))
    state["force_conv"] = "mock-conv-2"
    with pytest.raises(LLMError, match="local_proxy_conversation_changed"):
        call(adapter, payload(external_id="turn-2"))
    with sqlite3.connect(adapter.session_db) as db:
        assert db.execute("SELECT remote_id FROM local_proxy_quarantined_ids").fetchall() == [("mock-conv-1",)]
    state["force_conv"] = None
    assert call(adapter, payload("fictional-b", external_id="turn-2")).action == "send"
    assert state["requests"][-1]["body"]["conversation_id"] == "mock-conv-2"


def test_refused_recovery_keeps_fresh_context_requirement_and_original_unknown(proxy, monkeypatch):
    config, state = proxy
    adapter = LocalProxyReplyLLM(config, "doubao")
    state["status"] = 503
    with pytest.raises(LLMError, match="local_proxy_http_503"):
        call(adapter)
    state.update(status=200, now=state["now"] + 31)
    actual_post = adapter._post

    def refuse(*args):
        from app.local_proxy_llm import _NoSubmission
        raise _NoSubmission

    monkeypatch.setattr(adapter, "_post", refuse)
    with pytest.raises(LLMError, match="local_proxy_not_submitted"):
        call(adapter, recovery_payload())
    with sqlite3.connect(adapter.session_db) as db:
        assert db.execute("SELECT state,generation,rebuild_required FROM local_proxy_sessions").fetchone() == ("ready", 1, 1)
        assert db.execute("SELECT failures FROM local_proxy_backoff").fetchone() == (1,)
        assert db.execute("SELECT status FROM local_proxy_turns ORDER BY rowid").fetchall() == [("unknown",), ("not_submitted",)]
    monkeypatch.setattr(adapter, "_post", actual_post)
    with pytest.raises(LLMError, match="local_proxy_recovery_context_required"):
        call(adapter, payload(external_id="turn-3"))
    for lane in ("deepseek", "doubao"):
        with pytest.raises(LLMError, match="local_proxy_not_submitted"):
            call(LocalProxyReplyLLM(config, lane), recovery_payload())
    assert call(adapter, recovery_payload(external_id="turn-3")).action == "send"
    with sqlite3.connect(adapter.session_db) as db:
        assert db.execute("SELECT generation,rebuild_required FROM local_proxy_sessions").fetchone() == (1, 0)
        assert db.execute("SELECT count(*) FROM local_proxy_session_history").fetchone()[0] == 1
    assert len(state["requests"]) == 2


def test_fresh_current_png_can_rebuild_doubao_but_missing_asset_cannot(proxy):
    config, state = proxy
    adapter = LocalProxyReplyLLM(config, "doubao")
    state["status"] = 503
    with pytest.raises(LLMError, match="local_proxy_http_503"):
        call(adapter)
    state.update(status=200, now=state["now"] + 31)
    current = {"external_id": "fresh-image", "content": "[图片]", "message_type": "image"}
    with pytest.raises(LLMError, match="local_proxy_recovery_media_unresolved"):
        call(adapter, recovery_payload(external_id="fresh-image", incoming=current))
    root = config.resolve(config.paths.history_reader) / "image_assets"
    root.mkdir(parents=True)
    image = root / "fresh.png"
    Image.new("RGB", (2, 2), "blue").save(image)
    result = call(adapter, recovery_payload(external_id="fresh-image", incoming=current, __media_paths=[str(image)]))
    assert result.media_description == "测试图片"
    assert state["requests"][-1]["body"]["conversation_id"] == ""
    assert state["requests"][-1]["body"]["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_existing_database_migrates_without_rewriting_terminal_unknown_history(proxy):
    config, state = proxy
    adapter = LocalProxyReplyLLM(config, "deepseek")
    adapter.session_db.parent.mkdir(parents=True, exist_ok=True)
    contact = json.loads(payload())["conversation_key"]
    request_key = hashlib.sha256(b"turn-1").hexdigest()
    with sqlite3.connect(adapter.session_db) as db:
        db.executescript("""CREATE TABLE local_proxy_sessions(lane TEXT NOT NULL,conversation_key TEXT NOT NULL,
            session_key TEXT NOT NULL,remote_id TEXT,state TEXT NOT NULL DEFAULT 'ready',
            PRIMARY KEY(lane,conversation_key),UNIQUE(lane,remote_id));
            CREATE TABLE local_proxy_turns(lane TEXT NOT NULL,conversation_key TEXT NOT NULL,request_key TEXT NOT NULL,
            status TEXT NOT NULL,error_code TEXT NOT NULL DEFAULT '',created REAL NOT NULL,
            PRIMARY KEY(lane,conversation_key,request_key));""")
        db.execute("INSERT INTO local_proxy_sessions VALUES(?,?,?,NULL,'unknown')", ("deepseek", contact, "wechat-proxy-" + "a" * 32))
        db.execute("INSERT INTO local_proxy_turns VALUES(?,?,?,'unknown','local_proxy_http_error',?)", ("deepseek", contact, request_key, 123.0))
    call(adapter, recovery_payload())
    with sqlite3.connect(adapter.session_db) as db:
        assert db.execute("SELECT status,error_code,created,generation FROM local_proxy_turns WHERE request_key=?", (request_key,)).fetchone() == (
            "unknown", "local_proxy_http_error", 123.0, 0)
        assert db.execute("SELECT session_key FROM local_proxy_session_history").fetchone()[0] == "wechat-proxy-" + "a" * 32
    assert len(state["requests"]) == 1


def test_legacy_pending_after_restart_is_not_assumed_terminal_or_rotated(proxy):
    config, state = proxy
    adapter = LocalProxyReplyLLM(config, "deepseek")
    contact = json.loads(payload())["conversation_key"]
    adapter._reserve(contact, hashlib.sha256(b"turn-1").hexdigest(), "ready")
    state["now"] += 86400
    with pytest.raises(LLMError, match="local_proxy_unknown_session"):
        call(LocalProxyReplyLLM(config, "doubao"), recovery_payload())
    with sqlite3.connect(adapter.session_db) as db:
        assert db.execute("SELECT status,generation FROM local_proxy_turns").fetchone() == ("pending", 0)
        assert db.execute("SELECT count(*) FROM local_proxy_session_history").fetchone()[0] == 0
    assert not state["requests"]


def test_single_json_fence_is_allowed_with_the_same_strict_contract(proxy):
    config, state = proxy
    state["raw_assistant"] = "\n```json\n" + json.dumps(decision(), ensure_ascii=False) + "\n```\n"
    assert call(LocalProxyReplyLLM(config, "deepseek")).action == "send"


@pytest.mark.parametrize("wrapper", ["prefix", "suffix", "double", "wrong_label", "string_bool", "string_score", "missing_abc"])
def test_json_fence_does_not_allow_extra_prose_or_relaxed_schema(proxy, wrapper):
    config, state = proxy
    value = decision()
    if wrapper == "string_bool": value["candidates"][0]["invented_facts"] = "false"
    elif wrapper == "string_score": value["candidates"][0]["style_match"] = "4"
    elif wrapper == "missing_abc": value.pop("candidates")
    content = "```json\n" + json.dumps(value, ensure_ascii=False) + "\n```"
    if wrapper == "prefix": content = "这是我的回复：\n" + content
    elif wrapper == "suffix": content += "\n请采纳。"
    elif wrapper == "double": content += "\n" + content
    elif wrapper == "wrong_label": content = content.replace("```json", "```python")
    state["raw_assistant"] = content
    with pytest.raises(LLMError, match="local_proxy_invalid_decision"):
        call(LocalProxyReplyLLM(config, "deepseek"))
    assert len(state["requests"]) == 1

"""GLM routing and sender-policy contracts, using synthetic records and fake HTTP."""
import hashlib
import json
import sqlite3
import threading
import time
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.adapters.mock import MockAdapter
from app.config import AppConfig, OpenAISettings
from app.db import Database
from app.llm import LLMError, ReplyLLM
from app.local_proxy_llm import LocalProxyReplyLLM
from app.models import IncomingMessage, ReplyDecision, RiskLevel
from app.pipeline import ReplyPipeline
from app.reply_scheduler import ReplyScheduler
from app.reply_selection import select_reply


def decision():
    replies = ["说吧，我在。", "在，什么事？", "我看到了，你接着说。"]
    return {"action": "send", "risk": "low", "reply": replies[0], "confidence": .95,
            "reason": "具体回应", "facts_to_confirm": [], "memory_updates": [],
            "media_description": "", "media_confidence": 0, "holding_reply": "should-not-send-for-tools",
            "candidates": [{"label": label, "reply": text, "style_match": 4, "context_fit": 4,
                            "continuation": 4, "boundary_respect": 5, "invented_facts": False, "risk": "low"}
                           for label, text in zip("ABC", replies)], "selected_candidate": "A"}


def payload(contact="synthetic-a", external_id="turn-1", *, recovery=False, **extra):
    value = {"conversation_key": hashlib.sha256(contact.encode()).hexdigest(),
             "incoming": {"external_id": external_id, "content": "在吗", "message_type": "text"},
             "contact_profile": {"name": contact}, "reply_selection": {"mode": "abc"}, **extra}
    if recovery:
        value.update(recent_messages=[{"direction": "in", "sender": contact, "content": "在吗", "message_type": "text"}],
                     current_personal_conversation=[{"direction": "in", "content": "在吗", "kind": "text", "provenance": "wechat_original"}],
                     __local_proxy_recovery_context={"version": 1, "source": "local_wechat", "conversation_key": value["conversation_key"],
                                                    "external_id": external_id, "complete": True, "unresolved_media": False})
    return json.dumps(value, ensure_ascii=False)


def configured(tmp_path):
    key = tmp_path / "synthetic-key.txt"
    key.write_text("synthetic-key", encoding="utf-8")
    return AppConfig(project_root=tmp_path, adapter="mock", mode="low_risk_auto",
                     wechat={"sender_all_existing_chats": True, "allow_unknown_contacts": True,
                             "unknown_contact_mode": "low_risk_auto", "allow_groups": True,
                             "group_only_mentions": True, "send_holding_on_review": True, "use_greeting_cache": False},
                     openai={"provider": "hybrid_web", "everyday_lane": "glm", "disabled_lanes": ["deepseek"],
                             "local_proxies": {lane: {"enabled": True, "base_url": f"http://127.0.0.1:{port}/v1",
                                                       "api_key_file": str(key), "timeout_seconds": 90}
                                               for lane, port in (("glm", 19659), ("doubao", 19656))}})


@pytest.fixture
def glm(tmp_path, monkeypatch):
    config = configured(tmp_path)
    state = {"calls": [], "users": {}, "error": None, "tool": False, "missing_tool": False,
             "remote": None, "raw_decision": None, "now": 1_700_000_000.0}

    def fake_post(self, raw, key):
        assert key == "synthetic-key"
        body = json.loads(raw)
        state["calls"].append((self.lane, body))
        if state["error"]:
            raise LLMError(state["error"])
        if self.lane == "glm":
            assert self.port == 19659 and body["model"] == "chatglm-web"
            assert set(body) == {"model", "stream", "messages", "user", "request_id"}
            assert len(body["user"]) == len(body["request_id"]) == 64
            assert all(isinstance(message["content"], str) for message in body["messages"])
            remote = state["users"].setdefault(body["user"], "mock-glm-" + str(len(state["users"]) + 1))
        else:
            remote = body.get("conversation_id") or "mock-doubao-1"
        result = {"conversation_id": state["remote"] or remote,
                  "choices": [{"finish_reason": "stop", "message": {"role": "assistant",
                               "content": state["raw_decision"] or json.dumps(decision(), ensure_ascii=False)}}]}
        if not state["missing_tool"]:
            result["tool_metadata_seen"] = state["tool"]
        return result

    monkeypatch.setattr(LocalProxyReplyLLM, "_post", fake_post)
    monkeypatch.setattr("app.local_proxy_llm.time", SimpleNamespace(time=lambda: state["now"], monotonic=time.monotonic))
    return config, state


def call(config, value=None, lane="glm"):
    return LocalProxyReplyLLM(config, lane).decide("本轮synthetic本机文字", value or payload(), RiskLevel.low)


def test_glm_config_active_lanes_fixed_endpoint_and_shared_ledger(tmp_path):
    config = configured(tmp_path)
    assert config.openai.required_lanes() == ("chatgpt", "glm", "doubao")
    assert config.openai.disabled_lanes == ["deepseek"]
    assert config.wechat.sender_all_existing_chats and config.wechat.allow_unknown_contacts and config.wechat.group_only_mentions
    raw = config.openai.model_dump()
    raw["local_proxies"]["glm"]["base_url"] = "http://127.0.0.1:19656/v1"
    with pytest.raises(ValidationError, match="does not match its lane"):
        OpenAISettings(**raw)
    raw = config.openai.model_dump()
    raw["local_proxies"]["glm"]["session_db"] = "separate.sqlite3"
    with pytest.raises(ValidationError, match="share their session database"):
        OpenAISettings(**raw)


def test_glm_everyday_requires_enabled_proxy_but_legacy_defaults_remain():
    assert OpenAISettings(provider="hybrid_web").everyday_lane == "deepseek"
    assert OpenAISettings(provider="hybrid_web").required_lanes() == ("chatgpt", "deepseek", "doubao")
    with pytest.raises(ValidationError, match="enabled local proxy"):
        OpenAISettings(provider="hybrid_web", everyday_lane="glm")


def test_glm_normal_turns_persist_user_remote_and_request_id_and_separate_contacts(glm):
    config, state = glm
    call(config)
    call(config, payload("synthetic-b", external_id="b-turn-1"))
    call(config, payload(external_id="turn-2"))
    a, b, a2 = [body for _, body in state["calls"]]
    assert a["user"] == a2["user"] != b["user"]
    assert a["request_id"] == hashlib.sha256(b"turn-1").hexdigest()
    assert a2["request_id"] == hashlib.sha256(b"turn-2").hexdigest()
    with sqlite3.connect(config.resolve(config.openai.local_proxies["glm"].session_db)) as db:
        assert db.execute("SELECT count(DISTINCT remote_id) FROM local_proxy_sessions WHERE lane='glm'").fetchone()[0] == 2
    with pytest.raises(LLMError, match="duplicate_turn"):
        call(config)
    with pytest.raises(LLMError, match="duplicate_turn"):
        call(config, lane="doubao")
    assert len(state["calls"]) == 3


def test_routes_text_and_local_voice_to_glm_visual_to_doubao_professional_emotion_to_gpt(glm, monkeypatch):
    config, state = glm
    created, web_calls = [], []

    def web_client(value):
        created.append(value.openai.provider)
        def decide(*args):
            web_calls.append("chatgpt")
            return ReplyDecision.model_validate(decision())
        return SimpleNamespace(decide=decide)

    monkeypatch.setattr("app.web_llm.WebReplyLLM", web_client)
    router = ReplyLLM(config)
    assert set(router.hybrid) == {"chatgpt", "glm", "doubao"} and created == ["web"]
    router.decide("system", payload(), RiskLevel.low)
    voice = json.loads(payload(external_id="voice-1"))
    voice["transcription"] = {"source": "offline_whisper", "asr_uncertain": False}
    router.decide("system", json.dumps(voice), RiskLevel.low)
    for content in ("分析这个施工方案", "气死我了，你是不是有病"):
        value = json.loads(payload(external_id=content))
        value["incoming"]["content"] = content
        router.decide("system", json.dumps(value), RiskLevel.low)
    assert [lane for lane, _ in state["calls"]] == ["glm", "glm"] and web_calls == ["chatgpt", "chatgpt"]
    for kind in ("image", "sticker"):
        value = {"incoming": {"content": "看看这张图", "message_type": kind}}
        assert ReplyLLM.route(json.dumps(value), RiskLevel.low, "glm") == "doubao"
    assert ReplyLLM.route(payload(), RiskLevel.high, "glm") == "chatgpt"


def test_glm_failure_never_automatically_changes_lane(glm, monkeypatch):
    config, state = glm
    state["error"] = "local_proxy_http_503"
    monkeypatch.setattr("app.web_llm.WebReplyLLM", lambda cfg: SimpleNamespace(decide=lambda *args: pytest.fail("no GPT fallback")))
    router = ReplyLLM(config)
    with pytest.raises(LLMError) as failure:
        router.decide("system", payload(), RiskLevel.low)
    assert failure.value.fallback_safe is False and [lane for lane, _ in state["calls"]] == ["glm"]


@pytest.mark.parametrize("marker", [None, 0, 1, "false"])
def test_glm_requires_strict_transport_tool_boolean_before_done(glm, marker):
    config, state = glm
    state["tool"] = marker
    with pytest.raises(LLMError, match="invalid_tool_metadata"):
        call(config)
    with sqlite3.connect(config.resolve(config.openai.local_proxies["glm"].session_db)) as db:
        assert db.execute("SELECT status FROM local_proxy_turns").fetchone()[0] == "unknown"


def test_tool_review_flag_survives_selection_and_never_enters_schema_or_model_json(glm):
    config, state = glm
    state["tool"] = True
    result = call(config)
    selected = select_reply(result, mode="abc", user_payload={"incoming": {"content": "在吗"}}, incoming_risk=RiskLevel.low)
    assert selected._tool_metadata_seen is True
    assert selected.action == "review" and selected.confidence == 0 and selected.holding_reply is None
    assert "_tool_metadata_seen" not in selected.model_dump_json()
    assert "_tool_metadata_seen" not in ReplyDecision.model_json_schema()["properties"]
    forged = {**decision(), "_tool_metadata_seen": True, "_manual_review_required": True}
    plain = ReplyDecision.model_validate(forged)
    assert plain._tool_metadata_seen is False and plain._manual_review_required is False


@pytest.mark.parametrize("problem", ["image", "direct", "body_over_gateway_limit"])
def test_invalid_glm_contract_is_rejected_before_any_claim_or_http(glm, problem):
    config, state = glm
    value = json.loads(payload())
    if problem == "image":
        value["incoming"]["message_type"] = "image"
    elif problem == "direct":
        value["reply_selection"]["mode"] = "direct"
    else:
        # The input is below the payload cap, but nesting its JSON text into
        # the gateway messages doubles these escaped slashes past 256 KiB.
        value["context"] = chr(92) * 120000
    with pytest.raises(LLMError, match="invalid_payload"):
        call(config, json.dumps(value, ensure_ascii=False))
    assert not state["calls"] and not config.resolve(config.openai.local_proxies["glm"].session_db).exists()


def test_unknown_cannot_replay_but_new_verified_context_recovers_with_new_user_and_old_remote_quarantined(glm):
    config, state = glm
    state["error"] = "local_proxy_http_503"
    with pytest.raises(LLMError, match="http_503"):
        call(config)
    old_user = state["calls"][0][1]["user"]
    state["error"] = None
    state["now"] += 31
    with pytest.raises(LLMError, match="unknown_result"):
        call(config, payload(recovery=True))
    with pytest.raises(LLMError, match="recovery_context_required"):
        call(config, payload(external_id="turn-2"))
    recovered = call(config, payload(external_id="turn-2", recovery=True))
    assert recovered.action == "review" and recovered._manual_review_required and recovered.holding_reply is None
    assert len(state["calls"]) == 2 and state["calls"][1][1]["user"] != old_user
    with sqlite3.connect(config.resolve(config.openai.local_proxies["glm"].session_db)) as db:
        assert db.execute("SELECT generation FROM local_proxy_sessions WHERE lane='glm'").fetchone()[0] == 1
        assert db.execute("SELECT status FROM local_proxy_turns ORDER BY rowid").fetchall() == [("unknown",), ("done",)]


def test_changed_remote_is_quarantined_and_never_used_by_new_generation(glm):
    config, state = glm
    call(config)
    state["remote"] = "unexpected-glm-remote"
    with pytest.raises(LLMError, match="conversation_changed"):
        call(config, payload(external_id="turn-2"))
    state["now"] += 31
    with pytest.raises(LLMError, match="quarantined_conversation"):
        call(config, payload(external_id="turn-3", recovery=True))
    with sqlite3.connect(config.resolve(config.openai.local_proxies["glm"].session_db)) as db:
        assert set(row[0] for row in db.execute("SELECT remote_id FROM local_proxy_quarantined_ids")) == {
            "mock-glm-1", "unexpected-glm-remote"}


def test_verified_production_prompt_recovers_only_new_inbound_and_keeps_first_recovery_on_review(glm, monkeypatch):
    from app.models import ContactProfile
    from app.personal_memory import PersonalMemory
    from app.prompts import PromptBuilder
    from app.risk import assess_risk
    config, state = glm
    monkeypatch.setattr("app.web_llm.WebReplyLLM", lambda cfg: SimpleNamespace(decide=lambda *args: pytest.fail("wrong lane")))
    database = Database(config.resolve(config.paths.database))
    personal = PersonalMemory(config.resolve(config.paths.personal_database))
    builder = PromptBuilder(config)
    router = ReplyLLM(config)

    def local_payload(message):
        database.add_incoming(message, RiskLevel.low.value)
        personal.observe(message)
        return builder.user_payload(message, ContactProfile(name=message.contact), assess_risk(message.content),
                                    database.recent_messages(message.contact, 16), [])

    old = IncomingMessage(external_id="old-model-id", contact="synthetic-a", sender="synthetic-a", content="在吗")
    state["error"] = "local_proxy_request_unknown"
    old_payload = local_payload(old)
    with pytest.raises(LLMError, match="request_unknown"):
        router.decide(builder.system_prompt(), old_payload, RiskLevel.low)
    state["error"] = None
    state["now"] += 31
    with pytest.raises(LLMError, match="unknown_result"):
        router.decide(builder.system_prompt(), old_payload, RiskLevel.low)
    fresh = old.model_copy(update={"external_id": "new-model-id", "content": "我刚回来"})
    fresh_payload = local_payload(fresh)
    assert json.loads(fresh_payload)["__local_proxy_recovery_context"]["complete"] is True
    result = router.decide(builder.system_prompt(), fresh_payload, RiskLevel.low)
    assert result.action == "review" and result.confidence == 0 and result.holding_reply is None
    assert result._manual_review_required is True and len(state["calls"]) == 2
    old_body, new_body = [body for _, body in state["calls"]]
    assert old_body["user"] != new_body["user"]
    assert old_body["request_id"] != new_body["request_id"]
    restored = json.loads(new_body["messages"][1]["content"])
    assert restored["incoming"]["external_id"] == "new-model-id"
    assert "__local_proxy_recovery_context" not in restored
    assert "optional_model_candidates" not in restored
    with sqlite3.connect(config.resolve(config.openai.local_proxies["glm"].session_db)) as db:
        assert db.execute("SELECT status FROM local_proxy_turns ORDER BY rowid").fetchall() == [("unknown",), ("done",)]


def test_doctor_checks_only_actual_browser_and_local_proxy_lanes_without_inference_claim(glm, monkeypatch, capsys):
    import socket
    from app import cli
    config, _ = glm
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "openai_credentials", lambda: (None, None))
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: nullcontext())
    db = Database(config.resolve(config.paths.database))
    db.set_state("browser_bridge", {"heartbeat_seen_at": time.time(), "blocked": False})
    db.set_state("deepseek_bridge", {"blocked": True})
    assert cli.main(["doctor"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert set(report["browser_bridges"]) == {"chatgpt"}
    assert set(report["local_proxies"]) == {"glm", "doubao"}
    assert all(lane["inference_verified"] is False for lane in report["local_proxies"].values())
    assert report["model_inference_verified"] is False


@pytest.mark.parametrize("block", ["tool", "unknown", "resumed"])
def test_real_pipeline_with_mock_gateway_never_sends_review_or_holding_for_tools_unknown_or_resumed(glm, monkeypatch, block):
    config, state = glm
    state["tool"] = block == "tool"
    state["error"] = "local_proxy_request_unknown" if block == "unknown" else None
    monkeypatch.setattr("app.web_llm.WebReplyLLM", lambda cfg: SimpleNamespace(decide=lambda *args: pytest.fail("wrong lane")))
    db = Database(config.resolve(config.paths.database))
    pipeline = ReplyPipeline(config, db, llm=ReplyLLM(config))
    adapter = MockAdapter()
    incoming = IncomingMessage(external_id="pipeline-1", contact="synthetic-new-friend", sender="synthetic-new-friend",
                               content="在吗", raw_summary='{"resume_incomplete":true}' if block == "resumed" else "{}")
    result = pipeline.process(incoming, adapter)
    assert result.status == "draft" and result.decision.action == "review" and not adapter.sent
    if block in {"tool", "unknown"}:
        assert result.decision.confidence == 0 and result.decision.holding_reply is None
    assert [lane for lane, _ in state["calls"]] == ["glm"]


def test_real_pipeline_new_friend_can_auto_reply_through_mock_glm_without_scope_changes(glm, monkeypatch):
    config, state = glm
    monkeypatch.setattr("app.web_llm.WebReplyLLM", lambda cfg: SimpleNamespace(decide=lambda *args: pytest.fail("wrong lane")))
    db = Database(config.resolve(config.paths.database))
    pipeline = ReplyPipeline(config, db, llm=ReplyLLM(config))
    adapter = MockAdapter()
    result = pipeline.process(IncomingMessage(external_id="new-friend-1", contact="synthetic-new-friend",
                                               sender="synthetic-new-friend", content="在吗"), adapter)
    assert result.status == "sent" and adapter.sent == [("synthetic-new-friend", "说吧，我在。")]
    assert [lane for lane, _ in state["calls"]] == ["glm"]


def test_glm_ignore_without_candidates_is_done_and_never_sends(glm, monkeypatch):
    config, state = glm
    ignored = {**decision(), "action": "ignore", "reply": "", "reason": "对方已经收尾",
               "candidates": None, "selected_candidate": None}
    state["raw_decision"] = json.dumps(ignored, ensure_ascii=False)
    monkeypatch.setattr("app.web_llm.WebReplyLLM", lambda cfg: SimpleNamespace(decide=lambda *args: pytest.fail("wrong lane")))
    db = Database(config.resolve(config.paths.database))
    pipeline = ReplyPipeline(config, db, llm=ReplyLLM(config))
    adapter = MockAdapter()

    result = pipeline.process(IncomingMessage(external_id="glm-ignore-1", contact="synthetic-new-friend",
                                               sender="synthetic-new-friend", content="晚安"), adapter)

    assert result.status == "ignored" and result.decision.action == "ignore" and not adapter.sent
    assert [lane for lane, _ in state["calls"]] == ["glm"]
    with sqlite3.connect(config.resolve(config.openai.local_proxies["glm"].session_db)) as ledger:
        assert ledger.execute("SELECT status FROM local_proxy_turns").fetchone()[0] == "done"


def test_glm_non_ignore_still_requires_three_candidates(glm):
    config, state = glm
    incomplete = {**decision(), "candidates": None, "selected_candidate": None}
    state["raw_decision"] = json.dumps(incomplete, ensure_ascii=False)

    with pytest.raises(LLMError, match="local_proxy_invalid_decision"):
        call(config)

    with sqlite3.connect(config.resolve(config.openai.local_proxies["glm"].session_db)) as ledger:
        assert ledger.execute("SELECT status FROM local_proxy_turns").fetchone()[0] == "unknown"


def test_health_includes_three_actual_model_lanes_and_glm_requires_recent_done(glm, tmp_path, monkeypatch):
    import socket
    from app.webui import create_app
    config, state = glm
    db = Database(config.resolve(config.paths.database))
    db.set_state("browser_bridge", {"ready": True, "heartbeat_seen_at": time.time()})
    db.set_state("deepseek_bridge", {"blocked": True, "ready": False})
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: nullcontext())
    client = TestClient(create_app(config, db))
    before = client.get("/health").json()
    assert set(before["model_bridges"]) == {"chatgpt", "glm", "doubao"}
    assert before["model_bridges"]["glm"]["inference_verified"] is False
    state["now"] = time.time()
    call(config)
    after = client.get("/health").json()
    assert after["model_bridges"]["glm"]["status"] == "ready"
    assert after["model_bridges"]["doubao"]["status"] == "reachable_unverified"
    assert after["ok"] is False


def test_scheduler_fourth_glm_worker_handles_daily_while_gpt_waits(glm):
    config, _ = glm
    db = Database(config.resolve(config.paths.database))
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    seen = []
    class Pipeline:
        def send_approved(self, adapter):
            return 0
        def process(self, incoming, adapter):
            seen.append((incoming.external_id, threading.current_thread().name))
            if incoming.external_id == "gpt-1":
                entered.set()
                release.wait(4)
            else:
                done.set()
            return SimpleNamespace(status="draft")
    scheduler = ReplyScheduler(config, db, Pipeline(), SimpleNamespace())
    try:
        scheduler.submit([IncomingMessage(external_id="gpt-1", contact="synthetic-complex", sender="x", content="分析施工方案")])
        assert entered.wait(2)
        scheduler.submit([IncomingMessage(external_id="glm-1", contact="synthetic-daily", sender="y", content="在吗")])
        assert done.wait(2)
        assert ("glm-1", "wechat-reply-glm") in seen
        assert set(scheduler.health()["workers"]) == {"deepseek", "chatgpt", "doubao", "glm"}
    finally:
        release.set()
        scheduler.close()
        for thread in scheduler.threads:
            thread.join(2)

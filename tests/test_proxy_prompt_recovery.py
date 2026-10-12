"""Recovery evidence must come from the production local prompt path."""

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from PIL import Image

from app import media_context
from app.config import AppConfig
from app.db import Database
from app.llm import LLMError, ReplyLLM
from app.local_proxy_llm import LocalProxyReplyLLM
from app.models import ContactProfile, IncomingMessage, ReplyDecision, RiskLevel
from app.personal_memory import PersonalMemory
from app.prompts import PromptBuilder
from app.risk import assess_risk


CONTACT = "synthetic-recovery-contact"


def environment(tmp_path, *, proxy=False):
    openai = {"provider": "hybrid_web"}
    if proxy:
        key = tmp_path / "mock-private-key.txt"
        key.write_text("synthetic-key", encoding="utf-8")
        openai["local_proxies"] = {"deepseek": {
            "enabled": True,
            "base_url": "http://127.0.0.1:19655/v1",
            "api_key_file": str(key),
            "timeout_seconds": 1,
        }}
    config = AppConfig(project_root=tmp_path, paths={"database": "synthetic-history.sqlite3"}, openai=openai)
    database = Database(config.resolve(config.paths.database))
    personal = PersonalMemory(config.resolve(config.paths.personal_database))
    return config, database, personal, PromptBuilder(config)


def inbound(external_id, content, *, kind="text", when=None, **updates):
    fields = {"external_id": external_id, "contact": CONTACT, "sender": CONTACT,
              "content": content, "message_type": kind,
              "received_at": when or datetime.now(timezone.utc)}
    fields.update(updates)
    return IncomingMessage(**fields)


def payload(config, database, builder, message, *, recent=None):
    return json.loads(builder.user_payload(
        message, ContactProfile(name=message.contact), assess_risk(message.content),
        database.recent_messages(message.contact, 16) if recent is None else recent, [],
    ))


def record(database, personal, message):
    database.add_incoming(message, RiskLevel.low.value)
    personal.observe(message)


def test_marker_requires_current_local_inbound_and_matching_recent_core(tmp_path):
    config, database, personal, builder = environment(tmp_path)
    current = inbound("fresh-id", "今天去哪里？")
    real_recent = [{"direction": "in", "sender": CONTACT, "content": current.content,
                    "message_type": current.message_type}]
    assert payload(config, database, builder, current, recent=real_recent)[
        "__local_proxy_recovery_context"]["complete"] is False

    record(database, personal, current)
    result = payload(config, database, builder, current)
    marker = result["__local_proxy_recovery_context"]
    assert marker == {"version": 1, "source": "local_wechat",
                      "conversation_key": result["conversation_key"],
                      "external_id": "fresh-id", "complete": True, "unresolved_media": False}
    assert result["conversation_key"] == hashlib.sha256(
        (CONTACT + "|" + builder.identity_fingerprint).encode()).hexdigest()

    stale = [{**real_recent[0], "content": "前一条消息"}]
    assert payload(config, database, builder, current, recent=stale)[
        "__local_proxy_recovery_context"]["complete"] is False
    broken = [{**real_recent[0], "direction": "sideways"}]
    assert payload(config, database, builder, current, recent=broken)[
        "__local_proxy_recovery_context"]["complete"] is False

    # A repeated sentence from an earlier turn cannot stand in for this turn.
    earlier = inbound("earlier-id", current.content,
                      when=current.received_at - timedelta(seconds=30))
    record(database, personal, earlier)
    older_only = [row for row in database.recent_messages(CONTACT, 16)
                  if row["created_at"] == earlier.received_at.isoformat()]
    assert payload(config, database, builder, current, recent=older_only)[
        "__local_proxy_recovery_context"]["complete"] is False


@pytest.mark.parametrize("metadata", [
    {"merge_truncated": True}, {"resume_incomplete": True},
    {"original_type": "voice", "asr_uncertain": True},
    {"original_type": "voice", "asr_quality_reasons": ["unclear_number"]},
    {"original_type": "voice", "quality_reasons": ["bad_audio"]},
    {"image_partial": True}, {"image_thumbnail_only": True},
    {"owner_corrected_input": True},
])
def test_incomplete_or_uncertain_source_never_claims_recovery_complete(tmp_path, metadata):
    config, database, personal, builder = environment(tmp_path)
    current = inbound("uncertain-id", "明天几点？", raw_summary=json.dumps(metadata))
    record(database, personal, current)
    assert payload(config, database, builder, current)[
        "__local_proxy_recovery_context"]["complete"] is False


def test_unknown_prior_image_blocks_only_media_dependent_text(tmp_path):
    config, database, personal, builder = environment(tmp_path)
    now = datetime.now(timezone.utc)
    prior = inbound("unknown-image", "[图片]", kind="image", when=now - timedelta(seconds=15))
    record(database, personal, prior)
    dependent = inbound("dependent-text", "你看这个颜色怎么样", when=now)
    record(database, personal, dependent)
    marker = payload(config, database, builder, dependent)["__local_proxy_recovery_context"]
    assert marker["complete"] is True and marker["unresolved_media"] is True

    independent = inbound("independent-text", "明天几点出发？", when=now + timedelta(seconds=1))
    record(database, personal, independent)
    marker = payload(config, database, builder, independent)["__local_proxy_recovery_context"]
    assert marker["complete"] is True and marker["unresolved_media"] is False


def test_completed_preceding_sticker_resolves_text_reference(tmp_path):
    config, database, personal, builder = environment(tmp_path)
    now = datetime.now(timezone.utc)
    sticker = inbound("observed-sticker", "[表情]", kind="sticker",
                      when=now - timedelta(seconds=10),
                      raw_summary=json.dumps({"sticker_md5": "a" * 32}))
    record(database, personal, sticker)
    media_context.record(config.resolve(config.paths.database), sticker,
                         ReplyDecision(action="ignore", risk=RiskLevel.low,
                                       media_description="一只猫举起爪子。", media_confidence=.96))
    current = inbound("sticker-followup", "你看这个好看吗？", when=now)
    record(database, personal, current)
    result = payload(config, database, builder, current)
    assert result["preceding_sticker_context"][0]["external_id"] == "observed-sticker"
    assert result["__local_proxy_recovery_context"]["unresolved_media"] is False


def test_attached_owner_image_and_fresh_visual_have_resolved_media(tmp_path):
    config, database, personal, builder = environment(tmp_path)
    assets = config.resolve(config.paths.history_reader) / "image_assets"
    assets.mkdir(parents=True)
    image = assets / "synthetic.png"
    Image.new("RGB", (2, 2), "blue").save(image)
    now = datetime.now(timezone.utc)
    own = inbound("owner-image", "[图片]", kind="image", when=now - timedelta(seconds=5),
                  sender=config.owner_name, media_paths=[str(image)])
    personal.observe(own, direction="out")
    text = inbound("owner-reference", "你刚发的这张图片什么颜色？", when=now)
    record(database, personal, text)
    result = payload(config, database, builder, text)
    assert result["__media_paths"] == [str(image)]
    assert result["__local_proxy_recovery_context"]["unresolved_media"] is False

    visual = inbound("fresh-visual", "[图片]", kind="image", when=now + timedelta(seconds=1),
                     media_paths=[str(image)])
    record(database, personal, visual)
    marker = payload(config, database, builder, visual)["__local_proxy_recovery_context"]
    assert marker["complete"] is True and marker["unresolved_media"] is False


def test_production_prompt_through_reply_llm_quarantines_old_id_and_recovers_fresh_id(tmp_path, monkeypatch):
    config, database, personal, builder = environment(tmp_path, proxy=True)
    requests = []
    clock = [1_700_000_000.0]
    monkeypatch.setattr("app.local_proxy_llm.time", SimpleNamespace(time=lambda: clock[0],
                                                                     monotonic=time.monotonic))

    def mock_post(self, body, key):
        requests.append(json.loads(body))
        assert key == "synthetic-key"
        if len(requests) == 1:
            raise RuntimeError("response lost after local mock accepted the request")
        response = {"action": "send", "risk": "low", "reply": "你说，我在听。",
                    "confidence": .9, "reason": "接住当前文字", "facts_to_confirm": [],
                    "memory_updates": [],
                    "candidates": [{"label": label, "reply": reply, "style_match": 4,
                                    "context_fit": 4, "continuation": 4, "boundary_respect": 5,
                                    "invented_facts": False, "risk": "low"}
                                   for label, reply in zip("ABC", ["你说，我在听。", "在呢，怎么了？", "我在，接着说。"])],
                    "selected_candidate": "A"}
        return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant",
                            "content": json.dumps(response, ensure_ascii=False)}}]}

    monkeypatch.setattr(LocalProxyReplyLLM, "_post", mock_post)
    llm = ReplyLLM(config)
    old = inbound("old-id", "这周怎么样？")
    record(database, personal, old)
    old_payload = json.dumps(payload(config, database, builder, old), ensure_ascii=False)
    with pytest.raises(LLMError, match="local_proxy_request_unknown"):
        llm.decide(builder.system_prompt(), old_payload, RiskLevel.low)
    with pytest.raises(LLMError, match="local_proxy_unknown_result"):
        llm.decide(builder.system_prompt(), old_payload, RiskLevel.low)
    assert len(requests) == 1

    clock[0] += 31
    fresh = inbound("fresh-id", "周末准备去哪里？")
    record(database, personal, fresh)
    fresh_payload = payload(config, database, builder, fresh)
    assert fresh_payload["__local_proxy_recovery_context"]["complete"] is True
    assert llm.decide(builder.system_prompt(), json.dumps(fresh_payload, ensure_ascii=False),
                      RiskLevel.low).reply
    assert len(requests) == 2
    assert requests[0]["user"] != requests[1]["user"]
    recovered_text = requests[1]["messages"][1]["content"]
    assert "__local_proxy_recovery_context" not in recovered_text
    recovered_payload = json.loads(recovered_text)
    assert recovered_payload["incoming"]["external_id"] == "fresh-id"
    assert "optional_model_candidates" not in recovered_payload
    with sqlite3.connect(config.resolve(config.openai.local_proxies["deepseek"].session_db)) as db:
        assert db.execute("SELECT status FROM local_proxy_turns WHERE request_key=?",
                          (hashlib.sha256(b"old-id").hexdigest(),)).fetchone()[0] == "unknown"
        assert db.execute("SELECT status FROM local_proxy_turns WHERE request_key=?",
                          (hashlib.sha256(b"fresh-id").hexdigest(),)).fetchone()[0] == "done"

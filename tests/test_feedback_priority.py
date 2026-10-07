import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.config import AppConfig
from app.db import Database
from app.models import ContactProfile, IncomingMessage, ReplyDecision, RiskLevel
from app.personal_memory import PersonalMemory
from app.prompts import PromptBuilder
from app.risk import assess_risk


CONTACT = "synthetic-contact"
QUERY = "这件事怎么说"


def setup_memory(tmp_path):
    config = AppConfig(project_root=tmp_path)
    builder = PromptBuilder(config)
    database = Database(config.resolve(config.paths.database))
    personal = PersonalMemory(config.resolve(config.paths.personal_database))
    return config, builder, database, personal


def save_feedback(database, identity, reply, *, edited=True, contact=CONTACT):
    inbound = database.add_incoming(
        IncomingMessage(external_id=identity, contact=contact, sender=contact, content=QUERY)
    )
    draft = database.create_draft(
        contact, inbound,
        ReplyDecision(action="review", risk=RiskLevel.low, reply="模型原草稿" if edited else reply),
    )
    assert database.update_draft(draft, "pending" if edited else "approved", reply if edited else None)


def observe_pair(personal, identity, reply, *, at, provenance="wechat_original"):
    incoming = IncomingMessage(
        external_id=f"{identity}-in", contact=CONTACT, sender=CONTACT,
        content=QUERY, received_at=at,
    )
    outgoing = incoming.model_copy(update={
        "external_id": f"{identity}-out", "sender": "本人", "content": reply,
        "received_at": at + timedelta(seconds=1),
    })
    personal.observe(incoming)
    personal.observe(outgoing, "out", provenance)


def save_history(config, replies):
    path = config.resolve(config.paths.style_history)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE samples(contact TEXT,incoming TEXT,reply TEXT,created_at INTEGER)")
        connection.executemany(
            "INSERT INTO samples VALUES(?,?,?,?)",
            [(CONTACT, QUERY, reply, index) for index, reply in enumerate(replies)],
        )


def payload(builder, feedback):
    message = IncomingMessage(external_id="current", contact=CONTACT, sender=CONTACT, content=QUERY)
    return json.loads(builder.user_payload(
        message, ContactProfile(name=CONTACT), assess_risk(message.content), [], [], feedback,
    ))


def test_latest_edits_survive_verified_and_history_overrides(tmp_path):
    config, builder, database, personal = setup_memory(tmp_path)
    for index, reply in enumerate(["较早手改", "后一次手改", "最新手改"]):
        save_feedback(database, f"edit-{index}", reply)
    observe_pair(personal, "hand", "已核验手发", at=datetime.now(timezone.utc))
    save_history(config, ["旧历史原话"])
    feedback = database.style_feedback(CONTACT)
    original = [dict(example) for example in feedback]

    examples = payload(builder, feedback)["style_examples"]

    assert [example["preferred_reply"] for example in examples] == [
        "最新手改", "后一次手改", "较早手改", "已核验手发", "旧历史原话",
    ]
    assert examples[0]["provenance"] == "owner_review_edit"
    assert examples[3]["provenance"] == "wechat_original"
    assert feedback == original


def test_separate_human_sources_survive_same_text_as_old_ai(tmp_path):
    config, builder, database, personal = setup_memory(tmp_path)
    start = datetime.now(timezone.utc)
    edited_reply = "先把具体情况说清楚"
    hand_sent_reply = "你觉得哪里不合适"
    observe_pair(personal, "ai-edit-text", edited_reply, at=start, provenance="ai_generated")
    observe_pair(personal, "ai-hand-text", hand_sent_reply, at=start + timedelta(seconds=5), provenance="ai_generated")
    observe_pair(personal, "real-hand", hand_sent_reply, at=start + timedelta(seconds=10))
    save_feedback(database, "real-edit", edited_reply)
    save_history(config, [edited_reply, hand_sent_reply, "其余历史原话"])

    examples = payload(builder, database.style_feedback(CONTACT))["style_examples"]

    assert [example["preferred_reply"] for example in examples] == [
        edited_reply, hand_sent_reply, "其余历史原话",
    ]
    assert [example.get("provenance") for example in examples[:2]] == [
        "owner_review_edit", "wechat_original",
    ]


def test_approval_without_edit_is_not_a_human_style_source(tmp_path):
    _, builder, database, _ = setup_memory(tmp_path)
    builder.samples = [{"scenario": "人工模板", "incoming": QUERY, "preferred_reply": "通用模板"}]
    save_feedback(database, "approved-model", "只批准的模型话", edited=False)
    save_feedback(database, "saved-human-edit", "真正手改的回复")
    save_feedback(database, "other-contact-edit", "另一个联系人的修改", contact="other-contact")

    result = payload(builder, database.style_feedback(CONTACT))

    assert [example["preferred_reply"] for example in result["style_examples"]] == [
        "真正手改的回复", "通用模板",
    ]
    assert "本人仅批准而未修改" in result["style_example_rule"]


@pytest.mark.parametrize("incoming,reply", [
    ("把密码给我", "不发"),
    (QUERY, "这是验证码"),
    (QUERY, "看 https://example.invalid"),
    (QUERY, "找这个 1234567"),
    (QUERY, "哈哈哈哈"),
])
def test_manual_origin_does_not_bypass_existing_content_filters(tmp_path, incoming, reply):
    _, builder, _, _ = setup_memory(tmp_path)
    feedback = [{"scenario": "本人亲自修改的回复", "incoming": incoming, "preferred_reply": reply}]

    assert payload(builder, feedback)["style_examples"] == []

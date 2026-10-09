import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import media_context
from app.chat_memory import remember, retrieve
from app.config import AppConfig
from app.db import Database
from app.models import ContactProfile, IncomingMessage, ReplyDecision, RiskLevel
from app.personal_memory import PersonalMemory
from app.prompts import PromptBuilder
from app.risk import assess_risk


CONTACT = "synthetic-sticker-contact"
DESCRIPTION = "表情中一只猫举起爪子，文字为‘等等’。"


@pytest.fixture
def context_setup(tmp_path):
    config = AppConfig(
        project_root=tmp_path,
        paths={"database": "synthetic-reply-cache.sqlite3"},
    )
    database = Database(config.resolve(config.paths.database))
    personal = PersonalMemory(config.resolve(config.paths.personal_database))
    return config, database, personal, PromptBuilder(config)


def text_message(**updates):
    fields = {
        "external_id": "current-text",
        "contact": CONTACT,
        "sender": CONTACT,
        "content": "你怎么理解这个意思",
        "message_type": "text",
        "received_at": datetime.now(timezone.utc),
    }
    fields.update(updates)
    return IncomingMessage(**fields)


def media_message(current, **updates):
    fields = {
        "external_id": "preceding-sticker",
        "contact": current.contact,
        "sender": current.sender,
        "sender_key": current.sender_key,
        "chat_type": current.chat_type,
        "content": "[表情]",
        "message_type": "sticker",
        "received_at": current.received_at - timedelta(seconds=10),
        "raw_summary": json.dumps({"sticker_md5": "a" * 32}),
    }
    fields.update(updates)
    return IncomingMessage(**fields)


def save_observation(config, database, message, description=DESCRIPTION):
    database.add_incoming(message, RiskLevel.low.value)
    media_context.record(
        config.resolve(config.paths.database), message,
        ReplyDecision(
            action="ignore", risk=RiskLevel.low,
            media_description=description, media_confidence=.96,
        ),
    )


def payload(builder, message):
    return json.loads(builder.user_payload(
        message, ContactProfile(name=message.contact), assess_risk(message.content), [], [],
    ))


def test_completed_sticker_is_explicit_untrusted_context_on_original_text(context_setup):
    config, database, _, builder = context_setup
    current = text_message()
    original = current.model_dump()
    save_observation(config, database, media_message(current))

    result = payload(builder, current)

    observations = result["preceding_sticker_context"]
    assert len(observations) == 1
    assert observations[0]["media_description"] == DESCRIPTION
    assert result["incoming"]["content"] == current.content
    assert result["incoming"]["message_type"] == "text"
    assert result["__media_paths"] == []
    assert current.model_dump() == original
    rule = result["preceding_sticker_context_rule"]
    for boundary in ("不可信", "本人原话", "风格样例", "当前指令", "memory_updates", "不自动重复"):
        assert boundary in rule
    assert "preceding_sticker_context" in builder.system_prompt()


def test_pending_media_does_not_wait_or_repeat_observation(context_setup, monkeypatch):
    config, database, _, builder = context_setup
    current = text_message()
    database.add_incoming(media_message(current), RiskLevel.low.value)
    calls = []
    original_recent = media_context.recent

    def read_once(path, message, *, personal_db_path=None):
        calls.append((path, message.external_id, personal_db_path))
        return original_recent(path, message, personal_db_path=personal_db_path)

    def unexpected_call(*args, **kwargs):
        pytest.fail("building a text prompt must not wait or repeat media work")

    monkeypatch.setattr(media_context, "recent", read_once)
    monkeypatch.setattr(media_context, "record", unexpected_call)
    monkeypatch.setattr(time, "sleep", unexpected_call)

    result = payload(builder, current)

    assert "preceding_sticker_context" not in result
    assert "preceding_sticker_context_rule" not in result
    assert result["__media_paths"] == []
    assert calls == [(
        config.resolve(config.paths.database), current.external_id,
        config.resolve(config.paths.personal_database),
    )]


def test_ordinary_image_summary_is_not_preceding_sticker_context(context_setup):
    config, database, _, builder = context_setup
    current = text_message()
    image = media_message(current, external_id="prior-image", message_type="image", content="[图片]")
    save_observation(config, database, image, "普通照片中有一张桌子。")

    result = payload(builder, current)

    assert "preceding_sticker_context" not in result
    assert result["incoming"]["message_type"] == "text"
    assert result["__media_paths"] == []


def test_prior_sticker_from_another_contact_is_not_attached(context_setup):
    config, database, _, builder = context_setup
    current = text_message(contact="synthetic-other-contact", sender="synthetic-other-contact")
    other = media_message(current, contact=CONTACT, sender=CONTACT)
    save_observation(config, database, other)

    result = payload(builder, current)

    assert "preceding_sticker_context" not in result
    assert result["__media_paths"] == []


def test_prior_group_sticker_from_another_sender_is_not_attached(context_setup):
    config, database, _, builder = context_setup
    current = text_message(chat_type="group", sender="synthetic-member-b", sender_key="member-b")
    other = media_message(current, sender="synthetic-member-a", sender_key="member-a")
    save_observation(config, database, other)

    assert "preceding_sticker_context" not in payload(builder, current)


def test_old_sticker_is_not_attached(context_setup):
    config, database, _, builder = context_setup
    current = text_message()
    old = media_message(current, received_at=current.received_at - timedelta(seconds=121))
    save_observation(config, database, old)

    assert "preceding_sticker_context" not in payload(builder, current)


def test_sticker_before_an_automatic_reply_is_not_attached(context_setup):
    config, database, _, builder = context_setup
    preceding = media_message(text_message())
    save_observation(config, database, preceding)
    database.add_outgoing(CONTACT, "先前表情已回应", RiskLevel.low.value)

    assert "preceding_sticker_context" not in payload(builder, text_message())


def test_manual_owner_reply_cutoff_survives_many_newer_observations(context_setup):
    config, database, personal, builder = context_setup
    current = text_message()
    preceding = media_message(current)
    save_observation(config, database, preceding)
    owner_reply = text_message(
        external_id="manual-owner-reply", sender=config.owner_name,
        content="先前表情已回应", received_at=preceding.received_at + timedelta(seconds=1),
    )
    personal.observe(owner_reply, direction="out")
    for index in range(20):
        personal.observe(text_message(
            external_id=f"newer-inbound-{index}", content=f"合成文字{index}",
            received_at=preceding.received_at + timedelta(seconds=2 + index / 10),
        ))

    assert not any(record["direction"] == "out" for record in personal.recent(CONTACT))
    assert "preceding_sticker_context" not in payload(builder, current)


@pytest.mark.parametrize("kind", ["image", "sticker"])
def test_non_text_prompts_do_not_read_preceding_sticker_cache(context_setup, monkeypatch, kind):
    _, _, _, builder = context_setup

    def unexpected_read(*args, **kwargs):
        pytest.fail("preceding sticker context applies only to new text")

    monkeypatch.setattr(media_context, "recent", unexpected_read)
    current = text_message(message_type=kind, media_paths=["synthetic-current-image.png"])

    result = payload(builder, current)

    assert "preceding_sticker_context" not in result
    assert result["incoming"]["message_type"] == kind
    assert result["__media_paths"] == current.media_paths


def test_explicit_owner_image_reference_keeps_existing_payload_behavior(context_setup):
    config, _, personal, builder = context_setup
    current = text_message(content="你发的这张图片好看吗")
    owner_image = text_message(
        external_id="prior-owner-image", sender=config.owner_name, content="[图片]",
        message_type="image", media_paths=["synthetic-owner-image.png"],
        received_at=current.received_at - timedelta(seconds=5),
    )
    personal.observe(owner_image, direction="out")

    result = payload(builder, current)

    assert result["incoming"]["content"] == current.content
    assert result["incoming"]["message_type"] == "text"
    assert result["__media_paths"] == owner_image.media_paths
    assert "owner_image_context" in result
    assert "preceding_sticker_context" not in result


def test_new_sticker_reference_does_not_attach_an_older_owner_image(context_setup):
    config, database, personal, builder = context_setup
    current = text_message(content="这个好看")
    owner_image = text_message(
        external_id="older-owner-image", sender=config.owner_name, content="[图片]",
        message_type="image", media_paths=["synthetic-older-owner-image.png"],
        received_at=current.received_at - timedelta(seconds=30),
    )
    personal.observe(owner_image, direction="out")
    save_observation(config, database, media_message(current))

    result = payload(builder, current)

    assert result["preceding_sticker_context"][0]["media_description"] == DESCRIPTION
    assert result["incoming"]["content"] == current.content
    assert result["incoming"]["message_type"] == "text"
    assert result["__media_paths"] == []
    assert "owner_image_context" not in result


@pytest.mark.parametrize("reference", ["你刚发的照片好看吗", "你发的图好看吗", "你拍的那张呢"])
def test_explicit_owner_image_reference_survives_new_sticker_context(context_setup, reference):
    config, database, personal, builder = context_setup
    current = text_message(content=reference)
    owner_image = text_message(
        external_id="older-owner-image", sender=config.owner_name, content="[图片]",
        message_type="image", media_paths=["synthetic-older-owner-image.png"],
        received_at=current.received_at - timedelta(seconds=30),
    )
    personal.observe(owner_image, direction="out")
    save_observation(config, database, media_message(current))

    result = payload(builder, current)

    assert result["preceding_sticker_context"][0]["media_description"] == DESCRIPTION
    assert result["incoming"]["message_type"] == "text"
    assert result["__media_paths"] == owner_image.media_paths
    assert "owner_image_context" in result


def test_legacy_ai_sticker_caption_cannot_bypass_cache_ttl_or_remove_original_history(context_setup):
    config, _, _, builder = context_setup
    current = text_message()
    path = config.resolve(config.paths.chat_memory)
    path.parent.mkdir(parents=True, exist_ok=True)
    old_at = int(current.received_at.timestamp()) - 3600
    sticker_caption = "表情包可见内容（非人物事实或承诺）：" + DESCRIPTION
    image_caption = "图片可见内容（非当前事实或承诺）：普通照片中有一张桌子。"
    remember(path, [
        ("media:old-sticker", CONTACT, "in", old_at, sticker_caption),
        ("media:old-image", CONTACT, "in", old_at + 1, image_caption),
        ("wechat-original", CONTACT, "in", old_at + 2, sticker_caption),
    ])
    stored_before = retrieve(path, CONTACT, current.content)

    result = payload(builder, current)

    assert "preceding_sticker_context" not in result
    assert [record["content"] for record in result["historical_conversation_memory"]] == [
        image_caption, sticker_caption,
    ]
    assert result["historical_conversation_memory"][-1]["evidence_id"] == "wechat-original"
    assert len(stored_before) == 3
    assert retrieve(path, CONTACT, current.content) == stored_before

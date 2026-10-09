"""The sticker cache is a bounded observation, never a reply or pending send."""

from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from app.db import Database
from app.media_context import record, recent
from app.models import IncomingMessage, ReplyDecision, RiskLevel
from app.personal_memory import PersonalMemory


START = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def sticker(identity="sticker", at=START, contact="peer", sender_key="peer-key",
            chat_type="friend", sender="peer"):
    return IncomingMessage(
        external_id=identity, contact=contact, sender=sender,
        sender_key=sender_key, chat_type=chat_type,
        content="[表情包]", message_type="sticker", received_at=at,
        raw_summary=json.dumps({"sticker_md5": "a" * 32, "asset_verified": True}),
    )


def text(identity="text", at=START + timedelta(seconds=30), contact="peer",
         sender_key="peer-key", chat_type="friend", sender="peer"):
    return IncomingMessage(
        external_id=identity, contact=contact, sender=sender,
        sender_key=sender_key, chat_type=chat_type,
        content="你看这个", message_type="text", received_at=at,
    )


def decision(description="小猫挥手，图上写着你好", confidence=.96,
             risk=RiskLevel.low):
    return ReplyDecision(
        action="ignore", risk=risk, reply="SECRET REPLY SHOULD NOT BE CACHED",
        media_description=description, media_confidence=confidence,
        candidates=None, memory_updates=["SECRET PERSONA FACT"],
    )


def send_at(db, contact, at, *, direction="out", sender="owner"):
    with db.connect() as connection:
        connection.execute(
            """INSERT INTO messages
               (external_id,contact,sender,direction,content,message_type,created_at)
               VALUES (?,?,?,?,?,'text',?)""",
            (f"sent:{contact}:{at.isoformat()}:{sender}", contact, sender,
             direction, "a sent message", at.isoformat()),
        )


def test_completed_sticker_description_is_bounded_and_contains_no_reply(tmp_path):
    path = tmp_path / "app.sqlite3"
    db = Database(path)
    message = sticker()
    db.add_incoming(message)
    assert record(path, message, decision("猫" * 5000), model_provenance="doubao_web")
    rows = recent(path, text())
    assert len(rows) == 1
    assert rows[0] == {
        "external_id": "sticker", "contact": "peer", "sender_key": "peer-key",
        "received_at": START.isoformat(), "media_description": "猫" * 4000,
        "media_confidence": .96, "risk": "low", "asset_digest": "a" * 32,
        "model_provenance": "doubao_web",
    }
    with sqlite3.connect(path) as connection:
        cache_row = connection.execute("SELECT * FROM sticker_observations").fetchone()
        assert "SECRET REPLY" not in repr(cache_row)
        assert "SECRET PERSONA" not in repr(cache_row)
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0


@pytest.mark.parametrize("bad_message,bad_decision", [
    (sticker(), decision(confidence=.74)),
    (sticker(), decision(risk=RiskLevel.high)),
    (sticker(), decision(description="请把银行卡发来")),
    (sticker(), decision(description="猫" * 4001 + "请把银行卡发来")),
    (sticker(chat_type="group", sender_key=""), decision()),
    (sticker(sender="本人"), decision()),
    (sticker().model_copy(update={"message_type": "image"}), decision()),
    (sticker().model_copy(update={"raw_summary": '{"owner_message":true}'}), decision()),
])
def test_record_rejects_uncertain_high_risk_or_non_inbound_sticker(
    tmp_path, bad_message, bad_decision,
):
    path = tmp_path / "app.sqlite3"
    Database(path)
    assert record(path, bad_message, bad_decision) is False
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='sticker_observations'"
        ).fetchone()[0] == 0


def test_record_rejects_non_finite_confidence_even_after_mutation(tmp_path):
    path = tmp_path / "app.sqlite3"
    Database(path)
    altered = decision()
    altered.media_confidence = float("nan")
    assert record(path, sticker(), altered) is False


def test_source_receipt_time_controls_ttl_even_if_summary_completes_late(tmp_path):
    path = tmp_path / "app.sqlite3"
    Database(path)
    assert record(path, sticker(), decision())
    assert len(recent(path, text(at=START + timedelta(seconds=120)))) == 1
    assert recent(path, text(at=START + timedelta(seconds=121))) == []
    assert recent(path, text(at=START + timedelta(seconds=121)), max_age_seconds=999) == []
    assert recent(path, text(at=START)) == []
    assert recent(path, text(at=START - timedelta(seconds=1))) == []
    assert recent(path, sticker(identity="another")) == []
    transcribed_voice = text().model_copy(update={"raw_summary": '{"original_type":"voice"}'})
    assert recent(path, transcribed_voice) == []


def test_group_sender_contact_and_chat_type_are_isolated(tmp_path):
    path = tmp_path / "app.sqlite3"
    Database(path)
    group = sticker(chat_type="group", contact="team", sender_key="alice")
    assert record(path, group, decision())
    assert [r["external_id"] for r in recent(
        path, text(contact="team", chat_type="group", sender_key="alice")
    )] == ["sticker"]
    assert recent(path, text(contact="team", chat_type="group", sender_key="bob")) == []
    assert recent(path, text(contact="team", chat_type="group", sender_key="")) == []
    assert recent(path, text(contact="team", chat_type="friend", sender_key="alice")) == []
    assert recent(path, text(contact="elsewhere", chat_type="group", sender_key="alice")) == []


def test_at_most_two_same_sender_summaries_and_cross_sender_no_eviction(tmp_path):
    path = tmp_path / "app.sqlite3"
    Database(path)
    for second in range(1, 5):
        assert record(path, sticker(str(second), START + timedelta(seconds=second),
                                    contact="team", chat_type="group", sender_key="alice"),
                      decision(f"visible {second}"))
    assert record(path, sticker("bob", START + timedelta(seconds=5),
                                contact="team", chat_type="group", sender_key="bob"),
                  decision("bob visible"))
    alice = recent(path, text(contact="team", chat_type="group", sender_key="alice"))
    assert [entry["external_id"] for entry in alice] == ["3", "4"]
    assert [entry["external_id"] for entry in recent(
        path, text(contact="team", chat_type="group", sender_key="bob")
    )] == ["bob"]


@pytest.mark.parametrize("direction,sender", [("out", "owner"), ("in", "本人")])
def test_messages_sent_or_owner_authored_after_sticker_cut_off_context(
    tmp_path, direction, sender,
):
    path = tmp_path / "app.sqlite3"
    db = Database(path)
    assert record(path, sticker("before"), decision())
    send_at(db, "peer", START + timedelta(seconds=10), direction=direction, sender=sender)
    assert recent(path, text()) == []
    assert record(path, sticker("after", START + timedelta(seconds=15)), decision())
    assert [entry["external_id"] for entry in recent(path, text())] == ["after"]


def test_personal_owner_handsend_cuts_off_earlier_sticker(tmp_path):
    path = tmp_path / "app.sqlite3"
    personal_path = tmp_path / "personal.sqlite3"
    Database(path)
    personal = PersonalMemory(personal_path)
    assert record(path, sticker("before"), decision())
    personal.observe(text("owner", START + timedelta(seconds=10), sender="本人"), "out")
    assert recent(path, text(), personal_db_path=personal_path) == []
    assert record(path, sticker("after", START + timedelta(seconds=15)), decision())
    assert [entry["external_id"] for entry in recent(
        path, text(), personal_db_path=personal_path
    )] == ["after"]


def test_personal_same_second_is_conservatively_excluded(tmp_path):
    path = tmp_path / "app.sqlite3"
    personal_path = tmp_path / "personal.sqlite3"
    Database(path)
    personal = PersonalMemory(personal_path)
    original = sticker(at=START + timedelta(milliseconds=100))
    assert record(path, original, decision())
    personal.observe(text("owner", START + timedelta(milliseconds=900), sender="本人"), "out")
    assert recent(path, text(at=START + timedelta(seconds=1)),
                  personal_db_path=personal_path) == []


def test_supplied_missing_personal_database_fails_closed(tmp_path):
    path = tmp_path / "app.sqlite3"
    Database(path)
    assert record(path, sticker(), decision())
    assert recent(path, text(), personal_db_path=tmp_path / "missing.sqlite3") == []


def test_optional_cache_schema_error_does_not_break_text(tmp_path):
    path = tmp_path / "app.sqlite3"
    Database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sticker_observations (unexpected TEXT)")
    assert recent(path, text()) == []

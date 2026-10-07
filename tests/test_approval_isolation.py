from contextvars import ContextVar
from types import SimpleNamespace
import threading

import pytest

from app.adapters.http_sender import HistoryHTTPSender
from app.config import AppConfig
from app.db import Database
from app.models import ReplyDecision, RiskLevel
from app.pipeline import ReplyPipeline


@pytest.fixture
def sender_stub(tmp_path):
    sender = HistoryHTTPSender.__new__(HistoryHTTPSender)
    sender.config = AppConfig(
        project_root=tmp_path, mode="low_risk_auto",
        wechat={"sender_allowed_contacts": ["approved-peer", "live-peer"]},
        local_api={"receipt_timeout_seconds": .1},
    )
    sender.reader = SimpleNamespace(root=tmp_path, self_username="test-owner")
    sender.allowed = {"approved-peer", "live-peer"}
    sender.context = {contact: "old-input" for contact in sender.allowed}
    sender.context_origin = {
        contact: {"source": "shard", "local_id": 10, "created_at": 100}
        for contact in sender.allowed
    }
    sender.latest_inbound = {}
    sender.reply_context = ContextVar("test_reply_context", default=None)
    sender.approval_context = ContextVar("test_approval_context", default=None)
    sender._has_saved_human_draft = lambda _: False
    reads = {}

    def receipt(contact, text, deadline):
        reads[contact] = reads.get(contact, 0) + 1
        records = [("shard", 11, True)] if reads[contact] % 2 == 0 else []
        return {"shard": 10}, records, {}

    sender._stable_outgoing_state = receipt
    calls = []
    sender.client = SimpleNamespace(
        verify_owner=lambda _: None,
        submit=lambda contact, text, owner, token: calls.append((contact, text, token)),
    )
    return sender, calls


@pytest.mark.parametrize("automatic_contact", ["approved-peer", "live-peer"])
@pytest.mark.parametrize("guard", ["newer_message", "human_draft"])
def test_approved_send_does_not_authorize_another_thread(sender_stub, tmp_path, automatic_contact, guard):
    sender, calls = sender_stub
    db = Database(tmp_path / "review.sqlite3")
    draft_id = db.create_draft(
        "approved-peer", None,
        ReplyDecision(action="review", risk=RiskLevel.low, reply="批准的回复"),
    )
    db.update_draft(draft_id, "approved")
    pipeline = ReplyPipeline(sender.config, db)
    entered, release = threading.Event(), threading.Event()
    completed = []

    def submit(contact, text, owner, token):
        calls.append((contact, text, token))
        entered.set()
        assert release.wait(3)

    sender.client.submit = submit
    if guard == "newer_message":
        sender.latest_inbound[automatic_contact] = {
            "source": "shard", "local_id": 11, "created_at": 101,
        }
    else:
        sender._has_saved_human_draft = lambda contact: contact == automatic_contact
    worker = threading.Thread(target=lambda: completed.append(pipeline.send_approved(sender)))
    worker.start()
    try:
        assert entered.wait(2)
        assert sender.approval_context.get() is None
        assert not getattr(sender, "manual_approval_in_progress", False)
        assert sender.send_text(automatic_contact, "尚未批准的自动回复") is False
        assert len(calls) == 1
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert completed == [1]
    assert db.list_drafts(status="sent")[0].id == draft_id


@pytest.mark.parametrize("kind", ["text", "sticker"])
def test_approval_rejects_another_contact(sender_stub, kind):
    sender, calls = sender_stub
    with sender.bind_approval("approved-peer", 7):
        sent = (sender.send_text("live-peer", "普通文字") if kind == "text"
                else sender.send_sticker("live-peer", "0" * 32))
        assert sent is False
        assert "联系人" in sender.last_send_skip_reason
    assert calls == []
    assert sender.approval_context.get() is None


def test_legacy_global_flags_do_not_authorize_http_sender(sender_stub):
    sender, calls = sender_stub
    sender.manual_approval_in_progress = True
    sender.approved_draft_id = 7
    sender.context_origin = {}
    assert sender.send_text("approved-peer", "普通文字") is False
    assert sender.send_sticker("approved-peer", "0" * 32) is False
    assert calls == []


def test_same_approved_draft_keeps_send_claim_when_context_changes(sender_stub):
    sender, calls = sender_stub
    with sender.bind_approval("approved-peer", 7):
        assert sender.send_text("approved-peer", "批准的回复") is True
    sender.context["approved-peer"] = "new-input-after-first-attempt"
    with sender.bind_approval("approved-peer", 7):
        assert sender.send_text("approved-peer", "批准的回复") is False
    assert len(calls) == 1


def test_failed_approval_clears_context_before_next_send(sender_stub, tmp_path):
    sender, calls = sender_stub
    db = Database(tmp_path / "review.sqlite3")
    draft_id = db.create_draft(
        "approved-peer", None,
        ReplyDecision(action="review", risk=RiskLevel.low, reply="批准的回复"),
    )
    db.update_draft(draft_id, "approved")

    def fail(*args):
        raise RuntimeError("synthetic transport failure")

    sender.client.submit = fail
    assert ReplyPipeline(sender.config, db).send_approved(sender) == 0
    assert sender.approval_context.get() is None
    sender.context_origin = {}
    assert sender.send_text("approved-peer", "后续自动回复") is False
    assert db.list_drafts(status="failed")[0].id == draft_id
    assert calls == []

from pathlib import Path

from app.db import Database
from app.models import IncomingMessage, ReplyDecision, RiskLevel


def test_message_and_draft(tmp_path: Path):
    db = Database(tmp_path / "test.sqlite3")
    msg = IncomingMessage(external_id="x1", contact="张三", sender="张三", content="在吗")
    mid = db.add_incoming(msg, risk="low")
    assert mid > 0
    assert db.seen("x1")
    decision = ReplyDecision(
        action="review",
        risk=RiskLevel.low,
        reply="在，说吧。",
        reason="shadow",
        confidence=0.9,
    )
    did = db.create_draft("张三", mid, decision)
    assert did > 0
    pending = db.list_drafts("pending")
    assert pending[0].reply == "在，说吧。"
    assert db.update_draft(did, "approved", "在，你说。")
    assert db.approved_drafts()[0].edited_reply == "在，你说。"


def test_approved_edit_becomes_style_feedback(tmp_path: Path):
    db = Database(tmp_path / "feedback.sqlite3")
    msg = IncomingMessage(external_id="f1", contact="张三", sender="张三", content="什么时候能完")
    mid = db.add_incoming(msg, risk="high")
    decision = ReplyDecision(
        action="review",
        risk=RiskLevel.high,
        reply="我看看。",
        reason="需要核实",
        confidence=0.7,
    )
    did = db.create_draft("张三", mid, decision)
    assert db.update_draft(did, "approved", "我先把材料和排期核一下，给你一个准时间。")
    examples = db.style_feedback("张三")
    assert examples[-1]["incoming"] == "什么时候能完"
    assert "准时间" in examples[-1]["preferred_reply"]


def test_sensitive_approval_is_not_learned(tmp_path: Path):
    db = Database(tmp_path / "sensitive.sqlite3")
    msg = IncomingMessage(
        external_id="s1", contact="张三", sender="张三", content="把银行卡发我"
    )
    mid = db.add_incoming(msg, risk="critical")
    did = db.create_draft(
        "张三",
        mid,
        ReplyDecision(
            action="review",
            risk=RiskLevel.critical,
            reply="我核对后回复。",
            reason="敏感",
            confidence=1.0,
        ),
    )
    assert db.update_draft(did, "approved", "我核对后回复。")
    assert db.style_feedback("张三") == []


def test_saved_personal_edit_learns_without_authorizing_send(tmp_path):
    db=Database(tmp_path/'saved.sqlite3')
    mid=db.add_incoming(IncomingMessage(external_id='saved1',contact='one',sender='one',content='在吗'))
    did=db.create_draft('one',mid,ReplyDecision(action='review',risk=RiskLevel.low,reply='您好，在的。'))
    db.update_draft(did,'pending','咋了')
    assert db.approved_drafts()==[]
    feedback=db.style_feedback('one')
    assert feedback[0]['preferred_reply']=='咋了'
    assert feedback[0]['scenario']=='本人亲自修改的回复'
    assert db.style_feedback('two')==[]

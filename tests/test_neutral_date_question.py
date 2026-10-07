import json
from types import SimpleNamespace

import pytest

from app.adapters.mock import MockAdapter
from app.config import AppConfig
from app.db import Database
from app.models import IncomingMessage, ReplyDecision, RiskLevel
from app.pipeline import ReplyPipeline
from app.risk import assess_risk


def run_message(tmp_path, *, content="13号搬", reply="东西收拾得差不多了吗",
                origin=None, mode="low_risk_auto", **decision_updates):
    config = AppConfig(
        project_root=tmp_path, mode=mode, contacts=[{"name": "peer"}],
        wechat={"use_greeting_cache": False, "send_holding_on_review": False},
    )
    db = Database(tmp_path / "state.sqlite3")
    decision = ReplyDecision.model_validate({
        "action": "send", "risk": "low", "reply": reply, "confidence": .95,
        **decision_updates,
    })
    llm = SimpleNamespace(decide=lambda *args: decision.model_copy(deep=True))
    pipeline = ReplyPipeline(config, db, llm)
    pipeline.prompt_builder = SimpleNamespace(
        system_prompt=lambda: "synthetic test prompt",
        user_payload=lambda message, *args: json.dumps({
            "incoming": {"content": message.content, "message_type": message.message_type},
        }),
    )
    adapter = MockAdapter()
    message = IncomingMessage(
        external_id="synthetic-date", contact="peer", sender="peer", content=content,
        raw_summary=json.dumps(origin or {}),
    )
    result = pipeline.process(message, adapter)
    return result, adapter, db


@pytest.mark.parametrize("reply", [
    "东西收拾得差不多了吗", "行李都打包好了吗？", "你那边准备得怎么样了",
])
def test_plain_date_allows_only_the_neutral_question_and_preserves_audit(tmp_path, reply):
    result, adapter, db = run_message(
        tmp_path, reply=reply, memory_updates=["不应因发送问句而写入的日期记忆"],
    )
    assert result.status == "sent"
    assert adapter.sent == [("peer", reply)]
    assert result.decision.risk == RiskLevel.low
    assert assess_risk("13号搬").level == RiskLevel.medium
    assert db.memories("peer", 10) == []
    with db.connect() as connection:
        rows = connection.execute("SELECT direction,risk FROM messages ORDER BY id").fetchall()
    assert [tuple(row) for row in rows] == [("in", "medium"), ("out", "low")]
    assert any(event["event_type"] == "auto_sent" and "输入日期仍记为中风险" in event["detail"]
               for event in db.recent_events())


@pytest.mark.parametrize("reply", [
    "行", "好的", "我13号帮你搬", "东西13号收拾好了吗", "我东西收拾好了吗",
    "咱们东西收拾好了吗", "一起搬好吗", "保证帮你搬", "你可以准备好吗",
    "东西收拾好了", "东西收拾好了吗？我来帮忙", "东西收拾好了吗，行李打包好了吗",
    "东西" + "都" * 40 + "收拾好了吗", "东西一并收拾好了吗",
])
def test_agreements_commitments_numbers_and_extra_clauses_stay_review(tmp_path, reply):
    result, adapter, _ = run_message(tmp_path, reply=reply)
    assert result.status == "draft"
    assert not adapter.sent


@pytest.mark.parametrize("content", [
    "我13号帮你搬", "13号能来吗", "13号报价多少", "13号付款", "13号工期能保证吗",
    "13号搬，费用三千", "13号搬，3000块", "13号给五千", "13号完成", "13号送货",
    "有3个箱子", "解释清楚13号搬",
])
def test_action_requests_professional_work_money_and_quantities_stay_review(tmp_path, content):
    result, adapter, _ = run_message(tmp_path, content=content)
    assert result.status == "draft"
    assert not adapter.sent


@pytest.mark.parametrize("origin", [
    {"asr_uncertain": True, "original_type": "voice"},
    {"resume_incomplete": True}, {"owner_corrected_input": True},
])
def test_uncertain_voice_and_restored_or_corrected_messages_stay_review(tmp_path, origin):
    result, adapter, _ = run_message(tmp_path, origin=origin)
    assert result.status == "draft"
    assert not adapter.sent


@pytest.mark.parametrize("decision_updates", [
    {"risk": "medium"}, {"confidence": .89}, {"facts_to_confirm": ["需要本人确认"]},
    {"action": "review"},
])
def test_model_review_uncertainty_and_missing_facts_are_not_overridden(tmp_path, decision_updates):
    result, adapter, _ = run_message(tmp_path, **decision_updates)
    assert result.status == "draft"
    assert not adapter.sent


def test_shadow_mode_still_only_drafts_the_neutral_question(tmp_path):
    result, adapter, _ = run_message(tmp_path, mode="shadow")
    assert result.status == "draft"
    assert not adapter.sent

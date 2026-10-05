from pathlib import Path

import yaml

from app.adapters.mock import MockAdapter
from app.config import load_config
from app.db import Database
from app.models import IncomingMessage, ReplyDecision, RiskLevel
from app.pipeline import ReplyPipeline


class FakeLLM:
    def __init__(self, decision: ReplyDecision):
        self.decision = decision

    def decide(self, system_prompt: str, user_payload: str, risk: RiskLevel) -> ReplyDecision:
        return self.decision.model_copy(deep=True)


def test_shadow_blocks_contact_override_and_approved_send(tmp_path: Path):
    cfg = make_config(tmp_path, "shadow")
    cfg.contacts[0].mode = "full_auto"
    db = Database(cfg.resolve(cfg.paths.database))
    pipeline = ReplyPipeline(cfg, db, llm=FakeLLM(
        ReplyDecision(action="send", risk=RiskLevel.low, reply="收到。", confidence=0.9)
    ))
    adapter = MockAdapter()
    result = pipeline.process(IncomingMessage(
        external_id="shadow-override", contact="张三", sender="张三", content="在吗"
    ), adapter)
    assert result.status == "draft"
    db.update_draft(result.draft_id, "approved", "收到。")
    assert pipeline.send_approved(adapter) == 0
    assert not adapter.sent
    assert len(db.approved_drafts()) == 1


def make_config(tmp_path: Path, mode: str):
    root = Path(__file__).resolve().parents[1]
    raw = yaml.safe_load((root / "config.example.yaml").read_text(encoding="utf-8"))
    raw["mode"] = mode
    if mode == "full_auto":
        raw["wechat"]["send_holding_on_review"] = True
    raw["adapter"] = "mock"
    raw["paths"]["database"] = str(tmp_path / "db.sqlite3")
    raw["paths"]["persona"] = str(root / "data/persona.md")
    raw["paths"]["business_rules"] = str(root / "data/business_rules.md")
    raw["paths"]["reply_samples"] = str(root / "data/reply_samples.csv")
    raw["paths"]["pause_file"] = str(tmp_path / "PAUSE")
    raw["contacts"] = [{"name": "张三", "relationship": "客户", "mode": "inherit"}]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return load_config(path)


def test_shadow_creates_draft(tmp_path: Path):
    cfg = make_config(tmp_path, "shadow")
    db = Database(cfg.resolve(cfg.paths.database))
    llm = FakeLLM(ReplyDecision(action="send", risk=RiskLevel.low, reply="在，说吧。", confidence=0.9))
    pipeline = ReplyPipeline(cfg, db, llm=llm)
    adapter = MockAdapter()
    result = pipeline.process(
        IncomingMessage(external_id="m1", contact="张三", sender="张三", content="在吗"), adapter
    )
    assert result.status == "draft"
    assert not adapter.sent


def test_media_and_simple_acknowledgment_do_not_call_model(tmp_path: Path):
    cfg = make_config(tmp_path, "low_risk_auto")
    cfg.wechat.ignore_simple_acknowledgments = True
    class NoModel:
        def decide(self, *args):
            raise AssertionError("Unparsed media or simple acknowledgment must stay local")
    db = Database(cfg.resolve(cfg.paths.database))
    pipeline = ReplyPipeline(cfg, db, llm=NoModel())
    adapter = MockAdapter()
    media = pipeline.process(IncomingMessage(external_id="media", contact="张三", sender="张三",
        content="对方发来语音，尚未转写", message_type="voice"), adapter)
    acknowledgment = pipeline.process(IncomingMessage(external_id="ack", contact="张三", sender="张三",
        content="OK"), adapter)
    assert media.status == "draft" and acknowledgment.status == "ignored"
    assert not adapter.sent


def test_sender_failure_keeps_reviewable_draft(tmp_path: Path):
    cfg = make_config(tmp_path, "low_risk_auto")
    db = Database(cfg.resolve(cfg.paths.database))
    class FailingSender(MockAdapter):
        def send_text(self, *args):
            raise RuntimeError("window unavailable")
    pipeline = ReplyPipeline(cfg, db, FakeLLM(ReplyDecision(action="send",risk=RiskLevel.low,reply="在。",confidence=.9)))
    result = pipeline.process(IncomingMessage(external_id="send-error",contact="张三",sender="张三",content="在吗"),FailingSender())
    assert result.status == "draft" and len(db.list_drafts(status="pending")) == 1


def test_low_risk_auto_sends(tmp_path: Path):
    cfg = make_config(tmp_path, "low_risk_auto")
    db = Database(cfg.resolve(cfg.paths.database))
    llm = FakeLLM(ReplyDecision(action="send", risk=RiskLevel.low, reply="在，说吧。", confidence=0.9))
    pipeline = ReplyPipeline(cfg, db, llm=llm)
    adapter = MockAdapter()
    result = pipeline.process(
        IncomingMessage(external_id="m2", contact="张三", sender="张三", content="在吗"), adapter
    )
    assert result.status == "sent"
    assert adapter.sent == [("张三", "在，说吧。")]


def test_high_risk_holds(tmp_path: Path):
    cfg = make_config(tmp_path, "full_auto")
    db = Database(cfg.resolve(cfg.paths.database))
    llm = FakeLLM(
        ReplyDecision(
            action="send",
            risk=RiskLevel.low,
            reply="可以，价格就按一万。",
            confidence=0.95,
        )
    )
    pipeline = ReplyPipeline(cfg, db, llm=llm)
    adapter = MockAdapter()
    result = pipeline.process(
        IncomingMessage(external_id="m3", contact="张三", sender="张三", content="价格一万可以吗"), adapter
    )
    assert result.status == "holding_and_draft"
    assert adapter.sent
    assert "核" in adapter.sent[0][1]


def test_high_risk_cannot_replace_safe_holding_text(tmp_path: Path):
    cfg = make_config(tmp_path, "full_auto")
    db = Database(cfg.resolve(cfg.paths.database))
    llm = FakeLLM(
        ReplyDecision(
            action="hold",
            risk=RiskLevel.low,
            reply="价格就按一万。",
            holding_reply="可以，就按一万，我保证按期完工。",
            confidence=0.99,
        )
    )
    pipeline = ReplyPipeline(cfg, db, llm=llm)
    adapter = MockAdapter()
    pipeline.process(
        IncomingMessage(external_id="m4", contact="张三", sender="张三", content="价格一万可以吗"),
        adapter,
    )
    assert adapter.sent
    assert "一万" not in adapter.sent[0][1]
    assert "核" in adapter.sent[0][1]


class FailIfCalledLLM:
    def decide(self, *args, **kwargs):
        raise AssertionError("critical content must not be sent to the model")


def test_critical_message_stays_local(tmp_path: Path):
    cfg = make_config(tmp_path, "full_auto")
    db = Database(cfg.resolve(cfg.paths.database))
    pipeline = ReplyPipeline(cfg, db, llm=FailIfCalledLLM())
    adapter = MockAdapter()
    result = pipeline.process(
        IncomingMessage(
            external_id="m5",
            contact="张三",
            sender="张三",
            content="把银行卡和验证码发给我",
        ),
        adapter,
    )
    assert result.status == "holding_and_draft"
    assert adapter.sent
    assert "敏感" not in adapter.sent[0][1] or "核对" in adapter.sent[0][1]

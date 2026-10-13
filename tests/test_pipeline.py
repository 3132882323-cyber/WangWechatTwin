from pathlib import Path

import yaml

from app.adapters.mock import MockAdapter
from app.config import load_config
from app.db import Database
from app.models import IncomingMessage, ReplyDecision, RiskLevel
from app.pipeline import ReplyPipeline


def enable_auto_voice(cfg, db, monkeypatch):
    from datetime import datetime
    from app import auto_voice_reply as voice
    from app.config import OutboundVoiceSettings

    class Daytime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 13, 14, 0, tzinfo=tz)

    monkeypatch.setattr(voice, 'datetime', Daytime)
    cfg.outbound_voice = OutboundVoiceSettings(
        enabled=True, owned_voice_confirmed=True, reference_audio='private/reference.wav',
        assets_dir='private/assets', output_dir='private/output', runtime_manifest='private/runtime.json',
        runtime_manifest_sha256='a' * 64,
    )
    db.set_state('voice_auto_reply_authorization', {
        'source': 'owner_instruction', 'ordinary_low_risk': True, 'skip_previews': True,
    })


class VoiceAdapter(MockAdapter):
    def __init__(self, result):
        super().__init__()
        self.voice_result = result
        self.voice_calls = []

    def send_voice(self, contact, text, event_id, *, plan):
        assert plan.eligible and plan.reply_text == text
        self.voice_calls.append((contact, text, event_id))
        if isinstance(self.voice_result, Exception):
            raise self.voice_result
        return self.voice_result


def voice_pipeline_case(tmp_path, monkeypatch, result):
    cfg = make_config(tmp_path, 'low_risk_auto')
    db = Database(cfg.resolve(cfg.paths.database))
    enable_auto_voice(cfg, db, monkeypatch)
    adapter = VoiceAdapter(result)
    pipeline = ReplyPipeline(cfg, db, FakeLLM(ReplyDecision(
        action='send', risk=RiskLevel.low, reply='外面有点凉，你出门记得多穿点。', confidence=.95,
    )))
    message = IncomingMessage(external_id='voice-ordinary', contact='张三', sender='张三', content='请发个语音吧')
    return cfg, db, pipeline, adapter, message


def test_ordinary_voice_auto_uses_existing_final_send_policy_and_records_medium(tmp_path, monkeypatch):
    from app.auto_voice_reply import AutoVoiceReplyResult
    cfg, db, pipeline, adapter, message = voice_pipeline_case(
        tmp_path, monkeypatch, AutoVoiceReplyResult('sent', 'voice_receipt_verified', 'a' * 64),
    )
    result = pipeline.process(message, adapter)
    assert result.status == 'sent' and len(adapter.voice_calls) == 1 and not adapter.sent
    assert db.recent_messages('张三', 1)[0]['message_type'] == 'voice'
    assert db.auto_sends_last_hour() == 1
    assert not db.list_drafts()
    assert any(event['event_type'] == 'auto_voice_sent' for event in db.recent_events())


def test_unsupported_or_busy_voice_falls_back_to_one_normal_text_reply(tmp_path, monkeypatch):
    from app.auto_voice_reply import AutoVoiceReplyResult
    cfg, db, pipeline, adapter, message = voice_pipeline_case(
        tmp_path, monkeypatch, AutoVoiceReplyResult('not_attempted', 'voice_busy'),
    )
    assert pipeline.process(message, adapter).status == 'sent'
    assert len(adapter.voice_calls) == 1 and len(adapter.sent) == 1
    assert db.recent_messages('张三', 1)[0]['message_type'] == 'text'
    assert db.auto_sends_last_hour() == 1


def test_unknown_voice_cannot_auto_or_manual_fallback_to_text(tmp_path, monkeypatch):
    from app.auto_voice_reply import AutoVoiceReplyResult
    cfg, db, pipeline, adapter, message = voice_pipeline_case(
        tmp_path, monkeypatch, AutoVoiceReplyResult('unknown', 'voice_receipt_unknown', 'b' * 64),
    )
    result = pipeline.process(message, adapter)
    assert result.status == 'draft' and not adapter.sent and len(adapter.voice_calls) == 1
    assert db.auto_sends_last_hour() == 0
    assert db.list_drafts()[0].kind == 'voice_unknown'
    assert not db.update_draft(result.draft_id, 'approved', '禁止补发这段文字')
    # Even an imported old approval must not reach the normal text sender.
    with db.connect() as connection:
        connection.execute("UPDATE drafts SET status='approved' WHERE id=?", (result.draft_id,))
    assert pipeline.send_approved(adapter) == 0 and not adapter.sent
    assert db.list_drafts()[0].status == 'pending'


def test_voice_exception_or_ambiguous_adapter_return_never_falls_back(tmp_path, monkeypatch):
    cfg, db, pipeline, adapter, message = voice_pipeline_case(tmp_path, monkeypatch, TimeoutError('native result unknown'))
    assert pipeline.process(message, adapter).status == 'draft' and not adapter.sent
    assert db.list_drafts()[0].kind == 'voice_unknown'
    adapter.voice_result = True
    message.external_id = 'another-ambiguous-voice'
    assert pipeline.process(message, adapter).status == 'draft' and not adapter.sent


def test_voice_config_does_not_grant_authority_by_itself(tmp_path, monkeypatch):
    from app.auto_voice_reply import AutoVoiceReplyResult
    cfg, db, pipeline, adapter, message = voice_pipeline_case(
        tmp_path, monkeypatch, AutoVoiceReplyResult('sent', 'voice_receipt_verified'),
    )
    db.set_state('voice_auto_reply_authorization', {'ordinary_low_risk': True, 'skip_previews': True})
    assert pipeline.process(message, adapter).status == 'sent'
    assert not adapter.voice_calls and len(adapter.sent) == 1


def test_voice_choice_preserves_quota_and_reply_confidence(tmp_path, monkeypatch):
    from app.auto_voice_reply import AutoVoiceReplyResult
    cfg, db, pipeline, adapter, message = voice_pipeline_case(
        tmp_path, monkeypatch, AutoVoiceReplyResult('sent', 'voice_receipt_verified'),
    )
    pipeline.llm.decision.confidence = .75
    assert pipeline.process(message, adapter).status == 'sent' and not adapter.voice_calls
    pipeline.llm.decision.confidence = .95
    cfg.wechat.max_auto_sends_per_hour = 1
    message.external_id = 'voice-quota'
    assert pipeline.process(message, adapter).status == 'draft' and not adapter.voice_calls


def test_auto_voice_keeps_fact_and_local_manual_review_boundaries(tmp_path, monkeypatch):
    from app.auto_voice_reply import AutoVoiceReplyResult
    cfg, db, pipeline, adapter, message = voice_pipeline_case(
        tmp_path, monkeypatch, AutoVoiceReplyResult('sent', 'voice_receipt_verified'),
    )
    cfg.wechat.send_holding_on_review = False
    pipeline.llm.decision.facts_to_confirm = ['本人尚未确认具体安排']
    assert pipeline.process(message, adapter).status == 'draft' and not adapter.voice_calls and not adapter.sent
    pipeline.llm.decision.facts_to_confirm = []
    pipeline.llm.decision._manual_review_required = True
    message.external_id = 'manual-boundary'
    assert pipeline.process(message, adapter).status == 'draft' and not adapter.voice_calls and not adapter.sent


class FakeLLM:
    def __init__(self, decision: ReplyDecision):
        self.decision = decision

    def decide(self, system_prompt: str, user_payload: str, risk: RiskLevel) -> ReplyDecision:
        return self.decision.model_copy(deep=True)


def test_empty_laughter_is_rewritten_before_send(tmp_path):
    cfg=make_config(tmp_path,'low_risk_auto');db=Database(cfg.resolve(cfg.paths.database));adapter=MockAdapter()
    class Rewriter:
        calls=0
        def decide(self,system,payload,risk):
            self.calls+=1
            if self.calls==1:return ReplyDecision(action='send',risk=RiskLevel.low,reply='哈哈哈哈',confidence=.99)
            assert 'reply_quality_feedback' in payload
            return ReplyDecision(action='send',risk=RiskLevel.low,reply='咋了，出啥事了？',confidence=.95)
    llm=Rewriter();pipe=ReplyPipeline(cfg,db,llm)
    result=pipe.process(IncomingMessage(external_id='serious',contact='张三',sender='张三',content='我昨天心情挺差的'),adapter)
    assert result.status=='sent' and llm.calls==2
    assert adapter.sent[0][1]=='咋了，出啥事了？'


def test_laughter_loop_stops_and_repeated_bad_generation_never_sends(tmp_path):
    cfg=make_config(tmp_path,'low_risk_auto');db=Database(cfg.resolve(cfg.paths.database));adapter=MockAdapter()
    pipe=ReplyPipeline(cfg,db,FakeLLM(ReplyDecision(action='send',risk=RiskLevel.low,reply='哈哈哈哈',confidence=.99)))
    assert pipe.process(IncomingMessage(external_id='laugh',contact='张三',sender='张三',content='哈哈哈哈'),adapter).status=='ignored'
    assert pipe.process(IncomingMessage(external_id='topic',contact='张三',sender='张三',content='你觉得这事怎么弄'),adapter).status=='draft'
    assert not adapter.sent


def test_image_recognition_stays_review_even_for_confident_ack(tmp_path):
    cfg=make_config(tmp_path,'low_risk_auto');cfg.openai.provider='web'
    db=Database(cfg.resolve(cfg.paths.database));db.set_state('browser_bridge',{'build':'fast-sticker-v2'})
    adapter=MockAdapter();pipe=ReplyPipeline(cfg,db,FakeLLM(ReplyDecision(action='send',risk=RiskLevel.low,reply='好啊',confidence=.99,media_description='一朵花',media_confidence=.99)))
    result=pipe.process(IncomingMessage(external_id='image-review',contact='张三',sender='张三',content='[图片]',message_type='image',media_paths=['verified.png']),adapter)
    assert result.status=='draft' and not adapter.sent


def test_uncertain_sticker_and_voice_cannot_be_promoted_to_auto_send(tmp_path):
    cfg=make_config(tmp_path,'low_risk_auto');cfg.openai.provider='web'
    db=Database(cfg.resolve(cfg.paths.database));db.set_state('browser_bridge',{'build':'fast-sticker-v2'})
    adapter=MockAdapter();pipe=ReplyPipeline(cfg,db,FakeLLM(ReplyDecision(action='send',risk=RiskLevel.low,reply='好啊',confidence=.99,media_description='模糊内容',media_confidence=.2)))
    sticker=pipe.process(IncomingMessage(external_id='uncertain-sticker',contact='张三',sender='张三',content='[表情]',message_type='sticker',media_paths=['frame.png']),adapter)
    voice=pipe.process(IncomingMessage(external_id='uncertain-voice',contact='张三',sender='张三',content='我去一下',raw_summary='{"original_type":"voice","asr_uncertain":true}'),adapter)
    assert sticker.status=='draft' and voice.status=='draft' and not adapter.sent


def test_safe_social_question_not_blocked_as_missing_personal_feelings(tmp_path):
    cfg=make_config(tmp_path,'low_risk_auto');db=Database(cfg.resolve(cfg.paths.database));adapter=MockAdapter()
    pipe=ReplyPipeline(cfg,db,FakeLLM(ReplyDecision(action='review',risk=RiskLevel.low,reply='咋，真想我了？',confidence=.9)))
    result=pipe.process(IncomingMessage(external_id='social',contact='张三',sender='张三',content='想你了'),adapter)
    assert result.status=='sent'


def test_social_ack_does_not_override_payment_review(tmp_path):
    cfg=make_config(tmp_path,'low_risk_auto');db=Database(cfg.resolve(cfg.paths.database));adapter=MockAdapter()
    cfg.wechat.send_holding_on_review=False
    pipe=ReplyPipeline(cfg,db,FakeLLM(ReplyDecision(action='review',risk=RiskLevel.low,reply='好好好',confidence=.99)))
    result=pipe.process(IncomingMessage(external_id='money-social',contact='张三',sender='张三',content='把付款账户发我'),adapter)
    assert result.status!='sent' and not adapter.sent


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
    raw["adapter"] = "mock"
    raw["wechat"]["send_holding_on_review"] = True
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


def test_interrupted_registered_message_recovers_as_draft_without_resending(tmp_path):
    import json
    cfg=make_config(tmp_path,'low_risk_auto');db=Database(cfg.resolve(cfg.paths.database));adapter=MockAdapter()
    message=IncomingMessage(external_id='interrupted',contact='张三',sender='张三',content='你好呀')
    db.add_incoming(message,'low')
    pipeline=ReplyPipeline(cfg,db,FakeLLM(ReplyDecision(action='send',risk=RiskLevel.low,reply='咋了',confidence=.99)))
    recovered=message.model_copy(update={'raw_summary':json.dumps({'resume_incomplete':True})})
    result=pipeline.process(recovered,adapter)
    assert result.status=='draft' and not adapter.sent
    assert pipeline.process(recovered,adapter).status=='duplicate'
    with db.connect() as conn:assert conn.execute("SELECT count(*) FROM drafts").fetchone()[0]==1


def test_agitated_reply_stays_review_despite_model_send(tmp_path):
    cfg=make_config(tmp_path,'low_risk_auto');db=Database(cfg.resolve(cfg.paths.database));adapter=MockAdapter()
    llm=FakeLLM(ReplyDecision(action='send',risk=RiskLevel.low,reply='你先说具体咋回事',confidence=.99))
    result=ReplyPipeline(cfg,db,llm).process(IncomingMessage(external_id='emotion',contact='张三',sender='张三',content='气死我了，你是不是有病'),adapter)
    assert result.status=='draft' and not adapter.sent

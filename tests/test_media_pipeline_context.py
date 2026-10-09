import json
import sqlite3
import pytest
from datetime import datetime, timedelta, timezone

from app.config import AppConfig
from app.db import Database
from app.models import ContactProfile, IncomingMessage, ReplyDecision, RiskLevel
from app.pipeline import ReplyPipeline
from app.personal_memory import PersonalMemory


def configured(tmp_path):
    cfg = AppConfig(project_root=tmp_path, mode='low_risk_auto',
                    contacts=[ContactProfile(name='friend', mode='inherit')])
    cfg.openai.provider = 'hybrid_web'
    cfg.wechat.use_greeting_cache = False
    personal = cfg.resolve(cfg.paths.personal_database)
    personal.parent.mkdir(parents=True, exist_ok=True)
    PersonalMemory(personal)
    return cfg


def test_stale_sticker_is_cached_before_cancellation_and_following_text_sends_once(tmp_path):
    cfg = configured(tmp_path)
    db = Database(cfg.resolve(cfg.paths.database))
    db.set_state('doubao_bridge', {'ready': True})
    now = datetime.now(timezone.utc)

    class Model:
        calls = []

        def decide(self, system, payload, risk):
            data = json.loads(payload)
            self.calls.append(data)
            if data['incoming']['message_type'] == 'sticker':
                return ReplyDecision(action='send', risk=RiskLevel.low, reply='累了就歇会',
                                     confidence=.95, media_description='图片文字：我太困了', media_confidence=.95)
            assert data['incoming']['message_type'] == 'text'
            assert not data['__media_paths']
            assert data['preceding_sticker_context'][0]['external_id'] == 'sticker-source'
            return ReplyDecision(action='send', risk=RiskLevel.low, reply='那就早点歇着',
                                 confidence=.95, memory_updates=['图片中的状态不是本人长期事实'])

    class Sender:
        attempts = 0
        sent = []
        last_send_skip_reason = '生成期间已有新的对方消息'

        def send_text(self, contact, text):
            self.attempts += 1
            if self.attempts == 1:
                return False
            self.sent.append((contact, text))
            return True

    model, sender = Model(), Sender()
    pipeline = ReplyPipeline(cfg, db, model)
    sticker = IncomingMessage(external_id='sticker-source', contact='friend', sender='friend',
                              content='[表情包]', message_type='sticker',
                              media_paths=['a' * 32 + '-0.png'], received_at=now - timedelta(seconds=10))
    assert pipeline.process(sticker, sender).status == 'draft'
    assert not sender.sent
    text = IncomingMessage(external_id='new-text', contact='friend', sender='friend',
                           content='刚忙完', received_at=now - timedelta(seconds=2))
    result = pipeline.process(text, sender)
    assert result.status == 'sent'
    assert sender.sent == [('friend', '那就早点歇着')]
    assert result.decision.memory_updates == []
    assert db.memories('friend', 10) == []
    assert len(model.calls) == 2
    assert not cfg.resolve(cfg.paths.chat_memory).exists()


def test_sticker_cache_failure_does_not_break_reply_processing(tmp_path, monkeypatch):
    cfg = configured(tmp_path)
    db = Database(cfg.resolve(cfg.paths.database))
    db.set_state('doubao_bridge', {'ready': True})
    monkeypatch.setattr('app.media_context.record', lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError('locked')))

    class Model:
        def decide(self, *args):
            return ReplyDecision(action='review', risk=RiskLevel.low, reply='先歇会', confidence=.7,
                                 media_description='图片文字：我太困了', media_confidence=.95)

    class Sender:
        def send_text(self, *args):
            raise AssertionError('review must not send')

    message = IncomingMessage(external_id='cache-error', contact='friend', sender='friend',
                              content='[表情包]', message_type='sticker', media_paths=['frame.png'])
    assert ReplyPipeline(cfg, db, Model()).process(message, Sender()).status == 'draft'


@pytest.mark.parametrize('risk,confidence', [(RiskLevel.high, .95), (RiskLevel.low, .2)])
def test_rejected_retry_observation_never_publishes_initial_caption(tmp_path, risk, confidence):
    from app.media_context import recent
    cfg = configured(tmp_path)
    db = Database(cfg.resolve(cfg.paths.database))
    db.set_state('doubao_bridge', {'ready': True})
    now = datetime.now(timezone.utc)

    class Model:
        calls = 0

        def decide(self, *args):
            self.calls += 1
            if self.calls == 1:
                probe = IncomingMessage(external_id='probe', contact='friend', sender='friend',
                                        content='这个', received_at=now)
                assert recent(cfg.resolve(cfg.paths.database), probe,
                              personal_db_path=cfg.resolve(cfg.paths.personal_database)) == []
                return ReplyDecision(action='review', risk=RiskLevel.low, reply='', confidence=.95,
                                     media_description='一只猫', media_confidence=.95)
            return ReplyDecision(action='review', risk=risk, reply='这个先核实一下', confidence=.9,
                                 media_description='内容需要核对', media_confidence=confidence)

    class Sender:
        def send_text(self, *args):
            raise AssertionError('unsafe media must not send')

    model = Model()
    message = IncomingMessage(external_id='retried', contact='friend', sender='friend',
                              content='[表情包]', message_type='sticker', media_paths=['frame.png'],
                              received_at=now - timedelta(seconds=10))
    assert ReplyPipeline(cfg, db, model).process(message, Sender()).status == 'draft'
    assert model.calls == 2
    following = IncomingMessage(external_id='following', contact='friend', sender='friend',
                                content='这个', received_at=now)
    assert recent(cfg.resolve(cfg.paths.database), following,
                  personal_db_path=cfg.resolve(cfg.paths.personal_database)) == []

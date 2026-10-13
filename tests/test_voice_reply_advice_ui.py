import json
from datetime import datetime as real_datetime

import pytest

from app.config import AppConfig
from app.db import Database
from app.models import IncomingMessage, ReplyDecision, RiskLevel
from app import webui


class Daytime(real_datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 1, 1, 12, tzinfo=tz)


def desk(tmp_path, monkeypatch, *, original='voice', uncertain=None, resume=False):
    monkeypatch.setattr(webui, 'datetime', Daytime)
    config = AppConfig(project_root=tmp_path)
    db = Database(tmp_path / 'desk.sqlite3')
    metadata = {'original_type': original}
    if resume:
        metadata['resume_incomplete'] = True
    if uncertain is not None:
        metadata['asr_uncertain'] = uncertain
    inbound_id = db.add_incoming(IncomingMessage(
        external_id='synthetic-voice-request', contact='synthetic-peer', sender='synthetic-peer',
        content='发个语音吧', message_type='text', raw_summary=json.dumps(metadata)))
    db.create_draft('synthetic-peer', inbound_id, ReplyDecision(
        action='review', risk=RiskLevel.low, reply='刚回来，路上有点堵。', confidence=0.9))
    return config, db


@pytest.mark.parametrize('uncertain', [True, None, 'false'])
def test_uncertain_or_missing_asr_cannot_recommend_voice(tmp_path, monkeypatch, uncertain):
    config, db = desk(tmp_path, monkeypatch, uncertain=uncertain)
    page = webui._page(config, db)
    assert '建议文字回复' in page
    assert '建议短语音，先试听并逐条确认' not in page


def test_explicit_clear_voice_is_advice_not_a_voice_sender(tmp_path, monkeypatch):
    config, db = desk(tmp_path, monkeypatch, uncertain=False)
    page = webui._page(config, db)
    assert '建议短语音，先试听并逐条确认' in page
    assert '本条尚未生成语音，按现有文字审核流程处理' in page
    assert '/SendVoiceMsg' not in page
    assert '<audio' not in page
    assert db.list_drafts()[0].status == 'pending'


def test_interrupted_resume_preserves_text_and_review(tmp_path, monkeypatch):
    config, db = desk(tmp_path, monkeypatch, uncertain=False, resume=True)
    page = webui._page(config, db)
    assert '建议文字回复' in page
    assert '建议短语音，先试听并逐条确认' not in page


def test_confirmed_contact_habit_needs_no_configured_friendship(tmp_path, monkeypatch):
    from app.voice_reply_policy import OwnerHumanVoiceStats
    config, db = desk(tmp_path, monkeypatch, uncertain=False)
    draft = db.list_drafts()[0]
    draft.incoming_content = '刚到家，路上好堵。'
    stats = OwnerHumanVoiceStats(sample_count=10, voice_count=5,
        source='owner_confirmed_wechat_history', same_contact=True,
        verified_human_only=True, recent_window_days=30, last_voice_age_days=1)
    assert not config.contact_map().get(draft.contact)
    assert '建议短语音，先试听并逐条确认' in webui._voice_reply_advice(
        config, draft, {'original_type':'voice','asr_uncertain':False}, stats)
    assert '建议文字回复' in webui._voice_reply_advice(
        config, draft, {'original_type':'voice','asr_uncertain':False}, None)


def test_timing_distinguishes_audio_core_and_wall(tmp_path):
    db = Database(tmp_path / 'desk.sqlite3')
    assert '还没有完整生成耗时记录' in webui._voice_timing_summary(db)
    db.set_state('voice_generation_benchmark', {
        'source': 'local_single_sample', 'audio_seconds': 2.48,
        'native_core_seconds': 0.81, 'process_to_wav_seconds': 3.09})
    rendered = webui._voice_timing_summary(db)
    assert all(number in rendered for number in ('2.48', '0.81', '3.09'))
    assert '单次样本' in rendered
    assert '不包含回复起草' in rendered
    db.set_state('voice_generation_benchmark', {
        'source': 'local_single_sample', 'audio_seconds': True,
        'native_core_seconds': 0.81, 'process_to_wav_seconds': 3.09})
    assert webui._voice_timing_summary(db) == '生成耗时记录暂不可用。'

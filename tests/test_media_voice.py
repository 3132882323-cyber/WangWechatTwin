from concurrent.futures import Future
from types import SimpleNamespace
import json
from app.config import AppConfig
from app.db import Database
from app.models import IncomingMessage
from app.media_voice import OfflineVoiceProcessor


def test_pending_voice_does_not_block_text_and_remains_until_consumed(tmp_path):
    cfg=AppConfig(project_root=tmp_path);cfg.media.voice_enabled=True
    root=tmp_path/'reader';root.mkdir()
    processor=OfflineVoiceProcessor(cfg,SimpleNamespace(root=root))
    voice=IncomingMessage(external_id='voice',contact='peer',sender='peer',content='[语音]',message_type='voice')
    text=IncomingMessage(external_id='text',contact='peer',sender='peer',content='你好')
    future=Future();processor.pending[voice.external_id]=(voice,future)
    assert processor.poll([text])==[text]
    processor._persist()
    assert len(json.loads(processor.store.read_text(encoding='utf-8')))==1
    transcript=voice.model_copy(update={'content':'我到了','message_type':'text'})
    future.set_result(transcript)
    assert processor.poll([])==[transcript]
    assert 'voice' in processor.pending
    processor.db.add_incoming(transcript,'low')
    assert processor.poll([])==[] and not processor.pending
    assert json.loads(processor.store.read_text(encoding='utf-8'))==[]
    processor.close()


def test_uncertain_asr_metadata_is_preserved(tmp_path):
    cfg=AppConfig(project_root=tmp_path);root=tmp_path/'reader';root.mkdir()
    processor=OfflineVoiceProcessor(cfg,SimpleNamespace(root=root))
    voice=IncomingMessage(external_id='voice',contact='peer',sender='peer',content='[语音]',message_type='voice',raw_summary='{"local_id":7,"server_id":88}')
    result=processor._result(voice,{'text':'付款两百元','uncertain':True,'audio_sha256':'test','duration_seconds':2},False)
    origin=json.loads(result.raw_summary)
    assert origin['asr_uncertain'] and origin['original_type']=='voice' and origin['server_id']==88
    assert result.message_type=='text'
    processor.close()

from app.personal_memory import PersonalMemory
from app.models import IncomingMessage


def test_contact_isolation_owner_text_and_ai_receipt(tmp_path):
    memory=PersonalMemory(tmp_path/'personal.sqlite3')
    owner=IncomingMessage(external_id='owner',contact='alice',sender='本人',content='我已经到楼下了',raw_summary='{"source":"db","local_id":7}')
    memory.observe(owner,'out');memory.observe(owner.model_copy(update={'external_id':'other','contact':'bob','content':'别人的信息'}),'in')
    assert memory.recent('alice')[0]['content']=='我已经到楼下了'
    assert all(row['content']!='别人的信息' for row in memory.recent('alice'))
    memory.receipt('alice',owner.content,'db',7)
    assert memory.recent('alice')[0]['provenance']=='ai_generated'
    memory.correct('owner','alice',owner.content,'我还没到楼下')
    assert memory.corrected('owner','alice')=='我还没到楼下'
    assert memory.corrected('owner','bob') is None


def test_model_notes_are_only_optional_and_message_bound(tmp_path):
    memory=PersonalMemory(tmp_path/'personal.sqlite3');memory.note('message','alice','deepseek','候选回复')
    assert memory.notes(['message'],'bob')==[] and memory.notes(['other'],'alice')==[]
    assert memory.notes(['message'],'alice')[0]['provider']=='deepseek'


def test_unknown_voice_payload_is_not_learned_as_manual_even_before_receipt(tmp_path):
    import json
    memory=PersonalMemory(tmp_path/'personal.sqlite3')
    memory.register_generated_voice('alice','a'*64,'b'*64)
    message=IncomingMessage(external_id='unknown-voice',contact='alice',sender='本人',
        content='可能与原回复不同的听写',message_type='text',
        raw_summary=json.dumps({'original_type':'voice','audio_sha256':'a'*64,'source':'db','local_id':11}))
    assert memory.observe(message,'out','automatic_transcription')=='ai_generated'
    assert memory.observe(message.model_copy(update={'external_id':'incoming'}),'in','automatic_transcription')=='automatic_transcription'
    assert memory.observe(message.model_copy(update={'external_id':'other','contact':'bob'}),'out','automatic_transcription')=='automatic_transcription'


def test_only_verified_human_style_is_used_and_receipts_remove_ai_samples(tmp_path):
    from datetime import datetime,timezone,timedelta
    memory=PersonalMemory(tmp_path/'personal.sqlite3');start=datetime.now(timezone.utc)
    incoming=IncomingMessage(external_id='in',contact='alice',sender='alice',content='你好呀',received_at=start)
    own=incoming.model_copy(update={'external_id':'own','content':'咋了','received_at':start+timedelta(seconds=3),'raw_summary':'{"source":"db","local_id":9}'})
    memory.observe(incoming);memory.observe(own,'out')
    assert memory.human_style_examples('alice','你好呀')[0]['preferred_reply']=='咋了'
    assert not memory.human_style_examples('bob','你好呀')
    memory.receipt('alice','咋了','db',9)
    assert not memory.human_style_examples('alice','你好呀')
    assert not memory.exclude_ai_style('alice',[{'preferred_reply':'咋了'}])


def test_unknown_server_ack_still_never_becomes_human_style(tmp_path):
    memory=PersonalMemory(tmp_path/'personal.sqlite3')
    message=IncomingMessage(external_id='pending-ai',contact='alice',sender='本人',content='AI回复正文')
    memory.sending_intent('alice',message.content,'context')
    assert memory.observe(message,'out')=='ai_generated'

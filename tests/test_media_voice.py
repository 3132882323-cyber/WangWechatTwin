from concurrent.futures import Future
from types import SimpleNamespace
import json
import hashlib
import shutil
import sqlite3
from pathlib import Path
from contextlib import closing

import pytest

from app import media_voice
from app.config import AppConfig
from app.db import Database
from app.models import IncomingMessage
from app.media_voice import OfflineVoiceProcessor


SYNTHETIC_VOICE=b'\x02#!SILK_V3synthetic private payload'
MESSAGE_SOURCE='wxid_owner_test/message/message_0.db'
MEDIA_SOURCE='wxid_owner_test/message/media_0.db'


@pytest.fixture
def voice_corpus(tmp_path,monkeypatch):
    root=tmp_path/'reader';root.mkdir()
    original=tmp_path/'wxid_owner_test'/'db_storage'/'message'/'message_0.db'
    original.parent.mkdir(parents=True)
    original.write_bytes(b'synthetic original file; never decrypted')
    reader=SimpleNamespace(root=root,self_username='wxid_owner',files={MESSAGE_SOURCE:{'source':str(original)}})
    media={}

    def add(rows,*,key=MEDIA_SOURCE,path=None):
        path=Path(path or original.with_name('media_0.db'))
        path.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(path)) as db,db:
            db.executescript('CREATE TABLE IF NOT EXISTS Name2Id(user_name TEXT);'
                             'CREATE TABLE IF NOT EXISTS VoiceInfo(chat_name_id INTEGER,svr_id INTEGER,'
                             'local_id INTEGER,voice_data BLOB);')
            for peer,server,local,blob in rows:
                chat=db.execute('SELECT rowid FROM Name2Id WHERE user_name=?',(peer,)).fetchone()
                if not chat:chat=(db.execute('INSERT INTO Name2Id(user_name) VALUES(?)',(peer,)).lastrowid,)
                db.execute('INSERT INTO VoiceInfo VALUES(?,?,?,?)',(chat[0],server,local,blob))
        media[key]={'source':str(path)}
        (root/'media_keys.json').write_text(json.dumps(media),encoding='utf-8')
        return path

    def snapshot(info,directory):
        target=directory/'snapshot.db';shutil.copyfile(info['source'],target)
        return target,None

    monkeypatch.setattr(media_voice,'authenticated_snapshot',snapshot)
    message=IncomingMessage(external_id='synthetic-own-voice',contact='peer',sender='owner',content='[语音]',message_type='voice',
                            raw_summary=json.dumps({'owner_message':True,'source':MESSAGE_SOURCE,'local_id':704,'server_id':999}))
    return reader,message,add,media,original


def change_origin(message,**changes):
    origin=json.loads(message.raw_summary);origin.update(changes)
    return message.model_copy(update={'raw_summary':json.dumps(origin)})


def test_owner_server_ack_resolves_unique_same_shard_zero_media_id(voice_corpus):
    reader,message,add,_,_=voice_corpus;add([('peer',0,704,SYNTHETIC_VOICE)])
    path,digest=media_voice.resolve_voice(reader,message)
    assert path.read_bytes()==SYNTHETIC_VOICE
    assert digest==hashlib.sha256(SYNTHETIC_VOICE).hexdigest()


@pytest.mark.parametrize('flag',[False,None,1,'true'])
def test_positive_server_zero_link_requires_strict_owner_message(voice_corpus,flag):
    reader,message,add,_,_=voice_corpus;add([('peer',0,704,SYNTHETIC_VOICE)])
    with pytest.raises(media_voice.VoiceNotReady):
        media_voice.resolve_voice(reader,change_origin(message,owner_message=flag))


@pytest.mark.parametrize('changes',[{'local_id':True},{'local_id':'704'},{'server_id':'999'},{'server_id':True}])
def test_zero_link_requires_original_integer_numbers(voice_corpus,changes):
    reader,message,add,_,_=voice_corpus;add([('peer',0,704,SYNTHETIC_VOICE)])
    with pytest.raises(media_voice.VoiceNotReady):
        media_voice.resolve_voice(reader,change_origin(message,**changes))


@pytest.mark.parametrize('change',['unknown_key','wrong_owner','wrong_name','wrong_account_directory'])
def test_zero_link_requires_owned_key_and_physical_message_source(voice_corpus,change):
    reader,message,add,_,original=voice_corpus;add([('peer',0,704,SYNTHETIC_VOICE)])
    if change=='unknown_key':message=change_origin(message,source='wxid_owner_test/message/message_1.db')
    elif change=='wrong_owner':reader.self_username='wxid_another_owner'
    else:
        path=(original.with_name('message_1.db') if change=='wrong_name'
              else original.parents[3]/'wrong_account'/'db_storage'/'message'/'message_0.db')
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'synthetic wrong source')
        reader.files[MESSAGE_SOURCE]={'source':str(path)}
    with pytest.raises(media_voice.VoiceNotReady):media_voice.resolve_voice(reader,message)


@pytest.mark.parametrize('change',['wrong_key_shard','wrong_physical_shard','wrong_parent','wrong_key_account'])
def test_zero_link_requires_matching_media_shard_and_parent(voice_corpus,change):
    reader,message,add,_,original=voice_corpus
    key=MEDIA_SOURCE;path=original.with_name('media_0.db')
    if change=='wrong_key_shard':key='wxid_owner_test/message/media_1.db'
    elif change=='wrong_physical_shard':path=path.with_name('media_1.db')
    elif change=='wrong_parent':path=original.parents[3]/'another_owner'/'db_storage'/'message'/'media_0.db'
    else:key='wxid_someone_test/message/media_0.db'
    add([('peer',0,704,SYNTHETIC_VOICE)],key=key,path=path)
    with pytest.raises(media_voice.VoiceNotReady):media_voice.resolve_voice(reader,message)


@pytest.mark.parametrize('row',[('another-peer',0,704,SYNTHETIC_VOICE),('peer',0,705,SYNTHETIC_VOICE)])
def test_zero_link_never_uses_another_peer_or_local_id(voice_corpus,row):
    reader,message,add,_,_=voice_corpus;add([row])
    with pytest.raises(media_voice.VoiceNotReady):media_voice.resolve_voice(reader,message)


@pytest.mark.parametrize('row',[('peer',1000,704,SYNTHETIC_VOICE),('peer',999,705,SYNTHETIC_VOICE)])
def test_owner_conflicting_server_or_local_id_is_rejected(voice_corpus,row):
    reader,message,add,_,_=voice_corpus;add([row])
    with pytest.raises(ValueError,match='唯一对应'):media_voice.resolve_voice(reader,message)


@pytest.mark.parametrize('second_server',[0,999])
def test_identical_duplicate_rows_do_not_become_one_digest(voice_corpus,second_server):
    reader,message,add,_,_=voice_corpus
    add([('peer',0,704,SYNTHETIC_VOICE),('peer',second_server,704,SYNTHETIC_VOICE)])
    with pytest.raises(ValueError,match='唯一对应'):media_voice.resolve_voice(reader,message)


def test_cross_shard_positive_duplicate_is_rejected(voice_corpus):
    reader,message,add,_,original=voice_corpus;add([('peer',0,704,SYNTHETIC_VOICE)])
    add([('peer',999,704,SYNTHETIC_VOICE)],key='wxid_owner_test/message/media_1.db',path=original.with_name('media_1.db'))
    with pytest.raises(ValueError,match='唯一对应'):media_voice.resolve_voice(reader,message)


def test_alias_of_same_media_file_is_rejected(voice_corpus):
    reader,message,add,media,_=voice_corpus;path=add([('peer',0,704,SYNTHETIC_VOICE)])
    media[MEDIA_SOURCE.replace('/','\\')]={'source':str(path)}
    (reader.root/'media_keys.json').write_text(json.dumps(media),encoding='utf-8')
    with pytest.raises(ValueError,match='唯一对应'):media_voice.resolve_voice(reader,message)


def test_other_shard_zero_local_id_collision_is_ignored(voice_corpus):
    reader,message,add,_,original=voice_corpus;add([('peer',0,704,SYNTHETIC_VOICE)])
    add([('peer',0,704,b'\x02#!SILK_V3other audio')],key='wxid_owner_test/message/media_1.db',path=original.with_name('media_1.db'))
    assert media_voice.resolve_voice(reader,message)[0].read_bytes()==SYNTHETIC_VOICE


def test_normal_positive_server_inbound_keeps_independent_media_local_id(voice_corpus):
    reader,message,add,_,_=voice_corpus;add([('peer',999,705,SYNTHETIC_VOICE)])
    incoming=change_origin(message,owner_message=False)
    assert media_voice.resolve_voice(reader,incoming)[0].read_bytes()==SYNTHETIC_VOICE


def test_existing_zero_server_inbound_uses_its_corresponding_shard(voice_corpus):
    reader,message,add,_,_=voice_corpus;add([('peer',0,704,SYNTHETIC_VOICE)])
    incoming=change_origin(message,owner_message=False,server_id=0)
    assert media_voice.resolve_voice(reader,incoming)[0].read_bytes()==SYNTHETIC_VOICE


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

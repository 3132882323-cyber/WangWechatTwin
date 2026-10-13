"""Pinned client dispatch contracts; no native, audio or WeChat operation."""
import hashlib
import json
from types import SimpleNamespace

from app.adapters.http_sender import HistoryHTTPSender
from app.auto_voice_reply import AutoVoiceReplyResult, AutoVoicePlan
from app.config import AppConfig
from app.personal_memory import PersonalMemory


def sender(tmp_path, result):
    helper=tmp_path/'runtime_client.py'
    helper.write_text('def ready(*args, **kwargs):\n    return True\ndef send_prepared(*args, **kwargs):\n    return '+repr(result)+'\n',encoding='utf-8')
    reference=tmp_path/'reference.wav'
    reference.write_bytes(b'synthetic reference not real audio')
    manifest=tmp_path/'runtime.json'
    manifest.write_text(json.dumps({'client_module':helper.name,
        'owner_sha256':hashlib.sha256(b'synthetic-owner').hexdigest(),
        'reference_sha256':hashlib.sha256(reference.read_bytes()).hexdigest(),
        'client_sha256':hashlib.sha256(helper.read_bytes()).hexdigest()}),encoding='utf-8')
    cfg=AppConfig(project_root=tmp_path,outbound_voice={'enabled':True,'owned_voice_confirmed':True,
        'reference_audio':str(reference),'assets_dir':str(tmp_path/'assets'),'output_dir':str(tmp_path/'private'),
        'runtime_manifest':str(manifest),'runtime_manifest_sha256':hashlib.sha256(manifest.read_bytes()).hexdigest()})
    adapter=object.__new__(HistoryHTTPSender)
    adapter.config=cfg
    adapter.reader=SimpleNamespace(self_username='synthetic-owner',keys={'synthetic-source':{}},
        personal=PersonalMemory(tmp_path/'personal.sqlite3'))
    adapter._voice_context_current=lambda *args:True
    return adapter,manifest


def test_changed_client_is_not_loaded_or_synthesized(tmp_path,monkeypatch):
    adapter,manifest=sender(tmp_path,{'status':'verified_sent','verified_sent':True})
    (tmp_path/'runtime_client.py').write_text('raise RuntimeError("should not load")',encoding='utf-8')
    monkeypatch.setattr('app.auto_voice_reply.execute_auto_voice',lambda *a,**k:(_ for _ in ()).throw(AssertionError('no synthesis')))
    assert adapter.send_voice('peer','synthetic reply','event',plan=AutoVoicePlan(True,'test','synthetic reply')).status=='not_attempted'


def test_native_acceptance_without_receipt_is_not_success(tmp_path,monkeypatch):
    adapter,_=sender(tmp_path,{'status':'accepted','verified_sent':False})
    def execute(*args,**kwargs):
        assert kwargs['recheck']()
        assert kwargs['send_prepared']('peer',SimpleNamespace(silk_sha256='b'*64),'a'*64) is False
        return AutoVoiceReplyResult('unknown','receipt_unverified')
    monkeypatch.setattr('app.auto_voice_reply.execute_auto_voice',execute)
    assert adapter.send_voice('peer','synthetic reply','event',plan=AutoVoicePlan(True,'test','synthetic reply')).status=='unknown'


def test_exact_verified_result_can_complete(tmp_path,monkeypatch):
    adapter,_=sender(tmp_path,{'status':'verified_sent','verified_sent':True,
        'message_source':'synthetic-source','local_id':1,'server_id':2})
    def execute(*args,**kwargs):
        assert kwargs['send_prepared']('peer',SimpleNamespace(silk_sha256='b'*64),'a'*64) is True
        return AutoVoiceReplyResult('sent','verified')
    monkeypatch.setattr('app.auto_voice_reply.execute_auto_voice',execute)
    assert adapter.send_voice('peer','synthetic reply','event',plan=AutoVoicePlan(True,'test','synthetic reply')).status=='sent'
    with adapter.reader.personal.connect() as db:
        assert db.execute('SELECT count(*) FROM ai_receipts').fetchone()[0]==1

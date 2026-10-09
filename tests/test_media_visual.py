from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json

import httpx

from app.config import AppConfig
from app.db import Database
from app.media_visual import VisualMediaProcessor
from app.models import IncomingMessage


def processor_for(tmp_path):
    config=AppConfig(project_root=tmp_path)
    config.media.images_enabled=True
    root=tmp_path/'reader';root.mkdir()
    return VisualMediaProcessor(config,SimpleNamespace(root=root,files={}))


def sticker(received_at=None):
    return IncomingMessage(external_id='inbound-sticker',contact='peer',sender='peer',
        content='[表情包]',message_type='sticker',
        received_at=received_at or datetime.now(timezone.utc),
        raw_summary='{"sticker_source":"<msg><emoji/></msg>"}')


def test_failed_visual_fetch_stays_pending_and_retry_can_resolve(tmp_path):
    processor=processor_for(tmp_path)
    message=sticker();failed=Future();failed.set_exception(httpx.ReadTimeout('temporary'))
    processor.pending[message.external_id]=(message,failed)
    assert processor.poll([])==[]
    assert processor.pending[message.external_id][1] is None
    assert len(json.loads(processor.store.read_text(encoding='utf-8')))==1
    assert not processor.db.seen(message.external_id)
    assert processor.db.get_state('media_worker_visual')['last_error_code']=='sticker_fetch_timeout'

    processor._transcribe=lambda item:item.model_copy(update={'media_paths':['verified-frame.png']})
    processor.retry_at[message.external_id]=0
    assert processor.poll([])==[]
    processor.pending[message.external_id][1].result(timeout=2)
    ready=processor.poll([])
    assert len(ready)==1 and ready[0].media_paths==['verified-frame.png']
    processor.db.add_incoming(ready[0],'low')
    assert processor.poll([])==[] and not processor.pending
    processor.close()


def test_exhausted_visual_retries_produce_safe_review_with_fixed_token(tmp_path):
    processor=processor_for(tmp_path)
    message=sticker();failed=Future();failed.set_exception(ValueError('private XML detail'))
    processor.pending[message.external_id]=(message,failed)
    processor.retry_failures[message.external_id]=len(processor.retry_delays)
    output=processor.poll([])
    assert len(output)==1 and output[0].media_paths==[]
    origin=json.loads(output[0].raw_summary)
    assert origin['visual_error']=='sticker_asset_invalid'
    assert origin['resume_incomplete'] is True
    assert 'private XML detail' not in str(processor.db.recent_events())
    assert any(event['event_type']=='visual_media_unavailable' and event['detail']=='sticker_asset_invalid'
               for event in processor.db.recent_events())
    assert len(json.loads(processor.store.read_text(encoding='utf-8')))==1
    processor.close()


def test_slow_first_run_sticker_keeps_normal_policy(tmp_path):
    processor=processor_for(tmp_path)
    message=sticker(datetime.now(timezone.utc)-timedelta(seconds=40))
    ready=Future();ready.set_result(message.model_copy(update={'media_paths':['verified-frame.png']}))
    processor.pending[message.external_id]=(message,ready)
    processor.retry_failures[message.external_id]=1
    output=processor.poll([])
    assert len(output)==1 and 'resume_incomplete' not in json.loads(output[0].raw_summary)
    processor.close()


def test_restart_restored_sticker_is_review_only(tmp_path,monkeypatch):
    config=AppConfig(project_root=tmp_path);config.media.images_enabled=True
    root=tmp_path/'reader';root.mkdir()
    message=sticker()
    (root/'visual_pending.json').write_text(json.dumps([message.model_dump(mode='json')]),encoding='utf-8')
    monkeypatch.setattr(VisualMediaProcessor,'_transcribe',
        lambda self,item:item.model_copy(update={'media_paths':['verified-frame.png']}))
    processor=VisualMediaProcessor(config,SimpleNamespace(root=root,files={}))
    processor.pending[message.external_id][1].result(timeout=2)
    output=processor.poll([])
    assert len(output)==1 and json.loads(output[0].raw_summary)['resume_incomplete'] is True
    processor.close()


def test_already_seen_sticker_is_not_requeued_on_restart(tmp_path,monkeypatch):
    config=AppConfig(project_root=tmp_path);config.media.images_enabled=True
    root=tmp_path/'reader';root.mkdir()
    message=sticker()
    Database(config.resolve(config.paths.database)).add_incoming(message,'low')
    (root/'visual_pending.json').write_text(json.dumps([message.model_dump(mode='json')]),encoding='utf-8')
    calls=[]
    monkeypatch.setattr(VisualMediaProcessor,'_transcribe',lambda self,item:calls.append(item))
    processor=VisualMediaProcessor(config,SimpleNamespace(root=root,files={}))
    assert not processor.pending and processor.poll([])==[] and calls==[]
    processor.close()

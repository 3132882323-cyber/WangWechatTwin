"""Decode/download visual assets off the normal text polling path."""
import json
import time
from concurrent.futures import Future
from datetime import datetime, timezone
from pathlib import Path
import httpx
from app.media_voice import OfflineVoiceProcessor
from app.media_images import LocalImageReader
from app.stickers import acquire


class VisualMediaProcessor(OfflineVoiceProcessor):
    kind='visual'
    enable_field='images_enabled'
    workers=2
    retry_delays=(3,10)
    max_retry_age_seconds=75

    def __init__(self,config,reader):
        self.retry_failures={}
        self.retry_at={}
        self.last_error_code=None
        # Only messages loaded from the durable queue after a restart are
        # recovery work. Slow media on the first run keeps the normal policy.
        self.recovered_ids=set()
        stored=reader.root/'visual_pending.json'
        if stored.exists():
            try:
                records=json.loads(stored.read_text(encoding='utf-8'))
                self.recovered_ids={record['external_id'] for record in records
                    if isinstance(record,dict) and isinstance(record.get('external_id'),str)}
            except (OSError,ValueError,TypeError,KeyError):pass
        self.image_reader=LocalImageReader(config,reader)
        super().__init__(config,reader)
        self.recovered_ids.intersection_update(self.pending)

    @staticmethod
    def _error_code(message,error):
        kind='sticker' if message.message_type=='sticker' else 'image'
        if isinstance(error,httpx.TimeoutException):return kind+'_fetch_timeout'
        if isinstance(error,httpx.HTTPStatusError):return kind+'_cdn_http_error'
        if isinstance(error,httpx.TransportError):return kind+'_cdn_transport_error'
        if isinstance(error,ValueError):return kind+'_asset_invalid'
        return kind+'_asset_unavailable'

    def _record_failure(self,event,message,code):
        self.last_error_code=code
        try:self.db.add_event(event,code,level='warning',contact=message.contact)
        except Exception:pass

    @staticmethod
    def _manual_review(message,code):
        origin=json.loads(message.raw_summary or '{}')
        origin['visual_error']=code
        # The existing recovery policy forces a draft even if a future media
        # rule would otherwise allow this delayed message to send.
        origin['resume_incomplete']=True
        return message.model_copy(update={'raw_summary':json.dumps(origin)})

    def poll(self,messages):
        # A single failed fetch must not consume the message as an imageless
        # reply. Keep it in the persisted visual queue for bounded retries.
        output=[];changed=False
        for key,(message,future) in list(self.pending.items()):
            origin=json.loads(message.raw_summary or '{}')
            owner_done=origin.get('owner_message') and hasattr(self.reader,'personal') and self.reader.personal.observed(message.external_id)
            if self.db.seen(message.external_id) or owner_done:
                if future is not None:future.cancel()
                del self.pending[key]
                self.retry_failures.pop(key,None);self.retry_at.pop(key,None)
                self.recovered_ids.discard(key)
                changed=True
                continue
            if future is None:
                if time.monotonic()>=self.retry_at.get(key,0):
                    self.pending[key]=(message,self.pool.submit(self._transcribe,message))
                    changed=True
                continue
            if not future.done():continue
            try:
                resolved=future.result()
            except Exception as error:
                code=self._error_code(message,error)
                failures=self.retry_failures.get(key,0)
                age=(datetime.now(timezone.utc)-message.received_at).total_seconds()
                if failures<len(self.retry_delays) and age<self.max_retry_age_seconds:
                    self.retry_failures[key]=failures+1
                    self.retry_at[key]=time.monotonic()+self.retry_delays[failures]
                    self.pending[key]=(message,None)
                    self._record_failure('visual_media_retry',message,code)
                else:
                    manual=self._manual_review(message,code)
                    ready=Future();ready.set_result(manual)
                    self.pending[key]=(manual,ready)
                    output.append(manual)
                    self.retry_failures.pop(key,None);self.retry_at.pop(key,None)
                    self._record_failure('visual_media_unavailable',message,code)
                changed=True
            else:
                if key in self.recovered_ids:
                    origin=json.loads(resolved.raw_summary or '{}')
                    origin['resume_incomplete']=True
                    resolved=resolved.model_copy(update={'raw_summary':json.dumps(origin)})
                output.append(resolved)
                # Retain the durable queue entry until the reply scheduler
                # records the inbound message, including across a crash here.
        for message in messages:
            if not self._accept(message):
                output.append(message)
                continue
            origin=json.loads(message.raw_summary or '{}')
            owner_done=origin.get('owner_message') and hasattr(self.reader,'personal') and self.reader.personal.observed(message.external_id)
            if self.db.seen(message.external_id) or owner_done or message.external_id in self.pending:
                continue
            if len(self.pending)>=self.config.media.max_pending_voice:
                code='visual_queue_full'
                manual=self._manual_review(message,code)
                output.append(manual)
                self._record_failure('visual_media_unavailable',message,code)
                changed=True
                continue
            self.pending[message.external_id]=(message,self.pool.submit(self._transcribe,message))
            changed=True
        if changed:
            self._persist()
            self.db.set_state('media_worker_visual',{'pending_count':len(self.pending),
                'last_error_code':self.last_error_code,'updated_at':time.time()})
        return output

    def _accept(self,message):
        return message.message_type in {'image','sticker'} and self.config.media.images_enabled

    def _transcribe(self,message):
        origin=json.loads(message.raw_summary or '{}')
        if message.message_type=='image':
            deadline=time.monotonic()+10
            while True:
                paths,meta=self.image_reader.resolve(message)
                if paths or time.monotonic()>=deadline:break
                time.sleep(.4)
            origin.update(meta)
            content='对方发来图片，请只根据已附图片与当前聊天理解，不猜测缺失信息。' if paths else message.content
        else:
            source=origin.pop('sticker_source',None)
            if not source:raise ValueError('缺少已校验的表情元数据')
            accounts={p.parent for info in self.reader.files.values() for p in Path(info['source']).parents if p.name=='db_storage'}
            digest,paths,label=acquire(source,self.reader.root,accounts)
            origin.update(sticker_md5=digest,asset_verified=True)
            content='[表情包]'+('\n'+label if label else '')
        return message.model_copy(update={'content':content,'media_paths':paths,'raw_summary':json.dumps(origin)})

"""Local SILK extraction and offline ASR. Audio never leaves this computer.

Extraction/decoding adapted from ikevss/wechat-ai-memory (MIT).
See licenses/WECHAT_AI_MEMORY_LICENSE.txt.
"""
from pathlib import Path
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import sqlite3,json,tempfile,hashlib,io,wave,time,re
from datetime import datetime,timezone
from app.models import IncomingMessage
from app.db import Database
from app.adapters.history_reader import authenticated_snapshot,SnapshotBusyError
from app.stickers import _atomic


class VoiceNotReady(ValueError):
    pass


class _BoundedPCMWriter(io.RawIOBase):
    def __init__(self, output, max_bytes):
        super().__init__()
        self.output, self.max_bytes, self.size = output, max_bytes, 0

    def writable(self):
        return True

    def write(self, data):
        if self.size + len(data) > self.max_bytes:
            raise ValueError('语音时长超出自动转写范围')
        self.output.writeframesraw(data)
        self.size += len(data)
        return len(data)


def resolve_voice(reader,message):
    origin=json.loads(message.raw_summary or '{}')
    server=int(origin.get('server_id') or 0);local=int(origin.get('local_id') or 0)
    # Outgoing media can be written before the message receives its server ID.
    # Link that zero-ID payload only to the original owned message_N source
    # and corresponding media_N file, never to another shard's local ID.
    binding=None
    source=origin.get('source')
    owner=getattr(reader,'self_username',None)
    if isinstance(source,str) and isinstance(owner,str) and owner:
        source=source.replace('\\','/')
        shard=re.fullmatch(r'([^/]+)/message/message_([0-9]+)\.db',source)
        originals=[info for name,info in getattr(reader,'files',{}).items()
                   if isinstance(name,str) and name.replace('\\','/')==source]
        if (shard and shard[1].rsplit('_',1)[0]==owner and len(originals)==1
                and isinstance(originals[0],dict) and isinstance(originals[0].get('source'),str)):
            physical=Path(originals[0]['source'])
            try:
                canonical=physical.resolve(strict=True)
                if (physical.is_absolute() and canonical.is_file()
                        and canonical.name=='message_'+shard[2]+'.db'
                        and canonical.parent.name=='message'
                        and canonical.parent.parent.name=='db_storage'
                        and canonical.parent.parent.parent.name==shard[1]):
                    binding=(shard[1]+'/message/media_'+shard[2]+'.db',canonical.parent,
                             'media_'+shard[2]+'.db')
            except (OSError,RuntimeError):pass
    keys_path=reader.root/'media_keys.json'
    if not keys_path.exists():raise ValueError('尚未连接经认证的语音媒体库')
    keys=json.loads(keys_path.read_text())
    found=[]
    max_voice_bytes=4*1024*1024
    for rel,info in keys.items():
        same_shard=False
        if binding and isinstance(rel,str) and isinstance(info,dict) and isinstance(info.get('source'),str):
            physical=Path(info['source'])
            try:
                canonical=physical.resolve(strict=True)
                same_shard=(physical.is_absolute() and canonical.is_file() and rel.replace('\\','/')==binding[0]
                            and canonical.name==binding[2] and canonical.parent==binding[1])
            except (OSError,RuntimeError):pass
        zero_link=(server>0 and type(origin.get('server_id')) is int
                   and local>0 and type(origin.get('local_id')) is int
                   and origin.get('owner_message') is True and same_shard)
        with tempfile.TemporaryDirectory(dir=reader.root) as tmp:
            snapshot,_=authenticated_snapshot(info,Path(tmp))
            with closing(sqlite3.connect(snapshot.as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
                chats=db.execute('SELECT rowid FROM Name2Id WHERE user_name=? LIMIT 2',(message.contact,)).fetchall()
                if not chats:continue
                if len(chats)!=1:raise ValueError('语音数据未能与该联系人和消息编号唯一对应')
                chat=chats[0]
                if server:
                    selector='(svr_id=? OR local_id=?)' if zero_link else 'svr_id=?'
                    params=(max_voice_bytes,chat[0],server,local) if zero_link else (max_voice_bytes,chat[0],server)
                    rows=db.execute('SELECT CASE WHEN length(voice_data)<=? THEN voice_data ELSE NULL END,'
                                    'length(voice_data),svr_id,local_id FROM VoiceInfo WHERE chat_name_id=? AND '
                                    +selector+' LIMIT 2',params).fetchall()
                else:
                    # Local IDs are safe only in the media shard corresponding to this message shard.
                    if not same_shard or local<=0:continue
                    rows=db.execute('SELECT CASE WHEN length(voice_data)<=? THEN voice_data ELSE NULL END,'
                                    'length(voice_data),svr_id,local_id FROM VoiceInfo WHERE chat_name_id=? AND local_id=? LIMIT 2',
                                    (max_voice_bytes,chat[0],local)).fetchall()
                if len(rows)>1:raise ValueError('语音数据未能与该联系人和消息编号唯一对应')
                for raw,size,media_server,media_local in rows:
                    if server and (media_server not in (0,server) or (media_server==0 and not zero_link)
                                   or (origin.get('owner_message') is True and local>0 and media_local!=local)):
                        raise ValueError('语音数据未能与该联系人和消息编号唯一对应')
                    # Duplicate records are ambiguous even when their bytes
                    # happen to hash identically, including across databases.
                    if found:raise ValueError('语音数据未能与该联系人和消息编号唯一对应')
                    if size and size>max_voice_bytes:raise ValueError('语音格式或大小不受支持')
                    if not raw:continue
                    data=bytes(raw);digest=hashlib.sha256(data).hexdigest()
                    found.append((digest,data))
    if not found:raise VoiceNotReady('语音音频尚未写入本机媒体库')
    digest,data=found[0]
    if len(data)>4*1024*1024 or not (data.startswith(b'#!SILK_V3') or data.startswith(b'\x02#!SILK_V3')):
        raise ValueError('语音格式或大小不受支持')
    root=reader.root/'voice_assets';root.mkdir(exist_ok=True);path=root/(digest+'.silk')
    _atomic(path,data);return path,digest


class OfflineVoiceProcessor:
    kind='voice'
    enable_field='voice_enabled'
    workers=1
    def __init__(self,config,reader):
        self.config,self.reader=config,reader
        self.pool=ThreadPoolExecutor(max_workers=self.workers,thread_name_prefix='wechat-media-'+self.kind)
        self.pending={};self.model=None
        self.store=reader.root/(self.kind+'_pending.json');self.db=Database(config.resolve(config.paths.database))
        if getattr(config.media,self.enable_field) and self.store.exists():
            try:
                for record in json.loads(self.store.read_text(encoding='utf-8'))[:config.media.max_pending_voice]:
                    message=IncomingMessage.model_validate(record)
                    age=(datetime.now(timezone.utc)-message.received_at).total_seconds()
                    if -5<=age<=600 and not self.db.seen(message.external_id):
                        self.pending[message.external_id]=(message,self.pool.submit(self._transcribe,message))
            except (OSError,ValueError,TypeError):pass

    def _persist(self):
        _atomic(self.store,json.dumps([message.model_dump(mode='json') for message,_ in self.pending.values()],ensure_ascii=False).encode('utf-8'))

    def _transcribe(self,message):
        started=time.monotonic();deadline=started+10
        while True:
            try:path,digest=resolve_voice(self.reader,message);break
            except (VoiceNotReady,SnapshotBusyError):
                if time.monotonic()>=deadline:raise
                time.sleep(.4)
        if hasattr(self.reader,'personal'):
            origin=json.loads(message.raw_summary or '{}')
            if (origin.get('owner_message') is True
                    and self.reader.personal.is_generated_voice(message.contact,digest)
                    and isinstance(origin.get('source'),str)
                    and type(origin.get('local_id')) is int and origin['local_id']>0):
                # Provenance only, not a server receipt. Even failed ASR must
                # not turn our own generated payload into a manual sample.
                self.reader.personal.receipt(message.contact,message.content,origin['source'],origin['local_id'])
            correction=self.reader.personal.corrected(message.external_id,message.contact)
            if correction:return self._result(message,{'text':correction,'audio_sha256':digest,'uncertain':False,'duration_seconds':0},True)
        root=self.reader.root/'voice_transcripts';root.mkdir(exist_ok=True)
        model_path=self.config.resolve(self.config.media.voice_model)
        model_file=model_path/'model.bin'
        if not model_file.exists():raise ValueError('本机语音模型尚未准备好')
        vocabulary=self.reader.personal.voice_vocabulary(message.contact,self.config.owner_name,message.display_name or message.sender) if hasattr(self.reader,'personal') else self.config.owner_name
        model_stamp=str(model_file.stat().st_size)+':'+str(model_file.stat().st_mtime_ns)+':word-quality-v3:'+hashlib.sha256(vocabulary.encode()).hexdigest()[:16]
        cache=root/(digest+'-offline.json')
        if cache.exists():
            saved=json.loads(cache.read_text(encoding='utf-8'))
            if saved.get('model_stamp')==model_stamp and saved.get('audio_sha256')==digest:
                return self._result(message,saved,True)
        import pysilk
        with tempfile.TemporaryDirectory(dir=self.reader.root) as tmp:
            wav_path=Path(tmp)/'speech.wav'
            with wave.open(str(wav_path),'wb') as output:
                output.setnchannels(1);output.setsampwidth(2);output.setframerate(24000)
                pcm=_BoundedPCMWriter(output,int(self.config.media.max_voice_seconds*24000*2))
                with path.open('rb') as source:pysilk.decode(source,pcm,24000)
            seconds=pcm.size/(24000*2)
            if not .2<=seconds<=self.config.media.max_voice_seconds:raise ValueError('语音时长超出自动转写范围')
            if self.model is None:
                from faster_whisper import WhisperModel
                self.model=WhisperModel(str(model_path),device='cpu',compute_type='int8',cpu_threads=4,local_files_only=True)
            segments,info=self.model.transcribe(str(wav_path),language='zh',beam_size=5,vad_filter=True,condition_on_previous_text=False,
                hotwords=vocabulary,word_timestamps=True,hallucination_silence_threshold=1.0,
                vad_parameters={"min_silence_duration_ms":500})
            segments=list(segments);text=''.join(segment.text.strip() for segment in segments).strip()
        if not text or not segments:raise ValueError('语音未识别出可靠正文')
        from app.voice_quality import assess_transcript
        quality_reasons=assess_transcript(text,segments);uncertain=bool(quality_reasons)
        saved={'text':text,'audio_sha256':digest,'model_stamp':model_stamp,'uncertain':uncertain,
               'quality_reasons':quality_reasons,'segments':[{'start':round(segment.start,2),'end':round(segment.end,2),'text':segment.text.strip()} for segment in segments],
               'duration_seconds':round(seconds,2),'asr_seconds':round(time.monotonic()-started,2),'engine':'faster-whisper-small-int8-local'}
        _atomic(cache,json.dumps(saved,ensure_ascii=False).encode('utf-8'))
        return self._result(message,saved,False)

    def _result(self,message,result,cached):
        origin=json.loads(message.raw_summary or '{}')
        origin.update(original_type='voice',transcription_source='offline_whisper',asr_uncertain=result['uncertain'],
                      asr_quality_reasons=result.get('quality_reasons',[]),audio_sha256=result['audio_sha256'],duration_seconds=result['duration_seconds'],transcript_cached=cached)
        return message.model_copy(update={'message_type':'text','content':result['text'],'raw_summary':json.dumps(origin)})

    def poll(self,messages):
        output=[];changed=False
        for key,(message,future) in list(self.pending.items()):
            owner_done=json.loads(message.raw_summary or '{}').get('owner_message') and hasattr(self.reader,'personal') and self.reader.personal.observed(message.external_id)
            if self.db.seen(message.external_id) or owner_done:
                future.cancel()
                del self.pending[key];changed=True;continue
            if not future.done():continue
            try:output.append(future.result())
            except Exception as exc:
                origin=json.loads(message.raw_summary or '{}');origin[self.kind+'_error']=type(exc).__name__
                output.append(message.model_copy(update={'raw_summary':json.dumps(origin)}))
        for message in messages:
            if not self._accept(message):output.append(message);continue
            owner_done=json.loads(message.raw_summary or '{}').get('owner_message') and hasattr(self.reader,'personal') and self.reader.personal.observed(message.external_id)
            if self.db.seen(message.external_id) or owner_done:continue
            if message.external_id in self.pending:continue
            if len(self.pending)>=self.config.media.max_pending_voice:output.append(message);continue
            self.pending[message.external_id]=(message,self.pool.submit(self._transcribe,message))
            changed=True
        if changed:
            self._persist()
            self.db.set_state('media_worker_'+self.kind,{'pending_count':len(self.pending),'updated_at':time.time()})
        return output

    def _accept(self,message):
        return message.message_type=='voice' and self.config.media.voice_enabled

    def close(self):
        self.pool.shutdown(wait=False,cancel_futures=True)

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


def resolve_voice(reader,message):
    origin=json.loads(message.raw_summary or '{}')
    server=int(origin.get('server_id') or 0);local=int(origin.get('local_id') or 0)
    keys_path=reader.root/'media_keys.json'
    if not keys_path.exists():raise ValueError('尚未连接经认证的语音媒体库')
    keys=json.loads(keys_path.read_text())
    matches=[]
    for rel,info in keys.items():
        with tempfile.TemporaryDirectory(dir=reader.root) as tmp:
            snapshot,_=authenticated_snapshot(info,Path(tmp))
            with closing(sqlite3.connect(snapshot.as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
                chat=db.execute('SELECT rowid FROM Name2Id WHERE user_name=?',(message.contact,)).fetchone()
                if not chat:continue
                if server:
                    rows=db.execute('SELECT voice_data FROM VoiceInfo WHERE chat_name_id=? AND svr_id=?',(chat[0],server)).fetchall()
                else:
                    # Local IDs are safe only in the media shard corresponding to this message shard.
                    source=Path(origin.get('source','')).name
                    if Path(info['source']).name!='media_'+source.removeprefix('message_'):continue
                    rows=db.execute('SELECT voice_data FROM VoiceInfo WHERE chat_name_id=? AND local_id=?',(chat[0],local)).fetchall()
                matches.extend(bytes(row[0]) for row in rows if row[0])
    unique={hashlib.sha256(data).hexdigest():data for data in matches}
    if not unique:raise VoiceNotReady('语音音频尚未写入本机媒体库')
    if len(unique)!=1:raise ValueError('语音数据未能与该联系人和消息编号唯一对应')
    digest,data=next(iter(unique.items()))
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
        root=self.reader.root/'voice_transcripts';root.mkdir(exist_ok=True)
        model_path=self.config.resolve(self.config.media.voice_model)
        model_file=model_path/'model.bin'
        if not model_file.exists():raise ValueError('本机语音模型尚未准备好')
        model_stamp=str(model_file.stat().st_size)+':'+str(model_file.stat().st_mtime_ns)
        cache=root/(digest+'-offline.json')
        if cache.exists():
            saved=json.loads(cache.read_text(encoding='utf-8'))
            if saved.get('model_stamp')==model_stamp and saved.get('audio_sha256')==digest:
                return self._result(message,saved,True)
        import pysilk
        pcm=io.BytesIO()
        with path.open('rb') as source:pysilk.decode(source,pcm,24000)
        data=pcm.getvalue();seconds=len(data)/(24000*2)
        if not .2<=seconds<=self.config.media.max_voice_seconds:raise ValueError('语音时长超出自动转写范围')
        with tempfile.TemporaryDirectory(dir=self.reader.root) as tmp:
            wav_path=Path(tmp)/'speech.wav'
            with wave.open(str(wav_path),'wb') as output:
                output.setnchannels(1);output.setsampwidth(2);output.setframerate(24000);output.writeframes(data)
            if self.model is None:
                from faster_whisper import WhisperModel
                self.model=WhisperModel(str(model_path),device='cpu',compute_type='int8',cpu_threads=4,local_files_only=True)
            segments,info=self.model.transcribe(str(wav_path),language='zh',beam_size=5,vad_filter=True,condition_on_previous_text=False)
            segments=list(segments);text=''.join(segment.text.strip() for segment in segments).strip()
        if not text or not segments:raise ValueError('语音未识别出可靠正文')
        uncertain=any(s.avg_logprob<-1 or s.no_speech_prob>.6 for s in segments)
        uncertain=uncertain or bool(re.search(r'\d{2,}|[一二两三四五六七八九十百千万]+(?:元|号|点|天|月|年|块|分钟|小时)',text))
        saved={'text':text,'audio_sha256':digest,'model_stamp':model_stamp,'uncertain':uncertain,
               'duration_seconds':round(seconds,2),'asr_seconds':round(time.monotonic()-started,2),'engine':'faster-whisper-small-int8-local'}
        _atomic(cache,json.dumps(saved,ensure_ascii=False).encode('utf-8'))
        return self._result(message,saved,False)

    def _result(self,message,result,cached):
        origin=json.loads(message.raw_summary or '{}')
        origin.update(original_type='voice',transcription_source='offline_whisper',asr_uncertain=result['uncertain'],
                      audio_sha256=result['audio_sha256'],duration_seconds=result['duration_seconds'],transcript_cached=cached)
        return message.model_copy(update={'message_type':'text','content':result['text'],'raw_summary':json.dumps(origin)})

    def poll(self,messages):
        output=[];changed=False
        for key,(message,future) in list(self.pending.items()):
            if self.db.seen(message.external_id):
                del self.pending[key];changed=True;continue
            if not future.done():continue
            try:output.append(future.result())
            except Exception as exc:
                origin=json.loads(message.raw_summary or '{}');origin[self.kind+'_error']=type(exc).__name__
                output.append(message.model_copy(update={'raw_summary':json.dumps(origin)}))
        for message in messages:
            if not self._accept(message):output.append(message);continue
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

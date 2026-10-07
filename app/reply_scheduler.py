"""Durable reply generation queue; polling remains independent of model latency."""
import json,time,threading
from contextlib import nullcontext
from app.models import IncomingMessage


class ReplyScheduler:
    def __init__(self,config,db,pipeline,adapter):
        self.config,self.db,self.pipeline,self.adapter=config,db,pipeline,adapter
        self.stop=threading.Event();self.wake=threading.Event()
        with db.connect() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS reply_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,
                contact TEXT,payload TEXT,status TEXT,created REAL,updated REAL)''')
            for identity,payload in connection.execute("SELECT id,payload FROM reply_jobs WHERE status IN ('working','failed')").fetchall():
                message=IncomingMessage.model_validate_json(payload);origin=json.loads(message.raw_summary or '{}');origin['resume_incomplete']=True
                message=message.model_copy(update={'raw_summary':json.dumps(origin)})
                connection.execute("UPDATE reply_jobs SET status='pending',payload=?,updated=? WHERE id=?",(message.model_dump_json(),time.time(),identity))
        self.threads=[threading.Thread(target=self.run,args=(lane,),daemon=True,name='wechat-reply-'+lane) for lane in ('deepseek','chatgpt','doubao')]
        self.thread=self.threads[0]
        for thread in self.threads:thread.start()

    @staticmethod
    def category(message):return 'text' if message.message_type=='text' else 'visual'

    @staticmethod
    def recovering(message):return bool(json.loads(message.raw_summary or '{}').get('resume_incomplete'))

    @classmethod
    def compatible(cls,left,right):
        return not cls.recovering(left) and not cls.recovering(right) and cls.category(left)==cls.category(right)

    @classmethod
    def occupancy_key(cls,message):
        # Recovery can only draft; it must not hold another model's live replies.
        return message.contact,cls.category(message),cls.recovering(message)

    def submit(self,messages):
        from app.cli import merge_incoming_messages
        now=time.time()
        for message in messages:
            if self.db.seen(message.external_id):continue
            with self.db.connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                candidates=connection.execute("SELECT id,payload FROM reply_jobs WHERE contact=? AND status='pending' ORDER BY id DESC",(message.contact,)).fetchall()
                pending=next((row for row in candidates if self.compatible(IncomingMessage.model_validate_json(row[1]),message)),None)
                if pending:
                    previous=IncomingMessage.model_validate_json(pending[1]);merged=merge_incoming_messages([previous,message])[0]
                    connection.execute('UPDATE reply_jobs SET payload=?,updated=? WHERE id=?',(merged.model_dump_json(),now,pending[0]))
                else:
                    active_candidates=connection.execute("SELECT payload FROM reply_jobs WHERE contact=? AND status='working' ORDER BY id DESC",(message.contact,)).fetchall()
                    active=next((row for row in active_candidates if self.compatible(IncomingMessage.model_validate_json(row[0]),message)),None)
                    if active:
                        previous=IncomingMessage.model_validate_json(active[0]);message=merge_incoming_messages([previous,message])[0]
                    connection.execute('INSERT INTO reply_jobs(contact,payload,status,created,updated) VALUES(?,?,\'pending\',?,?)',(message.contact,message.model_dump_json(),now,now))
                connection.execute('COMMIT')
        self.wake.set()

    @staticmethod
    def lane(message):
        from app.llm import ReplyLLM
        from app.risk import assess_risk
        return ReplyLLM.route(json.dumps({'incoming':{'content':message.content,'message_type':message.message_type},'__media_paths':message.media_paths}),assess_risk(message.content).level)

    def run(self,lane='deepseek'):
        while not self.stop.is_set():
            if lane=='chatgpt' and (self.db.get_state('browser_failures') or {}).get('blocked_until',0)>time.time():
                self.stop.wait(.5);continue
            if self.config.resolve(self.config.paths.pause_file).exists():self.wake.wait(.5);self.wake.clear();continue
            try:
                row=None
                if lane=='deepseek':self.pipeline.send_approved(self.adapter)
                with self.db.connect() as connection:
                    connection.execute('BEGIN IMMEDIATE')
                    candidates=[(identity,payload,IncomingMessage.model_validate_json(payload)) for identity,payload in connection.execute("SELECT id,payload FROM reply_jobs WHERE status='pending' AND updated<? ORDER BY id",(time.time()-.35,))]
                    candidates.sort(key=lambda candidate:(self.recovering(candidate[2]),candidate[0]))
                    occupied={self.occupancy_key(IncomingMessage.model_validate_json(payload)) for (payload,) in connection.execute("SELECT payload FROM reply_jobs WHERE status='working'")}
                    row=next(((identity,payload) for identity,payload,message in candidates if self.occupancy_key(message) not in occupied and self.lane(message)==lane),None)
                    if row:connection.execute("UPDATE reply_jobs SET status='working',updated=? WHERE id=?",(time.time(),row[0]))
                    connection.execute('COMMIT')
                if not row:self.wake.wait(.5);self.wake.clear();continue
                message=IncomingMessage.model_validate_json(row[1])
                bind=self.adapter.bind_reply(message) if hasattr(self.adapter,'bind_reply') else nullcontext()
                started=time.monotonic()
                with bind:result=self.pipeline.process(message,self.adapter)
                self.db.add_event('reply_timing',json.dumps({'processing_ms':round((time.monotonic()-started)*1000),'result':result.status}),contact=message.contact)
                with self.db.connect() as connection:connection.execute("UPDATE reply_jobs SET status='done',payload='',updated=? WHERE id=?",(time.time(),row[0]))
                self.db.add_event('reply_job_completed',result.status,contact=message.contact)
            except Exception as error:
                if row:
                    with self.db.connect() as connection:connection.execute("UPDATE reply_jobs SET status='failed',updated=? WHERE id=?",(time.time(),row[0]))
                self.db.add_event('reply_worker_error',type(error).__name__,level='error')
                self.stop.wait(.5)

    def close(self):
        self.stop.set();self.wake.set()

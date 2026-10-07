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
            connection.execute("UPDATE reply_jobs SET status='pending' WHERE status='working'")
        self.thread=threading.Thread(target=self.run,daemon=True,name='wechat-reply-generation');self.thread.start()

    def submit(self,messages):
        from app.cli import merge_incoming_messages
        now=time.time()
        for message in messages:
            if self.db.seen(message.external_id):continue
            with self.db.connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                pending=connection.execute("SELECT id,payload FROM reply_jobs WHERE contact=? AND status='pending' ORDER BY id DESC LIMIT 1",(message.contact,)).fetchone()
                if pending:
                    previous=IncomingMessage.model_validate_json(pending[1]);merged=merge_incoming_messages([previous,message])[0]
                    connection.execute('UPDATE reply_jobs SET payload=?,updated=? WHERE id=?',(merged.model_dump_json(),now,pending[0]))
                else:
                    active=connection.execute("SELECT payload FROM reply_jobs WHERE contact=? AND status='working' ORDER BY id DESC LIMIT 1",(message.contact,)).fetchone()
                    if active:
                        previous=IncomingMessage.model_validate_json(active[0]);message=merge_incoming_messages([previous,message])[0]
                    connection.execute('INSERT INTO reply_jobs(contact,payload,status,created,updated) VALUES(?,?,\'pending\',?,?)',(message.contact,message.model_dump_json(),now,now))
                connection.execute('COMMIT')
        self.wake.set()

    def run(self):
        while not self.stop.is_set():
            if self.config.resolve(self.config.paths.pause_file).exists():self.wake.wait(.5);self.wake.clear();continue
            try:
                row=None
                self.pipeline.send_approved(self.adapter)
                with self.db.connect() as connection:
                    connection.execute('BEGIN IMMEDIATE')
                    row=connection.execute("SELECT id,payload FROM reply_jobs WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
                    if row:connection.execute("UPDATE reply_jobs SET status='working',updated=? WHERE id=?",(time.time(),row[0]))
                    connection.execute('COMMIT')
                if not row:self.wake.wait(.5);self.wake.clear();continue
                message=IncomingMessage.model_validate_json(row[1])
                bind=self.adapter.bind_reply(message) if hasattr(self.adapter,'bind_reply') else nullcontext()
                with bind:result=self.pipeline.process(message,self.adapter)
                with self.db.connect() as connection:connection.execute("UPDATE reply_jobs SET status='done',payload='',updated=? WHERE id=?",(time.time(),row[0]))
                self.db.add_event('reply_job_completed',result.status,contact=message.contact)
            except Exception as error:
                if row:
                    with self.db.connect() as connection:connection.execute("UPDATE reply_jobs SET status='failed',updated=? WHERE id=?",(time.time(),row[0]))
                self.db.add_event('reply_worker_error',type(error).__name__,level='error')
                self.stop.wait(.5)

    def close(self):
        self.stop.set();self.wake.set()

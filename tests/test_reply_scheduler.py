import threading,time,json
from types import SimpleNamespace
from app.config import AppConfig
from app.db import Database
from app.models import IncomingMessage
from app.reply_scheduler import ReplyScheduler


def test_reader_can_enqueue_while_model_waits_and_pending_burst_merges(tmp_path):
    cfg=AppConfig(project_root=tmp_path);db=Database(tmp_path/'review.sqlite3')
    started=threading.Event();release=threading.Event()
    class Pipeline:
        def send_approved(self,adapter):return 0
        def process(self,message,adapter):started.set();release.wait(3);return SimpleNamespace(status='draft')
    scheduler=ReplyScheduler(cfg,db,Pipeline(),SimpleNamespace())
    first=IncomingMessage(external_id='one',contact='alice',sender='alice',content='第一句')
    try:
        scheduler.submit([first]);assert started.wait(2)
        before=time.monotonic();scheduler.submit([first.model_copy(update={'external_id':'two','content':'第二句'})]);scheduler.submit([first.model_copy(update={'external_id':'three','content':'第三句'})])
        assert time.monotonic()-before<1
        with db.connect() as conn:
            pending=conn.execute("SELECT payload FROM reply_jobs WHERE status='pending'").fetchall()
        assert len(pending)==1
        merged=json.loads(pending[0][0]);assert all(word in merged['content'] for word in ['第一句','第二句','第三句'])
    finally:release.set();scheduler.close();scheduler.thread.join(2)

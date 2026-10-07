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


def test_gpt_wait_does_not_block_daily_deepseek_jobs(tmp_path):
    cfg=AppConfig(project_root=tmp_path);db=Database(tmp_path/'review.sqlite3')
    gpt_started=threading.Event();daily_done=threading.Event();release=threading.Event()
    class Pipeline:
        def send_approved(self,adapter):return 0
        def process(self,message,adapter):
            if message.contact=='complex':gpt_started.set();release.wait(3)
            else:daily_done.set()
            return SimpleNamespace(status='draft')
    scheduler=ReplyScheduler(cfg,db,Pipeline(),SimpleNamespace())
    try:
        scheduler.submit([IncomingMessage(external_id='gpt',contact='complex',sender='c',content='分析一下方案')]);assert gpt_started.wait(2)
        scheduler.submit([IncomingMessage(external_id='ds',contact='daily',sender='d',content='你好')]);assert daily_done.wait(2)
    finally:
        release.set();scheduler.close()
        for thread in scheduler.threads:thread.join(2)


def test_same_contact_text_after_sticker_runs_while_vision_waits(tmp_path):
    cfg=AppConfig(project_root=tmp_path);db=Database(tmp_path/'review.sqlite3')
    vision_started=threading.Event();text_done=threading.Event();release=threading.Event();received=[]
    class Pipeline:
        def send_approved(self,adapter):return 0
        def process(self,message,adapter):
            received.append(message)
            if message.message_type=='sticker':vision_started.set();release.wait(3)
            else:text_done.set()
            return SimpleNamespace(status='draft')
    scheduler=ReplyScheduler(cfg,db,Pipeline(),SimpleNamespace())
    try:
        scheduler.submit([IncomingMessage(external_id='sticker',contact='peer',sender='p',content='[表情]',message_type='sticker',media_paths=['frame.png'])]);assert vision_started.wait(2)
        scheduler.submit([IncomingMessage(external_id='text',contact='peer',sender='p',content='我看看有没有时间')]);assert text_done.wait(2)
        text=next(message for message in received if message.message_type=='text');assert text.external_id=='text' and not text.media_paths
    finally:
        release.set();scheduler.close()
        for thread in scheduler.threads:thread.join(2)

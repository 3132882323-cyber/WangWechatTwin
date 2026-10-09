import pytest
from app.config import AppConfig
from app.llm import ReplyLLM, LLMError
from app.models import RiskLevel


def test_web_never_constructs_codex_or_api(tmp_path,monkeypatch):
    monkeypatch.setattr('app.llm.openai_credentials',lambda: (_ for _ in ()).throw(AssertionError('credentials accessed')))
    config=AppConfig(project_root=tmp_path,openai={'provider':'web','web_reply_timeout_seconds':0})
    llm=ReplyLLM(config)
    with pytest.raises(LLMError,match='不会切回 Codex'):
        llm.decide('prompt','message',RiskLevel.low)
    with llm.account.connect() as db:
        assert db.execute('SELECT status,prompt FROM jobs').fetchone()==('expired','')


def test_late_browser_output_cannot_be_replayed(tmp_path):
    from app.web_llm import WebReplyLLM
    queue=WebReplyLLM(AppConfig(project_root=tmp_path))
    with queue.connect() as db:
        db.execute("INSERT INTO jobs(id,prompt,status,result,created,expires) VALUES('old','','claimed',NULL,0,1)")
    with pytest.raises(ValueError):
        queue.complete('old','{"action":"send","risk":"low","reply":"你好"}')


def test_professional_direct_completion_ignores_invalid_unused_candidates(tmp_path):
    import time
    from app.web_llm import WebReplyLLM
    queue = WebReplyLLM(AppConfig(project_root=tmp_path))
    with queue.connect() as connection:
        connection.execute("INSERT INTO jobs(id,prompt,status,created,expires,selection_mode) VALUES('direct-check','','claimed',?,?,'direct')", (time.time(),time.time()+60))
    queue.complete('direct-check','{"action":"review","risk":"low","reply":"专业直接答案","candidates":"not needed","selected_candidate":"Z"}')
    with queue.connect() as connection:
        raw = connection.execute("SELECT result FROM jobs WHERE id='direct-check'").fetchone()[0]
    assert '专业直接答案' in raw
    assert 'not needed' not in raw


def test_contact_namespace_is_stable_and_unknown_jobs_never_share_chat(tmp_path):
    import json
    c=AppConfig(project_root=tmp_path,openai={'provider':'web','web_reply_timeout_seconds':0})
    llm=ReplyLLM(c)
    for payload in [json.dumps({'conversation_key':'a'*64}), json.dumps({'conversation_key':'a'*64}), '{}', '{}']:
        with pytest.raises(LLMError):llm.decide('s',payload,RiskLevel.low)
    with llm.account.connect() as db:
        namespaces=[r[0] for r in db.execute('SELECT conversation_key FROM jobs ORDER BY created')]
    assert namespaces[:2]==['a'*64,'a'*64]
    assert namespaces[2].startswith('isolated:') and namespaces[2]!=namespaces[3]


def test_browser_completion_metadata_tracks_reuse_without_raw_urls(tmp_path):
    import time,json
    from app.web_llm import WebReplyLLM
    q=WebReplyLLM(AppConfig(project_root=tmp_path))
    with q.connect() as db:
        db.execute("INSERT INTO jobs(id,prompt,status,created,expires) VALUES('pool','','claimed',?,?)",(time.time(),time.time()+100))
    q.complete('pool','{"action":"review","risk":"low","reply":"第二轮"}',{'tab_id':1,'turn':2,'reused':True,'raw_url':'private-url'})
    with q.connect() as db:meta=json.loads(db.execute("SELECT browser_meta FROM jobs WHERE id='pool'").fetchone()[0])
    assert meta['reused'] is True and meta['turn']==2 and 'raw_url' not in meta


def test_pause_allows_only_explicit_virtual_queue_verification(tmp_path,monkeypatch):
    import json
    from app.web_llm import WebReplyLLM
    cfg=AppConfig(project_root=tmp_path,openai={'provider':'deepseek_web','web_reply_timeout_seconds':2})
    cfg.resolve(cfg.paths.pause_file).parent.mkdir(parents=True,exist_ok=True);cfg.resolve(cfg.paths.pause_file).touch()
    llm=WebReplyLLM(cfg)
    def finish(_):
        with llm.connect() as conn:
            row=conn.execute("SELECT id,is_test FROM jobs WHERE status='pending' ORDER BY created DESC LIMIT 1").fetchone()
        assert row[1]==1
        llm.complete(row[0],json.dumps({'action':'review','risk':'low','reply':'测试完成'}))
    monkeypatch.setattr('app.web_llm.time.sleep',finish)
    result=llm.decide('test',json.dumps({'__is_local_test':True,'incoming':{'content':'虚构'}}),RiskLevel.low)
    assert result.reply=='测试完成'
    with pytest.raises(LLMError,match='已暂停'):llm.decide('production',json.dumps({'incoming':{'content':'正常消息'}}),RiskLevel.low)


def test_failed_visual_job_retains_failure_status_and_clears_media(tmp_path,monkeypatch):
    import json
    from PIL import Image
    from app.web_llm import WebReplyLLM

    cfg=AppConfig(project_root=tmp_path,openai={'provider':'doubao_web','web_reply_timeout_seconds':2})
    image_dir=cfg.resolve(cfg.paths.history_reader)/'sticker_assets'
    image_dir.mkdir(parents=True)
    image_path=image_dir/'frame.png'
    Image.new('RGB',(2,2),'white').save(image_path)
    llm=WebReplyLLM(cfg)

    def fail_claimed_job(_):
        with llm.connect() as db:
            row=db.execute("SELECT id,images FROM jobs WHERE status='pending'").fetchone()
            assert row is not None and len(json.loads(row[1]))==1
            db.execute("UPDATE jobs SET status='failed' WHERE id=?",(row[0],))

    monkeypatch.setattr('app.web_llm.time.sleep',fail_claimed_job)
    payload=json.dumps({'__media_paths':[str(image_path)],'incoming':{'message_type':'sticker','content':'[表情包]'}})
    with pytest.raises(LLMError,match='网页回复失败'):
        llm.decide('visual',payload,RiskLevel.low)
    with llm.connect() as db:
        assert db.execute('SELECT status,prompt,images FROM jobs').fetchone()==('failed','','[]')

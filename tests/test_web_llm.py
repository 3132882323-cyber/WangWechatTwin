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

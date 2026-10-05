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

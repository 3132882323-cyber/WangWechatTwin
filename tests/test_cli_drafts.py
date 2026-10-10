import json
from pathlib import Path
import pytest
from app import cli
from app.config import AppConfig
from app.db import Database
from app.models import ReplyDecision, RiskLevel


def test_draft_import_cannot_send_even_with_auto_config(tmp_path, monkeypatch, capsys):
    root=Path(__file__).resolve().parents[1]
    cfg=AppConfig(project_root=tmp_path,mode='full_auto',adapter='wxauto',
                  contacts=[{'name':'虚构联系人','mode':'full_auto'}],
                  paths={'persona':str(root/'data/persona.md'),'business_rules':str(root/'data/business_rules.md'),
                         'reply_samples':str(root/'data/reply_samples.csv')})
    monkeypatch.setattr(cli,'load_config',lambda path:cfg)
    class LLM:
        def decide(self,*args):
            return ReplyDecision(action='send',risk=RiskLevel.low,reply='啥面',confidence=.99)
    real=cli.ReplyPipeline
    monkeypatch.setattr(cli,'ReplyPipeline',lambda config,db:real(config,db,llm=LLM()))
    monkeypatch.setattr(cli,'_adapter',lambda *a,**kw:pytest.fail('real adapter instantiated'))
    source=tmp_path/'messages.json'
    source.write_text(json.dumps({'contact':'虚构联系人','content':'准备吃面'},ensure_ascii=False),encoding='utf-8')
    assert cli.main(['--config','unused','draft','--input',str(source)])==0
    output=json.loads(capsys.readouterr().out)
    assert output['draft_only'] and output['results'][0]['wechat_sent'] is False
    assert cfg.mode=='full_auto' and cfg.contacts[0].mode=='full_auto'
    drafts=Database(cfg.resolve(cfg.paths.database)).list_drafts('pending')
    assert len(drafts)==1 and drafts[0].reply=='啥面'


@pytest.mark.parametrize('value',[[],[{'contact':'x','content':'y','media_paths':['private.png']}],{'contact':'x','content':2},[{'contact':'x','content':'y'}]*101])
def test_invalid_import_has_no_database_side_effect(tmp_path,monkeypatch,value):
    monkeypatch.setattr(cli,'load_config',lambda path:pytest.fail('config touched before input validated'))
    source=tmp_path/'bad.json';source.write_text(json.dumps(value),encoding='utf-8')
    assert cli.main(['draft','--input',str(source)])==2


def test_darwin_rejects_windows_adapter_before_import(tmp_path,monkeypatch):
    monkeypatch.setattr(cli.sys,'platform','darwin')
    with pytest.raises(cli.WxAutoUnavailable,match='尚未实现'):
        cli._adapter(AppConfig(project_root=tmp_path,adapter='history_http_sender'))


def test_web_doctor_checks_heartbeat_without_api_key(tmp_path,monkeypatch,capsys):
    import time
    cfg=AppConfig(project_root=tmp_path,adapter='mock',openai={'provider':'hybrid_web'})
    monkeypatch.setattr(cli,'load_config',lambda _:cfg)
    monkeypatch.setattr(cli,'openai_credentials',lambda:(None,None))
    db=Database(cfg.resolve(cfg.paths.database))
    for key in ['browser_bridge','deepseek_bridge','doubao_bridge']:
        db.set_state(key,{'heartbeat_seen_at':time.time(),'blocked':False})
    assert cli.main(['doctor'])==0
    report=json.loads(capsys.readouterr().out)
    assert report['inference_provider']=='hybrid_web' and report['model_inference_verified'] is False
    assert all(report['browser_bridges'].values())

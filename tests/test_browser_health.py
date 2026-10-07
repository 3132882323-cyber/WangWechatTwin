from app.config import AppConfig
from app.db import Database
from app.browser_health import failed,succeeded


def test_transient_failure_does_not_pause_everyone(tmp_path):
    c=AppConfig(project_root=tmp_path);db=Database(tmp_path/'app.sqlite3')
    assert failed(c,db,'load') is False
    assert not c.resolve(c.paths.pause_file).exists()
    assert failed(c,db,'reply') is False
    assert failed(c,db,'reply') is True
    assert c.resolve(c.paths.pause_file).exists()


def test_success_resets_consecutive_failures(tmp_path):
    c=AppConfig(project_root=tmp_path);db=Database(tmp_path/'app.sqlite3')
    failed(c,db,'load');failed(c,db,'load');succeeded(db)
    assert failed(c,db,'load') is False
    assert db.get_state('browser_failures')['consecutive']==1


def test_hybrid_gpt_failure_does_not_pause_daily_channels(tmp_path):
    from app.config import AppConfig
    from app.db import Database
    from app.browser_health import failed,succeeded
    cfg=AppConfig(project_root=tmp_path,openai={'provider':'hybrid_web'});db=Database(tmp_path/'db.sqlite3')
    for _ in range(3):failed(cfg,db,'reply')
    assert not cfg.resolve(cfg.paths.pause_file).exists()
    assert db.get_state('browser_failures')['lane']=='chatgpt'
    assert db.get_state('browser_failures')['blocked_until']>0
    succeeded(db);assert not db.get_state('browser_failures').get('blocked_until')

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

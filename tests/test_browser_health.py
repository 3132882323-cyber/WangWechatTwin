import os
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.config import AppConfig
from app.db import Database
from app.browser_health import failed, succeeded, auto_resume, pending_resume_seconds


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


def test_failure_pause_heals_by_itself_after_cooldown(tmp_path):
    c=AppConfig(project_root=tmp_path);db=Database(tmp_path/'heal.sqlite3')
    for _ in range(3):failed(c,db,'reply')
    assert c.resolve(c.paths.pause_file).exists()
    # Inside the cooldown: the pause holds.
    assert auto_resume(c,db,cooldown=300) is False
    assert c.resolve(c.paths.pause_file).exists()
    assert pending_resume_seconds(c,db,cooldown=300) > 0
    # Cooldown over: the backend resumes without the owner touching it.
    marker=db.get_state('auto_pause');marker['at']-=301
    db.set_state('auto_pause',marker)
    assert auto_resume(c,db,cooldown=300) is True
    assert not c.resolve(c.paths.pause_file).exists()
    assert db.get_state('browser_failures')['consecutive']==0
    assert db.get_state('auto_pause')=={}


def test_owner_pause_is_never_auto_resumed(tmp_path):
    c=AppConfig(project_root=tmp_path)
    target=c.resolve(c.paths.pause_file);target.parent.mkdir(parents=True,exist_ok=True);target.touch()
    db=Database(tmp_path/'manual.sqlite3')
    db.set_state('auto_pause',{})
    # No failure marker means the owner paused on purpose: never override it.
    assert auto_resume(c,db,cooldown=0) is False
    assert target.exists()
    assert pending_resume_seconds(c,db) is None


def test_success_during_pause_keeps_it_eligible_for_auto_resume(tmp_path):
    c=AppConfig(project_root=tmp_path);db=Database(tmp_path/'clear.sqlite3')
    for _ in range(3):failed(c,db,'reply')
    assert db.get_state('auto_pause').get('at')
    succeeded(db)
    assert db.get_state('browser_failures')['consecutive']==0
    assert pending_resume_seconds(c,db) is not None
    assert auto_resume(c,db,cooldown=0) is True
    assert not c.resolve(c.paths.pause_file).exists()


@pytest.mark.parametrize('pause_text', ['', 'paused', 'debugging'])
def test_failures_do_not_take_ownership_of_an_existing_pause(tmp_path, pause_text):
    config = AppConfig(project_root=tmp_path)
    db = Database(tmp_path / 'owner.sqlite3')
    pause = config.resolve(config.paths.pause_file)
    pause.parent.mkdir(parents=True, exist_ok=True)
    pause.write_text(pause_text, encoding='utf-8')
    before = pause.stat()
    # WorkBuddy's old marker cannot establish ownership of any current file.
    db.set_state('auto_pause', {'at': time.time() - 600, 'stage': 'reply'})
    for _ in range(4):
        failed(config, db, 'reply')
    assert pause.read_text(encoding='utf-8') == pause_text
    assert pause.stat().st_mtime_ns == before.st_mtime_ns
    assert pending_resume_seconds(config, db) is None
    assert auto_resume(config, db, cooldown=0) is False
    assert pause.exists()


def test_human_pause_overrides_an_older_automatic_pause(tmp_path):
    config = AppConfig(project_root=tmp_path)
    db = Database(tmp_path / 'owner.sqlite3')
    for _ in range(3):
        failed(config, db, 'reply')
    pause = config.resolve(config.paths.pause_file)
    # This is what the existing CLI and dashboard pause actions write.
    pause.write_text('paused', encoding='utf-8')
    assert pending_resume_seconds(config, db) is None
    assert auto_resume(config, db, cooldown=0) is False
    assert pause.read_text(encoding='utf-8') == 'paused'


def test_debug_touch_revokes_ownership_without_changing_pause_contents(tmp_path):
    config = AppConfig(project_root=tmp_path)
    db = Database(tmp_path / 'debug.sqlite3')
    for _ in range(3):
        failed(config, db, 'reply')
    pause = config.resolve(config.paths.pause_file)
    original = pause.read_bytes()
    stamp = pause.stat()
    os.utime(pause, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000_000))
    assert pause.read_bytes() == original
    assert pending_resume_seconds(config, db) is None
    assert auto_resume(config, db, cooldown=0) is False
    assert pause.exists()


def test_repeated_failure_does_not_postpone_existing_cooldown(tmp_path):
    config = AppConfig(project_root=tmp_path)
    db = Database(tmp_path / 'bounded.sqlite3')
    for _ in range(3):
        failed(config, db, 'reply')
    marker = db.get_state('auto_pause')
    marker['at'] -= 301
    db.set_state('auto_pause', marker)
    for _ in range(3):
        failed(config, db, 'reply')
    assert db.get_state('auto_pause') == marker
    assert auto_resume(config, db) is True
    # A subsequent single failure must not immediately trip another pause.
    assert failed(config, db, 'reply') is False
    assert not config.resolve(config.paths.pause_file).exists()


def test_replaced_pause_with_same_contents_and_timestamp_is_not_owned(tmp_path):
    config = AppConfig(project_root=tmp_path)
    db = Database(tmp_path / 'replacement.sqlite3')
    for _ in range(3):
        failed(config, db, 'reply')
    pause = config.resolve(config.paths.pause_file)
    stamp = pause.stat()
    replacement = pause.with_name('REPLACEMENT')
    replacement.write_bytes(pause.read_bytes())
    os.utime(replacement, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    replacement.replace(pause)
    assert auto_resume(config, db, cooldown=0) is False
    assert pending_resume_seconds(config, db) is None
    assert pause.exists()


def test_missing_pause_cleans_stale_marker_but_never_claims_a_future_pause(tmp_path):
    config = AppConfig(project_root=tmp_path)
    db = Database(tmp_path / 'missing.sqlite3')
    for _ in range(3):
        failed(config, db, 'reply')
    pause = config.resolve(config.paths.pause_file)
    pause.unlink()
    assert auto_resume(config, db, cooldown=0) is False
    assert db.get_state('auto_pause') == {}
    pause.touch()
    assert auto_resume(config, db, cooldown=0) is False
    assert pause.exists()


def test_auto_resume_remains_pending_if_pause_cannot_be_deleted(tmp_path, monkeypatch):
    config = AppConfig(project_root=tmp_path)
    db = Database(tmp_path / 'locked.sqlite3')
    for _ in range(3):
        failed(config, db, 'reply')
    pause = config.resolve(config.paths.pause_file)
    original_unlink = type(pause).unlink

    def denied(path, *args, **kwargs):
        if path == pause:
            raise PermissionError('synthetic locked pause')
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(type(pause), 'unlink', denied)
    assert auto_resume(config, db, cooldown=0) is False
    assert pause.exists()
    assert db.get_state('auto_pause').get('at')
    assert db.get_state('browser_failures')['consecutive'] == 3


def test_concurrent_workers_resume_only_once(tmp_path):
    config = AppConfig(project_root=tmp_path)
    db = Database(tmp_path / 'concurrent.sqlite3')
    for _ in range(3):
        failed(config, db, 'reply')
    with ThreadPoolExecutor(max_workers=3) as workers:
        results = list(workers.map(lambda _: auto_resume(config, db, cooldown=0), range(3)))
    assert results.count(True) == 1
    assert db.get_state('browser_failures')['consecutive'] == 0


def test_failed_marker_write_does_not_leave_an_unrecoverable_pause(tmp_path, monkeypatch):
    config = AppConfig(project_root=tmp_path)
    db = Database(tmp_path / 'failed-state.sqlite3')
    original_set = db.set_state

    def fail_marker(key, value):
        if key == 'auto_pause':
            raise RuntimeError('synthetic state write failure')
        return original_set(key, value)

    monkeypatch.setattr(db, 'set_state', fail_marker)
    for _ in range(2):
        failed(config, db, 'reply')
    with pytest.raises(RuntimeError, match='synthetic state write failure'):
        failed(config, db, 'reply')
    assert not config.resolve(config.paths.pause_file).exists()

"""Exercise automatic pause recovery through the idle production worker loop."""
import threading
from types import SimpleNamespace

from app import browser_health
from app.config import AppConfig
from app.db import Database
from app.reply_scheduler import ReplyScheduler


def failure_pause(tmp_path):
    config = AppConfig(project_root=tmp_path, openai={'provider': 'web'})
    db = Database(config.resolve(config.paths.database))
    for _ in range(3):
        browser_health.failed(config, db, 'reply')
    marker = db.get_state('auto_pause')
    marker['at'] -= 301
    db.set_state('auto_pause', marker)
    assert config.resolve(config.paths.pause_file).exists()
    return config, db


def close_scheduler(scheduler):
    scheduler.close()
    for thread in scheduler.threads:
        thread.join(5)
        assert not thread.is_alive()


class IdlePipeline:
    def __init__(self):
        self.polled = threading.Event()

    def send_approved(self, adapter):
        self.polled.set()
        return 0

    def process(self, message, adapter):
        raise AssertionError('This fixture has no messages or sends')


def test_idle_scheduler_resumes_owned_failure_pause_and_resets_failure_count(tmp_path, monkeypatch):
    config, db = failure_pause(tmp_path)
    recovery = threading.Condition()
    completed = False
    original_resume = browser_health.auto_resume

    def observe_resume(*args, **kwargs):
        nonlocal completed
        result = original_resume(*args, **kwargs)
        if result:
            with recovery:
                completed = True
                recovery.notify_all()
        return result

    monkeypatch.setattr(browser_health, 'auto_resume', observe_resume)
    pipeline = IdlePipeline()
    scheduler = ReplyScheduler(config, db, pipeline, SimpleNamespace())
    try:
        # Another lane can poll after unlink but before ownership and the event are persisted.
        with recovery:
            assert recovery.wait_for(lambda: completed, timeout=5), 'automatic recovery never completed'
        assert pipeline.polled.wait(5), 'idle workers never resumed after the cooldown'
        assert not config.resolve(config.paths.pause_file).exists()
        assert db.get_state('auto_pause') == {}
        assert db.get_state('browser_failures')['consecutive'] == 0
        assert scheduler.health()['last_error'] == {}
        with db.connect() as connection:
            assert connection.execute("SELECT count(*) FROM events WHERE event_type='auto_resume'").fetchone()[0] == 1
    finally:
        close_scheduler(scheduler)


def test_idle_scheduler_preserves_human_override_with_stale_automatic_marker(tmp_path, monkeypatch):
    config, db = failure_pause(tmp_path)
    pause = config.resolve(config.paths.pause_file)
    pause.write_text('paused', encoding='utf-8')
    checked = threading.Event()
    original_resume = browser_health.auto_resume

    def observe_resume(*args, **kwargs):
        result = original_resume(*args, **kwargs)
        checked.set()
        return result

    monkeypatch.setattr(browser_health, 'auto_resume', observe_resume)
    pipeline = IdlePipeline()
    scheduler = ReplyScheduler(config, db, pipeline, SimpleNamespace())
    try:
        assert checked.wait(5), 'worker did not check automatic recovery'
        assert not pipeline.polled.is_set()
        assert pause.read_text(encoding='utf-8') == 'paused'
        assert scheduler.health()['last_error'] == {}
    finally:
        close_scheduler(scheduler)

import json
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.config import AppConfig
from app.db import Database
from app.models import IncomingMessage
from app.reply_scheduler import ReplyScheduler


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    assert predicate()


def stop(scheduler):
    scheduler.close()
    for thread in scheduler.threads:
        thread.join(3)
        assert not thread.is_alive()


def incoming(identity, **updates):
    return IncomingMessage(external_id=identity, contact=identity, sender=identity,
                           content='hello').model_copy(update=updates)


def paused_config(tmp_path):
    config = AppConfig(project_root=tmp_path)
    pause = config.resolve(config.paths.pause_file)
    pause.parent.mkdir(parents=True, exist_ok=True)
    pause.touch()
    return config


def queue_table(db):
    with db.connect() as connection:
        connection.execute('''CREATE TABLE IF NOT EXISTS reply_jobs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,contact TEXT,payload TEXT,
            status TEXT,created REAL,updated REAL)''')


def insert(db, message, status='pending'):
    payload = message if isinstance(message, str) else message.model_dump_json()
    with db.connect() as connection:
        return connection.execute('INSERT INTO reply_jobs(contact,payload,status,created,updated) VALUES(?,?,?,?,?)',
                                  ('synthetic', payload, status, 1, 1)).lastrowid


def status(db, identity):
    with db.connect() as connection:
        return connection.execute('SELECT status FROM reply_jobs WHERE id=?', (identity,)).fetchone()[0]


class FaultDatabase(Database):
    cleanup_oom = False
    telemetry_oom = False
    preflight_oom = False

    @contextmanager
    def connect(self):
        with super().connect() as connection:
            owner = self

            class Proxy:
                def execute(self, sql, params=()):
                    if owner.cleanup_oom and "UPDATE reply_jobs SET status='failed'" in sql:
                        raise MemoryError('synthetic cleanup failure')
                    return connection.execute(sql, params)

                def __getattr__(self, name):
                    return getattr(connection, name)

            yield Proxy()

    def add_event(self, *args, **kwargs):
        if self.telemetry_oom:
            raise MemoryError('synthetic telemetry failure')
        return super().add_event(*args, **kwargs)

    def get_state(self, key, default=None):
        if self.preflight_oom and key == 'browser_failures':
            raise MemoryError('synthetic preflight failure')
        return super().get_state(key, default)


def test_secondary_cleanup_and_logging_oom_are_contained_and_uncertain_work_is_review_only(tmp_path):
    config = AppConfig(project_root=tmp_path)
    db = FaultDatabase(tmp_path / 'queue.sqlite3')
    db.cleanup_oom = db.telemetry_oom = True
    attempts, recovered = [], threading.Event()

    class Pipeline:
        def send_approved(self, adapter):
            return 0

        def process(self, message, adapter):
            attempts.append(message)
            raise RuntimeError('synthetic ambiguous outcome')

    scheduler = ReplyScheduler(config, db, Pipeline(), SimpleNamespace())
    try:
        scheduler.submit([incoming('ambiguous')])
        wait_for(lambda: not scheduler.thread.is_alive())
        assert len(attempts) == 1
        assert status(db, 1) == 'working'
        health = scheduler.health()
        assert health['status'] == 'degraded'
        assert health['last_error']['deepseek']['error'] == 'RuntimeError'
        assert health['last_error']['deepseek']['cleanup_error'] == 'MemoryError'
        assert 'deepseek' in db.get_state('reply_scheduler')['dead_lanes']
    finally:
        stop(scheduler)

    db.cleanup_oom = db.telemetry_oom = False

    class RecoveredPipeline:
        def send_approved(self, adapter):
            return 0

        def process(self, message, adapter):
            assert json.loads(message.raw_summary)['resume_incomplete'] is True
            recovered.set()
            return SimpleNamespace(status='draft')

    scheduler = ReplyScheduler(config, db, RecoveredPipeline(), SimpleNamespace())
    try:
        assert recovered.wait(2)
        wait_for(lambda: status(db, 1) == 'done')
        assert len(attempts) == 1
    finally:
        stop(scheduler)


def test_optional_telemetry_failure_does_not_undo_completion_or_kill_worker(tmp_path):
    db = FaultDatabase(tmp_path / 'queue.sqlite3')
    db.telemetry_oom = True
    seen = []

    class Pipeline:
        def send_approved(self, adapter):
            return 0

        def process(self, message, adapter):
            seen.append(message.external_id)
            return SimpleNamespace(status='sent')

    scheduler = ReplyScheduler(AppConfig(project_root=tmp_path), db, Pipeline(), SimpleNamespace())
    try:
        scheduler.submit([incoming('first')])
        wait_for(lambda: status(db, 1) == 'done')
        scheduler.submit([incoming('second')])
        wait_for(lambda: status(db, 2) == 'done')
        assert seen == ['first', 'second']
        assert scheduler.thread.is_alive()
        with db.connect() as connection:
            assert connection.execute("SELECT count(*) FROM reply_jobs WHERE payload!=''").fetchone()[0] == 0
    finally:
        stop(scheduler)


def test_memory_failure_in_preflight_is_reported_for_process_supervision(tmp_path):
    db = FaultDatabase(tmp_path / 'queue.sqlite3')
    db.preflight_oom = True
    scheduler = ReplyScheduler(AppConfig(project_root=tmp_path), db,
                               SimpleNamespace(send_approved=lambda _: 0), SimpleNamespace())
    try:
        wait_for(lambda: not scheduler.threads[1].is_alive())
        health = scheduler.health()
        assert 'chatgpt' in health['dead_lanes']
        assert health['last_error']['chatgpt']['error'] == 'MemoryError'
    finally:
        stop(scheduler)


def test_oversized_persisted_jobs_are_quarantined_without_parsing_and_preserved(tmp_path, monkeypatch):
    db = Database(tmp_path / 'queue.sqlite3')
    queue_table(db)
    oversized = '{' + 'x' * ReplyScheduler.MAX_PAYLOAD_CHARS
    first = insert(db, oversized, 'working')
    second = insert(db, oversized, 'pending')
    original = IncomingMessage.model_validate_json

    def bounded_parse(cls, payload):
        assert len(payload) <= ReplyScheduler.MAX_PAYLOAD_CHARS, 'oversized payload reached Python parsing'
        return original(payload)

    monkeypatch.setattr(IncomingMessage, 'model_validate_json', classmethod(bounded_parse))
    scheduler = ReplyScheduler(paused_config(tmp_path), db, SimpleNamespace(), SimpleNamespace())
    try:
        assert status(db, first) == status(db, second) == 'quarantined'
        with db.connect() as connection:
            assert [row[0] for row in connection.execute('SELECT payload FROM reply_jobs ORDER BY id')] == [oversized, oversized]
        drafts = db.list_drafts()
        assert len(drafts) == 2
        assert all(draft.action == 'review' and draft.reply == '' for draft in drafts)
    finally:
        stop(scheduler)
    scheduler = ReplyScheduler(paused_config(tmp_path), db, SimpleNamespace(), SimpleNamespace())
    try:
        assert len(db.list_drafts()) == 2, 'restart must not duplicate quarantine notices'
    finally:
        stop(scheduler)


@pytest.mark.parametrize('updates', [
    {'content': 'x' * (ReplyScheduler.MAX_PAYLOAD_CHARS + 1)},
    {'raw_summary': json.dumps({'merge_truncated': True, 'member_external_ids': ['synthetic-leaf']})},
])
def test_new_oversized_or_truncated_input_stays_out_of_model_queue(tmp_path, updates):
    db = Database(tmp_path / 'queue.sqlite3')
    scheduler = ReplyScheduler(paused_config(tmp_path), db, SimpleNamespace(), SimpleNamespace())
    try:
        scheduler.submit([incoming('unsafe', **updates)])
        assert status(db, 1) == 'quarantined'
        assert scheduler._claim('deepseek') is None
        drafts = db.list_drafts()
        assert len(drafts) == 1 and drafts[0].action == 'review' and not drafts[0].reply
    finally:
        stop(scheduler)


def test_paged_backlog_keeps_live_priority_across_multiple_pages(tmp_path, monkeypatch):
    db = Database(tmp_path / 'queue.sqlite3')
    scheduler = ReplyScheduler(paused_config(tmp_path), db, SimpleNamespace(), SimpleNamespace())
    try:
        for index in range(ReplyScheduler.FETCH_BATCH_SIZE * 3):
            insert(db, incoming(str(index), raw_summary=json.dumps({'resume_incomplete': True})))
        live = insert(db, incoming('live'))
        original_connect = db.connect
        sizes = []

        @contextmanager
        def bounded_connection():
            with original_connect() as connection:
                class Cursor:
                    def __init__(self, wrapped):
                        self.wrapped = wrapped

                    def fetchmany(self, size):
                        sizes.append(size)
                        assert size <= ReplyScheduler.FETCH_BATCH_SIZE
                        return self.wrapped.fetchmany(size)

                    def fetchall(self):
                        raise AssertionError('unbounded queue fetch')

                    def __getattr__(self, name):
                        return getattr(self.wrapped, name)

                class Connection:
                    def execute(self, sql, params=()):
                        if 'SELECT id,payload FROM reply_jobs' in sql:
                            assert 'LIMIT ?' in sql and 'length(payload)<=?' in sql
                        return Cursor(connection.execute(sql, params))

                yield Connection()

        monkeypatch.setattr(db, 'connect', bounded_connection)
        row = scheduler._claim('deepseek')
        assert row[0] == live
        assert len(sizes) > 3
        assert max(sizes) == ReplyScheduler.FETCH_BATCH_SIZE
    finally:
        stop(scheduler)


def test_resource_pressure_stops_generation_and_approved_send_polling(tmp_path):
    db = Database(tmp_path / 'queue.sqlite3')
    db.set_state('resource_pressure', {'active': True})
    calls = []
    finished = threading.Event()

    class Pipeline:
        def send_approved(self, adapter):
            calls.append('approved')
            return 0

        def process(self, message, adapter):
            calls.append('process')
            finished.set()
            return SimpleNamespace(status='draft')

    scheduler = ReplyScheduler(AppConfig(project_root=tmp_path), db, Pipeline(), SimpleNamespace())
    try:
        scheduler.submit([incoming('held')])
        time.sleep(.6)
        assert calls == [] and status(db, 1) == 'pending'
        db.set_state('resource_pressure', {'active': False})
        assert finished.wait(3)
    finally:
        stop(scheduler)


def test_database_cleanup_does_not_replace_original_exception(tmp_path, monkeypatch):
    calls = []

    class Connection:
        def execute(self, *args):
            return None

        def rollback(self):
            calls.append('rollback')
            raise MemoryError('synthetic rollback failure')

        def close(self):
            calls.append('close')
            raise RuntimeError('synthetic close failure')

    monkeypatch.setattr('app.db.sqlite3.connect', lambda *args, **kwargs: Connection())
    db = object.__new__(Database)
    db.path = tmp_path / 'unused.sqlite3'
    original = ValueError('original operation failure')
    with pytest.raises(ValueError) as caught:
        with db.connect():
            raise original
    assert caught.value is original
    assert calls == ['rollback', 'close']


def test_quarantine_rolls_back_if_review_notice_cannot_be_saved(tmp_path):
    db = Database(tmp_path / 'queue.sqlite3')
    queue_table(db)
    identity = insert(db, incoming('preserved'))
    with pytest.raises(MemoryError):
        with db.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')

            class FaultingConnection:
                def execute(self, sql, params=()):
                    if 'INSERT INTO drafts' in sql:
                        raise MemoryError('synthetic draft insert failure')
                    return connection.execute(sql, params)

            db.quarantine_reply_job(FaultingConnection(), identity, 'synthetic review')
            connection.execute('COMMIT')
    assert status(db, identity) == 'pending'
    assert db.list_drafts() == []

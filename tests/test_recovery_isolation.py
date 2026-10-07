from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json
import threading
import time

import pytest

from app.adapters.mock import MockAdapter
from app.config import AppConfig
from app.db import Database
from app.models import IncomingMessage, ReplyDecision, RiskLevel
from app.pipeline import ReplyPipeline
from app.reply_scheduler import ReplyScheduler


def stop_scheduler(scheduler):
    scheduler.close()
    for worker in scheduler.threads:
        worker.join(3)
        assert not worker.is_alive()


def incoming(identity, content, *, recovery=False, second=0):
    return IncomingMessage(
        external_id=identity, contact="peer", sender="peer", content=content,
        received_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=second),
        raw_summary=json.dumps({"resume_incomplete": True} if recovery else {}),
    )


def queue_insert(db, message, status="pending"):
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO reply_jobs(contact,payload,status,created,updated) VALUES(?,?,?,?,?)",
            (message.contact, message.model_dump_json(), status, time.time() - 60, time.time() - 60),
        )


@pytest.mark.parametrize("status", ["pending", "working"])
def test_live_burst_never_merges_with_recovery(status, tmp_path):
    config = AppConfig(project_root=tmp_path)
    pause = config.resolve(config.paths.pause_file)
    pause.parent.mkdir(parents=True, exist_ok=True)
    pause.touch()
    db = Database(tmp_path / "state.sqlite3")
    scheduler = ReplyScheduler(config, db, SimpleNamespace(), SimpleNamespace())
    old = incoming("old-task", "旧任务正文", recovery=True)
    try:
        queue_insert(db, old, status)
        scheduler.submit([incoming("fresh-one", "第一条新消息", second=1)])
        scheduler.submit([incoming("fresh-two", "第二条新消息", second=2)])
        with db.connect() as connection:
            rows = connection.execute("SELECT status,payload FROM reply_jobs ORDER BY id").fetchall()
        assert len(rows) == 2
        assert rows[0][0] == status
        assert IncomingMessage.model_validate_json(rows[0][1]) == old
        fresh = IncomingMessage.model_validate_json(rows[1][1])
        assert rows[1][0] == "pending"
        assert fresh.content == "第一条新消息\n第二条新消息"
        origin = json.loads(fresh.raw_summary)
        assert not origin.get("resume_incomplete")
        assert set(origin["member_external_ids"]) == {"fresh-one", "fresh-two"}
    finally:
        stop_scheduler(scheduler)


def make_pipeline(config, db, llm):
    pipeline = ReplyPipeline(config, db, llm)
    pipeline.prompt_builder = SimpleNamespace(
        system_prompt=lambda: "synthetic test prompt",
        user_payload=lambda message, *args: json.dumps({
            "incoming": {"content": message.content, "message_type": message.message_type},
        }),
    )
    return pipeline


@pytest.mark.parametrize("interrupted_status", ["working", "failed"])
def test_restored_gpt_draft_does_not_block_same_contact_live_reply(tmp_path, interrupted_status):
    config = AppConfig(
        project_root=tmp_path, mode="low_risk_auto",
        wechat={"use_greeting_cache": False}, contacts=[{"name": "peer"}],
    )
    db = Database(tmp_path / "state.sqlite3")
    with db.connect() as connection:
        connection.execute("CREATE TABLE reply_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, contact TEXT, payload TEXT, status TEXT, created REAL, updated REAL)")
    old = incoming("interrupted", "分析一下旧方案")
    queue_insert(db, old, interrupted_status)
    entered, release, live_done, old_done = (threading.Event() for _ in range(4))
    seen, results = {}, {}

    class LLM:
        def decide(self, system, payload, risk):
            content = json.loads(payload)["incoming"]["content"]
            if content == old.content:
                entered.set()
                assert release.wait(5)
                reply = "恢复任务的草稿"
            else:
                assert content == "你现在有空吗"
                reply = "你先说，怎么了？"
            return ReplyDecision(action="send", risk=RiskLevel.low, reply=reply, confidence=.99)

    pipeline = make_pipeline(config, db, LLM())
    process = pipeline.process

    def tracked(message, adapter):
        seen[message.external_id] = message
        result = process(message, adapter)
        results[message.external_id] = result.status
        (old_done if message.external_id == old.external_id else live_done).set()
        return result

    pipeline.process = tracked
    adapter = MockAdapter()
    scheduler = ReplyScheduler(config, db, pipeline, adapter)
    try:
        assert entered.wait(2)
        scheduler.submit([incoming("fresh", "你现在有空吗", second=1)])
        assert live_done.wait(2), "restoring GPT must not reserve the live text lane"
        assert not old_done.is_set()
        assert results["fresh"] == "sent"
        assert seen["fresh"].content == "你现在有空吗"
        assert not json.loads(seen["fresh"].raw_summary).get("resume_incomplete")
        assert adapter.sent == [("peer", "你先说，怎么了？")]
        release.set()
        assert old_done.wait(2)
        assert results["interrupted"] == "draft"
        assert json.loads(seen["interrupted"].raw_summary)["resume_incomplete"] is True
        assert len(adapter.sent) == 1
        assert process(seen["interrupted"], adapter).status == "duplicate"
        assert len(db.list_drafts()) == 1
        with db.connect() as connection:
            outcomes = dict(connection.execute("SELECT external_id,status FROM reply_outcomes"))
        assert outcomes == {"interrupted": "draft", "fresh": "sent"}
    finally:
        release.set()
        stop_scheduler(scheduler)


def test_ready_live_job_precedes_queued_recovery_on_same_lane(tmp_path):
    config = AppConfig(project_root=tmp_path)
    pause = config.resolve(config.paths.pause_file)
    pause.parent.mkdir(parents=True, exist_ok=True)
    pause.touch()
    db = Database(tmp_path / "state.sqlite3")
    processed, finished = [], threading.Event()

    class Pipeline:
        def send_approved(self, adapter):
            return 0

        def process(self, message, adapter):
            processed.append(message.external_id)
            if len(processed) == 2:
                finished.set()
            return SimpleNamespace(status="draft")

    scheduler = ReplyScheduler(config, db, Pipeline(), SimpleNamespace())
    try:
        queue_insert(db, incoming("old", "旧问题", recovery=True))
        queue_insert(db, incoming("live", "新问题", second=1))
        pause.unlink()
        scheduler.wake.set()
        assert finished.wait(2)
        assert processed == ["live", "old"]
    finally:
        stop_scheduler(scheduler)


def test_cached_greeting_completes_outcome_without_empty_payload_route(tmp_path, monkeypatch):
    config = AppConfig(
        project_root=tmp_path, mode="low_risk_auto",
        wechat={"use_greeting_cache": True}, contacts=[{"name": "peer"}],
    )
    db = Database(tmp_path / "state.sqlite3")
    monkeypatch.setattr("app.greeting_cache.lookup", lambda *args: "咋了，有事你说")

    class NoModel:
        def decide(self, *args):
            raise AssertionError("cached greeting must not call a model")

    pipeline = make_pipeline(config, db, NoModel())
    adapter = MockAdapter()
    message = incoming("cached", "你好")
    assert pipeline.process(message, adapter).status == "sent"
    assert pipeline.process(message, adapter).status == "duplicate"
    assert adapter.sent == [("peer", "咋了，有事你说")]
    with db.connect() as connection:
        assert connection.execute("SELECT status FROM reply_outcomes WHERE external_id='cached'").fetchone()[0] == "sent"

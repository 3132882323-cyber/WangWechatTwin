from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import sqlite3
import pytest
from pydantic import ValidationError
from app.config import AppConfig, ProactiveSettings
from app.db import Database
from app.proactive import ProactivePlanner


def setup_planner(tmp_path):
    cfg=AppConfig(project_root=tmp_path)
    cfg.proactive.enabled=True
    path=cfg.resolve(cfg.paths.chat_memory)
    path.parent.mkdir(parents=True,exist_ok=True)
    now=datetime(2026,10,6,6,tzinfo=timezone.utc)
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE history(contact TEXT,direction TEXT,content TEXT,created_at INTEGER)')
        for contact in ['friend','unanswered','group@chatroom','owner']:
            conn.execute('INSERT INTO history VALUES(?,?,?,?)',(contact,'in','最近准备去旅游了',int((now-timedelta(days=2,hours=1)).timestamp())))
            conn.execute('INSERT INTO history VALUES(?,?,?,?)',(contact,'in' if contact=='unanswered' else 'out','好呀',int((now-timedelta(days=2)).timestamp())))
    db=Database(tmp_path/'state.sqlite3')
    adapter=SimpleNamespace(reader=SimpleNamespace(existing_conversations={'friend','unanswered','group@chatroom','owner'},self_username='owner'))
    return ProactivePlanner(cfg,db,adapter),now


def test_draft_only_contact_isolation_and_no_repeat(tmp_path):
    planner,now=setup_planner(tmp_path)
    assert len(planner.tick(force=True,now=now))==1
    drafts=planner.db.list_drafts(status='pending')
    assert drafts[0].contact=='friend' and drafts[0].kind=='proactive'
    assert '旅游' in drafts[0].reply
    assert not planner.db.approved_drafts()
    assert planner.tick(force=True,now=now)==[]
    planner.db.update_draft(drafts[0].id,'dismissed')
    assert planner.tick(force=True,now=now+timedelta(days=1))==[]


def test_quiet_hours_pause_and_review_required(tmp_path):
    planner,now=setup_planner(tmp_path)
    assert planner.tick(force=True,now=now.replace(hour=14))==[]
    planner.config.resolve(planner.config.paths.pause_file).write_text('paused')
    assert planner.tick(force=True,now=now)==[]
    with pytest.raises(ValidationError):
        ProactiveSettings(review_only=False)

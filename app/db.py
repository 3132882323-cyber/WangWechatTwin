from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from app.models import DraftRecord, IncomingMessage, ReplyDecision


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=20, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    external_id TEXT UNIQUE,
                    contact TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    direction TEXT NOT NULL CHECK(direction IN ('in','out')),
                    content TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    risk TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS consumed_external_ids (
                    external_id TEXT PRIMARY KEY,
                    merged_into_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_contact_time
                    ON messages(contact, created_at DESC);

                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contact TEXT NOT NULL,
                    fact TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    UNIQUE(contact, fact)
                );

                CREATE TABLE IF NOT EXISTS drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contact TEXT NOT NULL,
                    inbound_message_id INTEGER,
                    reply TEXT NOT NULL,
                    edited_reply TEXT,
                    holding_reply TEXT,
                    action TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(inbound_message_id) REFERENCES messages(id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS idx_drafts_status
                    ON drafts(status, created_at DESC);

                CREATE TABLE IF NOT EXISTS style_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    draft_id INTEGER NOT NULL UNIQUE,
                    contact TEXT NOT NULL,
                    incoming TEXT NOT NULL,
                    draft_reply TEXT NOT NULL,
                    final_reply TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(draft_id) REFERENCES drafts(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_style_feedback_contact
                    ON style_feedback(contact, created_at DESC);

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    contact TEXT,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(drafts)")}
            for name, declaration in (("kind", "TEXT NOT NULL DEFAULT 'reply'"),
                                      ("sticker_id", "TEXT NOT NULL DEFAULT ''"),
                                      ('media_description',"TEXT NOT NULL DEFAULT ''"),
                                      ('media_confidence','REAL NOT NULL DEFAULT 0'),
                                      ("source_context_ts", "INTEGER")):
                if name not in columns:
                    conn.execute(f"ALTER TABLE drafts ADD COLUMN {name} {declaration}")
            message_columns={row[1] for row in conn.execute('PRAGMA table_info(messages)')}
            for name,declaration in [('media_paths',"TEXT NOT NULL DEFAULT '[]'"),('original_type',"TEXT NOT NULL DEFAULT ''")]:
                if name not in message_columns:conn.execute(f'ALTER TABLE messages ADD COLUMN {name} {declaration}')

    def seen(self, external_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM messages WHERE external_id=? UNION ALL SELECT 1 FROM consumed_external_ids WHERE external_id=? LIMIT 1", (external_id,external_id)
            ).fetchone()
            return row is not None

    def add_incoming(self, message: IncomingMessage, risk: str | None = None) -> int:
        try:origin=json.loads(message.raw_summary or '{}')
        except ValueError:origin={}
        if not isinstance(origin,dict):origin={}
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO messages
                (external_id, contact, sender, direction, content, message_type, risk, created_at,media_paths,original_type)
                VALUES (?, ?, ?, 'in', ?, ?, ?, ?,?,?)
                """,
                (
                    message.external_id,
                    message.contact,
                    message.sender,
                    message.content,
                    message.message_type,
                    risk,
                    message.received_at.isoformat(),
                    json.dumps(message.media_paths[:3]),
                    str(origin.get('original_type',''))[:20],
                ),
            )
            if cur.rowcount:
                incoming_id=int(cur.lastrowid)
            else:
                row=conn.execute("SELECT id FROM messages WHERE external_id=?",(message.external_id,)).fetchone()
                incoming_id=int(row['id'])
            try:
                origin=json.loads(message.raw_summary or '{}')
                members=origin.get('member_external_ids',[]) if isinstance(origin,dict) else []
                if not isinstance(members,list):members=[]
                members=[key for key in members[:100] if isinstance(key,str) and 0<len(key)<=512]
            except ValueError:members=[]
            conn.executemany('INSERT OR IGNORE INTO consumed_external_ids(external_id,merged_into_id,created_at) VALUES(?,?,?)',
                             [(key,incoming_id,message.received_at.isoformat()) for key in members])
            conn.execute('COMMIT')
            return incoming_id

    def add_outgoing(self, contact: str, content: str, risk: str | None = None) -> int:
        external_id = f"out:{contact}:{datetime.now(timezone.utc).timestamp()}"
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO messages
                (external_id, contact, sender, direction, content, message_type, risk, created_at)
                VALUES (?, ?, ?, 'out', ?, 'text', ?, ?)
                """,
                (external_id, contact, "owner", content, risk, utc_now()),
            )
            return int(cur.lastrowid)

    def recent_messages(self, contact: str, limit: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT direction, sender, content, message_type, risk, created_at
                FROM messages WHERE contact=? ORDER BY id DESC LIMIT ?
                """,
                (contact, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def add_memories(self, contact: str, facts: list[str], max_items: int) -> None:
        clean = []
        for fact in facts:
            value = " ".join(fact.strip().split())
            if value and len(value) <= 300:
                clean.append(value)
        if not clean:
            return
        with self.connect() as conn:
            for fact in clean:
                conn.execute(
                    "INSERT OR IGNORE INTO memories(contact, fact, created_at) VALUES (?, ?, ?)",
                    (contact, fact, utc_now()),
                )
            rows = conn.execute(
                "SELECT id FROM memories WHERE contact=? AND active=1 ORDER BY id DESC",
                (contact,),
            ).fetchall()
            for row in rows[max_items:]:
                conn.execute("UPDATE memories SET active=0 WHERE id=?", (row["id"],))

    def memories(self, contact: str, limit: int) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT fact FROM memories
                WHERE contact=? AND active=1 ORDER BY id DESC LIMIT ?
                """,
                (contact, limit),
            ).fetchall()
        return [str(row["fact"]) for row in reversed(rows)]

    def create_draft(
        self,
        contact: str,
        inbound_message_id: int | None,
        decision: ReplyDecision,
        *, kind: str = "reply", source_context_ts: int | None = None,
    ) -> int:
        now = utc_now()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO drafts
                (contact, inbound_message_id, reply, holding_reply, action, risk, reason,
                 confidence, status, created_at, updated_at, kind, sticker_id, source_context_ts,media_description,media_confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?,?,?)
                """,
                (
                    contact,
                    inbound_message_id,
                    decision.reply,
                    decision.holding_reply,
                    decision.action,
                    decision.risk.value,
                    decision.reason,
                    decision.confidence,
                    now,
                    now,
                    kind,
                    decision.sticker_id,
                    source_context_ts,
                    decision.media_description,
                    decision.media_confidence,
                ),
            )
            return int(cur.lastrowid)

    def list_drafts(self, status: str | None = "pending", limit: int = 100) -> list[DraftRecord]:
        query = (
            "SELECT d.*, m.content AS incoming_content, m.media_paths AS incoming_media_paths,COALESCE(m.original_type,'') AS original_type "
            "FROM drafts d LEFT JOIN messages m ON m.id=d.inbound_message_id"
        )
        params: list[Any] = []
        if status:
            query += " WHERE d.status=?"
            params.append(status)
        query += " ORDER BY d.id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        output=[]
        for row in rows:
            record=dict(row)
            try:paths=json.loads(record['incoming_media_paths'] or '[]')
            except (ValueError,TypeError):paths=[]
            record['incoming_media_paths']=[path for path in paths[:3] if isinstance(path,str)] if isinstance(paths,list) else []
            output.append(DraftRecord.model_validate(record))
        return output

    def update_draft(
        self,
        draft_id: int,
        status: str,
        edited_reply: str | None = None,
    ) -> bool:
        now = utc_now()
        with self.connect() as conn:
            if edited_reply is None:
                cur = conn.execute(
                    "UPDATE drafts SET status=?, updated_at=? WHERE id=?",
                    (status, now, draft_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE drafts SET status=?, edited_reply=?, updated_at=? WHERE id=?",
                    (status, edited_reply, now, draft_id),
                )
            if cur.rowcount == 1 and (status == "approved" or (status == 'pending' and edited_reply is not None)):
                row = conn.execute(
                    """
                    SELECT d.contact, d.reply, d.risk,
                           COALESCE(d.edited_reply, d.reply) AS final_reply,
                           COALESCE(m.content, '') AS incoming
                    FROM drafts d
                    LEFT JOIN messages m ON m.id=d.inbound_message_id
                    WHERE d.id=?
                    """,
                    (draft_id,),
                ).fetchone()
                if (
                    row
                    and row["risk"] != "critical"
                    and str(row["final_reply"]).strip()
                    and (status == 'approved' or str(row['final_reply']).strip() != str(row['reply']).strip())
                ):
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO style_feedback
                        (draft_id, contact, incoming, draft_reply, final_reply, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            draft_id,
                            row["contact"],
                            row["incoming"],
                            row["reply"],
                            row["final_reply"],
                            now,
                        ),
                    )
            return cur.rowcount == 1

    def style_feedback(self, contact: str, limit: int = 20) -> list[dict[str, str]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT incoming, final_reply, draft_reply FROM style_feedback
                WHERE contact=? ORDER BY CASE WHEN final_reply!=draft_reply THEN 0 ELSE 1 END, id DESC LIMIT ?
                """,
                (contact, limit),
            ).fetchall()
        return [
            {"scenario": "本人亲自修改的回复" if row['final_reply']!=row['draft_reply'] else '本人批准的模型草稿，不等同本人原话', "incoming": str(row["incoming"]),
             "preferred_reply": str(row["final_reply"])}
            for row in reversed(rows)
        ]

    def approved_drafts(self, limit: int = 20) -> list[DraftRecord]:
        return self.list_drafts(status="approved", limit=limit)

    def add_event(
        self,
        event_type: str,
        detail: str,
        level: str = "info",
        contact: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO events(level, event_type, contact, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                (level, event_type, contact, detail[:2000], utc_now()),
            )

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def auto_sends_last_hour(self) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE direction='out' AND created_at>=?",
                (cutoff,),
            ).fetchone()
        return int(row["n"])

    def set_state(self, key: str, value: Any) -> None:
        raw = json.dumps(value, ensure_ascii=False)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO state(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, raw, utc_now()),
            )

    def get_state(self, key: str, default: Any = None) -> Any:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def cleanup(self, retain_days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retain_days)).isoformat()
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM style_feedback WHERE created_at<?",
                (cutoff,),
            )
            conn.execute(
                "DELETE FROM drafts WHERE created_at<? AND status IN ('sent','dismissed','failed')",
                (cutoff,),
            )
            cur = conn.execute(
                """
                DELETE FROM messages
                WHERE created_at<?
                  AND id NOT IN (
                    SELECT inbound_message_id FROM drafts WHERE inbound_message_id IS NOT NULL
                  )
                """,
                (cutoff,),
            )
            return int(cur.rowcount)

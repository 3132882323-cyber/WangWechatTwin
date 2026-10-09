"""Short lived, bounded visible sticker observations for a following text turn.

This table contains only what was visibly described from an inbound sticker. It
does not retain a proposed reply, candidate selection, or persona inference.
The source message's receipt time, rather than completion of the visual model,
defines whether an observation is still relevant.
"""

from __future__ import annotations

import json
import hashlib
import math
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from app.models import IncomingMessage, ReplyDecision, RiskLevel
from app.risk import assess_risk


_MAX_DESCRIPTION = 4000
_MAX_PER_CONTACT = 2
_MAX_GLOBAL = 1024
_OWNER_SENDERS = ("本人", "夏鑫鑫", "owner", "self")
_RISK_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _origin(message: IncomingMessage) -> dict:
    try:
        value = json.loads(message.raw_summary or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _risk_value(value: object) -> str:
    return value.value if isinstance(value, RiskLevel) else str(value).lower()


def _metadata(value: object, max_length: int) -> str:
    return str(value).strip()[:max_length] if value is not None else ""


def _schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS sticker_observations (
            external_id TEXT PRIMARY KEY,
            contact TEXT NOT NULL,
            chat_type TEXT NOT NULL,
            sender_key TEXT NOT NULL,
            received_at TEXT NOT NULL,
            media_description TEXT NOT NULL,
            media_confidence REAL NOT NULL,
            risk TEXT NOT NULL,
            asset_digest TEXT NOT NULL,
            model_provenance TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sticker_observations_contact_time
            ON sticker_observations(contact, chat_type, sender_key, received_at DESC);
        """
    )


def record(
    db_path: str | Path,
    message: IncomingMessage,
    decision: ReplyDecision,
    *,
    model_provenance: str | None = None,
) -> bool:
    """Persist a completed, confident inbound sticker observation only.

    Returns False without changing the database if the description is unsafe or
    incomplete. This function does not mark a message seen, queue work, or send.
    """
    origin = _origin(message)
    full_description = decision.media_description.strip()
    description = full_description[:_MAX_DESCRIPTION]
    confidence = decision.media_confidence
    if (
        message.message_type != "sticker"
        or not message.external_id
        or not message.contact
        or origin.get("owner_message")
        or message.sender in _OWNER_SENDERS
        or (message.chat_type == "group" and not message.sender_key)
        or not description
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not 0.75 <= confidence <= 1
    ):
        return False

    decision_risk = _risk_value(decision.risk)
    if decision_risk not in _RISK_RANK:
        return False
    # Assess before truncating: a high-risk phrase near the tail must still
    # prevent a seemingly harmless truncated observation from being stored.
    visual_risk = assess_risk(full_description).level.value
    source_risk = assess_risk(message.content).level.value
    risk = max((decision_risk, visual_risk, source_risk), key=_RISK_RANK.__getitem__)
    if risk in {"high", "critical"}:
        return False

    source_at = _utc(message.received_at).isoformat()
    digest = _metadata(origin.get("sticker_md5"), 128)
    if not re.fullmatch(r"[a-fA-F0-9]{32}", digest):
        digest = ""
    provenance = _metadata(
        model_provenance or origin.get("model_provenance")
        or origin.get("vision_provider") or origin.get("media_model")
        or "unspecified", 80,
    )
    provenance = re.sub(r"[^a-zA-Z0-9._:/-]", "", provenance) or "unspecified"
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=20, isolation_level=None)) as db:
        _schema(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            inserted = db.execute(
                """INSERT INTO sticker_observations
                (external_id,contact,chat_type,sender_key,received_at,
                 media_description,media_confidence,risk,asset_digest,model_provenance)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(external_id) DO UPDATE SET
                    contact=excluded.contact, chat_type=excluded.chat_type,
                    sender_key=excluded.sender_key, received_at=excluded.received_at,
                    media_description=excluded.media_description,
                    media_confidence=excluded.media_confidence, risk=excluded.risk,
                    asset_digest=excluded.asset_digest,
                    model_provenance=excluded.model_provenance
                WHERE sticker_observations.contact=excluded.contact
                  AND sticker_observations.chat_type=excluded.chat_type
                  AND sticker_observations.sender_key=excluded.sender_key""",
                (
                    message.external_id, message.contact, message.chat_type,
                    message.sender_key, source_at, description, confidence, risk,
                    digest, provenance,
                ),
            )
            if inserted.rowcount:
                if message.chat_type == "group":
                    db.execute(
                        """DELETE FROM sticker_observations
                        WHERE contact=? AND chat_type='group' AND sender_key=?
                          AND external_id NOT IN (
                            SELECT external_id FROM sticker_observations
                            WHERE contact=? AND chat_type='group' AND sender_key=?
                            ORDER BY received_at DESC, external_id DESC LIMIT ?)""",
                        (message.contact, message.sender_key, message.contact,
                         message.sender_key, _MAX_PER_CONTACT),
                    )
                else:
                    db.execute(
                        """DELETE FROM sticker_observations
                        WHERE contact=? AND chat_type=? AND external_id NOT IN (
                            SELECT external_id FROM sticker_observations
                            WHERE contact=? AND chat_type=?
                            ORDER BY received_at DESC, external_id DESC LIMIT ?)""",
                        (message.contact, message.chat_type, message.contact,
                         message.chat_type, _MAX_PER_CONTACT),
                    )
                db.execute(
                    """DELETE FROM sticker_observations WHERE external_id NOT IN (
                        SELECT external_id FROM sticker_observations
                        ORDER BY received_at DESC, external_id DESC LIMIT ?)""",
                    (_MAX_GLOBAL,),
                )
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    return bool(inserted.rowcount)


def recent(
    db_path: str | Path,
    message: IncomingMessage,
    max_age_seconds: int | float = 120,
    *,
    personal_db_path: str | Path | None = None,
) -> list[dict]:
    """Return at most two safe sticker observations for this inbound text.

    Outgoing rows in the ordinary messages table, including AI sends, and
    owner-authored rows cut off any earlier sticker. Group matching requires an
    exact, nonempty sender_key so a different member's image cannot leak in.
    Equal receipt times are ambiguous and are excluded conservatively.
    """
    origin = _origin(message)
    if (
        message.message_type != "text"
        or not message.external_id
        or not message.contact
        or origin.get("owner_message")
        or origin.get("original_type") not in (None, "", "text")
        or message.sender in _OWNER_SENDERS
        or (message.chat_type == "group" and not message.sender_key)
        or not isinstance(max_age_seconds, (int, float))
        or not math.isfinite(max_age_seconds)
        or max_age_seconds <= 0
    ):
        return []
    text_at = _utc(message.received_at)
    max_age_seconds = min(max_age_seconds, 120)
    member_ids = origin.get("member_external_ids", [])
    if not isinstance(member_ids, list):
        member_ids = []
    path = Path(db_path)
    if not path.is_file():
        return []
    try:
        connection = sqlite3.connect(path, timeout=0.5)
    except (OSError, sqlite3.Error):
        return []
    with closing(connection) as db:
        db.row_factory = sqlite3.Row
        try:
            has_table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sticker_observations'"
            ).fetchone()
        except sqlite3.Error:
            return []
        if not has_table:
            return []
        where = "contact=? AND chat_type=?"
        params: list[object] = [message.contact, message.chat_type]
        if message.chat_type == "group":
            where += " AND sender_key=?"
            params.append(message.sender_key)
        try:
            candidates = db.execute(
                """SELECT external_id,contact,chat_type,sender_key,received_at,
                          media_description,media_confidence,risk,asset_digest,
                          model_provenance FROM sticker_observations WHERE """
                + where + " ORDER BY received_at DESC LIMIT ?",
                (*params, _MAX_PER_CONTACT),
            ).fetchall()
        except sqlite3.Error:
            return []
        eligible = []
        for row in candidates:
            if row["external_id"] == message.external_id or row["external_id"] in member_ids:
                continue
            if message.chat_type == "group" and row["sender_key"] != message.sender_key:
                continue
            if row["risk"] not in {"low", "medium"}:
                continue
            confidence = row["media_confidence"]
            if (not isinstance(confidence, (int, float))
                    or not math.isfinite(confidence) or not 0.75 <= confidence <= 1
                    or not isinstance(row["media_description"], str)
                    or not row["media_description"]
                    or len(row["media_description"]) > _MAX_DESCRIPTION):
                continue
            try:
                sticker_at = _utc(datetime.fromisoformat(row["received_at"]))
            except (TypeError, ValueError):
                continue
            age = (text_at - sticker_at).total_seconds()
            if not 0 < age <= max_age_seconds:
                continue
            eligible.append((sticker_at, row))

        if not eligible:
            return []
        oldest = min(sticker_at for sticker_at, _ in eligible)
        # julianday accepts the ISO timestamps used by messages and legacy
        # timestamps with Z or offsets. Compare exact times again in Python.
        try:
            barriers = db.execute(
                """SELECT direction,sender,created_at FROM messages
                   WHERE contact=? AND
                         (direction='out' OR sender IN (?,?,?,?)) AND
                         julianday(created_at)>=julianday(?) AND
                         julianday(created_at)<=julianday(?)""",
                (message.contact, *_OWNER_SENDERS, oldest.isoformat(), text_at.isoformat()),
            ).fetchall()
        except sqlite3.Error:
            return []
        cutoff = None
        for barrier in barriers:
            try:
                sent_at = _utc(datetime.fromisoformat(barrier["created_at"]))
            except (TypeError, ValueError):
                # A malformed owner-send time leaves chronology unknown.
                return []
            if oldest <= sent_at <= text_at:
                cutoff = max(cutoff, sent_at) if cutoff else sent_at

    personal_cutoff_second = None
    if personal_db_path is not None:
        personal_path = Path(personal_db_path)
        if not personal_path.is_file():
            return []
        contact_key = hashlib.sha256(message.contact.encode("utf-8")).hexdigest()
        try:
            uri = personal_path.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=0.5)) as personal:
                row = personal.execute(
                    """SELECT MAX(at) FROM observations
                       WHERE contact_key=? AND direction='out'
                         AND at BETWEEN ? AND ?""",
                    (contact_key, int(oldest.timestamp()), int(text_at.timestamp())),
                ).fetchone()
            if row and row[0] is not None:
                personal_cutoff_second = int(row[0])
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return []

    return [
        {
            "external_id": row["external_id"],
            "contact": row["contact"],
            "sender_key": row["sender_key"],
            "received_at": row["received_at"],
            "media_description": row["media_description"],
            "media_confidence": row["media_confidence"],
            "risk": row["risk"],
            "asset_digest": row["asset_digest"],
            "model_provenance": row["model_provenance"],
        }
        for sticker_at, row in sorted(eligible, key=lambda item: item[0])
        if (cutoff is None or sticker_at > cutoff)
        and (personal_cutoff_second is None
             or int(sticker_at.timestamp()) > personal_cutoff_second)
    ][-_MAX_PER_CONTACT:]

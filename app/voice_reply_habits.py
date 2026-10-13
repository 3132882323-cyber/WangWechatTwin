"""Read confirmed local medium habits without reading message bodies or audio.

Ownership/source mapping from the already connected history reader and a separate
owner confirmation are prerequisites. This helper does not decrypt the original
databases or independently authenticate each observation. An outgoing record,
media observation or ASR result alone never proves a manual send.

Confirmation covers only its fixed past window. The caller must list all known
generated events not already in ``ai_receipts`` in ``excluded_events``. The
result describes observed text/voice samples, not the speaker's acoustic identity
or a complete census of all messages. Missing or inconsistent evidence returns
an empty mapping; no database, confirmation or send state is created here.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from typing import Sequence

from app.voice_reply_policy import OwnerHumanVoiceStats


_HASH = re.compile(r"[0-9a-f]{64}")
_MESSAGE_SOURCE = re.compile(r"([A-Za-z0-9_-]+)/message/(message_\d+\.db)")
_PROVENANCE = {"wechat_original", "wechat_media_observation", "automatic_transcription"}
_OBS_COLUMNS = {"contact_key", "direction", "kind", "at", "provenance", "source", "local_id", "metadata"}
_MAX_ROWS = 100_000
_WINDOW_SECONDS = 30 * 86400


def _number(value) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        return False


def _read_small(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError("oversized local evidence")
    return value


def _source(value) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.replace("\\", "/")
    return name if _MESSAGE_SOURCE.fullmatch(name) else None


def _owned_sources(keys: dict, owner: str) -> set[str] | None:
    sources, prefixes = set(), set()
    for key, info in keys.items():
        if not isinstance(key, str):
            return None
        normalized = key.replace("\\", "/")
        if not re.search(r"/message/message_\d+\.db$", normalized):
            continue
        match = _MESSAGE_SOURCE.fullmatch(normalized)
        if (not match or not isinstance(info, dict) or normalized in sources
                or match[1].rsplit("_", 1)[0] != owner):
            return None
        physical = info.get("source")
        if not isinstance(physical, str) or not Path(physical).is_absolute():
            return None
        path = Path(physical).resolve(strict=True)
        if (not path.is_file() or path.name != match[2] or path.parent.name != "message"
                or path.parent.parent.name != "db_storage" or path.parent.parent.parent.name != match[1]):
            return None
        sources.add(normalized)
        prefixes.add(match[1])
    return sources if sources and len(prefixes) == 1 else None


def _confirmation_scope(confirmation: dict | None, owner: str, sources: set[str], now: float):
    if not isinstance(confirmation, dict):
        return None
    if (confirmation.get("source") != "owner_confirmation"
            or confirmation.get("confirmed_manual") is not True
            or confirmation.get("owner_sha256") != hashlib.sha256(owner.encode("utf-8")).hexdigest()
            or type(confirmation.get("window_days")) is not int or confirmation["window_days"] != 30):
        return None
    at, until = confirmation.get("confirmed_at_unix"), confirmation.get("confirmed_until_unix")
    if (not _number(at) or not _number(until) or at > now
            or until > min(now + 5, at + 5)):
        return None
    excluded = confirmation.get("excluded_events")
    if not isinstance(excluded, list) or len(excluded) > 10_000:
        return None
    events = {}
    for item in excluded:
        if not isinstance(item, dict):
            return None
        source, local_id, contact = _source(item.get("source")), item.get("local_id"), item.get("contact_hash")
        if (source not in sources or type(local_id) is not int or local_id <= 0
                or not isinstance(contact, str) or not _HASH.fullmatch(contact)):
            return None
        event = (source, local_id)
        if event in events and events[event] != contact:
            return None
        events[event] = contact
    return min(now, at, until), events


def _has_schema(connection: sqlite3.Connection) -> bool:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"observations", "ai_receipts"} <= tables:
        return False
    observed = {row[1] for row in connection.execute('PRAGMA table_info("observations")')}
    receipts = {row[1] for row in connection.execute('PRAGMA table_info("ai_receipts")')}
    return _OBS_COLUMNS <= observed and {"source", "local_id"} <= receipts


def _medium(kind: str, provenance: str, metadata: dict) -> str | None:
    original = metadata.get("original_type")
    if original == "voice" and kind in {"voice", "text"}:
        return "voice"
    if original not in (None, "", "text"):
        return None
    if kind == "voice" and provenance in {"wechat_original", "wechat_media_observation"}:
        return "voice" if original in (None, "") else None
    if kind == "text" and provenance == "wechat_original":
        return "text"
    return None


def load_owner_voice_habits(
    personal_database: Path,
    history_reader_root: Path,
    contacts: Sequence[str],
    *,
    confirmation: dict | None = None,
    now_unix: float | None = None,
) -> dict[str, OwnerHumanVoiceStats]:
    """Load advice-only counts, gated by a separately supplied human confirmation.

    Required confirmation fields: ``source='owner_confirmation'``,
    ``confirmed_manual=True``, ``owner_sha256`` (the stripped expected owner ID),
    ``window_days=30``, ``confirmed_at_unix``, ``confirmed_until_unix`` and
    ``excluded_events=[{source, local_id, contact_hash}, ...]``. Never fabricate
    this confirmation from the records being counted. Later events cannot extend
    it. Counts combine direct native voice observations and ASR's original voice
    metadata, with one count per source/contact/local ID (native Msg tables are
    per conversation). AI receipts and explicitly excluded source/local IDs
    remain conservatively excluded across every contact.
    """
    if confirmation is None:
        return {}
    now = time.time() if now_unix is None else now_unix
    if not _number(now) or not isinstance(contacts, (list, tuple, set, frozenset)) or len(contacts) > 500:
        return {}
    if any(not isinstance(contact, str) or not contact or len(contact) > 512 for contact in contacts):
        return {}
    contact_names = {hashlib.sha256(contact.encode("utf-8")).hexdigest(): contact for contact in contacts
                     if not contact.endswith("@chatroom")}
    if not contact_names:
        return {}
    try:
        root, database = Path(history_reader_root), Path(personal_database).resolve()
        if not database.is_file():
            return {}
        owner_bytes = _read_small(root / "expected_owner.private.txt", 256)
        owner = owner_bytes.decode("utf-8").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", owner):
            return {}
        keys_bytes = _read_small(root / "keys.json", 2 * 1024 * 1024)
        keys = json.loads(keys_bytes)
        if not isinstance(keys, dict):
            return {}
        sources = _owned_sources(keys, owner)
        if not sources:
            return {}
        scope = _confirmation_scope(confirmation, owner, sources, now)
        if scope is None:
            return {}
        cutoff, excluded = scope
        lower = now - _WINDOW_SECONDS
        if cutoff < lower:
            return {}
        samples = {}
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            if not _has_schema(connection):
                return {}
            ai_events = set()
            for source, local_id in connection.execute("SELECT source,local_id FROM ai_receipts"):
                source = _source(source)
                if source is None or type(local_id) is not int or local_id <= 0 or len(ai_events) >= _MAX_ROWS:
                    return {}
                ai_events.add((source, local_id))
            placeholders = ",".join("?" for _ in contact_names)
            rows = connection.execute(
                "SELECT contact_key,kind,at,provenance,source,local_id,metadata FROM observations "
                f"WHERE contact_key IN ({placeholders}) AND direction='out' AND at BETWEEN ? AND ? "
                "AND provenance IN ('wechat_original','wechat_media_observation','automatic_transcription') LIMIT ?",
                (*contact_names, lower, cutoff, _MAX_ROWS + 1),
            )
            for index, (contact, kind, at, provenance, source, local_id, metadata) in enumerate(rows):
                if index >= _MAX_ROWS:
                    return {}
                if kind not in {"voice", "text"} or provenance not in _PROVENANCE:
                    continue
                source = _source(source)
                if (source not in sources or type(local_id) is not int or local_id <= 0
                        or not _number(at) or not lower <= at <= cutoff):
                    return {}
                event = (source, local_id)
                if event in ai_events:
                    continue
                if event in excluded:
                    # Native local IDs can recur in different conversation
                    # tables. Excluding all contacts for a known generated ID
                    # may undercount, but cannot teach that generated habit.
                    continue
                if not isinstance(metadata, str) or len(metadata) > 16_384:
                    return {}
                meta = json.loads(metadata)
                if not isinstance(meta, dict):
                    return {}
                medium = _medium(kind, provenance, meta)
                if medium is None:
                    continue
                value = (contact, medium, at)
                observation = (source, contact, local_id)
                if observation in samples:
                    previous = samples[observation]
                    if previous[:2] != value[:2]:
                        return {}  # Conflicting media for one message is unknown.
                    # A later observation/transcription must not rejuvenate an
                    # old manual send. Count the event once, at its earlier time.
                    value = (contact, medium, min(previous[2], at))
                samples[observation] = value
        # A replaced identity/map cannot lend ownership to the completed read.
        if (_read_small(root / "expected_owner.private.txt", 256) != owner_bytes
                or _read_small(root / "keys.json", 2 * 1024 * 1024) != keys_bytes):
            return {}
        counts = {}
        for contact, medium, at in samples.values():
            entry = counts.setdefault(contact, {"sample_count": 0, "voice_count": 0, "last_voice": None})
            entry["sample_count"] += 1
            if medium == "voice":
                entry["voice_count"] += 1
                entry["last_voice"] = max(entry["last_voice"] or at, at)
        return {
            contact_names[key]: OwnerHumanVoiceStats(
                sample_count=value["sample_count"], voice_count=value["voice_count"],
                source="owner_confirmed_wechat_history", same_contact=True, verified_human_only=True,
                recent_window_days=30, last_voice_age_days=(now - value["last_voice"]) / 86400,
            ) for key, value in counts.items() if value["voice_count"]
        }
    except (OSError, sqlite3.Error, ValueError, TypeError, UnicodeError):
        return {}

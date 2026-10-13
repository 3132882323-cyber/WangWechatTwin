import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

import app.voice_reply_habits as habits
from app.voice_reply_policy import suggest_reply_medium


NOW = 1_800_000_000
SOURCE = "wxid_owner_abcd/message/message_0.db"


def contact_hash(contact):
    return hashlib.sha256(contact.encode()).hexdigest()


@pytest.fixture
def corpus(tmp_path):
    reader = tmp_path / "reader"
    reader.mkdir()
    native = tmp_path / "wxid_owner_abcd" / "db_storage" / "message" / "message_0.db"
    native.parent.mkdir(parents=True)
    native.write_bytes(b"synthetic original database placeholder; never decrypted")
    (reader / "expected_owner.private.txt").write_text("wxid_owner", encoding="utf-8")
    (reader / "keys.json").write_text(json.dumps({SOURCE: {"source": str(native)}}), encoding="utf-8")
    database = tmp_path / "personal.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executescript(
            "CREATE TABLE observations(id TEXT PRIMARY KEY,contact_key TEXT,direction TEXT,"
            "content TEXT,kind TEXT,at INTEGER,provenance TEXT,source TEXT,local_id INTEGER,metadata TEXT);"
            "CREATE TABLE ai_receipts(source TEXT,local_id INTEGER,contact_key TEXT,body_hash TEXT);"
        )
    confirmation = {
        "source": "owner_confirmation", "confirmed_manual": True,
        "owner_sha256": hashlib.sha256(b"wxid_owner").hexdigest(), "window_days": 30,
        "confirmed_at_unix": NOW, "confirmed_until_unix": NOW,
        "excluded_events": [{"source": SOURCE, "local_id": 99, "contact_hash": contact_hash("alice")}],
    }

    def add(local_id, *, contact="alice", kind="text", provenance="wechat_original", metadata=None,
            at=None, source=SOURCE, direction="out", identity=None):
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?)", (
                identity or f"{source}:{contact}:{local_id}", contact_hash(contact), direction,
                "private body not selected by the helper", kind, NOW - local_id * 86400 if at is None else at,
                provenance, source, local_id, json.dumps(metadata or {}),
            ))

    add(1, kind="voice")
    add(2, kind="voice", provenance="wechat_media_observation")
    add(3, kind="text", provenance="automatic_transcription", metadata={"original_type": "voice"})
    add(4)
    add(5)
    return database, reader, native, confirmation, add


def load(corpus, *, confirmation=None, contacts=("alice",), now=NOW):
    database, reader, _, original, _ = corpus
    return habits.load_owner_voice_habits(database, reader, contacts,
                                         confirmation=original if confirmation is None else confirmation, now_unix=now)


def test_counts_only_owner_confirmed_observed_medium_and_integrates_with_policy(corpus):
    stats = load(corpus)["alice"]
    assert stats.sample_count == 5 and stats.voice_count == 3
    assert stats.last_voice_age_days == 1
    assert stats.source == "owner_confirmed_wechat_history"
    assert stats.same_contact and stats.verified_human_only
    advice = suggest_reply_medium(incoming_text="有点冷，多穿点衣服。", incoming_type="voice",
                                  reply_text="好，我会注意保暖，你也别着凉。", local_hour=14,
                                  familiar_contact=True, owner_voice_stats=stats)
    assert advice.medium == "voice_review" and advice.requires_approval


def test_missing_confirmation_returns_no_evidence_and_does_not_create_files(tmp_path):
    assert habits.load_owner_voice_habits(tmp_path / "missing.db", tmp_path / "missing-reader", ["alice"]) == {}
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("changes", [
    {"source": "automatic_inference"}, {"confirmed_manual": False}, {"confirmed_manual": "true"},
    {"owner_sha256": "0" * 64}, {"window_days": 29}, {"window_days": True},
    {"confirmed_at_unix": NOW + 1}, {"confirmed_until_unix": NOW + 6},
    {"confirmed_at_unix": 0}, {"confirmed_until_unix": None}, {"confirmed_until_unix": float("nan")},
    {"confirmed_at_unix": 10**1000}, {"excluded_events": None},
    {"excluded_events": [{"source": "other_owner_abcd/message/message_0.db", "local_id": 99, "contact_hash": contact_hash("alice")}]},
    {"excluded_events": [{"source": SOURCE, "local_id": True, "contact_hash": contact_hash("alice")}]},
    {"excluded_events": [{"source": SOURCE, "local_id": 99, "contact_hash": "bad"}]},
])
def test_unverified_malformed_future_or_different_owner_confirmation_fails_closed(corpus, changes):
    confirmation = dict(corpus[3])
    confirmation.update(changes)
    assert load(corpus, confirmation=confirmation) == {}


@pytest.mark.parametrize("missing", ["owner", "keys", "native", "personal", "table", "column"])
def test_missing_identity_source_database_table_or_schema_returns_no_stats(corpus, missing):
    database, reader, native, _, _ = corpus
    if missing == "owner":
        (reader / "expected_owner.private.txt").unlink()
    elif missing == "keys":
        (reader / "keys.json").unlink()
    elif missing == "native":
        native.unlink()
    elif missing == "personal":
        database.unlink()
    else:
        with closing(sqlite3.connect(database)) as connection, connection:
            if missing == "table":
                connection.execute("DROP TABLE ai_receipts")
            else:
                connection.execute("ALTER TABLE observations RENAME COLUMN metadata TO missing_metadata")
    assert load(corpus) == {}
    if missing == "personal":
        assert not database.exists()


@pytest.mark.parametrize("change", ["wrong_owner", "wrong_physical", "foreign_manifest", "duplicate_source_alias"])
def test_keys_require_one_expected_owner_and_real_corresponding_physical_source(corpus, change):
    _, reader, native, _, _ = corpus
    mapping = {SOURCE: {"source": str(native)}}
    if change == "wrong_owner":
        (reader / "expected_owner.private.txt").write_text("wxid_someone_else", encoding="utf-8")
    elif change == "wrong_physical":
        mapping[SOURCE]["source"] = str(reader / "keys.json")
    elif change == "foreign_manifest":
        mapping["wxid_other_abcd/message/message_0.db"] = {"source": str(native)}
    else:
        mapping[SOURCE.replace("/", "\\")] = {"source": str(native)}
    (reader / "keys.json").write_text(json.dumps(mapping), encoding="utf-8")
    assert load(corpus) == {}


def test_any_ai_receipt_excludes_same_event_even_when_receipt_contact_is_different(corpus):
    database, _, _, _, add = corpus
    add(6, kind="voice", at=NOW - 20)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("INSERT INTO ai_receipts VALUES(?,?,?,?)", (SOURCE, 6, contact_hash("another-contact"), "unused"))
    assert load(corpus)["alice"].sample_count == 5
    assert load(corpus)["alice"].voice_count == 3


def test_known_generated_event_without_receipt_is_excluded_by_confirmation(corpus):
    corpus[4](99, kind="voice", provenance="wechat_media_observation", at=NOW - 20)
    assert load(corpus)["alice"].voice_count == 3
    confirmation = dict(corpus[3])
    confirmation["excluded_events"] = [{"source": SOURCE, "local_id": 99, "contact_hash": contact_hash("wrong-contact")}]
    # An excluded ID can recur in other native conversation tables. Removing it
    # globally is conservative; another contact must not invalidate all counts.
    assert load(corpus, confirmation=confirmation)["alice"].voice_count == 3


def test_unknown_generated_historical_incoming_and_other_media_do_not_supply_habit(corpus):
    add = corpus[4]
    for local_id, provenance in enumerate(("ai_generated", "unknown", "historical_unverified_origin"), 10):
        add(local_id, kind="voice", provenance=provenance, at=NOW - 20)
    add(20, kind="voice", direction="in", at=NOW - 20)
    add(21, kind="image", provenance="wechat_media_observation", at=NOW - 20)
    add(22, kind="text", provenance="automatic_transcription", at=NOW - 20)
    add(23, kind="text", provenance="wechat_media_observation", metadata={"original_type": "image"}, at=NOW - 20)
    stats = load(corpus)["alice"]
    assert (stats.sample_count, stats.voice_count) == (5, 3)


def test_duplicate_native_voice_and_its_transcription_count_as_one_event(corpus):
    corpus[4](1, identity="transcription-copy", kind="text", provenance="automatic_transcription",
              metadata={"original_type": "voice"})
    assert load(corpus)["alice"].sample_count == 5


def test_same_local_id_in_different_native_contact_tables_is_a_distinct_event(corpus):
    corpus[4](1, contact="bob", kind="voice", identity="conflicting-contact-copy")
    result = load(corpus, contacts=("alice", "bob"))
    assert result["alice"].sample_count == 5 and result["alice"].voice_count == 3
    assert result["bob"].sample_count == 1 and result["bob"].voice_count == 1


def test_same_contact_same_native_event_cannot_have_conflicting_media(corpus):
    corpus[4](1, kind="text", identity="conflicting-media-copy")
    assert load(corpus) == {}


def test_later_transcription_does_not_rejuvenate_the_same_manual_voice_event(corpus):
    corpus[4](1, identity="later-transcription-copy", kind="text", provenance="automatic_transcription",
              metadata={"original_type": "voice"}, at=NOW - 20)
    stats = load(corpus)["alice"]
    assert stats.sample_count == 5 and stats.voice_count == 3
    assert stats.last_voice_age_days == 1


def test_known_ai_exclusion_remains_global_when_another_contact_reuses_the_id(corpus):
    corpus[4](99, kind="voice", provenance="wechat_media_observation", at=NOW - 20)
    corpus[4](99, contact="bob", kind="voice", provenance="wechat_media_observation", at=NOW - 20)
    result = load(corpus, contacts=("alice", "bob"))
    assert set(result) == {"alice"} and result["alice"].voice_count == 3


def test_each_contact_has_separate_counts_and_groups_do_not_get_habits(corpus):
    add = corpus[4]
    add(10, contact="bob", kind="voice", at=NOW - 40)
    add(11, contact="bob", at=NOW - 80)
    add(12, contact="room@chatroom", kind="voice", at=NOW - 40)
    result = load(corpus, contacts=("alice", "bob", "room@chatroom"))
    assert set(result) == {"alice", "bob"}
    assert result["alice"].sample_count == 5
    assert result["bob"].sample_count == 2 and result["bob"].voice_count == 1


def test_confirmation_never_covers_future_or_later_new_generated_events(corpus):
    confirmation = dict(corpus[3])
    confirmation.update(confirmed_at_unix=NOW - 100, confirmed_until_unix=NOW - 100)
    corpus[4](30, kind="voice", at=NOW - 50)
    corpus[4](31, kind="voice", at=NOW + 1)
    assert load(corpus, confirmation=confirmation)["alice"].voice_count == 3
    confirmation.update(confirmed_at_unix=NOW, confirmed_until_unix=NOW + 5)
    assert load(corpus, confirmation=confirmation)["alice"].voice_count == 4


def test_observations_outside_rolling_thirty_days_are_not_recent_samples(corpus):
    corpus[4](35, kind="voice")
    assert load(corpus)["alice"].voice_count == 3
    assert load(corpus, now=NOW + 31 * 86400) == {}


@pytest.mark.parametrize("source", ["", "wxid_other_abcd/message/message_0.db", "wxid_owner_abcd/message/message_9.db"])
def test_unmapped_relevant_source_fails_closed(corpus, source):
    corpus[4](40, kind="voice", source=source, at=NOW - 20)
    assert load(corpus) == {}


def test_malformed_metadata_does_not_produce_verified_human_counts(corpus):
    with closing(sqlite3.connect(corpus[0])) as connection, connection:
        connection.execute("UPDATE observations SET metadata='not-json' WHERE local_id=1")
    assert load(corpus) == {}


def test_helper_does_not_read_bodies_or_write_local_evidence(corpus, monkeypatch):
    database, reader, native, _, _ = corpus
    before = {path: path.read_bytes() for path in (database, reader / "keys.json", reader / "expected_owner.private.txt", native)}
    real_connect = sqlite3.connect
    reads = []
    mutations = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                 sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_ALTER_TABLE}

    def readonly(*args, **kwargs):
        assert "mode=ro" in args[0] and kwargs["uri"] is True
        connection = real_connect(*args, **kwargs)

        def authorize(action, table, column, *_):
            if action == sqlite3.SQLITE_READ:
                reads.append((table, column))
                if column in {"content", "speaker", "body_hash"}:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_DENY if action in mutations else sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    monkeypatch.setattr(habits.sqlite3, "connect", readonly)
    assert load(corpus)["alice"].voice_count == 3
    assert not any(column in {"content", "speaker", "body_hash"} for _, column in reads)
    assert all(path.read_bytes() == value for path, value in before.items())

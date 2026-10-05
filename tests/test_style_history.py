import sqlite3

from app.style_history import examples


def test_examples_are_contact_isolated(tmp_path):
    path = tmp_path / "samples.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE samples(contact TEXT,incoming TEXT,reply TEXT,created_at INTEGER)")
    conn.executemany("INSERT INTO samples VALUES(?,?,?,?)", [
        ("customer-a", "在吗", "在。", 1), ("customer-b", "在吗", "other private conversation", 2)])
    conn.commit(); conn.close()
    result = examples(path, "customer-a", "在吗")
    assert len(result) == 1 and result[0]["preferred_reply"] == "在。"
    assert examples(path, "unknown", "在吗") == []

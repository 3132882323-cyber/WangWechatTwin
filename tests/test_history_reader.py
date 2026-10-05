import json
import shutil
import sqlite3
from pathlib import Path
from time import time

import pytest

from app.adapters.history_reader import HistoryReadOnlyAdapter, authenticated_snapshot
from app.config import AppConfig


def test_live_reader_baselines_filters_and_deduplicates(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    connection = sqlite3.connect(source)
    table = "Msg_" + __import__("hashlib").md5(b"friend").hexdigest()
    connection.execute("CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY,is_session INTEGER)")
    connection.executemany("INSERT INTO Name2Id VALUES(?,0)", [("wxid_owner",), ("friend",)])
    connection.execute(f'CREATE TABLE "{table}"(local_id INTEGER,server_id INTEGER,local_type INTEGER,'
                       'real_sender_id INTEGER,create_time INTEGER,message_content TEXT,WCDB_CT_message_content INTEGER)')
    connection.execute(f'INSERT INTO "{table}" VALUES(1,101,1,2,?,"old",0)', (int(time()),))
    connection.commit()
    runtime = tmp_path / ".runtime/history_reader"
    runtime.mkdir(parents=True)
    (runtime / "keys.json").write_text(json.dumps({
        "wxid_owner_abcd/message/message_0.db": {"source": str(source)}
    }))
    def snapshot(info, directory):
        target = directory / "copy.db"
        shutil.copyfile(info["source"], target)
        from app.adapters.history_reader import signature
        return target, signature(Path(info["source"]))
    monkeypatch.setattr("app.adapters.history_reader.authenticated_snapshot", snapshot)
    cfg = AppConfig(project_root=tmp_path, adapter="history_readonly", mode="shadow")
    adapter = HistoryReadOnlyAdapter(cfg)
    assert adapter.poll() == []
    connection.executemany(f'INSERT INTO "{table}" VALUES(?,?,?,?,?,?,0)', [
        (2,102,1,1,int(time()),"our own message"),
        (3,103,1,2,int(time()),"new incoming"),
        (4,104,3,2,int(time()),"image")])
    connection.commit()
    messages = adapter.poll()
    assert [m.message_type for m in messages] == ["text", "image"]
    assert messages[0].content == "new incoming"
    assert messages[0].contact == "friend"
    assert adapter.poll() == []
    connection.executemany(f'INSERT INTO "{table}" VALUES(?,?,?,?,?,?,0)', [
        (5,105,1,2,int(time()),"already manually answered"),
        (6,106,1,1,int(time()),"human answer")])
    connection.commit()
    assert adapter.poll() == []
    with pytest.raises(RuntimeError, match="不具备发送"):
        adapter.send_text("friend", "never")
    connection.close()


def test_authenticated_reader_rejects_corrupt_page(tmp_path):
    source = tmp_path / "corrupt.db"
    source.write_bytes(b"\x00" * 4096)
    directory = tmp_path / "snapshot"
    directory.mkdir()
    with pytest.raises(RuntimeError, match="认证失败"):
        authenticated_snapshot({"source": str(source), "enc_key": "01"*32, "salt": "00"*16}, directory)


def test_failed_later_shard_does_not_discard_earlier_messages():
    reader = object.__new__(HistoryReadOnlyAdapter)
    reader.cursors, reader.signatures = {"first": 10}, {"first": (1, 1)}
    reader.keys, reader.contact_signatures = {}, {}
    def partial_read():
        reader.cursors["first"] = 11
        reader.signatures["first"] = (2, 2)
        raise RuntimeError("second database changed while copying")
    reader._read = partial_read
    with pytest.raises(RuntimeError):
        reader.poll()
    assert reader.cursors == {"first": 10}
    assert reader.signatures == {"first": (1, 1)}

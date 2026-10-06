import sqlite3
from app.chat_memory import retrieve


def test_history_is_dated_contact_isolated_and_credentials_local(tmp_path):
    path = tmp_path / "memory.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE history(id TEXT,direction TEXT,created_at INTEGER,content TEXT,risk TEXT,contact TEXT)")
    conn.executemany("INSERT INTO history VALUES(?,?,?,?,?,?)", [
        ("a", "out", 100, "收到。", "low", "one"),
        ("b", "in", 101, "其他人的项目", "low", "two"),
        ("c", "in", 102, "密码不能外发", "critical", "one")])
    conn.commit();conn.close()
    result = retrieve(path, "one", "收到")
    assert len(result) == 1 and result[0]["content"] == "收到。"
    assert result[0]["time"].endswith("+08:00")
    assert retrieve(path, "unknown", "收到") == []


def test_new_owner_and_incoming_messages_append_idempotently(tmp_path):
    from app.chat_memory import remember
    path = tmp_path / 'history.sqlite3'
    rows = [('one', 'peer', 'in', 100, '现场怎么样'), ('two', 'peer', 'out', 101, '我先看看')]
    remember(path, rows)
    remember(path, rows)
    result = retrieve(path, 'peer', '现场')
    assert len(result) == 2
    assert {r['speaker'] for r in result} == {'本人','对方'}
    assert retrieve(path, 'another', '现场') == []


def test_topic_recalls_exchange_and_respects_time_and_speaker(tmp_path):
    from app.chat_memory import remember
    path=tmp_path/'people.sqlite3'
    remember(path,[('a','peer','in',100,'我老板王总做这个项目'),('b','peer','out',101,'王总不是我'),('c','peer','in',102,'以后再说'),('future','peer','out',1000,'未来才确认的新事实'),('other','someone','in',101,'别人的项目')])
    result=retrieve(path,'peer','王总项目',as_of=102)
    assert any(r['speaker']=='对方' and '我老板' in r['content'] for r in result)
    assert any(r['speaker']=='本人' and '不是我' in r['content'] for r in result)
    assert not any('未来' in r['content'] or '别人的' in r['content'] for r in result)

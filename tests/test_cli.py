from app.cli import merge_incoming_messages
from app.models import IncomingMessage


def test_merge_burst_by_contact():
    messages = [
        IncomingMessage(external_id="1", contact="张三", sender="张三", content="在吗"),
        IncomingMessage(external_id="2", contact="张三", sender="张三", content="问一下工期"),
        IncomingMessage(external_id="3", contact="李四", sender="李四", content="收到"),
    ]
    merged = merge_incoming_messages(messages)
    assert len(merged) == 2
    assert merged[0].contact == "张三"
    assert merged[0].content == "在吗\n问一下工期"
    assert merged[1].contact == "李四"


def test_single_instance_lock(tmp_path):
    from app.cli import SingleInstanceLock

    path = tmp_path / "RUNNING.lock"
    first = SingleInstanceLock(path)
    second = SingleInstanceLock(path)
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()

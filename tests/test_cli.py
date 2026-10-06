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


def test_group_burst_keeps_people_attribution_and_sticker_paths():
    messages=[IncomingMessage(external_id='a',contact='g',chat_type='group',sender='甲',sender_key='a',content='我答应的是另一件事'),
              IncomingMessage(external_id='b',contact='g',chat_type='group',sender='乙',sender_key='b',content='表情',message_type='sticker',media_paths=['frame.png'])]
    result=merge_incoming_messages(messages)[0]
    assert result.sender=='群中多位成员'
    assert '甲：我答应' in result.content and '乙：表情' in result.content
    assert result.message_type=='sticker' and result.media_paths==['frame.png']

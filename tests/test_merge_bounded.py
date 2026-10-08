import json
from app.cli import merge_incoming_messages
from app.models import IncomingMessage


def message(identity, content):
    return IncomingMessage(external_id=identity, contact='test', sender='test', content=content)


def test_repeated_active_pending_overlap_does_not_duplicate_messages():
    a, b = message('local:a', '第一条'), message('local:b', '第二条')
    merged = merge_incoming_messages([a, b])[0]
    for _ in range(1000):
        merged = merge_incoming_messages([merged, a, b])[0]
    assert merged.content == '第一条\n第二条'
    assert json.loads(merged.raw_summary)['member_external_ids'] == ['local:a', 'local:b']


def test_oversized_merge_is_bounded_and_marked_for_review():
    result = merge_incoming_messages([message('local:a', 'a'*24000), message('local:b', 'b'*24000)])[0]
    assert len(result.content) <= 32768
    assert json.loads(result.raw_summary)['merge_truncated'] is True

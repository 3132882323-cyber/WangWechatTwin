import pytest
from app.adapters.history_reader import mentions_owner


@pytest.mark.parametrize("source,content,expected", [
    ("<atuserlist><![CDATA[other,wxid_owner]]></atuserlist>", "消息", True),
    ("<atuserlist>wxid_owner_extra</atuserlist>", "消息", False),
    ("<atuserlist>notify@all</atuserlist>", "@所有人 消息", False),
    ("<atuserlist>other</atuserlist>", "@本人昵称 消息", False),
    ("<atuserlist/>", "@本人昵称 消息", False),
    ("<atuserlist>", "@本人昵称 消息", False),
    ("", "@本人昵称同学 消息", False),
    ("", "@本人昵称\u2005消息", True),
    ("", "@本人昵称", True),
])
def test_exact_protocol_membership_and_bounded_fallback(source, content, expected):
    assert mentions_owner(source, content, "wxid_owner", "本人昵称") is expected

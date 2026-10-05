from types import SimpleNamespace

import pytest

from app.adapters.verified_sender import HistoryVerifiedSender


def fake_sender(tmp_path, chat_name="friend"):
    sender = object.__new__(HistoryVerifiedSender)
    sender.config = SimpleNamespace(mode="low_risk_auto",
        wechat=SimpleNamespace(sender_all_existing_chats=False),
        paths=SimpleNamespace(pause_file=str(tmp_path / "PAUSE")), resolve=lambda value: __import__("pathlib").Path(value))
    sender.allowed, sender.context = {"uid"}, {"uid": "incoming-1"}
    sender.reader = SimpleNamespace(root=tmp_path, names={"uid": "friend"}, _load_names=lambda: None)
    gui = SimpleNamespace(ChatWith=lambda *a, **kw: None, ChatInfo=lambda: {"chat_name": chat_name})
    gui.sent = []
    gui.SendMsg = lambda **kw: gui.sent.append(kw)
    sender.check_sender_connection = lambda: setattr(sender, "wx", gui)
    calls = []
    def outgoing(contact, text):
        calls.append(1)
        return ({"shard": 10}, [], {}) if len(calls) == 1 else ({"shard": 11}, [("shard", 11, True)], {})
    sender._outgoing_state = outgoing
    sender._has_saved_human_draft = lambda contact: False
    return sender, gui


def test_only_authorized_peer_can_send_and_duplicate_is_blocked(tmp_path):
    sender, gui = fake_sender(tmp_path)
    assert sender.send_text("not-authorized", "收到。") is False
    assert not gui.sent
    assert sender.send_text("uid", "收到。") is True
    assert len(gui.sent) == 1
    assert sender.send_text("uid", "收到。") is False
    assert len(gui.sent) == 1


def test_wrong_chat_and_shadow_never_send(tmp_path):
    sender, gui = fake_sender(tmp_path, chat_name="different peer")
    with pytest.raises(RuntimeError, match="不一致"):
        sender.send_text("uid", "收到。")
    assert not gui.sent
    sender.config.mode = "shadow"
    assert sender.send_text("uid", "收到。") is False
    assert not gui.sent


def test_risky_generated_reply_never_reaches_gui(tmp_path):
    sender, gui = fake_sender(tmp_path)
    assert sender.send_text("uid", "合同赔偿我同意，付款账户给你。") is False
    assert not gui.sent


def test_manual_answer_during_generation_blocks_automatic_send(tmp_path):
    sender, gui = fake_sender(tmp_path)
    sender.context_origin = {"uid": {"source": "shard", "local_id": 10, "created_at": 100}}
    sender._outgoing_state = lambda *args: ({"shard": 11}, [], {"shard": (11, 101)})
    assert sender.send_text("uid", "收到。") is False
    assert not gui.sent


def test_saved_human_draft_is_not_overwritten(tmp_path):
    sender, gui = fake_sender(tmp_path)
    sender._has_saved_human_draft = lambda contact: True
    assert sender.send_text("uid", "收到。") is False
    assert not gui.sent

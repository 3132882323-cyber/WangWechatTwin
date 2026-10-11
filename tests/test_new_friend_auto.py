"""New friend discovery/automatic policy tests using only temporary fake data."""
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
from time import time
from types import SimpleNamespace

import pytest

from app.adapters.history_reader import HistoryReadOnlyAdapter, signature
from app.adapters.http_sender import HistoryHTTPSender
from app.adapters.mock import MockAdapter
from app.config import AppConfig
from app.db import Database
from app.models import ReplyDecision, RiskLevel
from app.pipeline import ReplyPipeline


OWNER = "wxid_fixture_owner"
OLD = "fixture_existing_peer"
NEW = "fixture_new_friend"
GROUP = "fixture_group@chatroom"


class FakeLLM:
    def __init__(self):
        self.payloads = []

    def decide(self, system, payload, risk):
        self.payloads.append(json.loads(payload))
        return ReplyDecision(action="send", risk=RiskLevel.low,
                             reply="可以，你想聊点什么？", confidence=.99)


@pytest.fixture
def discovered_reader(tmp_path, monkeypatch):
    config = AppConfig(project_root=tmp_path, adapter="mock", mode="low_risk_auto",
        owner_name="Fixture Owner", owner_alias="Fixture Alias",
        contacts=[{"name": OLD, "mode": "inherit"}],
        wechat={"sender_all_existing_chats": True, "sender_allowed_contacts": [],
                "allow_unknown_contacts": True, "unknown_contact_mode": "low_risk_auto",
                "allow_groups": True, "group_only_mentions": True,
                "send_holding_on_review": False})
    runtime = config.resolve(config.paths.history_reader)
    runtime.mkdir(parents=True)
    contact_source, message_source = tmp_path / "contacts.db", tmp_path / "messages.db"
    contacts = sqlite3.connect(contact_source)
    contacts.execute("CREATE TABLE contact(username TEXT,remark TEXT,nick_name TEXT,alias TEXT,delete_flag INTEGER)")
    contacts.executemany("INSERT INTO contact VALUES(?,?,?,?,0)",
                        [(OWNER, "", "Fixture Owner", "fixture-owner"),
                         (OLD, "Fixture Existing", "", "fixture-existing")])
    contacts.commit()
    messages = sqlite3.connect(message_source)
    messages.execute("CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY,is_session INTEGER)")
    messages.executemany("INSERT INTO Name2Id VALUES(?,0)", [(OWNER,), (OLD,)])

    def add_table(contact):
        table = "Msg_" + hashlib.md5(contact.encode()).hexdigest()
        messages.execute(f'CREATE TABLE IF NOT EXISTS "{table}"('
            'local_id INTEGER,server_id INTEGER,local_type INTEGER,real_sender_id INTEGER,'
            'create_time INTEGER,message_content TEXT,WCDB_CT_message_content INTEGER,'
            'source TEXT,WCDB_CT_source INTEGER)')
        return table

    old_table = add_table(OLD)
    messages.execute(f'INSERT INTO "{old_table}" VALUES(1,101,1,2,?,"old synthetic history",0,"",0)', (int(time()),))
    messages.commit()
    keys = {f"{OWNER}_test/contact/contact.db": {"source": str(contact_source)},
            f"{OWNER}_test/message/message_0.db": {"source": str(message_source)}}
    (runtime / "keys.json").write_text(json.dumps(keys), encoding="utf-8")

    def fake_snapshot(info, directory):
        target = directory / "fixture-copy.db"
        shutil.copyfile(info["source"], target)
        return target, signature(Path(info["source"]))

    monkeypatch.setattr("app.adapters.history_reader.authenticated_snapshot", fake_snapshot)
    reader = HistoryReadOnlyAdapter(config)
    assert reader.poll() == []  # Startup does not replay old messages.

    def add_new_friend():
        contacts.execute("INSERT INTO contact VALUES(?,?,?,?,0)", (NEW, "Fixture New", "", "fixture-new"))
        contacts.commit()
        messages.execute("INSERT INTO Name2Id VALUES(?,0)", (NEW,))
        table = add_table(NEW)
        messages.execute(f'INSERT INTO "{table}" VALUES(1,201,1,3,?,"你好，想跟你随便聊几句。",0,"",0)', (int(time()),))
        messages.commit()

    try:
        yield config, reader, contacts, messages, add_table, add_new_friend
    finally:
        contacts.close()
        messages.close()


def test_new_friend_first_inbound_enters_auto_policy_without_config_entry(discovered_reader):
    config, reader, _, _, _, add_new_friend = discovered_reader
    assert NEW not in reader.names and NEW not in reader.existing_conversations
    add_new_friend()
    received = reader.poll()
    assert len(received) == 1 and received[0].contact == NEW
    assert NEW in reader.names and NEW in reader.existing_conversations
    llm, mock_sender = FakeLLM(), MockAdapter()
    pipeline = ReplyPipeline(config, Database(config.resolve(config.paths.database)), llm)
    assert NEW not in pipeline.contact_map
    result = pipeline.process(received[0], mock_sender)
    assert result.status == "sent"
    assert llm.payloads[0]["contact_profile"]["mode"] == "low_risk_auto"
    assert mock_sender.sent == [(NEW, "可以，你想聊点什么？")]


def test_sender_refreshes_new_friend_from_discovered_map_without_allowlist(discovered_reader):
    config, reader, _, _, _, add_new_friend = discovered_reader
    sender = HistoryHTTPSender.__new__(HistoryHTTPSender)
    sender.config = config
    class ReaderView:
        root = reader.root
        self_username = OWNER

        @property
        def names(self):
            return reader.names

        @property
        def existing_conversations(self):
            return reader.existing_conversations

    sender.reader = ReaderView()
    sender.allowed = set()
    assert NEW not in sender._eligible_contacts()
    add_new_friend()
    incoming = reader.poll()[0]
    # Like production, this view reads the current reader map, including replacement
    # of names by poll. It deliberately exposes no personal/native services.
    calls = []
    sender.client = SimpleNamespace(
        verify_owner=lambda owner: calls.append(("verify_fake_owner", owner)),
        submit=lambda *args: calls.append(("submit_fake_only", args)))
    sender.context = {NEW: incoming.external_id}
    sender.context_origin = {NEW: json.loads(incoming.raw_summary)}
    sender.latest_inbound = {}
    sender._has_saved_human_draft = lambda _: False
    states = iter([({"fixture-shard": 10}, [], {}),
                   ({"fixture-shard": 11}, [("fixture-shard", 11, True)], {})])
    sender._stable_outgoing_state = lambda *args: next(states)
    assert sender.send_text(NEW, "可以，你说。") is True
    assert NEW in sender.allowed
    assert sum(kind == "submit_fake_only" for kind, _ in calls) == 1


def test_group_without_owner_mention_is_filtered_but_mentioned_message_is_received(discovered_reader):
    config, reader, contacts, messages, add_table, add_new_friend = discovered_reader
    add_new_friend()
    reader.poll()
    contacts.execute("INSERT INTO contact VALUES(?,?,?,?,0)", (GROUP, "Fixture Group", "", ""))
    contacts.commit()
    messages.execute("INSERT INTO Name2Id VALUES(?,0)", (GROUP,))
    table = add_table(GROUP)
    messages.execute(f'INSERT INTO "{table}" VALUES(1,301,1,3,?,"普通群消息",0,"",0)', (int(time()),))
    messages.commit()
    assert reader.poll() == []
    messages.execute(f'INSERT INTO "{table}" VALUES(2,302,1,3,?,"想聊两句",0,?,0)',
                     (int(time()), f"<msgsource><atuserlist>{OWNER}</atuserlist></msgsource>"))
    messages.commit()
    received = reader.poll()
    assert len(received) == 1 and received[0].chat_type == "group"
    assert received[0].contact == GROUP


def test_group_mention_of_another_uid_containing_owner_uid_is_not_owner_mention(discovered_reader):
    config, reader, contacts, messages, add_table, add_new_friend = discovered_reader
    add_new_friend()
    reader.poll()
    contacts.execute("INSERT INTO contact VALUES(?,?,?,?,0)", (GROUP, "Fixture Group", "", ""))
    contacts.commit()
    messages.execute("INSERT INTO Name2Id VALUES(?,0)", (GROUP,))
    table = add_table(GROUP)
    source = f"<msgsource><atuserlist>{OWNER}_another_person</atuserlist></msgsource>"
    messages.execute(f'INSERT INTO "{table}" VALUES(1,401,1,3,?,"没有提及本人",0,?,0)', (int(time()), source))
    messages.commit()
    assert reader.poll() == []


def test_explicit_group_mentions_of_other_people_override_nickname_prefix_fallback(discovered_reader):
    config, reader, contacts, messages, add_table, add_new_friend = discovered_reader
    add_new_friend()
    reader.poll()
    contacts.execute("INSERT INTO contact VALUES(?,?,?,?,0)", (GROUP, "Fixture Group", "", ""))
    contacts.commit()
    messages.execute("INSERT INTO Name2Id VALUES(?,0)", (GROUP,))
    table = add_table(GROUP)
    source = "<msgsource><atuserlist>wxid_otherfixture</atuserlist></msgsource>"
    messages.execute(f'INSERT INTO "{table}" VALUES(1,501,1,3,?,"@Fixture OwnerHelper 普通群消息",0,?,0)', (int(time()), source))
    messages.commit()
    assert reader.poll() == []

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from types import SimpleNamespace
from threading import Thread

import pytest

from app.adapters.http_sender import HistoryHTTPSender, LocalHookClient, LocalAPIError
from app.config import AppConfig, LocalAPISettings
from app.adapters.history_reader import SnapshotBusyError


@pytest.fixture
def local_server(tmp_path):
    calls = []
    token = "test-token-" + "x" * 32
    token_path = tmp_path / "token.txt"
    token_path.write_text(token, encoding="utf-8")
    state = {"owner": "test-self", "send_ret": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, body))
            if self.headers.get("Authorization") != "Bearer " + token:
                self.send_response(401)
                self.end_headers()
                return
            result = {"wxid": state["owner"]} if self.path == "/GetSelfProfile" else {"ret": state["send_ret"]}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = LocalHookClient(LocalAPISettings(port=server.server_port), token_path)
    try:
        yield client, calls, state, token_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def make_sender(tmp_path, client):
    sender = HistoryHTTPSender.__new__(HistoryHTTPSender)
    sender.config = AppConfig(project_root=tmp_path, mode="low_risk_auto",
                             wechat={"sender_allowed_contacts": ["test-peer"]},
                             local_api={"receipt_timeout_seconds": .02})
    sender.reader = SimpleNamespace(root=tmp_path, self_username="test-self")
    sender.client = client
    sender.allowed = {"test-peer"}
    sender.context = {"test-peer": "incoming-test-id"}
    sender.context_origin = {"test-peer": {"source": "shard", "local_id": 10, "created_at": 100}}
    sender._has_saved_human_draft = lambda _: False
    sender._outgoing_state = lambda *_: ({"shard": 10}, [], {})
    return sender


def test_actual_loopback_protocol_and_identity(local_server):
    client, calls, state, _ = local_server
    client.verify_owner("test-self")
    client.submit("test-peer", "咋了", "test-self", "a" * 64)
    assert calls[-1] == ("/SendTextMsg", {"wxidorgid": "test-peer", "msg": "咋了",
                                        "expected_wxid": "test-self", "request_id": "a" * 64})
    state["owner"] = "wrong-account"
    with pytest.raises(LocalAPIError, match="账号"):
        client.verify_owner("test-self")


def test_http_acceptance_is_not_delivery_and_no_retry(tmp_path, local_server):
    client, calls, *_ = local_server
    sender = make_sender(tmp_path, client)
    assert sender.send_text("test-peer", "咋了") is False
    claim = next((tmp_path / "send_claims").glob("*.json"))
    assert json.loads(claim.read_text())["status"] == "unknown_do_not_retry"
    assert sender.send_text("test-peer", "咋了") is False
    assert sum(path == "/SendTextMsg" for path, _ in calls) == 1


def test_success_requires_new_server_ack(tmp_path, local_server):
    client, calls, *_ = local_server
    sender = make_sender(tmp_path, client)
    states = iter([({"shard": 10}, [("shard", 8, True)], {}),
                   ({"shard": 11}, [("shard", 11, True)], {})])
    sender._outgoing_state = lambda *_: next(states)
    assert sender.send_text("test-peer", "咋了") is True
    claim = next((tmp_path / "send_claims").glob("*.json"))
    assert json.loads(claim.read_text())["status"] == "verified_sent"


def test_busy_receipt_retries_read_without_resending(tmp_path, local_server):
    client, calls, *_ = local_server
    sender = make_sender(tmp_path, client)
    sender.config.local_api.receipt_timeout_seconds = 2
    count = 0
    def state(*_):
        nonlocal count
        count += 1
        if count == 1:
            return {"shard": 10}, [], {}
        if count == 2:
            raise SnapshotBusyError("database updating")
        return {"shard": 11}, [("shard", 11, True)], {}
    sender._outgoing_state = state
    assert sender.send_text("test-peer", "咋了") is True
    assert sum(path == "/SendTextMsg" for path, _ in calls) == 1


@pytest.mark.parametrize("guard", ["paused", "draft", "manual_reply", "no_context", "risk", "wrong_owner"])
def test_guards_prevent_http_send(tmp_path, local_server, guard):
    client, calls, state, _ = local_server
    sender = make_sender(tmp_path, client)
    text = "咋了"
    if guard == "paused":
        pause = sender.config.resolve(sender.config.paths.pause_file)
        pause.parent.mkdir(parents=True)
        pause.touch()
    elif guard == "draft":
        sender._has_saved_human_draft = lambda _: True
    elif guard == "manual_reply":
        sender._outgoing_state = lambda *_: ({"shard": 11}, [], {"shard": (11, 101)})
    elif guard == "no_context":
        sender.context_origin = {}
    elif guard == "risk":
        text = "报价一万元，明天交货"
    elif guard == "wrong_owner":
        state["owner"] = "another-account"
    if guard == "wrong_owner":
        with pytest.raises(LocalAPIError):
            sender.send_text("test-peer", text)
    else:
        assert sender.send_text("test-peer", text) is False
    assert not any(path == "/SendTextMsg" for path, _ in calls)


def test_unauthorized_and_send_rejection(local_server):
    client, calls, state, token_path = local_server
    state["send_ret"] = -1
    with pytest.raises(LocalAPIError, match="未接受"):
        client.submit("test-peer", "咋了", "test-self", "a" * 64)
    token_path.write_text("z" * 40)
    with pytest.raises(LocalAPIError, match="401"):
        client.verify_owner("test-self")

"""Offline HTTP contract tests for the isolated normal-login GLM trial gateway.

All upstream responses come from httpx.MockTransport. No account files, browser,
model service, production ledger or WeChat sender are used.
"""

import asyncio
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[1] / 'experiments' / 'glm_web_trial'
PUBLIC = ROOT.parents[1]
sys.path.insert(0, str(PUBLIC))
from app.models import ReplyDecision, RiskLevel  # noqa: E402
from app.reply_selection import select_reply  # noqa: E402


def load_gateway():
    source = ROOT / "gateway.py"
    spec = importlib.util.spec_from_file_location("isolated_glm_trial_gateway", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def decision():
    replies = ["我听到了，你接着说。", "在呢，慢慢说。", "你说，我认真听。"]
    return {"action": "send", "risk": "low", "reply": replies[0],
            "confidence": .9, "reason": "回应本轮虚构文字", "facts_to_confirm": [],
            "memory_updates": [], "media_description": "", "media_confidence": 0,
            "sticker_id": "", "candidates": [
                {"label": label, "reply": reply, "style_match": 4, "context_fit": 4,
                 "continuation": 4, "boundary_respect": 5, "invented_facts": False,
                 "risk": "low"}
                for label, reply in zip("ABC", replies)
            ], "selected_candidate": "A"}


def request_body(contact="a", request_id="turn-1", content="今天过得怎么样？"):
    return {"model": "web_assistant_unverified", "stream": False,
            "user": contact * 64, "request_id": request_id,
            "messages": [{"role": "system", "content": "只起草虚构文字，不发送微信。"},
                         {"role": "user", "content": content}]}


def refresh_response():
    return httpx.Response(200, json={"code": 0, "result": {"access_token": "fake-access"}})


def generation_response(*, remote_id="remote-a", content=None, finish=True):
    event = {"conversation_id": remote_id,
             "parts": [{"logic_id": "1", "content": [
                 {"type": "text", "text": content if content is not None else json.dumps(decision(), ensure_ascii=False)}
             ]}]}
    if finish:
        event["status"] = "finish"
    return httpx.Response(200, text="data: " + json.dumps(event, ensure_ascii=False) + "\n\n",
                          headers={"content-type": "text/event-stream"})


def unsupported_response(item):
    event = {"conversation_id": "remote-a", "status": "finish",
             "parts": [{"logic_id": "1", "content": [item]}]}
    return httpx.Response(200, text="data: " + json.dumps(event, ensure_ascii=False) + "\n\n",
                          headers={"content-type": "text/event-stream"})


def event_stream(*events):
    return httpx.Response(200, text="".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                                           for event in events),
                          headers={"content-type": "text/event-stream"})


def metadata_event(secret, *, remote_id="remote-a", status=None, tool_calls=None):
    event = {"conversation_id": remote_id,
             "parts": [{"content": [{"type": "tool_calls", "tool_calls":
                                       {"arguments": secret} if tool_calls is None else tool_calls}]}]}
    if status:
        event["status"] = status
    return event


def text_event(content, *, remote_id="remote-a", status="finish"):
    return {"conversation_id": remote_id, "status": status,
            "parts": [{"logic_id": "1", "content": [{"type": "text", "text": content}]}]}


def upstream_current_text(request):
    body = json.loads(request.content)
    prompt = body["messages"][0]["content"][0]["text"]
    return json.loads(prompt.rsplit("\n本轮消息：\n", 1)[1])[-1]["content"]


def app_with_mock(tmp_path, upstream, *, ledger=None, now=None, credentials=None):
    module = load_gateway()
    factory_calls = []
    credential_calls = []

    def client_factory():
        factory_calls.append(1)
        return httpx.AsyncClient(transport=httpx.MockTransport(upstream))

    def credential_reader():
        credential_calls.append(1)
        return credentials if credentials is not None else {
            "source": "authorized_single_glm_tab", "refresh_token": "fake-refresh"}

    app = module.create_app(
        ledger_path=ledger or tmp_path / "synthetic-ledger.sqlite3",
        api_key="synthetic-local-key", credential_reader=credential_reader,
        client_factory=client_factory, now=now or (lambda: 1_700_000_000.0),
    )
    return app, factory_calls, credential_calls


async def post(app, body, *, token="synthetic-local-key"):
    headers = {"Authorization": "Bearer " + token} if token is not None else {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://isolated-test") as client:
        return await client.post("/v1/chat/completions", json=body, headers=headers)


async def health(app, *, token="synthetic-local-key"):
    headers = {"Authorization": "Bearer " + token} if token is not None else {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://isolated-test") as client:
        return await client.get("/health", headers=headers)


def test_finished_response_has_full_strict_decision_and_durable_done(tmp_path):
    calls = []

    async def upstream(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/refresh"):
            return refresh_response()
        assert request.url.path.endswith("/assistant/stream")
        assert json.loads(request.content)["conversation_id"] == ""
        return generation_response()

    app, factories, _ = app_with_mock(tmp_path, upstream)
    result = asyncio.run(post(app, request_body()))
    assert result.status_code == 200
    body = result.json()
    assert body["model"] == "web_assistant_unverified"
    assert body["conversation_id"] == "remote-a"
    choice = body["choices"][0]
    assert choice["finish_reason"] == "stop"
    parsed = ReplyDecision.model_validate_json(choice["message"]["content"], strict=True)
    assert {item.label for item in parsed.candidates} == {"A", "B", "C"}
    assert len({item.reply for item in parsed.candidates}) == 3
    assert len(factories) == 1 and len(calls) == 2
    with sqlite3.connect(tmp_path / "synthetic-ledger.sqlite3") as db:
        assert db.execute("SELECT status FROM requests WHERE request_id='turn-1'").fetchone()[0] == "done"
        assert db.execute("SELECT remote_id FROM sessions WHERE conversation_key=?", ("a" * 64,)).fetchone()[0] == "remote-a"
    duplicate = asyncio.run(post(app, request_body()))
    assert duplicate.status_code == 409 and len(calls) == 2


@pytest.mark.parametrize("failure", ["missing_finish", "html", "upstream_error", "invalid_decision"])
def test_incomplete_or_error_response_is_unknown_and_old_request_never_retried(tmp_path, failure):
    calls = []

    async def upstream(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/refresh"):
            return refresh_response()
        if failure == "missing_finish":
            return generation_response(finish=False)
        if failure == "html":
            return httpx.Response(200, text="<html>login needed</html>",
                                  headers={"content-type": "text/html"})
        if failure == "upstream_error":
            return httpx.Response(503, text="upstream unavailable")
        return generation_response(content=json.dumps({"action": "send", "risk": "low", "reply": "不完整"}))

    app, factories, _ = app_with_mock(tmp_path, upstream)
    first = asyncio.run(post(app, request_body()))
    assert first.status_code == 502
    assert isinstance(first.json()["error"]["code"], str)
    assert "fake-" not in json.dumps(first.json())
    assert asyncio.run(post(app, request_body())).status_code == 409
    assert asyncio.run(post(app, request_body(request_id="turn-2"))).status_code == 409
    assert len(calls) == 2 and len(factories) == 1
    with sqlite3.connect(tmp_path / "synthetic-ledger.sqlite3") as db:
        assert db.execute("SELECT status FROM requests WHERE request_id='turn-1'").fetchone()[0] == "unknown"
        assert db.execute("SELECT count(*) FROM requests").fetchone()[0] == 1


def test_contacts_keep_distinct_remote_sessions_and_reuse_only_their_own(tmp_path):
    sent = []

    async def upstream(request):
        if request.url.path.endswith("/refresh"):
            return refresh_response()
        body = json.loads(request.content)
        contact = upstream_current_text(request)[2]
        sent.append((contact, body["conversation_id"]))
        return generation_response(remote_id="remote-" + contact)

    app, _, _ = app_with_mock(tmp_path, upstream)

    async def exercise():
        for contact, turn in (("a", 1), ("b", 1), ("a", 2), ("b", 2)):
            response = await post(app, request_body(contact, f"{contact}-{turn}", f"合成{contact}{turn}"))
            assert response.status_code == 200

    asyncio.run(exercise())
    assert sent == [("a", ""), ("b", ""),
                    ("a", "remote-a"), ("b", "remote-b")]
    with sqlite3.connect(tmp_path / "synthetic-ledger.sqlite3") as db:
        assert db.execute("SELECT count(DISTINCT remote_id) FROM sessions").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM requests WHERE status='done'").fetchone()[0] == 4


def test_remote_id_collision_cannot_bind_second_contact(tmp_path):
    calls = []

    async def upstream(request):
        if request.url.path.endswith("/refresh"):
            return refresh_response()
        calls.append(1)
        return generation_response(remote_id="shared-remote")

    app, _, _ = app_with_mock(tmp_path, upstream)
    assert asyncio.run(post(app, request_body("a", "a-1"))).status_code == 200
    collision = asyncio.run(post(app, request_body("b", "b-1")))
    assert collision.status_code == 502
    assert asyncio.run(post(app, request_body("b", "b-1"))).status_code == 409
    assert len(calls) == 2
    with sqlite3.connect(tmp_path / "synthetic-ledger.sqlite3") as db:
        assert db.execute("SELECT conversation_key FROM remote_ids WHERE remote_id='shared-remote'").fetchone()[0] == "a" * 64
        assert db.execute("SELECT status FROM requests WHERE request_id='b-1'").fetchone()[0] == "unknown"


def test_unexpected_second_remote_id_stays_quarantined_to_original_contact(tmp_path):
    async def upstream(request):
        if request.url.path.endswith("/refresh"):
            return refresh_response()
        turn = upstream_current_text(request)
        return generation_response(remote_id="remote-a" if turn == "合成a1" else "remote-unexpected")

    app, _, _ = app_with_mock(tmp_path, upstream)
    assert asyncio.run(post(app, request_body("a", "a-1", "合成a1"))).status_code == 200
    changed = asyncio.run(post(app, request_body("a", "a-2", "合成a2")))
    assert changed.status_code == 502
    collision = asyncio.run(post(app, request_body("b", "b-1", "合成b1")))
    assert collision.status_code == 502
    with sqlite3.connect(tmp_path / "synthetic-ledger.sqlite3") as db:
        owners = dict(db.execute("SELECT remote_id,conversation_key FROM remote_ids"))
        assert owners == {"remote-a": "a" * 64, "remote-unexpected": "a" * 64}
        assert db.execute("SELECT status FROM requests WHERE request_id='a-2'").fetchone()[0] == "unknown"
        assert db.execute("SELECT status FROM requests WHERE request_id='b-1'").fetchone()[0] == "unknown"


def test_pending_claim_survives_restart_and_blocks_same_contact(tmp_path):
    ledger = tmp_path / "synthetic-ledger.sqlite3"

    async def exercise():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def upstream(request):
            if request.url.path.endswith("/refresh"):
                return refresh_response()
            entered.set()
            await release.wait()
            return generation_response()

        first_app, first_factories, _ = app_with_mock(tmp_path, upstream, ledger=ledger)
        first = asyncio.create_task(post(first_app, request_body()))
        await asyncio.wait_for(entered.wait(), 2)

        async def unexpected_upstream(request):
            pytest.fail("pending claim must block upstream before a new request")

        restarted, second_factories, _ = app_with_mock(tmp_path, unexpected_upstream, ledger=ledger)
        assert (await post(restarted, request_body())).status_code == 409
        assert (await post(restarted, request_body(request_id="turn-2"))).status_code == 409
        with sqlite3.connect(ledger) as db:
            assert db.execute("SELECT status FROM requests WHERE request_id='turn-1'").fetchone()[0] == "pending"
        assert len(first_factories) == 1 and not second_factories
        release.set()
        assert (await first).status_code == 200

    asyncio.run(exercise())


def test_concurrent_other_contact_gets_preclaim_busy_without_ledger_row(tmp_path):
    ledger = tmp_path / "synthetic-ledger.sqlite3"

    async def exercise():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def upstream(request):
            if request.url.path.endswith("/refresh"):
                return refresh_response()
            contact = upstream_current_text(request)[2]
            if contact == "a":
                entered.set()
                await release.wait()
            return generation_response(remote_id="remote-" + contact)

        app, _, _ = app_with_mock(tmp_path, upstream, ledger=ledger)
        first = asyncio.create_task(post(app, request_body("a", "a-1", "合成a1")))
        await asyncio.wait_for(entered.wait(), 2)
        busy = await post(app, request_body("b", "b-1", "合成b1"))
        assert busy.status_code == 429
        with sqlite3.connect(ledger) as db:
            assert db.execute("SELECT count(*) FROM requests WHERE request_id='b-1'").fetchone()[0] == 0
        release.set()
        assert (await first).status_code == 200
        assert (await post(app, request_body("b", "b-1", "合成b1"))).status_code == 200

    asyncio.run(exercise())


def test_local_key_authentication_rejects_before_credentials_or_upstream(tmp_path):
    async def unexpected_upstream(request):
        pytest.fail("unauthorized request reached upstream")

    app, factories, credentials = app_with_mock(tmp_path, unexpected_upstream)
    assert asyncio.run(post(app, request_body(), token=None)).status_code == 401
    assert asyncio.run(post(app, request_body(), token="wrong-key")).status_code == 401
    assert asyncio.run(health(app, token=None)).status_code == 401
    assert not factories and not credentials


def test_health_requires_recent_completed_inference_not_just_auth_presence(tmp_path):
    now = [1_700_000_000.0]

    async def upstream(request):
        return refresh_response() if request.url.path.endswith("/refresh") else generation_response()

    app, factories, _ = app_with_mock(tmp_path, upstream, now=lambda: now[0])
    before = asyncio.run(health(app))
    assert before.status_code == 200
    assert before.json()["ok"] is False
    assert before.json()["inference"]["verified"] is False
    assert not factories  # Health does not submit a model turn.
    assert asyncio.run(post(app, request_body())).status_code == 200
    recent = asyncio.run(health(app)).json()
    assert recent["ok"] is True and recent["inference"]["verified"] is True
    now[0] += 121
    old = asyncio.run(health(app)).json()
    assert old["ok"] is False and old["inference"]["verified"] is False


@pytest.mark.parametrize("raw_kind,expected", [
    ("code", "code"), ("execution_output", "code"),
    ("markdown", "markdown"), ("tool_call", "tool"),
    ("search_result", "search"), ("image_url", "image"),
    ("audio_url", "audio"), ("unrecognized_secret_type", "unknown"),
    ({"untrusted": "type value"}, "unknown"),
])
def test_non_text_diagnostic_is_fixed_private_and_persists_without_retry(tmp_path, raw_kind, expected):
    secret = "SYNTHETIC_SECRET_MUST_NEVER_LEAK_731"
    item = {"type": raw_kind, "text": secret, "code": secret,
            "markdown": secret, "content": {"private": secret},
            "tool_calls": [{"arguments": secret}], "image": {"url": secret},
            "audio": [secret], "language": secret, "value": secret,
            "unexpected_field": {"private": secret}}
    if raw_kind == "unrecognized_secret_type":
        item["type"] = "unrecognized-" + secret
    elif isinstance(raw_kind, dict):
        item["type"] = {"private": secret}
    upstream_calls = []

    async def upstream(request):
        upstream_calls.append(request.url.path)
        return refresh_response() if request.url.path.endswith("/refresh") else unsupported_response(item)

    ledger = tmp_path / "synthetic-ledger.sqlite3"
    app, factories, _ = app_with_mock(tmp_path, upstream, ledger=ledger)
    first = asyncio.run(post(app, request_body()))
    assert first.status_code == 502
    error = first.json()["error"]
    assert error["code"] == error["message"] == "non_text_output_" + expected
    diagnostic = error["diagnostic"]
    assert set(diagnostic) == {"category", "shape"} and diagnostic["category"] == expected
    fields = ("type", "text", "code", "markdown", "content", "tool_calls",
              "image", "audio", "language", "value")
    expected_shape_keys = {prefix + name for name in fields for prefix in ("has_",)}
    expected_shape_keys |= {name + suffix for name in fields
                            for suffix in ("_is_string", "_is_list", "_is_dict")}
    assert set(diagnostic["shape"]) == expected_shape_keys
    assert all(type(value) is bool for value in diagnostic["shape"].values())
    assert "has_unexpected_field" not in diagnostic["shape"]
    assert secret not in json.dumps(first.json(), ensure_ascii=False)
    assert len(upstream_calls) == 2 and len(factories) == 1

    observed = asyncio.run(health(app)).json()["diagnostic"]["last_unsupported_output"]
    assert observed == {**diagnostic, "captured_at": 1_700_000_000.0}
    assert secret not in json.dumps(observed, ensure_ascii=False)
    with sqlite3.connect(ledger) as db:
        assert db.execute("SELECT status,error_code FROM requests WHERE request_id='turn-1'").fetchone() == (
            "unknown", "non_text_output_" + expected)
        saved = db.execute("SELECT value FROM metadata WHERE key='last_unsupported_output'").fetchone()[0]
        assert secret not in saved
        assert json.loads(saved) == observed

    async def unexpected_upstream(request):
        pytest.fail("a quarantined turn must not be submitted again")

    restarted, restarted_factories, _ = app_with_mock(tmp_path, unexpected_upstream, ledger=ledger)
    assert asyncio.run(health(restarted)).json()["diagnostic"]["last_unsupported_output"] == observed
    assert asyncio.run(post(restarted, request_body())).status_code == 409
    assert asyncio.run(post(restarted, request_body(request_id="turn-2"))).status_code == 409
    assert not restarted_factories


def test_passive_tool_metadata_then_final_json_forces_review_without_leaking_arguments(tmp_path):
    secret = "SYNTHETIC_TOOL_ARGUMENT_NEVER_LEAK_942"
    calls = []

    async def upstream(request):
        calls.append(request.url.path)
        return (refresh_response() if request.url.path.endswith("/refresh") else
                event_stream(metadata_event(secret), text_event(json.dumps(decision(), ensure_ascii=False))))

    app, _, _ = app_with_mock(tmp_path, upstream)
    result = asyncio.run(post(app, request_body()))
    assert result.status_code == 200
    body = result.json()
    assert body["tool_metadata_seen"] is True
    assert body["conversation_id"] == "remote-a"
    assert body["choices"][0]["finish_reason"] == "stop"
    parsed = ReplyDecision.model_validate_json(body["choices"][0]["message"]["content"], strict=True)
    assert parsed.action == "review" and parsed.confidence == 0
    assert parsed.holding_reply is None
    assert parsed.reason == "上游返回了工具元数据，本轮草稿须先核对本轮本机事实后再决定是否发送。"
    assert {candidate.label for candidate in parsed.candidates} == {"A", "B", "C"}
    selected = select_reply(parsed, mode="abc", user_payload=json.dumps(request_body(), ensure_ascii=False),
                            incoming_risk=RiskLevel.low)
    assert selected.action == "review" and selected.holding_reply is None
    assert secret not in json.dumps(body, ensure_ascii=False)
    assert len(calls) == 2  # Refresh and generation only; no tool invocation.
    health_body = asyncio.run(health(app)).json()
    assert health_body["inference"]["tool_metadata_seen_count"] == 1
    assert secret not in json.dumps(health_body, ensure_ascii=False)
    with sqlite3.connect(tmp_path / "synthetic-ledger.sqlite3") as db:
        assert db.execute("SELECT status,tool_metadata_seen FROM requests WHERE request_id='turn-1'").fetchone() == (
            "done", 1)
        assert secret not in "\n".join(db.iterdump())


@pytest.mark.parametrize("final_text", [None, "not JSON", '{"action":"ignore","risk":"low"}'])
def test_tool_metadata_without_valid_final_abc_remains_unknown_and_never_retries(tmp_path, final_text):
    calls = []

    async def upstream(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/refresh"):
            return refresh_response()
        events = [metadata_event("SYNTHETIC_PRIVATE_TOOL_PAYLOAD", status="finish" if final_text is None else None)]
        if final_text is not None:
            events.append(text_event(final_text))
        return event_stream(*events)

    app, _, _ = app_with_mock(tmp_path, upstream)
    first = asyncio.run(post(app, request_body()))
    assert first.status_code == 502
    assert "SYNTHETIC_PRIVATE_TOOL_PAYLOAD" not in json.dumps(first.json(), ensure_ascii=False)
    assert asyncio.run(post(app, request_body())).status_code == 409
    assert asyncio.run(post(app, request_body(request_id="turn-2"))).status_code == 409
    assert len(calls) == 2
    with sqlite3.connect(tmp_path / "synthetic-ledger.sqlite3") as db:
        assert db.execute("SELECT status,tool_metadata_seen FROM requests WHERE request_id='turn-1'").fetchone() == (
            "unknown", 1)
        assert "SYNTHETIC_PRIVATE_TOOL_PAYLOAD" not in "\n".join(db.iterdump())


@pytest.mark.parametrize("raw_kind,malformed", [
    ("tool_calls", []), ("tool_calls", "private-arguments"),
    ("tool_call", {"arguments": "private-arguments"}),
])
def test_malformed_or_aliased_tool_metadata_remains_non_text_failure(tmp_path, raw_kind, malformed):
    calls = []

    async def upstream(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/refresh"):
            return refresh_response()
        event = metadata_event("unused", status="finish", tool_calls=malformed)
        event["parts"][0]["content"][0]["type"] = raw_kind
        return event_stream(event)

    app, _, _ = app_with_mock(tmp_path, upstream)
    result = asyncio.run(post(app, request_body()))
    assert result.status_code == 502
    assert result.json()["error"]["code"] == "non_text_output_tool"
    assert "private-arguments" not in json.dumps(result.json(), ensure_ascii=False)
    with sqlite3.connect(tmp_path / "synthetic-ledger.sqlite3") as db:
        assert db.execute("SELECT status,tool_metadata_seen FROM requests WHERE request_id='turn-1'").fetchone() == (
            "unknown", 0)
    assert len(calls) == 2


def test_tool_metadata_does_not_relax_same_remote_id_requirement(tmp_path):
    async def upstream(request):
        return refresh_response() if request.url.path.endswith("/refresh") else event_stream(
            metadata_event("SYNTHETIC_PRIVATE_TOOL_PAYLOAD", remote_id="remote-a"),
            text_event(json.dumps(decision(), ensure_ascii=False), remote_id="remote-b"))

    app, _, _ = app_with_mock(tmp_path, upstream)
    first = asyncio.run(post(app, request_body()))
    assert first.status_code == 502
    assert first.json()["error"]["code"] == "conversation_changed"
    assert asyncio.run(post(app, request_body())).status_code == 409
    with sqlite3.connect(tmp_path / "synthetic-ledger.sqlite3") as db:
        assert db.execute("SELECT status,tool_metadata_seen FROM requests WHERE request_id='turn-1'").fetchone() == (
            "unknown", 1)
        owners = dict(db.execute("SELECT remote_id,conversation_key FROM remote_ids"))
        assert owners == {"remote-a": "a" * 64, "remote-b": "a" * 64}

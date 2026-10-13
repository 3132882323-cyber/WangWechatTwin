import json
from dataclasses import replace
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from app import auto_voice_reply as voice
from app.config import OutboundVoiceSettings
from app.outbound_voice import PreparedVoiceArtifact
from app.voice_reply_policy import OwnerHumanVoiceStats


def plan(**changes):
    values = dict(authorized=True, action="send", incoming_text="请发个语音吧",
                  reply_text="外面有点凉，你出门记得多穿点。", local_hour=14)
    values.update(changes)
    return voice.plan_auto_voice(**values)


def test_standing_authorization_and_send_action_are_required():
    assert plan().eligible
    assert not plan(authorized=False).eligible
    assert not plan(authorized="true").eligible
    for action in ("hold", "review", "ignore"):
        assert not plan(action=action).eligible
    assert not plan(tool_metadata_seen=True).eligible
    assert not plan(review_required=True).eligible
    assert not plan(facts_to_confirm=["本人尚未确认"]).eligible


@pytest.mark.parametrize("changes", [
    {"risk": "medium"}, {"confidence": .79}, {"asr_uncertain": True},
    {"chat_type": "group"}, {"local_hour": 23}, {"local_hour": None},
    {"incoming_text": "发个语音吧，但我现在不方便听"},
    {"incoming_text": "以后发个语音吧"}, {"reply_text": "地址是西湖路。"},
    {"professional": True}, {"emotionally_activated": True},
])
def test_auto_mode_preserves_existing_modality_boundaries(changes):
    assert not plan(**changes).eligible


def test_history_does_not_turn_every_text_reply_into_voice():
    stats = OwnerHumanVoiceStats(12, 5, "owner_confirmed_wechat_history", True, True, 30, 2)
    values = dict(incoming_text="外面有点冷", familiar_contact=True, owner_voice_stats=stats)
    assert not plan(**values).eligible
    assert plan(**values, incoming_type="voice").eligible
    assert not plan(**{**values, "owner_voice_stats": replace(stats, verified_human_only=False)},
                    incoming_type="voice").eligible


@pytest.fixture
def harness(tmp_path):
    calls = []
    clock = [0.0]

    def generate(**kwargs):
        calls.append(("generate", kwargs))
        output = kwargs["output_dir"]
        output.mkdir(parents=True, exist_ok=True)
        (output / (kwargs["request_id"] + ".raw.wav")).write_bytes(b"synthetic-private-audio")
        return {"ok": True, "request_id": kwargs["request_id"], "native_exit_code": 0,
                "owned_voice_confirmed": True, "uploaded": False, "wechat_sent": False}

    def encode(**kwargs):
        calls.append(("encode", kwargs))
        root = kwargs["private_output_dir"] / kwargs["request_id"]
        return PreparedVoiceArtifact(kwargs["request_id"], root / "voice.silk", root / "roundtrip.wav",
                                     root / "manifest.json", "a" * 64,
                                     kwargs["expected_source_sha256"], 24000, 2000, 2000, 100)

    def send(contact, artifact, request_id):
        calls.append(("send", contact, artifact, request_id))
        claim = json.loads((tmp_path / "private" / request_id / "claim.json").read_text())
        assert claim["status"] == "send_started" and claim["send_attempted"] is True
        return True

    values = dict(contact="synthetic-peer", event_id="incoming-one", reference_audio=tmp_path / "reference.wav",
                  assets_dir=tmp_path / "assets", private_output_dir=tmp_path / "private",
                  owned_voice_confirmed=True, transport_supported=True, send_prepared=send,
                  recheck=lambda: True, generate=generate, encode=encode, monotonic=lambda: clock[0])
    return values, calls, clock


def execute(harness, **changes):
    values, _, _ = harness
    return voice.execute_auto_voice(plan(), **{**values, **changes})


def test_strict_receipt_records_voice_once_and_ids_ignore_changed_text(harness):
    values, calls, _ = harness
    result = execute(harness)
    assert result.status == "sent" and [call[0] for call in calls] == ["generate", "encode", "send"]
    assert 1 <= calls[0][1]["timeout_seconds"] <= 60
    assert calls[1][1]["max_seconds"] == 15
    claim = values["private_output_dir"] / result.request_id / "claim.json"
    journal = json.loads(claim.read_text())
    assert journal["status"] == "sent" and journal["send_attempted"] is True
    assert "synthetic-peer" not in claim.read_text() and plan().reply_text not in claim.read_text()
    replay = voice.execute_auto_voice(plan(reply_text="我会注意保暖，你也记得添衣。"), **values)
    assert replay.status == "unknown" and replay.code == "voice_request_already_claimed"
    assert len(calls) == 3


@pytest.mark.parametrize("changes, code", [
    ({"transport_supported": False}, "voice_transport_unsupported"),
    ({"send_prepared": None}, "voice_transport_unsupported"),
    ({"recheck": None}, "voice_transport_unsupported"),
    ({"owned_voice_confirmed": False}, "owned_voice_confirmation_required"),
    ({"contact": "synthetic@chatroom"}, "invalid_voice_request"),
    ({"event_id": ""}, "invalid_voice_request"),
    ({"timeout_seconds": 61}, "invalid_voice_request"),
    ({"recheck": lambda: False}, "voice_context_changed"),
])
def test_predispatch_failures_never_generate_or_send(harness, changes, code):
    values, calls, _ = harness
    result = execute(harness, **changes)
    assert result.status == "not_attempted" and result.code == code
    assert calls == [] and not values["private_output_dir"].exists()


@pytest.mark.parametrize('resources', [(2901, 6695), (8192, 4095)])
def test_default_generator_rejects_resources_before_claim_assets_or_tts(harness, monkeypatch, resources):
    from scripts import generate_voice_clone as generator
    monkeypatch.setattr(generator, 'resource_snapshot', lambda: generator.Resources(*resources, 0))
    monkeypatch.setattr(generator, 'verify_assets', lambda *args: pytest.fail('asset hash must not run'))
    monkeypatch.setattr(generator, 'run_tts', lambda *args, **kwargs: pytest.fail('TTS must not run'))
    result = execute(harness, generate=None)
    assert result.status == 'not_attempted' and result.code == 'voice_resources_unavailable'
    assert harness[1] == [] and not harness[0]['private_output_dir'].exists()
    # A later sufficiently resourced turn still uses the existing injected
    # generator; the admission check did not consume a permanent request ID.
    assert execute(harness).status == 'sent'


def test_resource_probe_failure_is_fixed_and_does_not_consume_request(harness, monkeypatch):
    from scripts import generate_voice_clone as generator

    def unavailable():
        raise OSError('private native diagnostic must not escape')

    monkeypatch.setattr(generator, 'resource_snapshot', unavailable)
    result = execute(harness, generate=None)
    assert result.status == 'not_attempted' and result.code == 'voice_resources_unavailable'
    assert not harness[0]['private_output_dir'].exists() and not harness[1]


def test_existing_possible_send_remains_unknown_even_when_resources_later_run_low(harness, monkeypatch):
    from scripts import generate_voice_clone as generator
    first = execute(harness, send_prepared=lambda *args: False)
    assert first.status == 'unknown'
    monkeypatch.setattr(generator, 'resource_snapshot', lambda: pytest.fail('old attempt must be read first'))
    replay = execute(harness, generate=None)
    assert replay.status == 'unknown' and replay.code == 'voice_request_already_claimed'
    assert len(harness[1]) == 2


def test_global_busy_slot_falls_back_without_waiting_or_claiming(harness):
    assert voice._VOICE_SLOT.acquire(blocking=False)
    try:
        result = execute(harness)
        assert result.status == "not_attempted" and result.code == "voice_busy"
        assert not harness[1]
    finally:
        voice._VOICE_SLOT.release()


def test_second_contact_uses_text_while_one_voice_worker_is_generating(harness):
    values, calls, _ = harness
    entered = threading.Event()
    release = threading.Event()
    original = values['generate']

    def held_generate(**kwargs):
        entered.set()
        assert release.wait(2)
        return original(**kwargs)

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(execute, harness, generate=held_generate)
        assert entered.wait(2)
        try:
            second = execute(harness, contact='another-synthetic-peer', event_id='another-incoming')
            assert second.status == 'not_attempted' and second.code == 'voice_busy'
            assert not first.done()
        finally:
            release.set()
        assert first.result(timeout=2).status == 'sent'
    assert [call[0] for call in calls] == ['generate', 'encode', 'send']


@pytest.mark.parametrize("phase", ["generate", "encode"])
def test_failure_before_sender_is_not_attempted_and_remains_permanently_claimed(harness, phase):
    values, calls, _ = harness

    def fail(**kwargs):
        raise RuntimeError("private speech and reference must not escape")

    result = execute(harness, **{phase: fail})
    assert result.status == "not_attempted" and result.code == ("voice_generation_failed" if phase == "generate"
                                                               else "voice_encoding_failed")
    assert all(call[0] != "send" for call in calls)
    assert "private speech" not in json.dumps(result.__dict__)
    assert execute(harness).status == "unknown"


@pytest.mark.parametrize("response", [False, None, "accepted", 1])
def test_only_exact_true_from_strict_receipt_sender_can_mean_sent(harness, response):
    result = execute(harness, send_prepared=lambda *args: response)
    assert result.status == "unknown" and result.code == "voice_receipt_unknown"
    assert execute(harness).status == "unknown"


def test_sender_exception_after_possible_dispatch_never_means_safe_text_fallback(harness):
    def sender(*args):
        raise TimeoutError("secret native body")

    result = execute(harness, send_prepared=sender)
    assert result.status == "unknown" and result.code == "voice_send_failed"
    assert execute(harness).status == "unknown"


def test_new_inbound_or_owner_reply_during_generation_prevents_native_send(harness):
    remaining = iter([True, True, False])
    result = execute(harness, recheck=lambda: next(remaining))
    assert result.status == "not_attempted" and result.code == "voice_context_changed"
    assert [call[0] for call in harness[1]] == ["generate"]


def test_stale_after_encoding_is_never_dispatched(harness):
    values, calls, clock = harness
    encode = values["encode"]

    def slow_encode(**kwargs):
        artifact = encode(**kwargs)
        clock[0] = 60
        return artifact

    result = execute(harness, encode=slow_encode)
    assert result.status == "not_attempted" and result.code == "voice_context_changed"
    assert [call[0] for call in calls] == ["generate", "encode"]


def test_context_is_rechecked_again_after_encoding(harness):
    remaining = iter([True, True, True, False])
    result = execute(harness, recheck=lambda: next(remaining))
    assert result.status == 'not_attempted' and result.code == 'voice_context_changed'
    assert [call[0] for call in harness[1]] == ['generate', 'encode']


def test_send_claim_failure_stops_before_the_native_callback(harness, monkeypatch):
    original = voice._save_claim

    def fail_before_dispatch(path, payload):
        if payload['status'] == 'send_started':
            raise OSError('synthetic disk failure')
        return original(path, payload)

    monkeypatch.setattr(voice, '_save_claim', fail_before_dispatch)
    result = execute(harness)
    assert result.status == 'not_attempted' and result.code == 'voice_recheck_failed'
    assert [call[0] for call in harness[1]] == ['generate', 'encode']


def test_journal_failure_after_verified_dispatch_is_unknown_without_retry(harness, monkeypatch):
    original = voice._save_claim

    def fail_after_dispatch(path, payload):
        if payload['status'] == 'sent':
            raise OSError('synthetic disk failure')
        return original(path, payload)

    monkeypatch.setattr(voice, '_save_claim', fail_after_dispatch)
    result = execute(harness)
    assert result.status == 'unknown' and result.code == 'voice_send_failed'
    assert [call[0] for call in harness[1]] == ['generate', 'encode', 'send']
    assert execute(harness).status == 'unknown'


@pytest.mark.parametrize("field, value", [
    ("duration_ms", 16000), ("duration_ms", True), ("voice_sent", True),
    ("request_id", "another-turn"), ("source_sha256", "b" * 64),
])
def test_unverified_or_long_artifact_never_reaches_sender(harness, field, value):
    encode = harness[0]["encode"]
    result = execute(harness, encode=lambda **kwargs: replace(encode(**kwargs), **{field: value}))
    assert result.status == "not_attempted" and result.code == "voice_artifact_unverified"
    assert all(call[0] != "send" for call in harness[1])


def test_public_outputs_and_network_paths_are_rejected_before_io(harness):
    for path in (voice._SOURCE_ROOT / ".runtime" / "must-not-create", Path("//synthetic-share/private")):
        result = execute(harness, private_output_dir=path)
        assert result.status == "not_attempted" and result.code == "voice_preflight_failed"
    assert harness[1] == []


@pytest.mark.parametrize("changes", [{"event_id": "\ud800"}, {"monotonic": lambda: float("nan")}])
def test_invalid_unicode_or_clock_does_not_take_the_voice_slot(harness, changes):
    result = execute(harness, **changes)
    assert result.status == "not_attempted" and result.code == "invalid_voice_request"
    assert execute(harness).status == "sent"


def test_config_defaults_off_and_enabled_requires_private_setup_and_voice_confirmation():
    assert OutboundVoiceSettings().enabled is False
    with pytest.raises(ValueError, match="confirmed owner voice"):
        OutboundVoiceSettings(enabled=True)
    with pytest.raises(ValueError, match="private local paths"):
        OutboundVoiceSettings(enabled=True, owned_voice_confirmed=True)
    with pytest.raises(ValueError, match="pinned runtime manifest"):
        OutboundVoiceSettings(enabled=True, owned_voice_confirmed=True, reference_audio="private/ref",
                              assets_dir="private/assets", output_dir="private/output", runtime_manifest="private/runtime")

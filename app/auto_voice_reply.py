"""Local ordinary voice replies under an explicit standing owner instruction.

Selection reuses the review desk's modality policy. Execution is synchronous in
one existing reply worker, with a nonblocking process-wide voice slot. The
caller supplies a native transport that returns True only for a strict receipt.
No account, reference recording, private path, native DLL or sender is bundled.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Callable, Literal

from app.outbound_voice import PreparedVoiceArtifact, prepare_voice_artifact
from app.voice_reply_policy import MAX_VOICE_SECONDS, suggest_reply_medium


_VOICE_SLOT = threading.Lock()
_SOURCE_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class AutoVoicePlan:
    eligible: bool
    reason: str
    reply_text: str
    estimated_voice_seconds: float | None = None


@dataclass(frozen=True)
class AutoVoiceReplyResult:
    status: Literal["sent", "not_attempted", "unknown"]
    code: str
    request_id: str = ""


def auto_voice_authorized(config, db) -> bool:
    policy = db.get_state("voice_auto_reply_authorization")
    return bool(
        config.outbound_voice.enabled is True and isinstance(policy, dict)
        and policy.get("source") == "owner_instruction"
        and policy.get("ordinary_low_risk") is True
        and policy.get("skip_previews") is True
    )


def plan_auto_voice(*, authorized: bool, action: str, reply_text: str = "",
                    tool_metadata_seen: bool = False, **policy_inputs) -> AutoVoicePlan:
    """Interpret a voice suggestion as eligibility only under standing consent.

    The original recommendation remains unchanged for review-only users. A
    send action must already have passed the pipeline's content and mode gates.
    """
    if authorized is not True or action != "send" or tool_metadata_seen is not False:
        return AutoVoicePlan(False, "voice_auto_not_authorized_or_review_required", reply_text)
    advice = suggest_reply_medium(reply_text=reply_text, **policy_inputs)
    return AutoVoicePlan(advice.medium == "voice_review", advice.reason,
                         reply_text, advice.estimated_voice_seconds)


def plan_pipeline_auto_voice(config, db, message, decision) -> AutoVoicePlan:
    """Load only this contact's confirmed manual history for modality selection."""
    if not auto_voice_authorized(config, db):
        return AutoVoicePlan(False, "voice_auto_disabled", decision.reply)
    try:
        origin = json.loads(message.raw_summary or "{}")
        if not isinstance(origin, dict):
            return AutoVoicePlan(False, "voice_source_metadata_uncertain", decision.reply)
    except (TypeError, ValueError):
        return AutoVoicePlan(False, "voice_source_metadata_uncertain", decision.reply)
    incoming_type = origin.get("original_type") or message.message_type
    from app.voice_reply_habits import load_owner_voice_habits
    stats = load_owner_voice_habits(
        config.resolve(config.paths.personal_database), config.resolve(config.paths.history_reader),
        [message.contact], confirmation=db.get_state("voice_habit_confirmation"),
    ).get(message.contact)
    established = bool(stats and stats.same_contact is True and stats.verified_human_only is True)
    profile = config.contact_map().get(message.contact)
    return plan_auto_voice(
        authorized=True, action=decision.action, reply_text=decision.reply,
        tool_metadata_seen=decision._tool_metadata_seen,
        incoming_text=message.content, incoming_type=incoming_type, chat_type=message.chat_type,
        risk=decision.risk, confidence=decision.confidence, facts_to_confirm=decision.facts_to_confirm,
        review_required=(decision._manual_review_required or origin.get("owner_corrected_input") is True
                         or origin.get("resume_incomplete") is True),
        asr_uncertain=(origin.get("asr_uncertain") is not False if incoming_type == "voice"
                       else origin.get("asr_uncertain") is True),
        local_hour=datetime.now(timezone(timedelta(hours=8))).hour,
        familiar_contact=(bool(profile and profile.relationship in {"friend", "family", "close_friend"})
                          or established),
        owner_voice_stats=stats,
    )


def _private_local(path: Path) -> Path:
    path = Path(path).expanduser()
    if str(path).startswith(("\\\\", "//")):
        raise ValueError("voice_paths_must_be_private_local")
    path = path.resolve()
    if (str(path).startswith(("\\\\", "//")) or path == _SOURCE_ROOT
            or _SOURCE_ROOT in path.parents):
        raise ValueError("voice_paths_must_be_private_local")
    return path


def _save_claim(path: Path, payload: dict) -> None:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix="claim-", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(payload, stream, ensure_ascii=True, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def execute_auto_voice(
    plan: AutoVoicePlan, *, contact: str, event_id: str, reference_audio: Path,
    assets_dir: Path, private_output_dir: Path, owned_voice_confirmed: bool,
    transport_supported: bool, send_prepared: Callable | None,
    recheck: Callable[[], bool] | None,
    generate: Callable | None = None, encode: Callable | None = None,
    timeout_seconds: float = 60.0, monotonic: Callable[[], float] | None = None,
) -> AutoVoiceReplyResult:
    """Generate once, encode once, then invoke the native callback at most once.

    ``recheck`` must revalidate pause, authorization and the same incoming turn
    before generation and again before dispatch. It must not create a new send
    context. False/exception from a called sender is unknown, so no text fallback
    or retry is permitted. Only ``not_attempted`` permits a normal text reply.
    Generation retains the existing full asset/resource/CLI validation and its
    owned-child timeout cleanup. The outer deadline also rejects stale results.
    """
    def result(status, code, request_id=""):
        return AutoVoiceReplyResult(status, code, request_id)

    if not isinstance(plan, AutoVoicePlan) or plan.eligible is not True:
        return result("not_attempted", "voice_not_selected")
    if transport_supported is not True or not callable(send_prepared) or not callable(recheck):
        return result("not_attempted", "voice_transport_unsupported")
    if owned_voice_confirmed is not True:
        return result("not_attempted", "owned_voice_confirmation_required")
    if (not isinstance(contact, str) or not contact or len(contact) > 512
            or contact.endswith("@chatroom") or not isinstance(event_id, str)
            or not event_id or len(event_id) > 512
            or type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or not 1 <= timeout_seconds <= 60):
        return result("not_attempted", "invalid_voice_request")
    now = monotonic or time.monotonic
    try:
        request_id = hashlib.sha256(json.dumps([contact, event_id], ensure_ascii=False,
                                              separators=(",", ":")).encode("utf-8")).hexdigest()
        started = now()
        if type(started) not in (int, float) or not math.isfinite(started):
            return result("not_attempted", "invalid_voice_request")
        deadline = started + timeout_seconds
    except Exception:
        return result("not_attempted", "invalid_voice_request")
    if not _VOICE_SLOT.acquire(blocking=False):
        return result("not_attempted", "voice_busy")
    claim = None
    journal = None
    send_started = False
    stage = "preflight"

    def current() -> bool:
        return now() < deadline and recheck() is True

    def finish(status, code):
        if claim is not None:
            journal.update(status=status, code=code, send_attempted=send_started)
            _save_claim(claim, journal)
        return result(status, code, request_id)

    try:
        output = _private_local(private_output_dir)
        reference = _private_local(reference_audio)
        assets = _private_local(assets_dir)
        root = output / request_id
        # Existing permanent attempts outrank a new admission failure: they
        # must never become safe text fallbacks when resources later run low.
        if root.exists():
            return result("unknown", "voice_request_already_claimed", request_id)
        if generate is None:
            # Fast admission check before permanent claims or expensive asset
            # hashing. The real generator still repeats the full checks and
            # takes a fresh resource reading immediately before model loading.
            from scripts.generate_voice_clone import resource_snapshot, _require_resources
            try:
                _require_resources(resource_snapshot())
            except Exception:
                return result("not_attempted", "voice_resources_unavailable", request_id)
        if not current():
            return result("not_attempted", "voice_context_changed", request_id)
        output.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            return result("unknown", "voice_request_already_claimed", request_id)
        claim = root / "claim.json"
        journal = {"request_id": request_id, "status": "preparing", "send_attempted": False,
                   "reply_sha256": hashlib.sha256(plan.reply_text.encode("utf-8")).hexdigest()}
        _save_claim(claim, journal)
        text_file = root / "text.private.txt"
        with text_file.open("x", encoding="utf-8") as stream:
            stream.write(plan.reply_text)
            stream.flush()
            os.fsync(stream.fileno())
        from scripts.generate_voice_clone import generate_preview, load_text
        if load_text(text_file) != plan.reply_text:
            return finish("not_attempted", "voice_text_changed")
        if not current():
            return finish("not_attempted", "voice_context_changed")
        stage = "generation"
        # Keep each permanent ID only once in a path; deeply nested duplicated
        # hashes can exceed Windows path limits even with ordinary local roots.
        preview_dir = output / "previews"
        report = (generate or generate_preview)(
            reference_audio=reference, i_own_this_voice=True, text_file=text_file,
            assets_dir=assets, request_id=request_id, output_dir=preview_dir,
            timeout_seconds=max(1.0, deadline - now()),
        )
        if (not isinstance(report, dict) or report.get("ok") is not True
                or report.get("request_id") != request_id or type(report.get("native_exit_code")) is not int
                or report.get("native_exit_code") != 0
                or report.get("owned_voice_confirmed") is not True
                or report.get("uploaded") is not False or report.get("wechat_sent") is not False):
            return finish("not_attempted", "voice_generation_unverified")
        if not current():
            return finish("not_attempted", "voice_context_changed")
        stage = "encoding"
        wav = preview_dir / (request_id + ".raw.wav")
        if wav.is_symlink() or not wav.is_file() or not 0 < wav.stat().st_size <= 4 * 1024 * 1024:
            return finish("not_attempted", "voice_wav_unavailable")
        wav_sha256 = hashlib.sha256(wav.read_bytes()).hexdigest()
        artifact = (encode or prepare_voice_artifact)(
            source_wav=wav, private_output_dir=output / "encoded", request_id=request_id,
            expected_source_sha256=wav_sha256, max_seconds=MAX_VOICE_SECONDS,
        )
        if (not isinstance(artifact, PreparedVoiceArtifact) or artifact.request_id != request_id
                or artifact.source_sha256 != wav_sha256 or artifact.voice_sent is not False
                or type(artifact.duration_ms) is not int
                or not 1000 <= artifact.duration_ms <= MAX_VOICE_SECONDS * 1000):
            return finish("not_attempted", "voice_artifact_unverified")
        stage = "recheck"
        if not current():
            return finish("not_attempted", "voice_context_changed")
        # Persist before entering the callback. A callback can dispatch and then
        # fail or lose its response; such outcomes must never become text sends.
        journal.update(status="send_started", send_attempted=True,
                       silk_sha256=artifact.silk_sha256)
        _save_claim(claim, journal)
        send_started = True
        stage = "send"
        verified = send_prepared(contact, artifact, request_id)
        return finish("sent" if verified is True else "unknown",
                      "voice_receipt_verified" if verified is True else "voice_receipt_unknown")
    except Exception:
        status = "unknown" if send_started else "not_attempted"
        code = "voice_" + stage + "_failed"
        if claim is not None:
            try:
                return finish(status, code)
            except OSError:
                pass  # The permanent directory still prevents every retry.
        return result(status, code, request_id)
    finally:
        _VOICE_SLOT.release()

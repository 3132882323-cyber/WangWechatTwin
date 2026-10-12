"""Offline preview contract, resource/child mocks, and synthetic local audio."""

import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import generate_voice_clone as voice


SECRET = "SYNTHETIC_PRIVATE_NATIVE_OUTPUT_429"


def assets_for(tmp_path):
    root = tmp_path / "assets"
    binary_dir = root / "bin" / "b10679-cuda12.4"
    binary_dir.mkdir(parents=True, exist_ok=True)
    return voice.Assets(root, binary_dir, binary_dir / "llama-tts.exe",
                        root / "models" / voice.MODEL_NAME, root / "models" / voice.MMPROJ_NAME)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"synthetic-owned-reference")
    text_file = tmp_path / "text.txt"
    text_file.write_text("你好，明天见。", encoding="utf-8")
    output = tmp_path / "private-preview"
    assets = assets_for(tmp_path)
    calls = []
    monkeypatch.setattr(voice, "verify_assets", lambda path: assets)
    monkeypatch.setattr(voice, "verify_cli", lambda _: None)
    monkeypatch.setattr(voice, "resource_snapshot", lambda: voice.Resources(8192, 6144, 1))
    monkeypatch.setattr(voice, "probe_audio", lambda path, *, expected: 1.1 if expected != "mp3" else 1.12)

    def run(assets, reference, text, wav, log, resources, checkpoint):
        calls.append("run")
        assert json.loads((output / "preview_01.attempt.json").read_text())["status"] == "pending"
        checkpoint("running", child_pid=42)
        wav.write_bytes(b"synthetic-wav")
        log.write_text(SECRET + "\ngenerated 12 frames, 13 bytes of WAV audio (24000 Hz)\n", encoding="utf-8")
        checkpoint("tts_exit", native_exit_code=0)
        return 0

    monkeypatch.setattr(voice, "run_tts", run)
    monkeypatch.setattr(voice, "convert_wav_to_mp3", lambda source, destination:
                        destination.write_bytes(b"synthetic-mp3"))
    args = dict(reference_audio=reference, i_own_this_voice=True, text_file=text_file,
                assets_dir=assets.root, output_dir=output, request_id="preview_01")
    return SimpleNamespace(args=args, output=output, calls=calls, assets=assets)


def test_complete_preview_is_private_no_upload_or_sender_and_id_never_reused(harness):
    result = voice.generate_preview(**harness.args)
    assert result["ok"] is True and result["wechat_sent"] is False and result["uploaded"] is False
    assert result["quality_not_yet_human_verified"] is True
    assert result["native_exit_code"] == 0 and result["release"] == "b10679"
    assert Path(result["audio_path"]).read_bytes() == b"synthetic-mp3"
    assert result["audio_sha256"] == hashlib.sha256(b"synthetic-mp3").hexdigest()
    serialized = json.dumps(result)
    assert SECRET not in serialized and "reference.wav" not in serialized and "你好" not in serialized
    assert json.loads((harness.output / "preview_01.attempt.json").read_text())["status"] == "done"
    assert json.loads((harness.output / "preview_01.progress.json").read_text())["phase"] == "complete"
    with pytest.raises(voice.PreviewFailure, match="request_already_recorded"):
        voice.generate_preview(**harness.args)
    assert harness.calls == ["run"]


@pytest.mark.parametrize("confirmed", [False, None, "true", 1])
def test_explicit_owned_voice_confirmation_precedes_any_file_or_resource(harness, monkeypatch, confirmed):
    monkeypatch.setattr(voice, "load_text", lambda _: pytest.fail("must not read"))
    with pytest.raises(voice.PreviewFailure, match="owned_voice_confirmation_required"):
        voice.generate_preview(**{**harness.args, "i_own_this_voice": confirmed})
    assert not harness.output.exists()


@pytest.mark.parametrize("bad_id", ["", "../escape", "id:one", "x" * 65, "汉字", "CON", "lpt9", None])
def test_request_id_preflight(harness, bad_id):
    with pytest.raises(voice.PreviewFailure, match="invalid_request_id"):
        voice.generate_preview(**{**harness.args, "request_id": bad_id})
    assert not harness.output.exists() and not harness.calls


@pytest.mark.parametrize("text", ["先休息。明天再聊。", "First. Second.", "第一句｡第二句｡", "先说；再说"])
def test_multi_sentence_is_rejected_before_claim_and_loading(harness, monkeypatch, text):
    harness.args["text_file"].write_text(text, encoding="utf-8")
    monkeypatch.setattr(voice, "verify_assets", lambda _: pytest.fail("must not load/check native"))
    with pytest.raises(voice.PreviewFailure, match="single_sentence_required"):
        voice.generate_preview(**harness.args)
    assert not harness.output.exists()


@pytest.mark.parametrize("text", ["", "第一句\n第二句", "x" * 161, "hello\x00"])
def test_bad_text_is_not_claimed(harness, text):
    harness.args["text_file"].write_text(text, encoding="utf-8")
    with pytest.raises(voice.PreviewFailure, match="invalid_preview_text"):
        voice.generate_preview(**harness.args)
    assert not harness.output.exists()


def test_public_output_or_reference_is_refused_but_ignored_runtime_is_private(tmp_path, monkeypatch, harness):
    public = tmp_path / "public"
    public.mkdir()
    monkeypatch.setattr(voice, "PUBLIC_ROOT", public)
    for name, value, error in [("output_dir", public / "previews", "output_must_be_private"),
                               ("reference_audio", public / "voice.wav", "reference_must_be_private")]:
        with pytest.raises(voice.PreviewFailure, match=error):
            voice.generate_preview(**{**harness.args, name: value})
    assert voice._private_path(public / ".runtime" / "voice_clones", code="private") == public / ".runtime" / "voice_clones"


@pytest.mark.parametrize("status", ["pending", "unknown", "done"])
def test_any_existing_claim_is_permanent_without_inspecting_or_resuming(harness, monkeypatch, status):
    harness.output.mkdir()
    claim = harness.output / "preview_01.attempt.json"
    claim.write_text(json.dumps({"status": status}), encoding="utf-8")
    monkeypatch.setattr(voice, "verify_assets", lambda _: pytest.fail("must not resubmit"))
    with pytest.raises(voice.PreviewFailure, match="request_already_recorded"):
        voice.generate_preview(**harness.args)
    assert json.loads(claim.read_text())["status"] == status


def test_native_crash_has_exact_exit_and_failed_checkpoint_unknown_no_retry(harness, monkeypatch):
    def crash(*args):
        args[-1]("tts_exit", native_exit_code=3221225477)
        raise voice.PreviewFailure("tts_process_failed", phase="tts_exit", native_exit_code=3221225477)

    monkeypatch.setattr(voice, "run_tts", crash)
    with pytest.raises(voice.PreviewFailure, match="tts_process_failed") as failed:
        voice.generate_preview(**harness.args)
    assert failed.value.native_exit_code == 3221225477
    report = json.loads((harness.output / "preview_01.report.json").read_text())
    progress = json.loads((harness.output / "preview_01.progress.json").read_text())
    assert report["phase"] == progress["failed_phase"] == "tts_exit"
    assert report["native_exit_code"] == progress["native_exit_code"] == 3221225477
    assert progress["phase"] == "unknown"
    assert json.loads((harness.output / "preview_01.attempt.json").read_text())["status"] == "unknown"
    assert not (harness.output / "preview_01.mp3").exists()
    with pytest.raises(voice.PreviewFailure, match="request_already_recorded"):
        voice.generate_preview(**harness.args)


def test_conversion_failure_preserves_successful_native_exit_and_does_not_publish(harness, monkeypatch):
    def fail(*args):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(voice, "convert_wav_to_mp3", fail)
    with pytest.raises(voice.PreviewFailure, match="local_preview_unknown"):
        voice.generate_preview(**harness.args)
    report = (harness.output / "preview_01.report.json").read_text()
    assert SECRET not in report
    assert json.loads(report)["phase"] == "conversion" and json.loads(report)["native_exit_code"] == 0
    assert not (harness.output / "preview_01.mp3").exists()


def test_conversion_duration_mismatch_is_unknown(harness, monkeypatch):
    monkeypatch.setattr(voice, "probe_audio", lambda path, *, expected: 9 if expected == "mp3" else 1)
    with pytest.raises(voice.PreviewFailure, match="converted_audio_invalid"):
        voice.generate_preview(**harness.args)
    assert not (harness.output / "preview_01.mp3").exists()


@pytest.mark.parametrize("resources", [voice.Resources(7167, 5000, 0), voice.Resources(9000, 4095, 0)])
def test_preclaim_resource_thresholds(harness, monkeypatch, resources):
    monkeypatch.setattr(voice, "resource_snapshot", lambda: resources)
    with pytest.raises(voice.PreviewFailure, match="insufficient_local_memory"):
        voice.generate_preview(**harness.args)
    assert not harness.output.exists() and not harness.calls


class FakeChild:
    pid = 4242

    def __init__(self, exit_code=None, *, terminate_timeout=False):
        self.returncode = exit_code
        self.operations = []
        self.terminate_timeout = terminate_timeout

    def poll(self):
        return self.returncode

    def terminate(self):
        self.operations.append("terminate")
        if not self.terminate_timeout:
            self.returncode = -15

    def kill(self):
        self.operations.append("kill")
        self.returncode = -9

    def wait(self, timeout):
        self.operations.append("wait")
        if self.returncode is None:
            raise subprocess.TimeoutExpired("synthetic-child", timeout)
        return self.returncode


def run_fake(tmp_path, *, child, commits=(8192,), gpu_free_mib=5000,
             clock=(0, 0), checkpoint=None):
    assets = assets_for(tmp_path)
    calls = []
    readings = iter(commits)
    times = iter(clock)

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return child

    checkpoints = []
    callback = checkpoint or (lambda phase, **details: checkpoints.append((phase, details)))
    result = voice.run_tts(assets, tmp_path / "private-reference.wav", "你好，明天见。",
                          tmp_path / "raw.wav", tmp_path / "private.log", voice.Resources(8192, 5000, 1),
                          callback, popen=popen, commit_reader=lambda: next(readings),
                          resource_reader=lambda: voice.Resources(next(readings), gpu_free_mib, 1),
                          monotonic=lambda: next(times), sleep=lambda _: pytest.fail("no unmocked wait"))
    return result, calls, checkpoints


def test_child_is_local_pinned_command_single_visible_gpu_no_shell(tmp_path):
    result, calls, checkpoints = run_fake(tmp_path, child=FakeChild(0))
    command, options = calls[0]
    assert result == 0 and len(calls) == 1
    assert command[command.index("-c") + 1] == "4096"
    assert command[command.index("-t") + 1] == "4"
    assert command[command.index("--tts-speaker-file") + 1].endswith("private-reference.wav")
    assert "-hf" not in command and "shell" not in options
    assert options["env"]["HF_HUB_OFFLINE"] == "1"
    assert options["env"]["CUDA_VISIBLE_DEVICES"] == "1"
    assert checkpoints[-1] == ("tts_exit", {"child_pid": 4242, "native_exit_code": 0})


def test_fresh_commit_gate_never_spawns_child(tmp_path):
    child = FakeChild(0)
    with pytest.raises(voice.PreviewFailure, match="insufficient_local_memory"):
        run_fake(tmp_path, child=child, commits=(7167,))
    assert child.operations == [] and not (tmp_path / "private.log").exists()


def test_fresh_gpu_gate_never_spawns_child(tmp_path):
    child = FakeChild(0)
    with pytest.raises(voice.PreviewFailure, match="insufficient_local_memory"):
        run_fake(tmp_path, child=child, gpu_free_mib=4095)
    assert child.operations == [] and not (tmp_path / "private.log").exists()


@pytest.mark.parametrize("needs_kill", [False, True])
def test_low_commit_stops_only_this_owned_child_and_preserves_exit(tmp_path, needs_kill):
    child = FakeChild(terminate_timeout=needs_kill)
    with pytest.raises(voice.PreviewFailure, match="commit_watchdog_stop") as failed:
        run_fake(tmp_path, child=child, commits=(8192, 1023), clock=(0, 0))
    assert child.operations == (["terminate", "wait", "kill", "wait"] if needs_kill else ["terminate", "wait"])
    assert failed.value.native_exit_code == (-9 if needs_kill else -15)


def test_timeout_stops_own_child(tmp_path):
    child = FakeChild()
    with pytest.raises(voice.PreviewFailure, match="tts_timeout"):
        run_fake(tmp_path, child=child, commits=(8192,), clock=(0, 301))
    assert child.operations == ["terminate", "wait"]


def test_native_nonzero_is_never_masked_as_complete(tmp_path):
    child = FakeChild(1)
    with pytest.raises(voice.PreviewFailure, match="tts_process_failed") as failed:
        run_fake(tmp_path, child=child)
    assert failed.value.native_exit_code == 1 and child.operations == []


def test_checkpoint_failure_after_spawn_stops_child(tmp_path):
    child = FakeChild()

    def checkpoint(phase, **details):
        if phase == "running":
            raise OSError(SECRET)

    with pytest.raises(voice.PreviewFailure, match="tts_launch_or_monitor_failed") as failed:
        run_fake(tmp_path, child=child, checkpoint=checkpoint)
    assert child.operations == ["terminate", "wait"] and SECRET not in str(failed.value)


def test_child_stop_failure_does_not_hide_original_watchdog_failure(tmp_path):
    child = FakeChild()

    def terminate():
        raise OSError(SECRET)

    child.terminate = terminate
    with pytest.raises(voice.PreviewFailure, match="commit_watchdog_stop") as failed:
        run_fake(tmp_path, child=child, commits=(8192, 0))
    assert failed.value.fields()["child_stop_failed"] is True
    assert SECRET not in json.dumps(failed.value.fields())


def test_journal_failure_is_explicit_and_pending_claim_still_blocks(harness, monkeypatch):
    def save(*args):
        raise OSError(SECRET)

    monkeypatch.setattr(voice, "_save_json", save)
    with pytest.raises(voice.PreviewFailure, match="local_preview_unknown") as failed:
        voice.generate_preview(**harness.args)
    assert failed.value.fields()["journal_failed"] is True
    assert json.loads((harness.output / "preview_01.attempt.json").read_text())["status"] == "pending"
    assert harness.calls == []
    with pytest.raises(voice.PreviewFailure, match="request_already_recorded"):
        voice.generate_preview(**harness.args)


def test_resource_probe_selects_gpu_with_headroom_using_mocked_native_tool(monkeypatch):
    monkeypatch.setattr(voice, "free_commit_mib", lambda: 8192)
    monkeypatch.setattr(voice.subprocess, "run", lambda command, **options:
                        SimpleNamespace(returncode=0, stdout="0, 1000\n1, 6000\n", stderr=""))
    assert voice.resource_snapshot() == voice.Resources(8192, 6000, 1)


@pytest.mark.parametrize("frames", [256, 257])
def test_frame_cap_is_unknown_even_when_native_exit_is_zero(harness, monkeypatch, frames):
    previous = voice.run_tts

    def capped(*args):
        code = previous(*args)
        args[4].write_text(f"generated {frames} frames, 13 bytes of WAV audio (24000 Hz)\n")
        return code

    monkeypatch.setattr(voice, "run_tts", capped)
    with pytest.raises(voice.PreviewFailure, match="tts_frame_limit_reached"):
        voice.generate_preview(**harness.args)
    report = json.loads((harness.output / "preview_01.report.json").read_text())
    assert report["native_exit_code"] == 0 and report["phase"] == "tts_result"
    assert not (harness.output / "preview_01.mp3").exists()


@pytest.mark.parametrize("line", ["", "generated 12 frames, 999 bytes of WAV audio (24000 Hz)",
                                  "generated 0 frames, 13 bytes of WAV audio (24000 Hz)",
                                  "generated 12 frames, 13 bytes of WAV audio (8000 Hz)"])
def test_unverified_native_result_is_unknown(harness, monkeypatch, line):
    previous = voice.run_tts

    def unverified(*args):
        code = previous(*args)
        args[4].write_text(line + "\n")
        return code

    monkeypatch.setattr(voice, "run_tts", unverified)
    with pytest.raises(voice.PreviewFailure, match="tts_result_unverified"):
        voice.generate_preview(**harness.args)
    assert not (harness.output / "preview_01.mp3").exists()


def pinned_fixture(tmp_path, monkeypatch):
    assets = assets_for(tmp_path)
    assets.model.parent.mkdir()
    assets.model.write_bytes(b"synthetic-pinned-model")
    assets.mmproj.write_bytes(b"synthetic-pinned-mmproj")
    monkeypatch.setattr(voice, "MODEL_SHA256", voice._sha256(assets.model))
    monkeypatch.setattr(voice, "MMPROJ_SHA256", voice._sha256(assets.mmproj))
    downloads = assets.root / "downloads"
    downloads.mkdir()
    pins = {}
    for number, name in enumerate(voice.ARCHIVE_SHA256):
        path = downloads / (name + (".part" if number else ""))
        names = sorted(voice.REQUIRED_BINARIES)
        subset = names[:3] if number == 0 else names[3:]
        with zipfile.ZipFile(path, "w") as archive:
            for binary_name in subset:
                content = ("official-synthetic-" + binary_name).encode()
                archive.writestr("release/" + binary_name, content)
                (assets.bin_dir / binary_name).write_bytes(content)
        pins[name] = voice._sha256(path)
    monkeypatch.setattr(voice, "ARCHIVE_SHA256", pins)
    return assets


def test_pinned_model_archives_and_extracted_contents_all_verified(tmp_path, monkeypatch):
    assets = pinned_fixture(tmp_path, monkeypatch)
    assert voice.verify_assets(assets.root) == assets
    assets.executable.write_bytes(b"unapproved-native-code")
    with pytest.raises(voice.PreviewFailure, match="pinned_binary_invalid"):
        voice.verify_assets(assets.root)


def test_extra_loadable_dll_is_refused(tmp_path, monkeypatch):
    assets = pinned_fixture(tmp_path, monkeypatch)
    (assets.bin_dir / "extra.dll").write_bytes(b"foreign")
    with pytest.raises(voice.PreviewFailure, match="pinned_binary_invalid"):
        voice.verify_assets(assets.root)


def test_model_tampering_fails_before_native_invocation(tmp_path, monkeypatch):
    assets = pinned_fixture(tmp_path, monkeypatch)
    assets.model.write_bytes(b"tampered")
    with pytest.raises(voice.PreviewFailure, match="pinned_model_invalid"):
        voice.verify_assets(assets.root)


def test_missing_packaged_dll_cannot_fall_back_to_unverified_path(tmp_path, monkeypatch):
    assets = pinned_fixture(tmp_path, monkeypatch)
    archive_name = next(iter(voice.ARCHIVE_SHA256))
    path = assets.root / "downloads" / archive_name
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("ggml-base.dll", b"synthetic-pinned-dependency")
    monkeypatch.setattr(voice, "ARCHIVE_SHA256", {**voice.ARCHIVE_SHA256,
                                                archive_name: voice._sha256(path)})
    with pytest.raises(voice.PreviewFailure, match="pinned_binary_unavailable"):
        voice.verify_assets(assets.root)


def test_version_and_required_flags_are_checked_with_mocked_native_process(tmp_path, monkeypatch):
    results = iter([SimpleNamespace(returncode=0, stdout="build: 10679\n", stderr=""),
                    SimpleNamespace(returncode=0, stdout="--tts-speaker-file --tts-lang --mmproj --ctx-size --threads --gpu-layers", stderr="")])
    monkeypatch.setattr(voice.subprocess, "run", lambda *args, **kwargs: next(results))
    voice.verify_cli(assets_for(tmp_path))


def test_cli_native_exit_is_reported_without_raw_native_body(tmp_path, monkeypatch, capsys):
    def failure(**kwargs):
        raise voice.PreviewFailure("tts_process_failed", phase="tts_exit", native_exit_code=1)

    monkeypatch.setattr(voice, "generate_preview", failure)
    assert voice.main(["--reference-audio", str(tmp_path / SECRET), "--i-own-this-voice",
                       "--text-file", str(tmp_path / "text.txt"), "--assets-dir", str(tmp_path),
                       "--request-id", "preview_01"]) == 1
    stdout = capsys.readouterr().out
    assert SECRET not in stdout and json.loads(stdout)["native_exit_code"] == 1


def test_local_sine_wav_mp3_and_silence_validation_without_models(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("local ffmpeg and ffprobe required")
    wav = tmp_path / "sine.wav"
    mp3 = tmp_path / "sine.mp3.part"
    silent = tmp_path / "silent.wav"
    for destination, source in [(wav, "sine=frequency=440:duration=0.5"),
                                (silent, "anullsrc=r=24000:cl=mono")]:
        result = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                                 "-f", "lavfi", "-i", source, "-t", "0.5", "-ar", "24000",
                                 "-ac", "1", "-c:a", "pcm_s16le", str(destination)],
                                capture_output=True, timeout=10, check=False)
        assert result.returncode == 0
    assert .2 <= voice.probe_audio(wav, expected="reference") <= 1
    voice.convert_wav_to_mp3(wav, mp3)
    assert .2 <= voice.probe_audio(mp3, expected="mp3") <= 1
    with pytest.raises(voice.PreviewFailure, match="audio_empty_or_silent"):
        voice.probe_audio(silent, expected="wav")

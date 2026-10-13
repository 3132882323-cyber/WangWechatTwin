"""Local-only Tencent SILK preparation tests; no owner audio or WeChat sender."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import math
from pathlib import Path, PureWindowsPath
import struct
import threading
from types import SimpleNamespace
import wave

import pytest

from app import outbound_voice as voice


def wav_file(tmp_path, *, rate=24000, frames=24001, channels=1, width=2,
             amplitude=4000, name="synthetic-private.wav"):
    path = tmp_path / name
    pcm = b"".join(struct.pack("<h", round(amplitude * math.sin(2 * math.pi * 220 * i / rate)))
                   for i in range(frames))
    with wave.open(str(path), "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(width)
        target.setframerate(rate)
        target.writeframes(pcm)
    return path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FakeCodec:
    def __init__(self, *, mode=None):
        self.mode = mode
        self.calls = 0
        self.pcm = b""
        self.options = None

    def encode(self, source, output, rate, bit_rate, **options):
        self.calls += 1
        self.pcm = source.read()
        self.options = options
        assert bit_rate == 24000
        assert len(self.pcm) % (rate // 50 * 2) == 0
        assert options == {"max_internal_sample_rate": 24000, "packet_loss_percentage": 0,
                           "complexity": 2, "use_inband_fec": False, "use_dtx": False, "tencent": True}
        if self.mode == "encode_error":
            raise RuntimeError("DO_NOT_LOG_SYNTHETIC_CODEC_SECRET")
        count = len(self.pcm) // (rate // 50 * 2)
        data = voice.TENCENT_HEADER + (struct.pack("<h", 4) + b"FAKE") * count
        if self.mode == "ordinary_header":
            data = data[1:]
        elif self.mode == "truncated_packet":
            data = data[:-1]
        elif self.mode == "missing_packet":
            data = data[:-6]
        elif self.mode == "extra_byte":
            data += b"\x00"
        elif self.mode == "oversize_silk":
            data += b"x" * voice.MAX_SILK_BYTES
        output.write(data)

    def decode(self, source, output, rate):
        if self.mode == "decode_error":
            raise RuntimeError("DO_NOT_LOG_SYNTHETIC_CODEC_SECRET")
        pcm = self.pcm
        if self.mode == "silent_decoded":
            pcm = b"\x00" * len(pcm)
        elif self.mode == "short_decoded":
            pcm = pcm[:-2]
        elif self.mode == "long_decoded":
            pcm += b"\x00\x00"
        output.write(pcm)


@pytest.fixture
def fake_codec(monkeypatch):
    codec = FakeCodec()
    monkeypatch.setattr(voice, "_load_codec", lambda: codec)
    return codec


def prepare(tmp_path, *, request_id="local_voice_001", path=None):
    path = path or wav_file(tmp_path)
    return voice.prepare_voice_artifact(path, tmp_path / "artifacts-private", request_id, sha(path))


def test_artifact_complete_roundtrip_and_private_manifest(tmp_path, fake_codec):
    source = wav_file(tmp_path, frames=34201, name="private-source-name-never-in-manifest.wav")
    artifact = prepare(tmp_path, path=source)
    manifest = json.loads(artifact.manifest_path.read_text())
    assert artifact.voice_sent is False
    assert artifact.source_duration_ms == 1425
    assert artifact.duration_ms == 1440
    assert artifact.packet_count == 72
    assert artifact.sample_rate == 24000
    assert manifest["pcm"]["padding_frames"] == 359
    assert manifest["pcm"]["frames"] == 34560
    assert manifest["voice_sent"] is False
    assert manifest["send_authorized"] is False
    assert manifest["native_send_verified"] is False
    assert manifest["source"]["sha256"] == sha(source)
    assert manifest["silk"]["sha256"] == sha(artifact.silk_path)
    assert manifest["roundtrip"]["sha256"] == sha(artifact.roundtrip_wav_path)
    assert source.name not in artifact.manifest_path.read_text()
    assert str(source) not in artifact.manifest_path.read_text()
    assert set(p.name for p in artifact.manifest_path.parent.iterdir()) == {
        "claim.json", "manifest.json", "voice.silk", "roundtrip.wav"}
    assert fake_codec.pcm[-359 * 2:] == b"\x00" * (359 * 2)
    assert voice.load_prepared_voice_artifact(artifact.manifest_path) == artifact
    assert fake_codec.calls == 1


@pytest.mark.parametrize("rate", voice.SUPPORTED_SAMPLE_RATES)
def test_real_installed_pysilk_roundtrip_with_tail_padding(tmp_path, rate):
    codec = pytest.importorskip("pysilk")
    if getattr(codec, "__version__", None) != "0.2.8":
        pytest.skip("This artifact protocol validates silk-python 0.2.8")
    source = wav_file(tmp_path, rate=rate, frames=rate + 1)
    artifact = prepare(tmp_path, path=source)
    assert artifact.packet_count == 51
    assert artifact.duration_ms == 1020
    silk = artifact.silk_path.read_bytes()
    assert silk.startswith(b"\x02#!SILK_V3")
    assert voice.inspect_tencent_silk(silk) == 51
    with wave.open(str(artifact.roundtrip_wav_path), "rb") as decoded:
        assert (decoded.getframerate(), decoded.getnchannels(), decoded.getsampwidth()) == (rate, 1, 2)
        assert decoded.getnframes() == rate // 50 * 51
        decoded_pcm = decoded.readframes(decoded.getnframes())
    independently_decoded = io.BytesIO()
    codec.decode(io.BytesIO(silk), independently_decoded, rate)
    assert independently_decoded.getvalue() == decoded_pcm
    assert voice._pcm_metrics(decoded_pcm)["rms_dbfs"] > -55
    assert voice.load_prepared_voice_artifact(artifact.manifest_path) == artifact


@pytest.mark.parametrize(("kwargs", "code"), [
    ({"channels": 2}, "mono_pcm16_wav_required"),
    ({"width": 1}, "mono_pcm16_wav_required"),
    ({"rate": 44100}, "unsupported_sample_rate"),
    ({"frames": 4000}, "voice_duration_out_of_range"),
    ({"frames": 24000 * 31}, "voice_duration_out_of_range"),
    ({"amplitude": 0}, "source_silent_or_too_quiet"),
    ({"amplitude": 5}, "source_silent_or_too_quiet"),
])
def test_source_preflight_before_claim(tmp_path, fake_codec, kwargs, code):
    source = wav_file(tmp_path, **kwargs)
    with pytest.raises(voice.VoiceArtifactFailure, match=code):
        prepare(tmp_path, path=source)
    assert not (tmp_path / "artifacts-private").exists()
    assert fake_codec.calls == 0


def test_truncated_wav_preflight(tmp_path, fake_codec):
    source = wav_file(tmp_path)
    source.write_bytes(source.read_bytes()[:-5])
    with pytest.raises(voice.VoiceArtifactFailure, match="truncated_wav"):
        prepare(tmp_path, path=source)
    assert fake_codec.calls == 0


def test_wav_hash_required_and_checked_before_claim(tmp_path, fake_codec):
    source = wav_file(tmp_path)
    for expected, code in [(None, "source_sha256_required"), ("x", "source_sha256_required"),
                           ("0" * 64, "source_sha256_mismatch")]:
        with pytest.raises(voice.VoiceArtifactFailure, match=code):
            voice.prepare_voice_artifact(source, tmp_path / "artifacts-private", "hash_001", expected)
    assert not (tmp_path / "artifacts-private").exists()
    assert fake_codec.calls == 0


@pytest.mark.parametrize("limit", [float("inf"), float("nan"), 61, 0.1, 0.2, 0.99, True, "30"])
def test_duration_limit_requires_finite_bounded_number(tmp_path, fake_codec, limit):
    source = wav_file(tmp_path)
    with pytest.raises(voice.VoiceArtifactFailure, match="invalid_duration_limit"):
        voice.prepare_voice_artifact(source, tmp_path / "artifacts-private", "duration_001", sha(source), max_seconds=limit)
    assert fake_codec.calls == 0


@pytest.mark.parametrize("request_id", ["", "../other", "a/b", "CON", "com1", "LPT9", "x" * 65])
def test_invalid_ids_do_not_touch_audio_or_claims(tmp_path, request_id):
    with pytest.raises(voice.VoiceArtifactFailure, match="invalid_request_id"):
        voice.prepare_voice_artifact(tmp_path / "missing.wav", tmp_path / "private", request_id, "0" * 64)
    assert not (tmp_path / "private").exists()


def test_no_public_source_tree_outputs_or_network_shares(tmp_path, fake_codec):
    source = wav_file(tmp_path)
    for output in [voice._SOURCE_ROOT / "outputs" / "voice-private", Path("//server/share/voice")]:
        with pytest.raises(voice.VoiceArtifactFailure, match="output_must_be_private_local"):
            voice.prepare_voice_artifact(source, output, "private_001", sha(source))
    with pytest.raises(voice.VoiceArtifactFailure, match="source_wav_must_be_private_local"):
        voice.prepare_voice_artifact(voice._SOURCE_ROOT / "synthetic.wav", tmp_path / "private", "private_002", "0" * 64)
    assert fake_codec.calls == 0


@pytest.mark.parametrize("target", ["source", "output", "manifest"])
def test_local_link_resolving_to_unc_is_rejected_before_audio_io(tmp_path, fake_codec, monkeypatch, target):
    # Simulate Windows' resolved result without requiring symlink privilege or
    # touching a real network share. Exercise each public entry point.
    source = wav_file(tmp_path)
    output = tmp_path / "artifacts-private"
    manifest = tmp_path / "local_manifest_link" / "manifest.json"
    selected = {"source": source, "output": output, "manifest": manifest}[target]
    original_resolve = Path.resolve
    def resolve(path, *args, **kwargs):
        if path == selected:
            return PureWindowsPath(r"\\synthetic-server\share\private-audio")
        return original_resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(voice, "_read_bytes", lambda *args: pytest.fail("UNC target must not be opened"))
    if target == "manifest":
        with pytest.raises(voice.VoiceArtifactFailure, match="manifest_must_be_private_local"):
            voice.load_prepared_voice_artifact(manifest)
    else:
        expected_code = "source_wav_must_be_private_local" if target == "source" else "output_must_be_private_local"
        with pytest.raises(voice.VoiceArtifactFailure, match=expected_code):
            voice.prepare_voice_artifact(source, output, "unc_link_001", sha(source))
    assert fake_codec.calls == 0
    assert not output.exists()


@pytest.mark.parametrize("frames", [23999, 24000])
def test_native_candidate_preparation_minimum_is_one_second(tmp_path, fake_codec, frames):
    source = wav_file(tmp_path, frames=frames)
    if frames < 24000:
        with pytest.raises(voice.VoiceArtifactFailure, match="voice_duration_out_of_range"):
            prepare(tmp_path, path=source)
        assert fake_codec.calls == 0
        assert not (tmp_path / "artifacts-private").exists()
    else:
        artifact = prepare(tmp_path, path=source)
        assert artifact.duration_ms == 1000
        assert artifact.source_duration_ms == 1000


@pytest.mark.parametrize(("mode", "code"), [
    ("encode_error", "silk_encode_failed"),
    ("ordinary_header", "invalid_tencent_silk"),
    ("truncated_packet", "invalid_silk_packet"),
    ("missing_packet", "silk_packet_count_mismatch"),
    ("extra_byte", "truncated_silk_packet"),
    ("oversize_silk", "codec_output_too_large"),
    ("decode_error", "silk_decode_failed"),
    ("silent_decoded", "decoded_silent_or_too_quiet"),
    ("short_decoded", "decoded_duration_mismatch"),
    ("long_decoded", "codec_output_too_large"),
])
def test_failures_are_persistent_unknown_no_secret_and_no_retry(tmp_path, monkeypatch, mode, code):
    codec = FakeCodec(mode=mode)
    monkeypatch.setattr(voice, "_load_codec", lambda: codec)
    source = wav_file(tmp_path)
    with pytest.raises(voice.VoiceArtifactFailure, match=code) as failure:
        prepare(tmp_path, path=source)
    assert "SECRET" not in str(failure.value)
    request_dir = tmp_path / "artifacts-private" / "local_voice_001"
    claim = json.loads((request_dir / "claim.json").read_text())
    assert claim["status"] == "unknown"
    assert claim["error_code"] == code
    assert claim["voice_sent"] is False
    assert not (request_dir / "manifest.json").exists()
    with pytest.raises(voice.VoiceArtifactFailure, match="request_id_already_claimed"):
        prepare(tmp_path, path=source)
    assert codec.calls == 1


@pytest.mark.parametrize("status", ["pending", "unknown", "done", None, "broken"])
def test_existing_claim_and_even_incomplete_directory_block_reencode(tmp_path, fake_codec, status):
    root = tmp_path / "artifacts-private" / "local_voice_001"
    root.mkdir(parents=True)
    if status == "broken":
        (root / "claim.json").write_bytes(b"{")
    elif status:
        (root / "claim.json").write_text(json.dumps({"status": status}))
    with pytest.raises(voice.VoiceArtifactFailure, match="request_id_already_claimed"):
        prepare(tmp_path)
    assert fake_codec.calls == 0


def test_done_reencode_rejected_but_loader_reads_without_codec(tmp_path, fake_codec, monkeypatch):
    artifact = prepare(tmp_path)
    with pytest.raises(voice.VoiceArtifactFailure, match="request_id_already_claimed"):
        prepare(tmp_path)
    monkeypatch.setattr(voice, "_load_codec", lambda: pytest.fail("Loading must never encode or decode"))
    assert voice.load_prepared_voice_artifact(artifact.manifest_path) == artifact
    assert fake_codec.calls == 1


def test_concurrent_same_id_claims_only_once(tmp_path, monkeypatch):
    codec = FakeCodec()
    barrier = threading.Barrier(2)
    def load():
        barrier.wait(timeout=5)
        return codec
    monkeypatch.setattr(voice, "_load_codec", load)
    source = wav_file(tmp_path)
    def attempt():
        try:
            return prepare(tmp_path, path=source)
        except voice.VoiceArtifactFailure as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=10) for future in [pool.submit(attempt), pool.submit(attempt)]]
    assert sum(isinstance(result, voice.PreparedVoiceArtifact) for result in results) == 1
    assert results.count("request_id_already_claimed") == 1
    assert codec.calls == 1


@pytest.mark.parametrize("filename", ["voice.silk", "roundtrip.wav"])
def test_artifact_byte_changes_detected(tmp_path, fake_codec, filename):
    artifact = prepare(tmp_path)
    path = artifact.manifest_path.parent / filename
    path.write_bytes(path.read_bytes()[:-1] + b"!")
    with pytest.raises(voice.VoiceArtifactFailure, match="artifact_hash_mismatch"):
        voice.load_prepared_voice_artifact(artifact.manifest_path)


def test_manifest_changes_or_unknown_claim_never_load(tmp_path, fake_codec):
    artifact = prepare(tmp_path)
    manifest_blob = artifact.manifest_path.read_bytes()
    artifact.manifest_path.write_bytes(manifest_blob + b" ")
    with pytest.raises(voice.VoiceArtifactFailure, match="artifact_not_prepared"):
        voice.load_prepared_voice_artifact(artifact.manifest_path)
    artifact.manifest_path.write_bytes(manifest_blob)
    claim_path = artifact.manifest_path.parent / "claim.json"
    claim = json.loads(claim_path.read_text())
    claim["status"] = "unknown"
    claim_path.write_text(json.dumps(claim))
    with pytest.raises(voice.VoiceArtifactFailure, match="artifact_not_prepared"):
        voice.load_prepared_voice_artifact(artifact.manifest_path)


@pytest.mark.parametrize("data", [b"", b"#!SILK_V3", voice.TENCENT_HEADER,
    voice.TENCENT_HEADER + b"\xff\xff", voice.TENCENT_HEADER + b"\x00\x00",
    voice.TENCENT_HEADER + b"\xff\x7f", voice.TENCENT_HEADER + b"\x01\x00"])
def test_silk_parser_rejects_incomplete_and_loss_frames(data):
    with pytest.raises(voice.VoiceArtifactFailure):
        voice.inspect_tencent_silk(data)


def test_missing_or_changed_codec_version_rejected(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "pysilk", None)
    with pytest.raises(voice.VoiceArtifactFailure, match="silk_python_unavailable"):
        voice._load_codec()
    monkeypatch.setitem(__import__("sys").modules, "pysilk", SimpleNamespace(__version__="99.0", encode=lambda: None, decode=lambda: None))
    with pytest.raises(voice.VoiceArtifactFailure, match="unsupported_codec_version"):
        voice._load_codec()


def test_cli_stdout_only_status_and_manifest_path(tmp_path, fake_codec, capsys):
    source = wav_file(tmp_path)
    assert voice.main(["--source-wav", str(source), "--output-dir", str(tmp_path / "private"),
                       "--request-id", "cli_001", "--expected-source-sha256", sha(source)]) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert set(result) == {"status", "manifest_path"}
    assert result["status"] == "prepared"
    assert captured.err == ""
    assert str(source) not in captured.out
    assert sha(source) not in captured.out


def test_cli_failure_is_fixed_code_without_source_contents(tmp_path, fake_codec, capsys):
    source = wav_file(tmp_path)
    assert voice.main(["--source-wav", str(source), "--output-dir", str(tmp_path / "private"),
                       "--request-id", "cli_002", "--expected-source-sha256", "0" * 64]) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"status": "failed"}
    assert captured.err.strip() == "source_sha256_mismatch"
    assert str(source) not in captured.out + captured.err

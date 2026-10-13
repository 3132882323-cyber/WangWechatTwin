"""Prepare a private Tencent SILK artifact; this module cannot send messages.

Uses the already-installed ``silk-python`` 0.2.8 API (``pysilk``). Protocol
reference: https://github.com/synodriver/pysilk/tree/b0fbba74506bb8be23ef5e120b983bbb2f70ac19
No upstream codec source or binary is copied or distributed here. Its package
metadata declares BSD; the underlying Skype SDK has its own BSD-style notice
and explicitly grants no patent rights. Check those notices when distributing
the codec, rather than treating this application's Apache license as theirs.

Keep one stable private output directory. A request directory is an exclusive,
persistent claim: pending, unknown, done, or even an incomplete claim cannot be
encoded again. Read a done artifact with ``load_prepared_voice_artifact``. A
prepared artifact does not establish native WeChat support or send approval.
Preparation requires at least one second of audio, matching the currently
observed native voice candidate range. Codec support for shorter frames alone
does not establish that those clips are acceptable native voice messages.
The path guard excludes this source tree and network shares; it cannot inspect
the caller's other publishing/sync settings or establish a Windows ACL. The
caller must select an unpublished private local directory.
"""

from __future__ import annotations

import argparse
from array import array
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import struct
import sys
import tempfile
import wave


TENCENT_HEADER = b"\x02#!SILK_V3"
SUPPORTED_SAMPLE_RATES = (8000, 12000, 16000, 24000)
SUPPORTED_CODEC_VERSION = "0.2.8"
MAX_WAV_BYTES = 4 * 1024 * 1024
MAX_SILK_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024
BIT_RATE = 24000
MIN_VOICE_SECONDS = 1.0
SCHEMA = "wechat_tencent_silk_artifact_v1"
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_ROOT = Path(__file__).resolve().parents[1]
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    prefix + str(number) for prefix in ("COM", "LPT") for number in range(1, 10)
}


class VoiceArtifactFailure(Exception):
    """A fixed error code, with no source path, speech, or codec exception text."""


@dataclass(frozen=True)
class PreparedVoiceArtifact:
    request_id: str
    silk_path: Path
    roundtrip_wav_path: Path
    manifest_path: Path
    silk_sha256: str
    source_sha256: str
    sample_rate: int
    source_duration_ms: int
    duration_ms: int
    packet_count: int
    voice_sent: bool = False


class _BoundedBuffer(io.BytesIO):
    def __init__(self, limit: int):
        super().__init__()
        self.limit = limit

    def write(self, data):
        if self.tell() + len(data) > self.limit:
            raise VoiceArtifactFailure("codec_output_too_large")
        return super().write(data)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _private_path(path: Path, *, code: str) -> Path:
    path = Path(path).expanduser()
    # Do not read or write network shares or resolve a caller's symlink into
    # the public source tree. The caller chooses a private local directory.
    if str(path).startswith(("\\\\", "//")):
        raise VoiceArtifactFailure(code)
    try:
        resolved = path.resolve()
    except OSError:
        raise VoiceArtifactFailure(code) from None
    # A local symlink/junction can resolve to a UNC share. Recheck the final
    # path before opening any audio, claim, manifest, or output directory.
    if str(resolved).startswith(("\\\\", "//")):
        raise VoiceArtifactFailure(code)
    if resolved == _SOURCE_ROOT or _SOURCE_ROOT in resolved.parents:
        raise VoiceArtifactFailure(code)
    return resolved


def _validate_request_id(request_id: str) -> None:
    if (not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id)
            or request_id.upper() in _RESERVED_NAMES):
        raise VoiceArtifactFailure("invalid_request_id")


def _read_bytes(path: Path, limit: int, code: str) -> bytes:
    try:
        with path.open("rb") as source:
            value = source.read(limit + 1)
        if len(value) > limit:
            raise VoiceArtifactFailure(code)
        return value
    except OSError:
        raise VoiceArtifactFailure(code) from None


def _pcm_metrics(pcm: bytes) -> dict:
    if not pcm or len(pcm) % 2:
        raise VoiceArtifactFailure("invalid_pcm_length")
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    peak = max(abs(value) for value in samples)
    rms = math.sqrt(sum(value * value for value in samples) / len(samples))
    active_fraction = sum(abs(value) >= 32 for value in samples) / len(samples)
    return {
        "peak_dbfs": round(20 * math.log10(max(peak, 1) / 32768), 4),
        "rms_dbfs": round(20 * math.log10(max(rms, 1) / 32768), 4),
        "active_fraction": round(active_fraction, 6),
    }


def _require_audible(metrics: dict, code: str) -> None:
    if (metrics["rms_dbfs"] < -55 or metrics["peak_dbfs"] < -45
            or metrics["active_fraction"] < 0.02):
        raise VoiceArtifactFailure(code)


def _read_wav(blob: bytes, max_seconds: float) -> tuple[bytes, int, int, dict]:
    try:
        with wave.open(io.BytesIO(blob), "rb") as source:
            rate, frames = source.getframerate(), source.getnframes()
            if (source.getnchannels() != 1 or source.getsampwidth() != 2
                    or source.getcomptype() != "NONE"):
                raise VoiceArtifactFailure("mono_pcm16_wav_required")
            if rate not in SUPPORTED_SAMPLE_RATES:
                raise VoiceArtifactFailure("unsupported_sample_rate")
            if not MIN_VOICE_SECONDS <= frames / rate <= max_seconds:
                raise VoiceArtifactFailure("voice_duration_out_of_range")
            pcm = source.readframes(frames + 1)
            if len(pcm) != frames * 2:
                raise VoiceArtifactFailure("truncated_wav")
    except (wave.Error, EOFError, struct.error):
        raise VoiceArtifactFailure("invalid_wav") from None
    metrics = _pcm_metrics(pcm)
    _require_audible(metrics, "source_silent_or_too_quiet")
    return pcm, rate, frames, metrics


def inspect_tencent_silk(data: bytes, *, expected_packet_count: int | None = None) -> int:
    """Parse every positive length and payload before letting C decode it.

    pysilk's Tencent encoder writes 20ms packets with no -1 terminator. A
    missing/trailing byte, zero/loss packet, ordinary SILK header, or -1 marker
    is not a complete artifact produced by this preparation protocol.
    """
    if not data.startswith(TENCENT_HEADER) or len(data) > MAX_SILK_BYTES:
        raise VoiceArtifactFailure("invalid_tencent_silk")
    offset, count = len(TENCENT_HEADER), 0
    while offset < len(data):
        if offset + 2 > len(data):
            raise VoiceArtifactFailure("truncated_silk_packet")
        length = struct.unpack_from("<h", data, offset)[0]
        offset += 2
        if not 1 <= length <= 1250 or offset + length > len(data):
            raise VoiceArtifactFailure("invalid_silk_packet")
        offset += length
        count += 1
        if count > 3000:
            raise VoiceArtifactFailure("too_many_silk_packets")
    if count == 0 or (expected_packet_count is not None and count != expected_packet_count):
        raise VoiceArtifactFailure("silk_packet_count_mismatch")
    return count


def _load_codec():
    try:
        import pysilk
    except ImportError:
        raise VoiceArtifactFailure("silk_python_unavailable") from None
    if (getattr(pysilk, "__version__", None) != SUPPORTED_CODEC_VERSION
            or not callable(getattr(pysilk, "encode", None))
            or not callable(getattr(pysilk, "decode", None))):
        raise VoiceArtifactFailure("unsupported_codec_version")
    return pysilk


def _write_private(path: Path, data: bytes) -> None:
    # All artifact files are new; never replace a prior artifact under an ID.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as target:
        target.write(data)
        target.flush()
        os.fsync(target.fileno())


def _write_claim(path: Path, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".claim-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _wav_bytes(pcm: bytes, rate: int) -> bytes:
    destination = io.BytesIO()
    with wave.open(destination, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(pcm)
    return destination.getvalue()


def _encode_and_decode(codec, pcm: bytes, rate: int) -> tuple[bytes, bytes, int]:
    packet_frames = rate // 50
    padding_frames = (-len(pcm) // 2) % packet_frames
    padded = pcm + b"\x00\x00" * padding_frames
    packet_count = len(padded) // (packet_frames * 2)
    encoded = _BoundedBuffer(min(MAX_SILK_BYTES, 10 + packet_count * 1252))
    try:
        codec.encode(io.BytesIO(padded), encoded, rate, BIT_RATE,
                     max_internal_sample_rate=24000, packet_loss_percentage=0,
                     complexity=2, use_inband_fec=False, use_dtx=False, tencent=True)
    except VoiceArtifactFailure:
        raise
    except Exception:
        raise VoiceArtifactFailure("silk_encode_failed") from None
    silk = encoded.getvalue()
    inspect_tencent_silk(silk, expected_packet_count=packet_count)
    decoded = _BoundedBuffer(len(padded))
    try:
        codec.decode(io.BytesIO(silk), decoded, rate)
    except VoiceArtifactFailure:
        raise
    except Exception:
        raise VoiceArtifactFailure("silk_decode_failed") from None
    roundtrip = decoded.getvalue()
    if len(roundtrip) != len(padded):
        raise VoiceArtifactFailure("decoded_duration_mismatch")
    _require_audible(_pcm_metrics(roundtrip), "decoded_silent_or_too_quiet")
    return silk, roundtrip, padding_frames


def prepare_voice_artifact(source_wav: Path, private_output_dir: Path,
                           request_id: str, expected_source_sha256: str, *,
                           max_seconds: float = 30.0) -> PreparedVoiceArtifact:
    """Only transform verified local PCM16; never invoke TTS, ASR, or a sender.

    The caller must use the same private directory for all request IDs. Output
    paths inside this application's source tree and network shares are refused.
    No reference recording, transcript, contact identity, or source path is
    written to the manifest. Files remain local and must not be published.
    The caller must confirm the output is unpublished; the path guard cannot
    identify other public or synchronized directories. Clips shorter than one
    second are refused even when the codec itself could encode them.
    """
    _validate_request_id(request_id)
    if not isinstance(expected_source_sha256, str) or not _SHA256.fullmatch(expected_source_sha256):
        raise VoiceArtifactFailure("source_sha256_required")
    if (isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float))
            or not math.isfinite(max_seconds) or not MIN_VOICE_SECONDS <= max_seconds <= 60):
        raise VoiceArtifactFailure("invalid_duration_limit")
    source_wav = _private_path(source_wav, code="source_wav_must_be_private_local")
    output = _private_path(private_output_dir, code="output_must_be_private_local")
    request_dir = output / request_id
    if request_dir.exists():
        raise VoiceArtifactFailure("request_id_already_claimed")
    blob = _read_bytes(source_wav, MAX_WAV_BYTES, "source_wav_unavailable_or_too_large")
    source_sha256 = _digest(blob)
    if source_sha256 != expected_source_sha256:
        raise VoiceArtifactFailure("source_sha256_mismatch")
    pcm, rate, source_frames, source_metrics = _read_wav(blob, max_seconds)
    codec = _load_codec()
    try:
        output.mkdir(mode=0o700, parents=True, exist_ok=True)
        request_dir.mkdir(mode=0o700)
    except FileExistsError:
        raise VoiceArtifactFailure("request_id_already_claimed") from None
    except OSError:
        raise VoiceArtifactFailure("private_output_unavailable") from None
    created = datetime.now(timezone.utc).isoformat()
    claim = {"schema": SCHEMA, "request_id": request_id, "status": "pending",
             "created_at": created, "source_sha256": source_sha256, "voice_sent": False}
    claim_path = request_dir / "claim.json"
    try:
        _write_claim(claim_path, claim)
        silk, decoded, padding_frames = _encode_and_decode(codec, pcm, rate)
        roundtrip_wav = _wav_bytes(decoded, rate)
        frames = len(decoded) // 2
        packet_count = frames // (rate // 50)
        duration_ms = packet_count * 20
        source_duration_ms = round(source_frames / rate * 1000)
        manifest = {
            "schema": SCHEMA, "request_id": request_id, "status": "prepared",
            "created_at": created, "voice_sent": False, "send_authorized": False,
            "native_send_verified": False, "expected_wechat_message_type": 34,
            "codec": {"package": "silk-python", "version": SUPPORTED_CODEC_VERSION,
                      "sample_rate": rate, "bit_rate": BIT_RATE, "tencent": True,
                      "packet_duration_ms": 20, "use_dtx": False, "use_inband_fec": False},
            "source": {"sha256": source_sha256, "bytes": len(blob), "format": "mono_pcm16_wav",
                       "frames": source_frames, "duration_ms": source_duration_ms, **source_metrics},
            "pcm": {"sha256": _digest(pcm), "padded_sha256": _digest(pcm + b"\x00\x00" * padding_frames),
                    "padding_frames": padding_frames, "frames": frames},
            "silk": {"filename": "voice.silk", "sha256": _digest(silk), "bytes": len(silk),
                     "header_hex": TENCENT_HEADER.hex(), "packet_count": packet_count,
                     "duration_ms": duration_ms},
            "roundtrip": {"filename": "roundtrip.wav", "sha256": _digest(roundtrip_wav),
                          "bytes": len(roundtrip_wav), "pcm_sha256": _digest(decoded),
                          "frames": frames, "duration_ms": duration_ms, **_pcm_metrics(decoded)},
        }
        _write_private(request_dir / "voice.silk", silk)
        _write_private(request_dir / "roundtrip.wav", roundtrip_wav)
        _write_private(request_dir / "manifest.json",
                       json.dumps(manifest, ensure_ascii=True, sort_keys=True, indent=2).encode("utf-8"))
        claim.update(status="done", manifest_sha256=_digest((request_dir / "manifest.json").read_bytes()))
        _write_claim(claim_path, claim)
        return load_prepared_voice_artifact(request_dir / "manifest.json")
    except BaseException as exc:
        claim.update(status="unknown", error_code=(str(exc) if isinstance(exc, VoiceArtifactFailure)
                                                     else "voice_preparation_interrupted"))
        try:
            _write_claim(claim_path, claim)
        except OSError:
            pass  # An incomplete persistent directory still blocks this ID.
        if isinstance(exc, (VoiceArtifactFailure, KeyboardInterrupt, SystemExit)):
            raise
        raise VoiceArtifactFailure("voice_preparation_failed") from None


def load_prepared_voice_artifact(manifest_path: Path) -> PreparedVoiceArtifact:
    """Read a done claim without encoding or retrying; verify every audio byte.

    This is a local artifact validator, not authentication or permission to
    send. A native sender must separately bind approval, contact and receipt.
    """
    manifest_path = _private_path(manifest_path, code="manifest_must_be_private_local")
    if manifest_path.name != "manifest.json":
        raise VoiceArtifactFailure("invalid_manifest_path")
    root = manifest_path.parent
    try:
        claim_blob = _read_bytes(root / "claim.json", MAX_MANIFEST_BYTES, "claim_unavailable")
        manifest_blob = _read_bytes(manifest_path, MAX_MANIFEST_BYTES, "manifest_unavailable")
        claim = json.loads(claim_blob)
        manifest = json.loads(manifest_blob)
        if (not isinstance(claim, dict) or not isinstance(manifest, dict)
                or claim.get("status") != "done" or claim.get("schema") != SCHEMA
                or claim.get("manifest_sha256") != _digest(manifest_blob)
                or manifest.get("schema") != SCHEMA or manifest.get("status") != "prepared"
                or claim.get("request_id") != root.name or manifest.get("request_id") != root.name
                or manifest.get("voice_sent") is not False or manifest.get("send_authorized") is not False
                or manifest.get("native_send_verified") is not False):
            raise VoiceArtifactFailure("artifact_not_prepared")
        _validate_request_id(root.name)
        source, silk_info, roundtrip_info, codec = (manifest[name] for name in ("source", "silk", "roundtrip", "codec"))
        if (silk_info["filename"] != "voice.silk" or roundtrip_info["filename"] != "roundtrip.wav"
                or source["sha256"] != claim["source_sha256"] or not _SHA256.fullmatch(source["sha256"])
                or codec["sample_rate"] not in SUPPORTED_SAMPLE_RATES or codec["tencent"] is not True
                or codec["version"] != SUPPORTED_CODEC_VERSION or codec["packet_duration_ms"] != 20):
            raise VoiceArtifactFailure("invalid_artifact_metadata")
        silk_path, wav_path = root / "voice.silk", root / "roundtrip.wav"
        if silk_path.is_symlink() or wav_path.is_symlink():
            raise VoiceArtifactFailure("artifact_symlink_refused")
        silk = _read_bytes(silk_path, MAX_SILK_BYTES, "silk_unavailable")
        wav_blob = _read_bytes(wav_path, MAX_WAV_BYTES, "roundtrip_unavailable")
        if (len(silk) != silk_info["bytes"] or _digest(silk) != silk_info["sha256"]
                or len(wav_blob) != roundtrip_info["bytes"] or _digest(wav_blob) != roundtrip_info["sha256"]):
            raise VoiceArtifactFailure("artifact_hash_mismatch")
        count = inspect_tencent_silk(silk, expected_packet_count=silk_info["packet_count"])
        pcm, rate, frames, _ = _read_wav(wav_blob, 60)
        if (rate != codec["sample_rate"] or frames != count * (rate // 50)
                or frames != roundtrip_info["frames"] or _digest(pcm) != roundtrip_info["pcm_sha256"]
                or silk_info["duration_ms"] != count * 20 or roundtrip_info["duration_ms"] != count * 20):
            raise VoiceArtifactFailure("invalid_artifact_duration")
        return PreparedVoiceArtifact(root.name, silk_path, wav_path, manifest_path,
                                     silk_info["sha256"], source["sha256"], rate,
                                     source["duration_ms"], count * 20, count)
    except VoiceArtifactFailure:
        raise
    except (KeyError, TypeError, ValueError, OSError):
        raise VoiceArtifactFailure("invalid_artifact_manifest") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare local Tencent SILK and decoded audition; does not send WeChat.")
    parser.add_argument("--source-wav", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path,
                        help="Caller-confirmed unpublished private local directory outside the source tree")
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--expected-source-sha256", required=True)
    parser.add_argument("--max-seconds", default=30.0, type=float)
    args = parser.parse_args(argv)
    try:
        prepared = prepare_voice_artifact(args.source_wav, args.output_dir, args.request_id,
                                          args.expected_source_sha256, max_seconds=args.max_seconds)
    except VoiceArtifactFailure as exc:
        print(json.dumps({"status": "failed"}))
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({"status": "prepared", "manifest_path": str(prepared.manifest_path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""One-shot local preview using an explicitly confirmed, owned voice.

Windows x64/CUDA 12.4 preview only. No downloader, uploader, ASR, WeChat sender,
or app routing is involved. Use a stable private output directory: an existing
pending/unknown/done request ID is never submitted again there. Reference audio,
native logs, checkpoints, and previews stay local; sound quality needs listening.

Assets layout (complete SHA-verified .zip.part archives also accepted):
  models/{Qwen3-TTS-12Hz-1.7B-Base-Q4_K_M,mmproj-Qwen3-TTS-12Hz-1.7B-Base-Q8_0}.gguf
  bin/b10679-cuda12.4/ (official executable and DLLs extracted by the caller)
  downloads/{llama-b10679-bin-win-cuda-12.4-x64,cudart-llama-bin-win-cuda-12.4-x64}.zip

Protocol/CLI reference (MIT; no upstream implementation copied):
https://github.com/ggml-org/llama.cpp/blob/b10679/tools/tts/README.md
https://github.com/ggml-org/llama.cpp/releases/tag/b10679
Model: https://huggingface.co/ggml-org/Qwen3-TTS-12Hz-1.7B-Base-GGUF
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path


PUBLIC_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = PUBLIC_ROOT / ".runtime" / "voice_clones"
RELEASE = "b10679"
MODEL_REPO = "ggml-org/Qwen3-TTS-12Hz-1.7B-Base-GGUF"
MODEL_REVISION = "ca27d74bc954b73dadab5b71ca265d87fc861a7c"
MODEL_NAME = "Qwen3-TTS-12Hz-1.7B-Base-Q4_K_M.gguf"
MMPROJ_NAME = "mmproj-Qwen3-TTS-12Hz-1.7B-Base-Q8_0.gguf"
MODEL_SHA256 = "8d18c94acb2addd042f97da63c98be144eafa76d0d9495177eab65130cf85129"
MMPROJ_SHA256 = "6fd65188839bcd6ecc91b277ad471e22a0edfada4699a0fe82f1165c18cfcce2"
ARCHIVE_SHA256 = {
    "llama-b10679-bin-win-cuda-12.4-x64.zip":
        "46e8c7f80b540befb10625f20c54b54777857e6c2df51c349312ccfb4a0a83fb",
    "cudart-llama-bin-win-cuda-12.4-x64.zip":
        "8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6",
}
REQUIRED_BINARIES = {"llama-tts.exe", "cudart64_12.dll", "cublas64_12.dll",
                     "cublasLt64_12.dll", "ggml-cuda.dll", "mtmd.dll"}
MIN_COMMIT_MIB = 7 * 1024
MIN_GPU_MIB = 4 * 1024
STOP_COMMIT_MIB = 1024
MAX_AUDIO_BYTES = 16 * 1024 * 1024
MAX_MP3_BYTES = 2 * 1024 * 1024
MAX_SECONDS = 30
FRAME_LIMIT = 256
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_RESERVED_IDS = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)}
_TERMINATORS = ".。!?！？;；…"


class PreviewFailure(Exception):
    """Fixed diagnostics; never include native stderr, input text, or reference."""

    def __init__(self, code: str, *, phase: str | None = None,
                 native_exit_code: int | None = None):
        super().__init__(code)
        self.phase = phase
        self.native_exit_code = native_exit_code
        self.journal_failed = False
        self.child_stop_failed = False

    def fields(self) -> dict:
        data = {"error_code": str(self)}
        if self.phase:
            data["phase"] = self.phase
        if type(self.native_exit_code) is int:
            data["native_exit_code"] = self.native_exit_code
        if self.journal_failed:
            data["journal_failed"] = True
        if self.child_stop_failed:
            data["child_stop_failed"] = True
        return data


@dataclass(frozen=True)
class Assets:
    root: Path
    bin_dir: Path
    executable: Path
    model: Path
    mmproj: Path


@dataclass(frozen=True)
class Resources:
    commit_free_mib: int
    gpu_free_mib: int
    gpu_index: int


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return _stream_sha256(stream)


def _stream_sha256(stream) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _private_path(path: Path, *, code: str) -> Path:
    path = path.expanduser().resolve()
    public = PUBLIC_ROOT.resolve()
    runtime = public / ".runtime"
    if (path == public or public in path.parents) and runtime not in path.parents:
        raise PreviewFailure(code)
    return path


def _request_id(value: str) -> str:
    if (not isinstance(value, str) or not _REQUEST_ID.fullmatch(value)
            or value.upper() in _RESERVED_IDS):
        raise PreviewFailure("invalid_request_id")
    return value


def load_text(path: Path) -> str:
    try:
        if path.stat().st_size > 1200:
            raise PreviewFailure("preview_text_too_large")
        text = path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError):
        raise PreviewFailure("preview_text_unavailable") from None
    if (not text or len(text) > 160 or not all(char.isprintable() for char in text)
            or "\n" in text or "\r" in text):
        raise PreviewFailure("invalid_preview_text")
    core = unicodedata.normalize("NFKC", text).rstrip(_TERMINATORS).strip()
    if not core or any(char in _TERMINATORS for char in core):
        raise PreviewFailure("single_sentence_required")
    return text


def verify_assets(directory: Path) -> Assets:
    root = directory.expanduser().resolve()
    binary_dir = root / "bin" / "b10679-cuda12.4"
    model, mmproj = root / "models" / MODEL_NAME, root / "models" / MMPROJ_NAME
    try:
        if _sha256(model) != MODEL_SHA256 or _sha256(mmproj) != MMPROJ_SHA256:
            raise PreviewFailure("pinned_model_invalid")
        archived: dict[str, str] = {}
        for filename, expected in ARCHIVE_SHA256.items():
            archive = root / "downloads" / filename
            if not archive.exists():
                archive = archive.with_name(archive.name + ".part")
            if _sha256(archive) != expected:
                raise PreviewFailure("pinned_archive_invalid")
            with zipfile.ZipFile(archive) as zipped:
                for item in zipped.infolist():
                    name = Path(item.filename).name
                    if not name.lower().endswith((".exe", ".dll")):
                        continue
                    with zipped.open(item) as stream:
                        digest = _stream_sha256(stream)
                    if name in archived and archived[name] != digest:
                        raise PreviewFailure("pinned_binary_ambiguous")
                    archived[name] = digest
        installed = {path.name: path for path in binary_dir.iterdir()
                     if path.is_file() and path.suffix.lower() in {".exe", ".dll"}}
        required = REQUIRED_BINARIES | {name for name in archived if name.lower().endswith(".dll")}
        if not required.issubset(installed):
            raise PreviewFailure("pinned_binary_unavailable")
        # Verify the actual executable and every adjacent loadable DLL, not
        # merely the original archives or a self-reported build number.
        for name, path in installed.items():
            if name not in archived or _sha256(path) != archived[name]:
                raise PreviewFailure("pinned_binary_invalid")
    except PreviewFailure:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
        raise PreviewFailure("pinned_asset_unavailable") from None
    return Assets(root, binary_dir, binary_dir / "llama-tts.exe", model, mmproj)


def _environment(assets: Assets, gpu_index: int | None = None) -> dict:
    environment = os.environ.copy()
    environment.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                        "HF_HUB_DISABLE_TELEMETRY": "1"})
    environment["PATH"] = str(assets.bin_dir) + os.pathsep + environment.get("PATH", "")
    if gpu_index is not None:
        environment["CUDA_VISIBLE_DEVICES"] = str(gpu_index)
    return environment


def _no_window_kwargs() -> dict[str, int]:
    """Background CLI tools must not flash a Windows console window."""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def verify_cli(assets: Assets) -> None:
    try:
        version = subprocess.run([str(assets.executable), "--version"], cwd=assets.bin_dir,
                                 env=_environment(assets), capture_output=True, text=True,
                                 timeout=8, check=False, **_no_window_kwargs())
        help_result = subprocess.run([str(assets.executable), "--help"], cwd=assets.bin_dir,
                                     env=_environment(assets), capture_output=True, text=True,
                                     timeout=8, check=False, **_no_window_kwargs())
        version_text = version.stdout + version.stderr
        help_text = help_result.stdout + help_result.stderr
        if (version.returncode != 0 or not re.search(r"\bbuild\s*:?\s*10679\b", version_text)
                or help_result.returncode != 0
                or not all(flag in help_text for flag in
                           ("--tts-speaker-file", "--tts-lang", "--mmproj",
                            "--ctx-size", "--threads", "--gpu-layers"))):
            raise PreviewFailure("cli_version_or_flags_unverified", phase="cli_verification",
                                 native_exit_code=version.returncode if version.returncode else help_result.returncode)
    except (OSError, subprocess.TimeoutExpired, TypeError):
        raise PreviewFailure("cli_unavailable") from None


def free_commit_mib() -> int:
    if os.name != "nt":
        raise PreviewFailure("windows_cuda_preview_required")

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    state = MemoryStatus()
    state.dwLength = ctypes.sizeof(state)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(state)):
        raise PreviewFailure("commit_unavailable")
    return state.ullAvailPageFile // (1024 * 1024)


def resource_snapshot() -> Resources:
    commit = free_commit_mib()
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=False, **_no_window_kwargs())
        values = []
        for line in result.stdout.splitlines():
            if line.strip():
                index, free = (int(part.strip()) for part in line.split(","))
                if index < 0 or free < 0:
                    raise ValueError
                values.append((free, index))
        if result.returncode != 0 or not values:
            raise PreviewFailure("gpu_unavailable")
        gpu, index = max(values, key=lambda value: (value[0], -value[1]))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise PreviewFailure("gpu_unavailable") from None
    return Resources(commit, gpu, index)


def _require_resources(resources: Resources) -> None:
    if (not isinstance(resources, Resources)
            or any(type(value) is not int or value < 0 for value in
                   (resources.commit_free_mib, resources.gpu_free_mib, resources.gpu_index))):
        raise PreviewFailure("resource_reading_invalid")
    if resources.commit_free_mib < MIN_COMMIT_MIB or resources.gpu_free_mib < MIN_GPU_MIB:
        raise PreviewFailure("insufficient_local_memory")


def build_command(assets: Assets, reference: Path, text: str, wav: Path) -> list[str]:
    return [str(assets.executable), "-m", str(assets.model), "--mmproj", str(assets.mmproj),
            "--tts-speaker-file", str(reference), "--tts-lang", "zh", "-p", text,
            "-o", str(wav), "-c", "4096", "-t", "4", "-b", "256", "-ub", "64",
            "-ngl", "99", "-n", str(FRAME_LIMIT)]


def _save_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".next")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _stop_child(child) -> None:
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)


def run_tts(assets: Assets, reference: Path, text: str, wav: Path, log_path: Path,
            resources: Resources, checkpoint, *, popen=None, commit_reader=None,
            resource_reader=None, monotonic=None, sleep=None) -> int:
    popen = popen or subprocess.Popen
    commit_reader = commit_reader or free_commit_mib
    resource_reader = resource_reader or resource_snapshot
    monotonic = monotonic or time.monotonic
    sleep = sleep or time.sleep
    # This reading is after asset/reference checks and immediately before the
    # child is spawned; a stale earlier high reading never authorizes loading.
    try:
        _require_resources(resources)
        fresh_resources = resource_reader()
        _require_resources(fresh_resources)
    except PreviewFailure as failure:
        failure.phase = "launch"
        raise
    checkpoint("launch", commit_free_mib=fresh_resources.commit_free_mib,
               gpu_free_mib=fresh_resources.gpu_free_mib, gpu_index=fresh_resources.gpu_index)
    child = None
    try:
        with log_path.open("xb") as log:
            child = popen(build_command(assets, reference, text, wav), cwd=assets.bin_dir,
                          env=_environment(assets, fresh_resources.gpu_index),
                          stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                          **_no_window_kwargs())
            started = monotonic()
            checkpoint("running", child_pid=child.pid)
            while child.poll() is None:
                if monotonic() - started > 300:
                    raise PreviewFailure("tts_timeout", phase="running")
                if commit_reader() < STOP_COMMIT_MIB:
                    raise PreviewFailure("commit_watchdog_stop", phase="running")
                checkpoint("running", child_pid=child.pid,
                           elapsed_seconds=int(monotonic() - started))
                sleep(2)
            checkpoint("tts_exit", child_pid=child.pid, native_exit_code=child.returncode)
            if child.returncode != 0:
                raise PreviewFailure("tts_process_failed", phase="tts_exit",
                                     native_exit_code=child.returncode)
            return child.returncode
    except BaseException as exc:
        stop_failed = False
        if child is not None:
            try:
                _stop_child(child)
            except (OSError, subprocess.TimeoutExpired):
                stop_failed = True
        if isinstance(exc, PreviewFailure):
            if child is not None:
                exc.native_exit_code = child.returncode
            exc.child_stop_failed = stop_failed
            raise
        code = "preview_interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "tts_launch_or_monitor_failed"
        failure = PreviewFailure(code, phase="running" if child else "launch",
                                 native_exit_code=child.returncode if child else None)
        failure.child_stop_failed = stop_failed
        raise failure from None


def parse_generated_wav(log: Path, wav: Path) -> int:
    """The pinned CLI can exit 0 at -n; require its final result below that cap."""
    try:
        if not 0 < log.stat().st_size <= 8 * 1024 * 1024:
            raise PreviewFailure("tts_result_unverified")
        output = log.read_text(encoding="utf-8", errors="replace")
        output = re.sub(r"\x1b\[[0-9;]*m", "", output)
        # b10679 common/log.cpp prints optional [M.s.ms.us] and INFO "I "
        # prefixes. Accept those exact formats, never an arbitrary line prefix.
        matches = re.findall(r"^(?:(?:[0-9]+\.[0-5][0-9]\.[0-9]{3}\.[0-9]{3} )?I )?"
                             r"generated (\d+) frames, (\d+) bytes of WAV audio \((\d+) Hz\)[ \t]*\r?$",
                             output, flags=re.MULTILINE)
        if len(matches) != 1:
            raise PreviewFailure("tts_result_unverified")
        frames, size, rate = map(int, matches[0])
        if frames >= FRAME_LIMIT:
            raise PreviewFailure("tts_frame_limit_reached")
        if frames < 1 or size != wav.stat().st_size or rate != 24000:
            raise PreviewFailure("tts_result_unverified")
        return frames
    except PreviewFailure:
        raise
    except (OSError, ValueError, TypeError):
        raise PreviewFailure("tts_result_unverified") from None


def probe_audio(path: Path, *, expected: str) -> float:
    ffprobe, ffmpeg = shutil.which("ffprobe"), shutil.which("ffmpeg")
    if not ffprobe or not ffmpeg:
        raise PreviewFailure("audio_tools_unavailable")
    try:
        if not 0 < path.stat().st_size <= MAX_AUDIO_BYTES:
            raise PreviewFailure("audio_invalid")
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries",
             "format=format_name,duration:stream=codec_name,codec_type,channels,sample_rate",
            "-of", "json", str(path)], capture_output=True, text=True, timeout=10, check=False,
            **_no_window_kwargs())
        data = json.loads(result.stdout)
        info = data.get("format", {})
        duration = float(info.get("duration"))
        formats = str(info.get("format_name", "")).split(",")
        streams = data.get("streams", [])
        if not isinstance(streams, list) or len(streams) != 1:
            raise PreviewFailure("audio_invalid")
        audio = streams[0]
        allowed = {"wav", "mp3"} if expected == "reference" else {expected}
        codec = audio.get("codec_name")
        if (result.returncode != 0 or not set(formats) & allowed
                or audio.get("codec_type") != "audio"
                or ("wav" in formats and codec not in {"pcm_s16le", "pcm_f32le"})
                or ("mp3" in formats and codec != "mp3")
                or audio.get("channels") not in {1, 2}
                or not 8000 <= int(audio.get("sample_rate")) <= 48000
                or not math.isfinite(duration) or not .2 <= duration <= MAX_SECONDS):
            raise PreviewFailure("audio_invalid")
        # Decoding a bounded 30 seconds also rejects silence/empty output;
        # a container duration or file size alone is not audio evidence.
        decoded = subprocess.run(
            [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "1",
             "-i", str(path), "-t", str(MAX_SECONDS), "-map", "0:a:0", "-vn",
             "-ac", "1", "-ar", "24000", "-f", "s16le", "-"],
            capture_output=True, timeout=15, check=False, **_no_window_kwargs())
        pcm = decoded.stdout
        if (decoded.returncode != 0 or not isinstance(pcm, bytes)
                or not 9600 <= len(pcm) <= MAX_SECONDS * 24000 * 2
                or not any(pcm)):
            raise PreviewFailure("audio_empty_or_silent")
        return duration
    except PreviewFailure:
        raise
    except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired):
        raise PreviewFailure("audio_invalid") from None


def convert_wav_to_mp3(wav: Path, destination: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise PreviewFailure("audio_tools_unavailable")
    try:
        result = subprocess.run(
            [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-n",
             "-i", str(wav), "-map", "0:a:0", "-vn", "-c:a", "libmp3lame",
             "-q:a", "4", "-f", "mp3", str(destination)],
            capture_output=True, timeout=25, check=False, **_no_window_kwargs())
    except (OSError, subprocess.TimeoutExpired):
        raise PreviewFailure("mp3_conversion_failed") from None
    if result.returncode != 0 or not destination.is_file():
        raise PreviewFailure("mp3_conversion_failed", native_exit_code=result.returncode)


def generate_preview(*, reference_audio: Path, i_own_this_voice: bool,
                     text_file: Path, assets_dir: Path, request_id: str,
                     output_dir: Path | None = None) -> dict:
    if i_own_this_voice is not True:
        raise PreviewFailure("owned_voice_confirmation_required")
    request_id = _request_id(request_id)
    output = _private_path(output_dir or DEFAULT_OUTPUT_DIR, code="output_must_be_private")
    reference = _private_path(reference_audio, code="reference_must_be_private")
    paths = {suffix: output / (request_id + suffix) for suffix in
             (".attempt.json", ".progress.json", ".report.json", ".raw.wav",
              ".mp3.part", ".mp3", ".private.log")}
    if any(path.exists() for path in paths.values()):
        raise PreviewFailure("request_already_recorded")
    text = load_text(text_file)
    assets = verify_assets(assets_dir)
    resources = resource_snapshot()
    _require_resources(resources)
    verify_cli(assets)
    probe_audio(reference, expected="reference")
    output.mkdir(parents=True, exist_ok=True)
    claim = paths[".attempt.json"]
    try:
        with claim.open("x", encoding="utf-8") as stream:
            json.dump({"request_id": request_id, "status": "pending"}, stream)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        raise PreviewFailure("request_already_recorded") from None
    phase = "claimed"
    native_exit_code = None
    load_resources = {}

    def checkpoint(new_phase: str, **details) -> None:
        nonlocal phase
        phase = new_phase
        if new_phase == "launch":
            load_resources.update(details)
        _save_json(paths[".progress.json"], {"request_id": request_id, "phase": phase, **details})

    try:
        checkpoint("claimed")
        exit_code = run_tts(assets, reference, text, paths[".raw.wav"],
                            paths[".private.log"], resources, checkpoint)
        native_exit_code = exit_code
        if type(exit_code) is not int or exit_code != 0:
            raise PreviewFailure("tts_process_failed", native_exit_code=exit_code)
        checkpoint("tts_result", native_exit_code=exit_code)
        frames_generated = parse_generated_wav(paths[".private.log"], paths[".raw.wav"])
        checkpoint("wav_validation", native_exit_code=exit_code)
        wav_duration = probe_audio(paths[".raw.wav"], expected="wav")
        checkpoint("conversion", native_exit_code=exit_code)
        convert_wav_to_mp3(paths[".raw.wav"], paths[".mp3.part"])
        checkpoint("mp3_validation", native_exit_code=exit_code)
        mp3_duration = probe_audio(paths[".mp3.part"], expected="mp3")
        if (not 0 < paths[".mp3.part"].stat().st_size <= MAX_MP3_BYTES
                or abs(mp3_duration - wav_duration) > max(.25, wav_duration * .05)):
            raise PreviewFailure("converted_audio_invalid")
        checkpoint("publishing", native_exit_code=exit_code)
        os.replace(paths[".mp3.part"], paths[".mp3"])
        result = {"ok": True, "request_id": request_id, "audio_path": str(paths[".mp3"]),
                  "audio_sha256": _sha256(paths[".mp3"]),
                  "audio_bytes": paths[".mp3"].stat().st_size,
                  "duration_seconds": round(mp3_duration, 3),
                  "wav_duration_seconds": round(wav_duration, 3),
                  "model": MODEL_REPO, "model_revision": MODEL_REVISION,
                  "release": RELEASE, "native_exit_code": exit_code,
                  "frames_generated": frames_generated, "frame_limit": FRAME_LIMIT,
                  "owned_voice_confirmed": True, "clone_mode": "local_reference_audio",
                  "quality_not_yet_human_verified": True, "wechat_sent": False,
                  "uploaded": False, "offline_local_assets": True,
                  "commit_free_mib_before_load": load_resources.get("commit_free_mib", resources.commit_free_mib),
                  "gpu_free_mib_before_load": load_resources.get("gpu_free_mib", resources.gpu_free_mib)}
        checkpoint("complete", native_exit_code=exit_code)
        _save_json(paths[".report.json"], result)
        _save_json(claim, {"request_id": request_id, "status": "done"})
        return result
    except BaseException as exc:
        failure = exc if isinstance(exc, PreviewFailure) else PreviewFailure(
            "preview_interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "local_preview_unknown")
        failure.phase = failure.phase or phase
        if failure.native_exit_code is None:
            failure.native_exit_code = native_exit_code
        diagnostic = {"ok": False, "request_id": request_id, **failure.fields(),
                      "wechat_sent": False, "uploaded": False}
        try:
            _save_json(claim, {"request_id": request_id, "status": "unknown"})
            _save_json(paths[".report.json"], diagnostic)
            failure_details = failure.fields()
            failure_details.pop("phase", None)
            _save_json(paths[".progress.json"], {"request_id": request_id,
                       "phase": "unknown", "failed_phase": failure.phase, **failure_details})
        except OSError:
            failure.journal_failed = True
        raise failure from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One-shot local preview of your explicitly confirmed voice")
    parser.add_argument("--reference-audio", required=True, type=Path, help="Private local WAV/MP3 of your own voice")
    parser.add_argument("--i-own-this-voice", required=True, action="store_true")
    parser.add_argument("--text-file", required=True, type=Path, help="UTF-8, one sentence, at most 160 characters")
    parser.add_argument("--assets-dir", required=True, type=Path, help="Pinned local Windows CUDA assets; no download")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--request-id", required=True, help="Unique permanent ID within the stable output directory")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        result = generate_preview(reference_audio=args.reference_audio,
                                  i_own_this_voice=args.i_own_this_voice, text_file=args.text_file,
                                  assets_dir=args.assets_dir, output_dir=args.output_dir,
                                  request_id=args.request_id)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except PreviewFailure as failure:
        print(json.dumps({"ok": False, **failure.fields(), "wechat_sent": False, "uploaded": False}))
        return 130 if str(failure) == "preview_interrupted" else 1
    except (OSError, ValueError, TypeError):
        print(json.dumps({"ok": False, "error_code": "local_preflight_failed",
                          "wechat_sent": False, "uploaded": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

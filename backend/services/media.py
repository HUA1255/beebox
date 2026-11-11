from __future__ import annotations

import json
import math
import os
import re
import subprocess
from pathlib import Path

from backend.services.errors import BeeBoxError
from backend.services.ffmpeg_tools import resolve_tools
from backend.services.process_utils import no_window_kwargs

SUPPORTED_MEDIA_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".mp3", ".wav", ".m4a", ".flac", ".ogg"}
DEFAULT_PROBE_TIMEOUT_SECONDS = 30
DEFAULT_UNKNOWN_MEDIA_TIMEOUT_SECONDS = 6 * 60 * 60

# Gate 2 contract: audio longer than 120 s runs in 120 s windows with a 2 s overlap.
CHUNK_WINDOW_MS = 120_000
CHUNK_OVERLAP_MS = 2_000
CHUNK_THRESHOLD_MS = CHUNK_WINDOW_MS


def _configured_timeout(default: int) -> int:
    try:
        return max(1, int(os.environ.get("BEEBOX_MEDIA_PROCESS_TIMEOUT_SECONDS", default)))
    except ValueError:
        return default


def _media_timeout(duration_ms: int | None = None) -> int:
    configured = os.environ.get("BEEBOX_MEDIA_PROCESS_TIMEOUT_SECONDS")
    if configured:
        return _configured_timeout(DEFAULT_UNKNOWN_MEDIA_TIMEOUT_SECONDS)
    if duration_ms and duration_ms > 0:
        return max(300, math.ceil(duration_ms / 1000 * 4 + 60))
    return DEFAULT_UNKNOWN_MEDIA_TIMEOUT_SECONDS


def _optional_nonnegative_float(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _optional_positive_int(value: object) -> int | None:
    parsed = _optional_nonnegative_float(value)
    return int(parsed) if parsed is not None and parsed > 0 else None


def probe_media(path: Path) -> dict:
    ffprobe = resolve_tools(check_version=False)["ffprobe"]
    command = [
        ffprobe.path or "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    if not ffprobe.available:
        raise BeeBoxError("FFPROBE_FAILED", f"ffprobe is required to inspect media files: {ffprobe.error or 'not found'}", stage="preprocessing", command=" ".join(command))
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=True,
            encoding="utf-8",
            errors="replace",
            timeout=_configured_timeout(DEFAULT_PROBE_TIMEOUT_SECONDS),
            **no_window_kwargs(),
        )
    except FileNotFoundError as exc:
        raise BeeBoxError("FFPROBE_FAILED", "ffprobe is required to inspect media files", stage="preprocessing", command=" ".join(command)) from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise BeeBoxError(
            "FFPROBE_FAILED",
            f"Failed to inspect media with ffprobe: {detail}",
            stage="preprocessing",
            command=" ".join(command),
            stderr_excerpt=detail,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise BeeBoxError(
            "FFPROBE_FAILED",
            "ffprobe timed out while inspecting media",
            stage="preprocessing",
            command=" ".join(command),
        ) from exc

    try:
        raw = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise BeeBoxError(
            "FFPROBE_FAILED",
            "ffprobe returned invalid JSON",
            stage="preprocessing",
            command=" ".join(command),
        ) from exc
    fmt = raw.get("format") or {}
    audio_streams = [stream for stream in raw.get("streams", []) if stream.get("codec_type") == "audio"]
    audio_stream = audio_streams[0] if audio_streams else {}
    duration = _optional_nonnegative_float(fmt.get("duration"))
    if duration is None:
        duration = _optional_nonnegative_float(audio_stream.get("duration"))
    return {
        "duration_ms": int(duration * 1000) if duration is not None else None,
        "format": fmt.get("format_name"),
        "audio_codec": audio_stream.get("codec_name"),
        "sample_rate": _optional_positive_int(audio_stream.get("sample_rate")),
        "channels": audio_stream.get("channels"),
        "has_audio_stream": bool(audio_streams),
        "audio_stream_count": len(audio_streams),
    }


def prepare_media_for_asr(path: Path, *, output_dir: Path | None = None, output_name: str | None = None) -> tuple[Path, dict]:
    if path.suffix.lower() not in SUPPORTED_MEDIA_EXTENSIONS:
        raise BeeBoxError("UNSUPPORTED_MEDIA_FORMAT", f"Unsupported media format: {path.suffix or 'unknown'}", stage="preprocessing")

    metadata = probe_media(path)
    if metadata.get("has_audio_stream") is False:
        raise BeeBoxError("NO_AUDIO_STREAM", "Media file does not contain an audio stream", stage="preprocessing", audio_metadata=metadata)
    if path.suffix.lower() == ".wav":
        return path, metadata

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / (output_name or f"{path.stem}.wav")
    else:
        target = path.with_suffix(".wav")
    ffmpeg = resolve_tools(check_version=False)["ffmpeg"]
    command = [
        ffmpeg.path or "ffmpeg",
        "-i",
        str(path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "wav",
        "-y",
        str(target),
    ]
    if not ffmpeg.available:
        raise BeeBoxError("FFMPEG_FAILED", f"ffmpeg is required to extract audio from media files: {ffmpeg.error or 'not found'}", stage="preprocessing", command=" ".join(command))
    try:
        subprocess.run(
            command,
            capture_output=True,
            check=True,
            encoding="utf-8",
            errors="replace",
            timeout=_media_timeout(metadata.get("duration_ms")),
            **no_window_kwargs(),
        )
    except FileNotFoundError as exc:
        raise BeeBoxError("FFMPEG_FAILED", "ffmpeg is required to extract audio from media files", stage="preprocessing", command=" ".join(command)) from exc
    except subprocess.CalledProcessError as exc:
        target.unlink(missing_ok=True)
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise BeeBoxError(
            "FFMPEG_FAILED",
            f"Failed to normalize media with ffmpeg: {detail}",
            stage="preprocessing",
            command=" ".join(command),
            stderr_excerpt=detail,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        target.unlink(missing_ok=True)
        raise BeeBoxError(
            "FFMPEG_FAILED",
            "ffmpeg timed out while normalizing media",
            stage="preprocessing",
            command=" ".join(command),
        ) from exc
    return target, metadata


def analyze_audio_quality(path: Path) -> dict:
    ffmpeg = resolve_tools(check_version=False)["ffmpeg"]
    if not ffmpeg.available:
        return {"warnings": [f"ffmpeg is not available for volume analysis: {ffmpeg.error or 'not found'}"]}
    command = [ffmpeg.path or "ffmpeg", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            timeout=_media_timeout(),
            **no_window_kwargs(),
        )
    except FileNotFoundError:
        return {"warnings": ["ffmpeg is not available for volume analysis"]}
    except subprocess.TimeoutExpired:
        return {"warnings": ["ffmpeg timed out during volume analysis"]}
    output = "\n".join([completed.stdout or "", completed.stderr or ""])
    mean_match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB", output)
    max_match = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?) dB", output)
    mean_volume = float(mean_match.group(1)) if mean_match else None
    peak_volume = float(max_match.group(1)) if max_match else None
    near_silence = bool((peak_volume is not None and peak_volume < -45) or (mean_volume is not None and mean_volume < -55))
    warnings = ["Audio appears close to silence"] if near_silence else []
    return {
        "mean_volume_db": mean_volume,
        "peak_volume_db": peak_volume,
        "near_silence": near_silence,
        "warnings": warnings,
    }


def plan_chunk_windows(
    duration_ms: int | None,
    *,
    window_ms: int = CHUNK_WINDOW_MS,
    overlap_ms: int = CHUNK_OVERLAP_MS,
) -> list[tuple[int, int]]:
    """Pure chunk geometry used by splitting, preflight and both ASR paths."""
    if not duration_ms or duration_ms <= 0:
        return []
    window = max(1, int(window_ms))
    overlap = max(0, min(int(overlap_ms), window - 1))
    step_ms = max(1, window - overlap)
    windows: list[tuple[int, int]] = []
    start_ms = 0
    while start_ms < duration_ms:
        end_ms = min(duration_ms, start_ms + window)
        windows.append((start_ms, end_ms))
        if end_ms >= duration_ms:
            break
        start_ms += step_ms
    return windows


def exceeds_chunk_threshold(duration_ms: int | None, threshold_ms: int = CHUNK_THRESHOLD_MS) -> bool:
    """The single long-audio decision used by upload preflight and chunking."""
    return bool(duration_ms and duration_ms > threshold_ms)


def validate_upload_media(path: Path) -> dict:
    """Reject unsupported, undecodable or audio-less uploads before task creation.

    Raises ``BeeBoxError("UNSUPPORTED_FORMAT")`` for every client-side media
    problem so the route can answer 400 without leaving an upload or task row.
    A missing/unusable ffprobe stays ``FFPROBE_FAILED``: that is an environment
    problem, not the uploader's fault.
    """
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_MEDIA_EXTENSIONS:
        raise BeeBoxError(
            "UNSUPPORTED_FORMAT",
            f"Unsupported media format: {suffix or 'unknown'}",
            stage="upload",
        )
    try:
        metadata = probe_media(path)
    except BeeBoxError as exc:
        if exc.code == "FFPROBE_FAILED" and "is required" not in exc.message:
            raise BeeBoxError(
                "UNSUPPORTED_FORMAT",
                f"Unsupported or invalid media: {exc.message}",
                stage="upload",
            ) from exc
        raise
    if not metadata.get("has_audio_stream"):
        raise BeeBoxError(
            "UNSUPPORTED_FORMAT",
            "Media file does not contain an audio stream",
            stage="upload",
        )
    return metadata


def preflight_media(path: Path, *, long_audio_threshold_ms: int = CHUNK_THRESHOLD_MS, chunk_window_ms: int = CHUNK_WINDOW_MS, chunk_overlap_ms: int = CHUNK_OVERLAP_MS) -> dict:
    supported = path.suffix.lower() in SUPPORTED_MEDIA_EXTENSIONS
    if not supported:
        return {
            "supported_format": False,
            "has_audio_stream": False,
            "needs_normalization": True,
            "will_chunk": False,
            "chunk_count": 0,
            "warnings": [f"Unsupported media format: {path.suffix or 'unknown'}"],
        }
    metadata = probe_media(path)
    quality = analyze_audio_quality(path) if metadata.get("has_audio_stream") else {"warnings": []}
    duration_ms = metadata.get("duration_ms")
    will_chunk = exceeds_chunk_threshold(duration_ms, long_audio_threshold_ms)
    chunk_count = (
        len(plan_chunk_windows(duration_ms, window_ms=chunk_window_ms, overlap_ms=chunk_overlap_ms))
        if will_chunk
        else 1
    )
    return {
        **metadata,
        **quality,
        "supported_format": True,
        "has_audio_stream": bool(metadata.get("has_audio_stream")),
        "needs_normalization": path.suffix.lower() != ".wav",
        "will_chunk": will_chunk,
        "chunk_count": chunk_count,
        "warnings": quality.get("warnings", []),
    }


def split_audio_chunks(
    audio_path: Path,
    output_dir: Path,
    duration_ms: int,
    *,
    window_ms: int = CHUNK_WINDOW_MS,
    overlap_ms: int = CHUNK_OVERLAP_MS,
) -> list[tuple[Path, int, int]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[tuple[Path, int, int]] = []
    attempted_paths: list[Path] = []
    ffmpeg = resolve_tools(check_version=False)["ffmpeg"]
    windows = plan_chunk_windows(duration_ms, window_ms=window_ms, overlap_ms=overlap_ms)
    try:
        for index, (start_ms, end_ms) in enumerate(windows, 1):
            target = output_dir / f"chunk-{index:04}.wav"
            attempted_paths.append(target)
            command = [
                ffmpeg.path or "ffmpeg",
                "-ss",
                f"{start_ms / 1000:.3f}",
                "-t",
                f"{(end_ms - start_ms) / 1000:.3f}",
                "-i",
                str(audio_path),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "wav",
                "-y",
                str(target),
            ]
            if not ffmpeg.available:
                raise BeeBoxError("FFMPEG_FAILED", f"ffmpeg is required to split long media files: {ffmpeg.error or 'not found'}", stage="chunking", command=" ".join(command))
            try:
                subprocess.run(
                    command,
                    capture_output=True,
                    check=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=_media_timeout(end_ms - start_ms),
                    **no_window_kwargs(),
                )
            except FileNotFoundError as exc:
                raise BeeBoxError("FFMPEG_FAILED", "ffmpeg is required to split long media files", stage="chunking", command=" ".join(command)) from exc
            except subprocess.CalledProcessError as exc:
                detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
                raise BeeBoxError(
                    "CHUNK_FAILED",
                    f"Failed to split media chunk {index}: {detail}",
                    stage="chunking",
                    command=" ".join(command),
                    stderr_excerpt=detail,
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise BeeBoxError(
                    "CHUNK_FAILED",
                    f"ffmpeg timed out while splitting media chunk {index}",
                    stage="chunking",
                    command=" ".join(command),
                ) from exc
            chunks.append((target, start_ms, end_ms))
        return chunks
    except BaseException:
        for path in attempted_paths:
            path.unlink(missing_ok=True)
        try:
            output_dir.rmdir()
        except OSError:
            pass
        raise

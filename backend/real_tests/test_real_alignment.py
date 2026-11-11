"""Real Qwen3-ASR + ForcedAligner smoke, opt-in via BEEBOX_RUN_REAL_MODELS=1.

Ticket #4 acceptance: both real models load on CUDA as fp16 and a real short clip
comes back as non-empty segments with ``start < end``. No fake models here; the
only injected input is the audio path (``BEEBOX_REAL_ALIGNMENT_AUDIO`` or the
first supported file under ``BEEBOX_REAL_MEDIA_DIR``).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import torch

from backend.backends.local_asr import TransformersSpeechLMBackend
from backend.backends.registry import get_model_config
from backend.services import aligner
from backend.services.errors import BeeBoxError

SUPPORTED_SUFFIXES = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".mp4", ".mkv", ".mov", ".webm"}
ASR_MODEL_NAME = "qwen3-asr-1.7b"


def _require_real() -> None:
    if os.environ.get("BEEBOX_RUN_REAL_MODELS") != "1":
        raise AssertionError("BEEBOX_RUN_REAL_MODELS=1 is required; real model tests must not be skipped")


def _audio_path() -> Path:
    explicit = os.environ.get("BEEBOX_REAL_ALIGNMENT_AUDIO")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise AssertionError(f"BEEBOX_REAL_ALIGNMENT_AUDIO does not exist: {path}")
        return path
    media_dir = os.environ.get("BEEBOX_REAL_MEDIA_DIR")
    if not media_dir:
        raise AssertionError("BEEBOX_REAL_ALIGNMENT_AUDIO or BEEBOX_REAL_MEDIA_DIR is required")
    root = Path(media_dir).expanduser()
    candidates = sorted(
        path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    )
    if not candidates:
        raise AssertionError(f"No supported media files found in {root}")
    return candidates[0]


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _audio_duration(path: Path) -> float:
    import soundfile

    return float(soundfile.info(str(path)).duration)


def test_real_qwen3_asr_and_aligner_produce_timestamped_segments() -> None:
    _require_real()
    assert torch.cuda.is_available(), "Gate 1 acceptance requires a CUDA device"
    audio_path = _audio_path()
    duration = _audio_duration(audio_path)
    model_config = get_model_config(ASR_MODEL_NAME)
    assert model_config is not None
    assert model_config.supports_timestamps is True
    assert aligner.is_aligner_ready() is True

    backend = TransformersSpeechLMBackend()
    try:
        result = backend.transcribe(str(audio_path), model_config, {"language": "auto"})
        asr_model = backend._models[ASR_MODEL_NAME]
        asr_parameter = next(asr_model.parameters())
        assert asr_parameter.dtype == torch.float16
        assert asr_parameter.device.type == "cuda"
    finally:
        backend.unload(ASR_MODEL_NAME)

    assert result.text.strip()
    assert result.segments, "aligned transcription must return segments"
    assert result.raw_result_summary["aligned"] is True
    assert result.raw_result_summary["alignment_items"] > 0
    assert all(segment.start < segment.end for segment in result.segments)
    previous_end = 0.0
    for segment in result.segments:
        assert segment.start >= previous_end - 1e-6
        previous_end = segment.end
    assert result.segments[0].start >= 0.0
    assert result.segments[-1].end <= duration + 0.5
    assert _compact("".join(segment.text for segment in result.segments)) == _compact(result.text)

    processor, aligner_model = aligner._get_aligner()
    aligner_parameter = next(aligner_model.parameters())
    assert aligner_parameter.dtype == torch.float16
    assert aligner_parameter.device.type == "cuda"
    assert processor is not None


def test_real_aligner_missing_directory_fails_without_fallback(tmp_path: Path, monkeypatch) -> None:
    _require_real()
    audio_path = _audio_path()
    import soundfile

    audio, sample_rate = soundfile.read(str(audio_path), dtype="float32", always_2d=True)
    assert sample_rate == 16000, "the alignment fixture must be 16 kHz mono"
    waveform = audio.mean(axis=1)

    monkeypatch.setenv("BEEBOX_ALIGNER_MODEL_DIR", str(tmp_path / "missing-aligner"))
    aligner.unload_aligner()
    try:
        assert aligner.is_aligner_ready() is False
        with pytest.raises(BeeBoxError) as excinfo:
            aligner.align_transcript(waveform, "你好", "Chinese")
        assert excinfo.value.code == "ALIGNMENT_FAILED"
        assert excinfo.value.stage == "align"
    finally:
        aligner.unload_aligner()

"""Real pyannote community-1 smoke, opt-in via BEEBOX_RUN_REAL_MODELS=1.

Ticket #6 acceptance: the preseeded community-1 directory loads offline with no
HF token, the audio is passed as an in-memory waveform (never a path), and an
explicit ``num_speakers`` produces exactly that many labels. The segments are
synthetic fixed windows here — this test checks the real model and the
assignment seam, not ASR quality. The API end-to-end run in the ticket report
covers real ASR + aligner + diarization together.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import soundfile

from backend.models import TranscriptSegment
from backend.services import diarization


def _require_real() -> None:
    if os.environ.get("BEEBOX_RUN_REAL_MODELS") != "1":
        raise AssertionError("BEEBOX_RUN_REAL_MODELS=1 is required; real model tests must not be skipped")


def _audio_path() -> Path:
    explicit = os.environ.get("BEEBOX_REAL_DIARIZATION_AUDIO")
    if not explicit:
        raise AssertionError("BEEBOX_REAL_DIARIZATION_AUDIO must point at a real 16 kHz mono wav clip")
    path = Path(explicit).expanduser()
    if not path.exists():
        raise AssertionError(f"BEEBOX_REAL_DIARIZATION_AUDIO does not exist: {path}")
    if path.suffix.lower() != ".wav":
        raise AssertionError("BEEBOX_REAL_DIARIZATION_AUDIO must be a wav file for the soundfile path")
    return path


def _window_segments(duration: float, window: float = 5.0, step: float = 10.0) -> list[TranscriptSegment]:
    segments: list[TranscriptSegment] = []
    start = 0.0
    index = 1
    while start + window <= duration:
        segments.append(TranscriptSegment(id=index, start=start, end=start + window, text=f"窗口{index}"))
        index += 1
        start += step
    return segments


def test_real_diarization_without_token_assigns_numeric_labels() -> None:
    _require_real()
    audio_path = _audio_path()
    duration = float(soundfile.info(str(audio_path)).duration)
    segments = _window_segments(duration)
    assert segments

    assigned = diarization.apply_diarization(str(audio_path), segments)

    assert len(assigned) == len(segments)
    assert all(segment.speaker is None or segment.speaker.isdigit() for segment in assigned)
    first_seen: list[str] = []
    for segment in assigned:
        if segment.speaker is not None and segment.speaker not in first_seen:
            first_seen.append(segment.speaker)
    assert first_seen == [str(index) for index in range(1, len(first_seen) + 1)]


@pytest.mark.parametrize("num_speakers", [2, 3])
def test_real_diarization_honours_num_speakers(num_speakers: int) -> None:
    _require_real()
    audio_path = _audio_path()
    duration = float(soundfile.info(str(audio_path)).duration)
    segments = _window_segments(duration)

    assigned = diarization.apply_diarization(str(audio_path), segments, num_speakers=num_speakers)

    labels = {segment.speaker for segment in assigned if segment.speaker is not None}
    assert len(labels) == num_speakers

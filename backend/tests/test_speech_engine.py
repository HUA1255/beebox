"""Ticket #35: the speech-engine boundary and its configurable fake."""

from __future__ import annotations

import pytest

from backend.models import TranscriptSegment
from backend.services.errors import BeeBoxError
from backend.services.speech_engine import (
    DiarizationRequest,
    FakeSpeechEngine,
    LocalSpeechEngine,
    SpeechRequest,
    get_speech_engine,
    speech_engine_name,
)


def _request(**overrides) -> SpeechRequest:
    values = {"audio_path": "audio.wav", "model_name": "whisper-base"}
    values.update(overrides)
    return SpeechRequest(**values)


def test_default_engine_is_the_real_local_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BEEBOX_SPEECH_ENGINE", raising=False)

    assert speech_engine_name() == "local"
    assert isinstance(get_speech_engine(), LocalSpeechEngine)


def test_unknown_engine_name_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEEBOX_SPEECH_ENGINE", "magic")

    with pytest.raises(BeeBoxError) as excinfo:
        get_speech_engine()
    assert excinfo.value.code == "SPEECH_ENGINE_INVALID"


def test_fake_engine_returns_configured_text_timestamps_and_speakers() -> None:
    engine = FakeSpeechEngine(
        {
            "segments": [
                {"start": 0.0, "end": 1.0, "text": "你好", "speaker": "S01"},
                {"start": 1.5, "end": 2.5, "text": "世界", "speaker": "S02"},
            ],
            "text": "你好 世界",
            "language": "zh",
            "duration": 3.0,
            "aligned": True,
        }
    )

    result = engine.transcribe(_request(chunk_index=1, chunk_total=2))

    assert result.text == "你好 世界"
    assert result.language == "zh"
    assert result.duration == 3.0
    assert [(segment.start, segment.end, segment.text, segment.speaker) for segment in result.segments] == [
        (0.0, 1.0, "你好", "S01"),
        (1.5, 2.5, "世界", "S02"),
    ]
    assert result.raw_result_summary["aligned"] is True
    assert result.raw_result_summary["chunk_index"] == 1


def test_fake_engine_can_fail_a_specific_chunk_mid_run() -> None:
    engine = FakeSpeechEngine(
        {
            "segments": [{"start": 0.0, "end": 1.0, "text": "chunk"}],
            "fail": {
                "stage": "transcribe",
                "chunk_index": 2,
                "code": "DECODER_FAILED",
                "message": "decoder exploded",
            },
        }
    )

    first = engine.transcribe(_request(chunk_index=1, chunk_total=3))
    assert first.text == "chunk"
    with pytest.raises(BeeBoxError) as excinfo:
        engine.transcribe(_request(chunk_index=2, chunk_total=3))
    assert excinfo.value.code == "DECODER_FAILED"
    assert "decoder exploded" in excinfo.value.message


def test_fake_engine_can_fail_the_diarization_stage() -> None:
    engine = FakeSpeechEngine(
        {"diarization": {"fail": {"stage": "diarize", "code": "DIARIZATION_FAILED", "message": "no turns"}}}
    )

    with pytest.raises(BeeBoxError) as excinfo:
        engine.diarize(
            DiarizationRequest(
                audio_path="audio.wav",
                segments=[TranscriptSegment(id=1, start=0.0, end=1.0, text="hello")],
            )
        )
    assert excinfo.value.code == "DIARIZATION_FAILED"


def test_fake_engine_assigns_configurable_diarization_speakers_and_stats() -> None:
    engine = FakeSpeechEngine(
        {
            "diarization": {
                "speakers": ["S01", "S02"],
                "stats": {"cross_speaker_segments": 1, "cross_speaker_definition": "definition"},
            }
        }
    )

    segments, stats = engine.diarize(
        DiarizationRequest(
            audio_path="audio.wav",
            segments=[
                TranscriptSegment(id=1, start=0.0, end=1.0, text="a"),
                TranscriptSegment(id=2, start=1.0, end=2.0, text="b"),
            ],
        )
    )

    assert [segment.speaker for segment in segments] == ["S01", "S02"]
    assert stats["cross_speaker_segments"] == 1
    assert stats["cross_speaker_definition"] == "definition"
    assert stats["fake_engine"] is True

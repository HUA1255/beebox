"""The one boundary between the transcription pipeline and the models.

Everything that turns audio into timestamped text (and, when requested, speaker
labels) goes through :class:`SpeechEngine`. The pipeline never imports a model
runtime, an ASR backend, the forced aligner or pyannote directly; it builds a
:class:`SpeechRequest` and consumes the resulting :class:`TranscriptionResult`.

Two implementations ship with the service:

``local``
    Wraps the real ASR backends (Qwen3-ASR, FireRed, MOSS, ...) and pyannote
    diarization. Behaviour is unchanged from before the boundary existed.
``fake``
    A configurable stand-in used by tests (and any deployment that wants to
    exercise the full pipeline without model files). It returns scripted
    segments, speakers and failures; chunking, merging, postprocessing,
    paragraph grouping and exports still run the real code on its output. It
    also remembers which models a transcription loaded, so unload behaviour is
    observable through the public model endpoints.

Selection is configuration only::

    BEEBOX_SPEECH_ENGINE=fake
    BEEBOX_SPEECH_FAKE_RESULT='{"segments": [...]}'

The default engine is ``local`` so production is unaffected.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from backend.models import TranscriptSegment, TranscriptionResult
from backend.services.errors import BeeBoxError
from backend.utils.transcript_text import transcript_text_from_segments

SPEECH_ENGINE_ENV = "BEEBOX_SPEECH_ENGINE"
FAKE_SPEECH_RESULT_ENV = "BEEBOX_SPEECH_FAKE_RESULT"

ENGINE_LOCAL = "local"
ENGINE_FAKE = "fake"
SUPPORTED_ENGINES = (ENGINE_LOCAL, ENGINE_FAKE)

DEFAULT_FAKE_TEXT = "fake speech transcript"

# The fake engine mirrors the real backends: loaded state is process-wide, so a
# transcription marks the model loaded until the caller unloads it. Tests reset
# it through :func:`reset_speech_engine_state`.
_fake_state_lock = threading.Lock()
_fake_loaded_models: set[str] = set()


def reset_speech_engine_state() -> None:
    """Forget every fake-engine load (test lifecycle hook)."""
    with _fake_state_lock:
        _fake_loaded_models.clear()


@dataclass(frozen=True)
class SpeechRequest:
    """One ASR call: a complete file or one chunk of it.

    ``chunk_index`` is 1-based; it lets a scripted engine fail mid-run on a
    specific chunk while earlier chunks keep their real merge behaviour.
    """

    audio_path: str
    model_name: str
    language: str | None = None
    vad: bool = True
    word_timestamps: bool = False
    chunk_index: int = 1
    chunk_total: int = 1
    chunk_start_ms: int | None = None


@dataclass(frozen=True)
class DiarizationRequest:
    """One diarization call: assign speakers to already-timestamped segments."""

    audio_path: str
    segments: list[TranscriptSegment]
    num_speakers: int | None = None
    model_name: str | None = None


class SpeechEngine(Protocol):
    name: str

    def ensure_ready(self, model_name: str) -> None:
        """Raise the contract error when the model cannot run."""

    def transcribe(self, request: SpeechRequest) -> TranscriptionResult:
        """Audio (or one chunk) -> text, timestamps and optional speakers."""

    def diarize(self, request: DiarizationRequest) -> tuple[list[TranscriptSegment], dict[str, Any]]:
        """Audio + segments -> segments labelled with speakers, plus stage stats."""

    def is_model_loaded(self, model_name: str) -> bool:
        """Whether inference state for ``model_name`` is resident right now."""

    def unload(self, model_name: str) -> bool:
        """Drop the resident state; ``True`` when something was released."""


class LocalSpeechEngine:
    """Production engine: the existing ASR backends and pyannote behind a seam."""

    name = ENGINE_LOCAL

    def ensure_ready(self, model_name: str) -> None:
        from backend.services import models as model_service

        model_service.ensure_model_ready(model_name)

    def transcribe(self, request: SpeechRequest) -> TranscriptionResult:
        from backend.services import transcribe as transcribe_service

        return transcribe_service.transcribe_with_local_model(
            request.model_name,
            request.audio_path,
            {
                "language": request.language,
                "vad": request.vad,
                "word_timestamps": request.word_timestamps,
            },
        )

    def diarize(self, request: DiarizationRequest) -> tuple[list[TranscriptSegment], dict[str, Any]]:
        from backend.services import aligner
        from backend.services import diarization as diarization_service

        # The aligned transcript is final by now; drop the forced aligner so
        # pyannote does not have to share the 16 GB budget with it.
        aligner.unload_aligner()
        stats: dict[str, Any] = {}
        try:
            segments = diarization_service.apply_diarization(
                request.audio_path,
                request.segments,
                num_speakers=request.num_speakers,
                model_name=request.model_name,
                stats=stats,
            )
        finally:
            # Model lifecycle: a stage-loaded pipeline must not stay cached for
            # the lifetime of the process.
            diarization_service.unload_pipeline()
        return segments, stats

    def is_model_loaded(self, model_name: str) -> bool:
        from backend.backends import get_model_config
        from backend.backends.local_asr import is_model_loaded

        model_config = get_model_config(model_name)
        if model_config is None:
            return False
        return is_model_loaded(model_name, model_config.engine)

    def unload(self, model_name: str) -> bool:
        from backend.backends import get_model_config
        from backend.backends.local_asr import unload_model

        model_config = get_model_config(model_name)
        if model_config is None:
            return False
        released = unload_model(model_name, model_config.engine)
        if released:
            _release_cuda_cache()
        return released


def _release_cuda_cache() -> None:
    """Best effort: give the freed weights back to the allocator."""
    try:
        import gc

        gc.collect()
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def load_fake_speech_config() -> dict[str, Any]:
    raw = os.environ.get(FAKE_SPEECH_RESULT_ENV, "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BeeBoxError(
            "SPEECH_ENGINE_CONFIG_INVALID",
            f"{FAKE_SPEECH_RESULT_ENV} is not valid JSON: {exc}",
            stage="transcribing",
        ) from exc
    if not isinstance(payload, dict):
        raise BeeBoxError(
            "SPEECH_ENGINE_CONFIG_INVALID",
            f"{FAKE_SPEECH_RESULT_ENV} must be a JSON object",
            stage="transcribing",
        )
    return payload


def _scripted_failure(failure: Any, *, stage: str, chunk_index: int) -> None:
    if not isinstance(failure, dict):
        return
    target_stage = str(failure.get("stage") or "transcribe")
    if target_stage not in {stage, "any"}:
        return
    target_chunk = failure.get("chunk_index")
    if target_chunk is not None and int(target_chunk) != chunk_index:
        return
    raise BeeBoxError(
        str(failure.get("code") or "SPEECH_ENGINE_FAILED"),
        str(failure.get("message") or f"Scripted {stage} failure"),
        stage=str(failure.get("error_stage") or ("diarize" if stage == "diarize" else "transcribing")),
    )


def _segment_payloads(config: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = config.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _parse_segments(payloads: list[dict[str, Any]]) -> list[TranscriptSegment]:
    segments: list[TranscriptSegment] = []
    for index, item in enumerate(payloads, 1):
        text = str(item.get("text") or "")
        segments.append(
            TranscriptSegment(
                id=index,
                start=float(item.get("start") or 0.0),
                end=float(item.get("end") or 0.0),
                text=text,
                speaker=None if item.get("speaker") in (None, "") else str(item.get("speaker")),
                confidence=item.get("confidence"),
            )
        )
    return segments


def _render(template: str, request: SpeechRequest) -> str:
    """Let a scripted result name the chunk/model it belongs to."""
    return (
        template.replace("{audio_stem}", Path(request.audio_path).stem)
        .replace("{model_name}", request.model_name)
        .replace("{chunk_index}", str(request.chunk_index))
        .replace("{chunk_total}", str(request.chunk_total))
    )


def _scripted_segments(config: dict[str, Any], request: SpeechRequest) -> list[TranscriptSegment]:
    if "segments" in config and isinstance(config.get("segments"), list):
        segments = _parse_segments(_segment_payloads(config, "segments"))
        return [
            segment.model_copy(update={"text": _render(segment.text, request)})
            for segment in segments
        ]
    text = _render(str(config.get("text") or DEFAULT_FAKE_TEXT), request)
    return [TranscriptSegment(id=1, start=0.0, end=float(config.get("duration") or 1.0), text=text)]


class FakeSpeechEngine:
    """Scripted engine: deterministic output and injectable failures."""

    name = ENGINE_FAKE

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config if config is not None else load_fake_speech_config()

    def ensure_ready(self, model_name: str) -> None:
        return None

    def transcribe(self, request: SpeechRequest) -> TranscriptionResult:
        config = self._config
        _scripted_failure(config.get("fail"), stage="transcribe", chunk_index=request.chunk_index)
        delay = config.get("delay_seconds")
        if delay:
            time.sleep(float(delay))
        with _fake_state_lock:
            _fake_loaded_models.add(request.model_name)
        segments = _scripted_segments(config, request)
        text = config.get("text")
        if not isinstance(text, str) or not text:
            text = transcript_text_from_segments(segments) or ("" if not segments else DEFAULT_FAKE_TEXT)
        else:
            text = _render(text, request)
        duration = config.get("duration")
        if duration is None:
            duration = max((segment.end for segment in segments), default=1.0) or 1.0
        return TranscriptionResult(
            text=text,
            language=config.get("language") if isinstance(config.get("language"), str) else request.language,
            duration=float(duration),
            segments=segments,
            model_name=request.model_name,
            raw_result_summary={
                "aligned": bool(config.get("aligned", False)),
                "fake_engine": True,
                "chunk_index": request.chunk_index,
                "chunk_total": request.chunk_total,
            },
        )

    def diarize(self, request: DiarizationRequest) -> tuple[list[TranscriptSegment], dict[str, Any]]:
        config = self._config
        diarization = config.get("diarization") if isinstance(config.get("diarization"), dict) else {}
        _scripted_failure(diarization.get("fail"), stage="diarize", chunk_index=1)
        scripted = _parse_segments(_segment_payloads(diarization, "segments"))
        speakers = diarization.get("speakers") or config.get("speakers") or ["1"]
        if not isinstance(speakers, list) or not speakers:
            speakers = ["1"]
        labels = [str(item) for item in speakers]
        assigned: list[TranscriptSegment] = []
        for index, segment in enumerate(request.segments):
            speaker = segment.speaker
            if index < len(scripted) and scripted[index].speaker:
                speaker = scripted[index].speaker
            elif not speaker:
                speaker = labels[index % len(labels)]
            assigned.append(segment.model_copy(update={"speaker": speaker}))
        stats: dict[str, Any] = {"cross_speaker_segments": 0, "fake_engine": True}
        if isinstance(diarization.get("stats"), dict):
            stats.update(diarization["stats"])
        return assigned, stats

    def is_model_loaded(self, model_name: str) -> bool:
        with _fake_state_lock:
            return model_name in _fake_loaded_models

    def unload(self, model_name: str) -> bool:
        with _fake_state_lock:
            removed = model_name in _fake_loaded_models
            _fake_loaded_models.discard(model_name)
        return removed


def speech_engine_name() -> str:
    value = os.environ.get(SPEECH_ENGINE_ENV, "").strip().lower()
    return value or ENGINE_LOCAL


def get_speech_engine() -> SpeechEngine:
    """Build the configured engine; cheap, stateless and safe per call."""
    name = speech_engine_name()
    if name in {ENGINE_LOCAL, "real"}:
        return LocalSpeechEngine()
    if name == ENGINE_FAKE:
        return FakeSpeechEngine()
    raise BeeBoxError(
        "SPEECH_ENGINE_INVALID",
        f"Unsupported speech engine {name!r}; expected one of {', '.join(SUPPORTED_ENGINES)}",
        stage="transcribing",
    )

"""Diarization failure and cache-release contracts (service seam).

The legacy HTTP upload routes were removed with ticket #56; the remaining
tests drive the task service the /v1 job worker uses. Upload validation now
lives in ``test_v1_api.py`` / ``test_v1_files`` coverage.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import config
from backend.database import session as db_session
from backend.database.models import (
    TaskLog,
    TranscriptSegment as DBSegment,
    TranscriptionChunk,
    TranscriptionTask,
)
from backend.models import TranscriptSegment, TranscriptionResult
from backend.services import tasks as task_service
from backend.services.speech_engine import DiarizationRequest, LocalSpeechEngine
from support import fake_speech_env


def db():
    return db_session.SessionLocal()


def _task(db, task_id: str) -> TranscriptionTask:
    row = db.get(TranscriptionTask, task_id)
    assert row is not None
    return row


def _seed_task(
    db,
    *,
    task_id: str,
    status: str = "failed_resumable",
    options: dict | None = None,
    duration_ms: int = 240_000,
) -> TranscriptionTask:
    row = TranscriptionTask(
        id=task_id,
        filename="meeting.wav",
        source="local",
        audio_path="uploads/meeting.wav",
        status=status,
        progress=100,
        model_name="qwen3-asr-1.7b",
        duration_ms=duration_ms,
        options_json=json.dumps(options or {"diarization": False}),
        error="Chunk 2 failed" if status == "failed_resumable" else None,
        error_code="CHUNK_FAILED" if status == "failed_resumable" else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_missing_normalized_audio_fails_the_task_with_diarization_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    fake_speech_env(monkeypatch)
    db_session.init_db()
    session = db()
    try:
        row = _seed_task(
            session,
            task_id="missing-diarization-audio",
            status="queued",
            options={"diarization": True},
            duration_ms=2_000,
        )
        row.progress = 0
        session.commit()
        monkeypatch.setattr(task_service, "_prepare_task_media", lambda _db, _row: tmp_path / "missing.wav")
        monkeypatch.setattr(
            task_service,
            "_transcribe_with_local_model",
            lambda _db, _row: TranscriptionResult(
                text="hello",
                segments=[TranscriptSegment(id=1, start=0.0, end=1.0, text="hello")],
                raw_result_summary={"aligned": True},
            ),
        )

        task_service.run_task(session, row)
        session.refresh(row)

        assert row.status == "failed"
        assert row.error_code == "DIARIZATION_FAILED"
        assert session.query(DBSegment).filter(DBSegment.task_id == row.id).count() == 0
    finally:
        session.close()


def test_diarization_pipeline_is_unloaded_after_the_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.services import aligner, diarization as diarization_service

    audio = tmp_path / "normalized.wav"
    audio.write_bytes(b"wav")
    unloaded: list[str] = []
    monkeypatch.setattr(aligner, "unload_aligner", lambda: unloaded.append("aligner") or True)
    monkeypatch.setattr(
        diarization_service,
        "apply_diarization",
        lambda _path, segments, **_kwargs: [
            segment.model_copy(update={"speaker": "1"}) for segment in segments
        ],
    )
    monkeypatch.setattr(
        diarization_service, "unload_pipeline", lambda: unloaded.append("diarization") or True
    )

    segments, stats = LocalSpeechEngine().diarize(
        DiarizationRequest(
            audio_path=str(audio),
            segments=[TranscriptSegment(id=1, start=0.0, end=1.0, text="hello")],
        )
    )

    assert [segment.speaker for segment in segments] == ["1"]
    assert stats == {}
    assert unloaded == ["aligner", "diarization"]


def test_diarization_cross_speaker_stats_land_in_the_options_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio = tmp_path / "normalized.wav"
    audio.write_bytes(b"wav")
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    fake_speech_env(
        monkeypatch,
        diarization={
            "speakers": ["1"],
            "stats": {
                "cross_speaker_segments": 2,
                "cross_speaker_definition": "definition",
                "cross_speaker_details": [{"start": 1.0, "end": 2.0, "speaker": "1", "secondary": []}],
            },
        },
    )
    db_session.init_db()
    session = db()
    try:
        row = _seed_task(session, task_id="diarize-cross-speaker", status="completed", options={"diarization": True})
        options = {"diarization": True, "raw_result_summary": {"aligned": True, "chunks": 2}}

        segments, _mode, diarized = task_service._apply_speaker_labels_and_postprocess(
            session,
            row,
            [TranscriptSegment(id=1, start=1.0, end=2.0, text="hello")],
            options=options,
            settings=SimpleNamespace(diarization=True),
            normalized_path=audio,
            aligned=True,
        )

        assert diarized is True
        assert [segment.speaker for segment in segments] == ["1"]
        summary = options["raw_result_summary"]
        assert summary["aligned"] is True
        assert summary["chunks"] == 2
        assert summary["cross_speaker_segments"] == 2
        assert summary["cross_speaker_definition"] == "definition"
        assert summary["cross_speaker_details"][0]["speaker"] == "1"
    finally:
        session.close()


def test_a_clip_without_speaker_turns_succeeds_as_one_speaker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ticket #36: default diarization on an 8 s clip must not fail the job."""
    import numpy as np
    import soundfile as sf

    from backend.services import diarization as diarization_service

    audio = tmp_path / "short-clip.wav"
    sf.write(str(audio), np.zeros(129_600, dtype=np.float32), 16_000)
    model_dir = tmp_path / "pyannote-community-1"
    model_dir.mkdir()
    (model_dir / "config.yaml").write_text("pipeline: {}\n", encoding="utf-8")

    class _EmptyAnnotation:
        def itertracks(self, yield_label: bool = False):
            return iter(())

    class _EmptyPipeline:
        def __call__(self, audio, **kwargs):
            return SimpleNamespace(speaker_diarization=_EmptyAnnotation())

    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BEEBOX_SPEECH_ENGINE", "local")
    monkeypatch.setattr(
        diarization_service, "diarization_model_dir", lambda model_name=None: model_dir
    )
    monkeypatch.setattr(
        diarization_service, "_load_pipeline", lambda directory: _EmptyPipeline()
    )
    diarization_service.unload_pipeline()
    db_session.init_db()
    session = db()
    try:
        row = _seed_task(
            session,
            task_id="short-clip-diarization",
            status="completed",
            options={"_source_kind": "direct"},
        )
        # No diarization option: the service falls back to settings.diarization.
        options: dict = {}

        segments, _mode, diarized = task_service._apply_speaker_labels_and_postprocess(
            session,
            row,
            [TranscriptSegment(id=1, start=0.0, end=0.4, text="哎。")],
            options=options,
            settings=SimpleNamespace(diarization=True),
            normalized_path=audio,
            aligned=True,
        )

        assert diarized is True
        assert [segment.speaker for segment in segments] == ["1"]
        summary = options["raw_result_summary"]
        assert summary["single_speaker_fallback"] is True
        log = (
            session.query(TaskLog)
            .filter(
                TaskLog.task_id == row.id,
                TaskLog.message == "Speaker diarization completed",
            )
            .one()
        )
        assert json.loads(log.data_json)["single_speaker_fallback"] is True
    finally:
        session.close()
        diarization_service.unload_pipeline()


def test_chunk_retry_merge_keeps_cross_speaker_stats_in_the_persisted_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    db_session.init_db()
    session = db()
    try:
        audio = tmp_path / "normalized.wav"
        audio.write_bytes(b"wav")
        row = _seed_task(session, task_id="retry-cross-speaker", options={"diarization": True})
        row.audio_path = config.to_storage_path(audio)
        row.normalized_audio_path = config.to_storage_path(audio)
        for idx in (1, 2):
            session.add(
                TranscriptionChunk(
                    task_id=row.id,
                    idx=idx,
                    audio_path=config.to_storage_path(tmp_path / f"chunk-{idx}.wav"),
                    start_ms=(idx - 1) * 120_000,
                    end_ms=idx * 120_000,
                    status="completed",
                    progress=100,
                    segments_json=json.dumps(
                        [{"id": 1, "start": 0.5, "end": 1.5, "text": f"chunk{idx}"}]
                    ),
                )
            )
        session.commit()

        fake_speech_env(
            monkeypatch,
            diarization={
                "speakers": ["1"],
                "stats": {
                    "cross_speaker_segments": 1,
                    "cross_speaker_definition": "definition",
                    "cross_speaker_details": [],
                },
            },
        )

        task_service._merge_completed_chunks(session, row)

        session.expire_all()
        stored = session.get(TranscriptionTask, row.id)
        assert stored.status == "completed"
        summary = json.loads(stored.options_json)["raw_result_summary"]
        assert summary["retry"] is True
        assert summary["merged"] is True
        assert summary["cross_speaker_segments"] == 1
        assert summary["cross_speaker_definition"] == "definition"
    finally:
        session.close()

"""Chunk-retry, resource-ticket and chunk-splitting lifecycle contracts.

Ticket #56 removed the legacy per-chunk HTTP routes, so the tests that pinned
those route shapes are gone; the task-service behaviour they exercised remains
covered here (and the /v1 job retry route is covered by ``test_v1_api.py``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.database import session as db_session
from backend.database.models import TranscriptionChunk, TranscriptionTask
from backend.models import TranscriptionResult
from backend.services import media
from backend.services import tasks as task_service
from backend.services.ffmpeg_tools import ToolStatus
from support import fake_speech_env


def _init_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    fake_speech_env(monkeypatch)
    db_session.init_db()


def test_retry_failed_chunks_preserves_partial_progress_until_remaining_chunk_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_db(tmp_path, monkeypatch)
    task_id = "chunk-retry-partial"
    chunks_dir = tmp_path / "derived-audio" / f"{task_id}_chunks"
    chunks_dir.mkdir(parents=True)
    db = db_session.SessionLocal()
    try:
        task = TranscriptionTask(
            id=task_id,
            filename="partial.wav",
            source="local",
            audio_path="uploads/partial.wav",
            status="failed",
            progress=70,
            error="chunk failed",
            error_code="CHUNK_FAILED",
            options_json='{"error_code":"CHUNK_FAILED","diarization":false}',
        )
        db.add(task)
        for index in (1, 2):
            chunk_path = chunks_dir / f"chunk-{index:04}.wav"
            chunk_path.write_bytes(b"audio")
            db.add(
                TranscriptionChunk(
                    task_id=task_id,
                    idx=index,
                    audio_path=str(chunk_path),
                    start_ms=(index - 1) * 1000,
                    end_ms=index * 1000,
                    status="failed",
                    progress=20,
                    error="chunk failed",
                    error_code="CHUNK_FAILED",
                )
            )
        db.commit()

        calls: list[str] = []

        def partially_failing_transcribe(_db, _row, path: Path, **_kwargs) -> TranscriptionResult:
            calls.append(path.name)
            if path.name == "chunk-0002.wav":
                raise RuntimeError("second chunk still fails")
            return TranscriptionResult(text="first recovered", segments=[])

        monkeypatch.setattr(
            task_service,
            "_transcribe_path_for_row",
            partially_failing_transcribe,
        )

        def run_queued(task_id: str) -> None:
            worker_db = db_session.SessionLocal()
            try:
                task_service.run_task(worker_db, worker_db.get(TranscriptionTask, task_id))
            finally:
                worker_db.close()

        monkeypatch.setattr(task_service, "start_task_in_background", run_queued)
        queued = task_service.retry_failed_chunks(db, task_id)
        assert queued is not None
        assert queued.status == "queued"

        db.expire_all()
        partial = task_service.get_task(db, task_id)
        assert partial is not None
        # A failed retry is still a chunk failure: the task stays resumable.
        assert partial.status == "failed_resumable"
        assert partial.error_code == "CHUNK_FAILED"
        chunks = (
            db.query(TranscriptionChunk)
            .filter_by(task_id=task_id)
            .order_by(TranscriptionChunk.idx)
            .all()
        )
        assert [chunk.status for chunk in chunks] == ["completed", "failed"]
        assert chunks[0].text == "first recovered"

        monkeypatch.setattr(
            task_service,
            "_transcribe_path_for_row",
            lambda _db, _row, path, **_kwargs: (
                calls.append(path.name),
                TranscriptionResult(text="second recovered", segments=[]),
            )[1],
        )
        queued = task_service.retry_failed_chunks(db, task_id)
        assert queued is not None
        assert queued.status == "queued"

        db.expire_all()
        completed = task_service.get_task(db, task_id)

        assert completed is not None
        assert completed.status == "completed"
        assert completed.text == "first recovered second recovered"
        assert calls == [
            "chunk-0001.wav",
            "chunk-0002.wav",
            "chunk-0002.wav",
        ]
    finally:
        db.close()


def test_split_audio_failure_removes_chunks_created_by_same_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "chunks"
    monkeypatch.setattr(
        media,
        "resolve_tools",
        lambda **_kwargs: {
            "ffmpeg": ToolStatus(True, "/tmp/ffmpeg", "manual", "ffmpeg test"),
            "ffprobe": ToolStatus(True, "/tmp/ffprobe", "manual", "ffprobe test"),
        },
    )
    calls = 0

    def fake_run(command, **_kwargs):
        nonlocal calls
        calls += 1
        target = Path(command[-1])
        target.write_bytes(b"partial")
        if calls == 2:
            raise subprocess.CalledProcessError(1, command, stderr="failed")
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(media.subprocess, "run", fake_run)

    with pytest.raises(Exception, match="Failed to split"):
        media.split_audio_chunks(
            tmp_path / "source.wav",
            output_dir,
            duration_ms=2500,
            window_ms=1000,
            overlap_ms=0,
        )

    assert list(output_dir.glob("*.wav")) == []

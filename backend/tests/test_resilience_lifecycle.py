"""Ticket #12: long-audio resilience and data lifecycle.

Every test drives a public boundary (the ``/v1`` job API or the service entry
points the API calls) and observable state (task/chunk rows, files on disk),
not private helpers. Ticket #56 replaced the legacy ``/transcriptions`` and
``/tasks`` routes with ``/v1/files`` + ``/v1/jobs``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from backend import config
from backend.database import session as db_session
from backend.database.models import (
    SchemaMigration,
    TaskDiagnostic,
    TaskLog,
    TranscriptSegment as DBSegment,
    TranscriptionChunk,
    TranscriptionTask,
)
from backend.models import TranscriptSegment, TranscriptionResult
from backend.services import tasks as task_service
from support import fake_speech_env, make_wav_bytes


def make_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BEEBOX_MODEL_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("BEEBOX_INLINE_LOCAL_TRANSCRIPTION", "1")
    monkeypatch.delenv("BEEBOX_API_TOKEN", raising=False)
    from backend.app import create_app

    return TestClient(create_app())


def stub_media(monkeypatch: pytest.MonkeyPatch, duration_ms: int) -> None:
    def prepare(audio_path: Path, *, output_dir: Path, output_name: str):
        target = output_dir / output_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"normalized-wav")
        return target, {"duration_ms": duration_ms, "has_audio_stream": True}

    monkeypatch.setattr(task_service, "prepare_media_for_asr", prepare)
    monkeypatch.setattr(task_service, "preflight_media", lambda *_args, **_kwargs: {"warnings": []})


def chunk_split(windows: list[tuple[int, int]]):
    def split(audio_path: Path, output_dir: Path, duration_ms: int, *, window_ms: int, overlap_ms: int):
        assert (window_ms, overlap_ms) == (120_000, 2_000)
        output_dir.mkdir(parents=True, exist_ok=True)
        planned = []
        for index, (start_ms, end_ms) in enumerate(windows, 1):
            path = output_dir / f"chunk-{index:04}.wav"
            path.write_bytes(b"chunk-audio")
            planned.append((path, start_ms, end_ms))
        return planned

    return split


THREE_CHUNKS = [(0, 120_000), (118_000, 238_000), (236_000, 300_000)]
# Slow CI runners need headroom; local runs still settle quickly.
WAIT_TIMEOUT_SECONDS = 60.0


def create_long_job(client: TestClient, *, filename: str = "long.m4a") -> str:
    uploaded = client.post(
        "/v1/files",
        files={"file": (filename, make_wav_bytes(), "audio/mp4")},
    )
    assert uploaded.status_code == 201, uploaded.text
    created = client.post(
        "/v1/jobs",
        json={
            "source": {"type": "file", "file_id": uploaded.json()["id"]},
            "options": {"model": "whisper-base", "diarization": False},
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def wait_for_job(
    client: TestClient,
    job_id: str,
    predicate,
    description: str,
) -> dict:
    deadline = time.time() + WAIT_TIMEOUT_SECONDS
    last: dict = {}
    while time.time() < deadline:
        response = client.get(f"/v1/jobs/{job_id}")
        assert response.status_code == 200
        last = response.json()
        if predicate(last):
            return last
        time.sleep(0.05)
    raise AssertionError(f"{job_id} never reached {description}; last={last}")


def chunk_rows(task_id: str) -> list[TranscriptionChunk]:
    db = db_session.SessionLocal()
    try:
        return (
            db.query(TranscriptionChunk)
            .filter(TranscriptionChunk.task_id == task_id)
            .order_by(TranscriptionChunk.idx.asc())
            .all()
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Slice 1: a real chunk failure is resumable, not a terminal "failed"
# ---------------------------------------------------------------------------


def test_chunk_failure_marks_task_failed_resumable_and_exposes_failed_idx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 1.0, "text": "{audio_stem}"}],
        text="{audio_stem}",
        aligned=True,
        fail={
            "stage": "transcribe",
            "chunk_index": 2,
            "code": "DECODER_FAILED",
            "message": "decoder exploded",
        },
    )
    stub_media(monkeypatch, 300_000)
    monkeypatch.setattr("backend.services.chunking.split_audio_chunks", chunk_split(THREE_CHUNKS))
    client = make_client(tmp_path, monkeypatch)

    job_id = create_long_job(client)
    task = wait_for_job(client, job_id, lambda row: row["status"] == "failed", "chunk failure")

    assert task["status"] == "failed"
    assert task["error_code"] == "CHUNK_FAILED"
    assert "Chunk 2/3 failed" in task["error"]
    assert "decoder exploded" in task["error"]
    assert task["progress"] == 1.0
    assert client.get(f"/v1/jobs/{job_id}/result").status_code == 409

    db = db_session.SessionLocal()
    try:
        stored = db.get(TranscriptionTask, job_id)
        assert stored.text is None
        assert db.query(DBSegment).filter(DBSegment.task_id == job_id).count() == 0
        logs = db.query(TaskLog).filter(TaskLog.task_id == job_id).all()
        assert any(
            json.loads(entry.data_json or "{}").get("chunk_idx") == 2
            and json.loads(entry.data_json or "{}").get("error_code") == "CHUNK_FAILED"
            for entry in logs
        )
        diagnostics = db.query(TaskDiagnostic).filter(TaskDiagnostic.task_id == job_id).all()
        assert any(
            item.error_code == "CHUNK_FAILED" and "Chunk 2/3 failed" in item.message
            for item in diagnostics
        )
    finally:
        db.close()

    chunks = chunk_rows(job_id)
    assert [(chunk.idx, chunk.status, chunk.error_code) for chunk in chunks] == [
        (1, "completed", None),
        (2, "failed", "CHUNK_FAILED"),
        (3, "queued", None),
    ]
    assert "Chunk 2/3 failed" in chunks[1].error


# ---------------------------------------------------------------------------
# Slice 2: per-chunk aligned segments survive for a later merge
# ---------------------------------------------------------------------------


def test_legacy_database_gains_chunk_segments_column_additively(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    db_session.init_db()

    columns = {column["name"] for column in inspect(db_session.engine).get_columns("transcription_chunks")}
    assert "segments_json" in columns

    db = db_session.SessionLocal()
    try:
        applied = {row.version for row in db.query(SchemaMigration).all()}
    finally:
        db.close()
    assert "20261101_001_chunk_segments" in applied
    # The legacy chunk columns keep their meaning; nothing was renamed or dropped.
    assert {"text", "error", "error_code", "status", "audio_path"} <= columns


def test_inline_chunk_pipeline_persists_chunk_local_aligned_segments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_speech_env(
        monkeypatch,
        segments=[
            {"start": 0.5, "end": 1.5, "text": "{audio_stem} one"},
            {"start": 2.0, "end": 3.0, "text": "{audio_stem} two"},
        ],
        text="{audio_stem} text",
        aligned=True,
    )
    stub_media(monkeypatch, 300_000)
    monkeypatch.setattr("backend.services.chunking.split_audio_chunks", chunk_split(THREE_CHUNKS))
    client = make_client(tmp_path, monkeypatch)

    job_id = create_long_job(client)
    task = wait_for_job(client, job_id, lambda row: row["status"] == "succeeded", "chunk completion")
    assert task["status"] == "succeeded"

    chunks = chunk_rows(job_id)
    assert [chunk.status for chunk in chunks] == ["completed", "completed", "completed"]
    stored = json.loads(chunks[1].segments_json)
    assert [(item["start"], item["end"], item["text"]) for item in stored] == [
        (0.5, 1.5, "chunk-0002 one"),
        (2.0, 3.0, "chunk-0002 two"),
    ]
    # Chunk rows keep chunk-local time; the global offset is applied at merge.
    assert chunks[1].start_ms == 118_000


# ---------------------------------------------------------------------------
# Slice 3: retry reuses completed chunks and rebuilds fine segments
# ---------------------------------------------------------------------------


def _seed_resumable_task(tmp_path: Path, *, task_id: str = "retry-reuse") -> tuple[str, list[int], Path]:
    chunks_dir = tmp_path / "derived-audio" / f"{task_id}_chunks"
    chunks_dir.mkdir(parents=True)
    normalized = tmp_path / "derived-audio" / f"{task_id}.wav"
    normalized.write_bytes(b"normalized")
    for index in (1, 2, 3):
        (chunks_dir / f"chunk-{index:04}.wav").write_bytes(b"chunk")
    db = db_session.SessionLocal()
    try:
        db.add(
            TranscriptionTask(
                id=task_id,
                filename="meeting.m4a",
                source="local",
                audio_path="uploads/meeting.m4a",
                normalized_audio_path=config.to_storage_path(normalized),
                status="failed_resumable",
                progress=100,
                duration_ms=360_000,
                model_name="whisper-base",
                error="Chunk 2/3 failed: decoder exploded",
                error_code="CHUNK_FAILED",
                options_json=json.dumps({"diarization": True, "num_speakers": 2}),
            )
        )
        db.flush()
        db.add(
            TranscriptionChunk(
                task_id=task_id,
                idx=1,
                audio_path=config.to_storage_path(chunks_dir / "chunk-0001.wav"),
                start_ms=0,
                end_ms=120_000,
                status="completed",
                progress=100,
                text="first block",
                segments_json=json.dumps(
                    [
                        {"id": 1, "start": 0.5, "end": 1.0, "text": "alpha"},
                        {"id": 2, "start": 1.5, "end": 2.0, "text": "beta"},
                    ]
                ),
            )
        )
        db.add(
            TranscriptionChunk(
                task_id=task_id,
                idx=2,
                audio_path=config.to_storage_path(chunks_dir / "chunk-0002.wav"),
                start_ms=118_000,
                end_ms=238_000,
                status="failed",
                progress=100,
                error="Chunk 2/3 failed: decoder exploded",
                error_code="CHUNK_FAILED",
            )
        )
        db.add(
            TranscriptionChunk(
                task_id=task_id,
                idx=3,
                audio_path=config.to_storage_path(chunks_dir / "chunk-0003.wav"),
                start_ms=236_000,
                end_ms=360_000,
                status="queued",
                progress=0,
            )
        )
        db.commit()
        chunk_ids = [
            row.id
            for row in db.query(TranscriptionChunk)
            .filter(TranscriptionChunk.task_id == task_id)
            .order_by(TranscriptionChunk.idx)
            .all()
        ]
    finally:
        db.close()
    return task_id, chunk_ids, normalized


def test_retry_failed_reuses_completed_chunk_and_rebuilds_aligned_segments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BEEBOX_MODEL_STORAGE_ROOT", str(tmp_path))
    fake_speech_env(
        monkeypatch,
        diarization={"speakers": ["1", "2"]},
    )
    db_session.init_db()
    task_id, chunk_ids, _normalized = _seed_resumable_task(tmp_path)

    calls: list[str] = []

    def fake_transcribe(_db, _row, path: Path, **_kwargs) -> TranscriptionResult:
        calls.append(path.name)
        if path.name == "chunk-0002.wav":
            return TranscriptionResult(
                text="delta",
                segments=[TranscriptSegment(id=1, start=0.4, end=1.4, text="delta")],
                raw_result_summary={"aligned": True},
            )
        return TranscriptionResult(
            text="epsilon",
            segments=[TranscriptSegment(id=1, start=0.2, end=1.2, text="epsilon")],
            raw_result_summary={"aligned": True},
        )

    monkeypatch.setattr(task_service, "_transcribe_path_for_row", fake_transcribe)

    from backend.app import create_app

    with TestClient(create_app()) as client:
        response = client.post(f"/v1/jobs/{task_id}/retry")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "queued"

        body = wait_for_job(
            client,
            task_id,
            lambda row: row["status"] in {"succeeded", "failed", "cancelled"},
            "retry completion",
        )
        assert body["status"] == "succeeded", (body.get("status"), body.get("error"), body.get("error_code"))
        assert body["error"] is None
        assert body["error_code"] is None

        result = client.get(f"/v1/jobs/{task_id}/result").json()

    # The completed chunk was reused, never re-transcribed.
    assert calls == ["chunk-0002.wav", "chunk-0003.wav"]
    db = db_session.SessionLocal()
    try:
        chunks = (
            db.query(TranscriptionChunk)
            .filter(TranscriptionChunk.task_id == task_id)
            .order_by(TranscriptionChunk.idx)
            .all()
        )
        assert [chunk.id for chunk in chunks] == chunk_ids
        assert [chunk.status for chunk in chunks] == ["completed", "completed", "completed"]
        assert chunks[0].text == "first block"
        assert json.loads(chunks[0].segments_json)[0]["text"] == "alpha"
        assert json.loads(chunks[1].segments_json)[0]["text"] == "delta"
        assert json.loads(chunks[2].segments_json)[0]["text"] == "epsilon"

        stored_task = db.get(TranscriptionTask, task_id)
        summary = json.loads(stored_task.options_json)["raw_result_summary"]
        assert summary["retry"] is True
        assert summary["cross_speaker_segments"] == 0
    finally:
        db.close()

    # Fine-grained aligned segments from every chunk landed in the transcript.
    assert [segment["text"] for segment in result["segments"]] == ["alpha", "beta", "delta", "epsilon"]
    assert [segment["start"] for segment in result["segments"]] == [0.5, 1.5, 118.4, 236.2]
    assert all(segment["speaker"] in {"1", "2"} for segment in result["segments"])
    assert result["segments"][0]["end"] == 1.0
    assert result["segments"][2]["start"] == 118.4
    assert result["text"] == "alpha beta delta epsilon"


# ---------------------------------------------------------------------------
# Slice 4: cancel stops the running worker and leaves no half transcript
# ---------------------------------------------------------------------------


def test_cancel_running_task_stops_chunks_and_writes_no_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 1.0, "text": "cancelled text"}],
        text="cancelled text",
        aligned=True,
        delay_seconds=1.5,
    )
    stub_media(monkeypatch, 300_000)
    monkeypatch.setattr("backend.services.chunking.split_audio_chunks", chunk_split(THREE_CHUNKS))
    client = make_client(tmp_path, monkeypatch)

    job_id = create_long_job(client)
    wait_for_job(client, job_id, lambda row: row["stage"] == "transcribe", "chunk 1 transcribing")

    cancelled = client.post(f"/v1/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["error_code"] == "TASK_CANCELLED"

    deadline = time.time() + WAIT_TIMEOUT_SECONDS
    chunks: list[TranscriptionChunk] = []
    while time.time() < deadline:
        chunks = chunk_rows(job_id)
        if chunks and all(chunk.status == "cancelled" for chunk in chunks):
            break
        time.sleep(0.05)

    task = client.get(f"/v1/jobs/{job_id}").json()
    assert task["status"] == "cancelled"
    assert task["error_code"] == "TASK_CANCELLED"
    assert client.get(f"/v1/jobs/{job_id}/result").status_code == 409
    assert [chunk.status for chunk in chunks] == ["cancelled", "cancelled", "cancelled"]

    db = db_session.SessionLocal()
    try:
        assert db.query(DBSegment).filter(DBSegment.task_id == job_id).count() == 0
    finally:
        db.close()
    exports_dir = tmp_path / "exports"
    assert not exports_dir.exists() or not any(exports_dir.rglob("*"))


# ---------------------------------------------------------------------------
# Slice 5: successful completion cleans derived audio
# ---------------------------------------------------------------------------


def test_delete_derived_on_complete_defaults_on_and_can_be_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The legacy task switch still resolves; /v1 jobs always clean up (#40)."""
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("BEEBOX_DELETE_DERIVED_ON_COMPLETE", raising=False)
    # Contract default: cleanup is on even without an explicit setting.
    assert config.delete_derived_audio_on_complete() is True
    # An explicit persisted setting can still disable it.
    config.update_media_storage_config(delete_derived_on_complete=False)
    assert config.delete_derived_audio_on_complete() is False
    # The environment switch overrides the setting in both directions.
    monkeypatch.setenv("BEEBOX_DELETE_DERIVED_ON_COMPLETE", "1")
    assert config.delete_derived_audio_on_complete() is True
    monkeypatch.setenv("BEEBOX_DELETE_DERIVED_ON_COMPLETE", "0")
    assert config.delete_derived_audio_on_complete() is False
    monkeypatch.setenv("BEEBOX_DELETE_DERIVED_ON_COMPLETE", "false")
    assert config.delete_derived_audio_on_complete() is False
    monkeypatch.delenv("BEEBOX_DELETE_DERIVED_ON_COMPLETE")
    config.update_media_storage_config(delete_derived_on_complete=True)
    assert config.delete_derived_audio_on_complete() is True


def test_successful_completion_cleans_derived_audio_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No env override and no media-storage setting: the contract default applies.
    monkeypatch.delenv("BEEBOX_DELETE_DERIVED_ON_COMPLETE", raising=False)
    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.5, "end": 1.5, "text": "{audio_stem} text"}],
        text="{audio_stem} text",
        aligned=True,
    )
    stub_media(monkeypatch, 300_000)
    monkeypatch.setattr("backend.services.chunking.split_audio_chunks", chunk_split(THREE_CHUNKS))
    client = make_client(tmp_path, monkeypatch)

    job_id = create_long_job(client)
    task = wait_for_job(client, job_id, lambda row: row["status"] == "succeeded", "chunk completion")
    assert task["status"] == "succeeded"

    normalized = tmp_path / "derived-audio" / f"{job_id}.wav"
    chunks_dir = tmp_path / "derived-audio" / f"{job_id}_chunks"
    deadline = time.time() + WAIT_TIMEOUT_SECONDS
    while time.time() < deadline:
        db = db_session.SessionLocal()
        try:
            cleared = db.get(TranscriptionTask, job_id).normalized_audio_path is None
        finally:
            db.close()
        if cleared and not normalized.exists() and not chunks_dir.exists():
            break
        time.sleep(0.05)

    assert not normalized.exists()
    assert not chunks_dir.exists()
    db = db_session.SessionLocal()
    try:
        stored = db.get(TranscriptionTask, job_id)
        assert stored.normalized_audio_path is None
        assert stored.text
    finally:
        db.close()
    # Chunk rows stay for auditability even though their wavs are gone.
    chunks = chunk_rows(job_id)
    assert [chunk.status for chunk in chunks] == ["completed", "completed", "completed"]
    assert all(chunk.text for chunk in chunks)


# ---------------------------------------------------------------------------
# Slice 6: delete removes every task artefact
# ---------------------------------------------------------------------------


def _seed_completed_task(
    tmp_path: Path,
    *,
    task_id: str,
    with_artifacts: bool = True,
) -> dict[str, Path]:
    upload = tmp_path / "uploads" / f"{task_id}.m4a"
    normalized = tmp_path / "derived-audio" / f"{task_id}.wav"
    chunks_dir = tmp_path / "derived-audio" / f"{task_id}_chunks"
    chunk = chunks_dir / "chunk-0001.wav"
    worker_dir = tmp_path / "cache" / "task-workers" / task_id
    export = tmp_path / "exports" / f"archive-{task_id}.txt"
    if with_artifacts:
        for path in (upload, normalized, chunk, worker_dir / "request.json", export):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"data")
    db = db_session.SessionLocal()
    try:
        db.add(
            TranscriptionTask(
                id=task_id,
                filename=f"{task_id}.m4a",
                source="local",
                audio_path=config.to_storage_path(upload),
                normalized_audio_path=config.to_storage_path(normalized),
                status="completed",
                progress=100,
                duration_ms=120_000,
                text="hello",
                options_json="{}",
            )
        )
        db.add(DBSegment(task_id=task_id, idx=1, start_ms=0, end_ms=1000, text="hello", speaker="1"))
        db.add(
            TranscriptionChunk(
                task_id=task_id,
                idx=1,
                audio_path=config.to_storage_path(chunk),
                start_ms=0,
                end_ms=120_000,
                status="completed",
                progress=100,
                text="hello",
            )
        )
        db.add(TaskLog(task_id=task_id, stage="completed", level="info", message="done", data_json="{}"))
        db.add(TaskDiagnostic(task_id=task_id, stage="x", error_code="X", message="diag"))
        db.commit()
    finally:
        db.close()
    return {
        "upload": upload,
        "normalized": normalized,
        "chunks_dir": chunks_dir,
        "chunk": chunk,
        "worker_dir": worker_dir,
        "export": export,
    }


def test_delete_task_removes_exports_and_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    db_session.init_db()
    first = "delete-first"
    second = "delete-second"
    first_paths = _seed_completed_task(tmp_path, task_id=first)
    _seed_completed_task(tmp_path, task_id=second)
    model_sentinel = tmp_path / "models" / "qwen3-asr-1.7b" / "marker"
    model_sentinel.parent.mkdir(parents=True)
    model_sentinel.write_text("keep", encoding="utf-8")

    from backend.app import create_app

    with TestClient(create_app()) as client:
        deleted = client.delete(f"/v1/jobs/{first}")

        assert deleted.status_code == 204, deleted.text
        assert client.get(f"/v1/jobs/{first}").status_code == 404
        assert client.get(f"/v1/jobs/{second}").status_code == 200

        second_deleted = client.delete(f"/v1/jobs/{second}")
        assert second_deleted.status_code == 204, second_deleted.text

    for path in first_paths.values():
        assert not path.exists()
    assert model_sentinel.read_text(encoding="utf-8") == "keep"

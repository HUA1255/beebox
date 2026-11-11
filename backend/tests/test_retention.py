"""Acceptance tests for ticket #40: media-once semantics and retention.

Every assertion goes through a public seam:

* ``/v1`` HTTP routes for job creation, results, exports and file downloads;
* :class:`backend.services.retention.RetentionService` with an injected clock
  for expiry, which is the ticket's explicit test hook;
* the documented fake speech engine for deterministic success/failure.

Only setup writes rows directly (old timestamps and orphaned files cannot be
produced deterministically through HTTP).
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.database import session as db_session
from backend.database.models import (
    TaskLog,
    TranscriptSegment as DBSegment,
    TranscriptionChunk,
    TranscriptionTask,
    V1File,
)
from backend.services.retention import RetentionService
from support import fake_speech_env, make_wav_bytes


def make_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BEEBOX_MODEL_STORAGE_ROOT", str(tmp_path / "models-root"))
    monkeypatch.delenv("BEEBOX_API_TOKEN", raising=False)
    # Drive the real pipeline in-process on the scripted engine (no model files,
    # no GPU, no subprocess worker).
    monkeypatch.setenv("BEEBOX_INLINE_LOCAL_TRANSCRIPTION", "1")
    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 1.0, "text": "retention transcript"}],
        text="retention transcript",
        duration=1.0,
        aligned=True,
    )
    from backend.app import create_app

    return TestClient(create_app())


def upload(client: TestClient, *, filename: str = "clip.wav", content: bytes | None = None):
    return client.post(
        "/v1/files",
        files={"file": (filename, content if content is not None else make_wav_bytes(), "audio/wav")},
    )


def create_job(client: TestClient, file_id: str):
    return client.post(
        "/v1/jobs",
        json={"kind": "transcription", "source": {"type": "file", "file_id": file_id}},
    )


def wait_for_status(client: TestClient, job_id: str, *, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/v1/jobs/{job_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] in {"succeeded", "failed", "cancelled"}:
            return body
        time.sleep(0.05)
    pytest.fail(f"job {job_id} did not reach a terminal status: {body}")


def wait_until(predicate, *, timeout: float = 10.0, message: str = "condition not met") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail(message)


def _source_path(tmp_path: Path, file_id: str, suffix: str = ".mp4") -> Path:
    return tmp_path / "uploads" / "v1" / f"{file_id}{suffix}"


def _db():
    db_session.init_db()
    return db_session.SessionLocal()


# ---------------------------------------------------------------------------
# Successful jobs delete source + derived media, keep results/exports
# ---------------------------------------------------------------------------


def test_successful_job_deletes_source_and_derived_media_but_keeps_result_and_exports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(tmp_path, monkeypatch)
    # A non-WAV source forces a derived/transcoded audio file.
    uploaded = upload(client, filename="clip.mp4")
    assert uploaded.status_code == 201, uploaded.text
    file_id = uploaded.json()["id"]
    source = _source_path(tmp_path, file_id)
    assert source.is_file()

    created = create_job(client, file_id)
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]
    finished = wait_for_status(client, job_id)
    assert finished["status"] == "succeeded", finished

    derived = tmp_path / "derived-audio" / f"{job_id}.wav"
    wait_until(lambda: not source.exists(), message="source media was not deleted")
    assert not derived.exists()
    assert not (tmp_path / "derived-audio" / f"{job_id}_chunks").exists()

    # The file resource no longer stores media.
    assert client.get(f"/v1/files/{file_id}").status_code == 404
    assert client.get(f"/v1/files/{file_id}/content").status_code == 404
    assert client.get("/v1/files").json()["total"] == 0

    # The job, structured result and every export stay available.
    assert client.get(f"/v1/jobs/{job_id}").json()["status"] == "succeeded"
    result = client.get(f"/v1/jobs/{job_id}/result")
    assert result.status_code == 200, result.text
    assert result.json()["text"] == "retention transcript"
    for export_format in ("txt", "md", "srt"):
        exported = client.get(f"/v1/jobs/{job_id}/exports/{export_format}")
        assert exported.status_code == 200, exported.text
        assert exported.content


def test_shared_upload_is_kept_until_the_last_referencing_job_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 1.0, "text": "shared transcript"}],
        text="shared transcript",
        duration=1.0,
        delay_seconds=0.5,
    )
    client = make_client(tmp_path, monkeypatch)
    file_id = upload(client, filename="shared.mp4").json()["id"]
    source = _source_path(tmp_path, file_id)

    first = create_job(client, file_id).json()["id"]
    second = create_job(client, file_id).json()["id"]

    assert wait_for_status(client, first)["status"] == "succeeded"
    # The second job still needs the source, so success cleanup must leave it.
    assert source.is_file()
    assert client.get(f"/v1/files/{file_id}/content").status_code == 200

    assert wait_for_status(client, second)["status"] == "succeeded"
    wait_until(lambda: not source.exists(), message="shared source was not deleted after the last job")
    assert client.get(f"/v1/files/{file_id}").status_code == 404


# ---------------------------------------------------------------------------
# Failed jobs keep the source for a retry
# ---------------------------------------------------------------------------


def test_failed_job_keeps_source_media_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(tmp_path, monkeypatch)
    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 1.0, "text": "never produced"}],
        fail={"stage": "transcribe", "code": "MODEL_LOAD_FAILED", "message": "scripted failure"},
    )
    file_id = upload(client, filename="failed.mp4").json()["id"]
    source = _source_path(tmp_path, file_id)

    job_id = create_job(client, file_id).json()["id"]
    finished = wait_for_status(client, job_id)
    assert finished["status"] == "failed", finished

    # The source still exists and is downloadable, so a retry can reuse it.
    assert source.is_file()
    content = client.get(f"/v1/files/{file_id}/content")
    assert content.status_code == 200
    assert content.content


# ---------------------------------------------------------------------------
# Retention: expiry with an injectable clock
# ---------------------------------------------------------------------------


def test_retention_configuration_defaults_to_30_days_and_reads_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BEEBOX_RETENTION_DAYS", raising=False)
    assert config.get_retention_days() == 30

    monkeypatch.setenv("BEEBOX_RETENTION_DAYS", "7")
    assert config.get_retention_days() == 7

    monkeypatch.setenv("BEEBOX_RETENTION_DAYS", "not-a-number")
    assert config.get_retention_days() == 30


def test_expired_jobs_and_all_remaining_files_are_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEEBOX_RETENTION_DAYS", "30")
    client = make_client(tmp_path, monkeypatch)

    succeeded_file = upload(client, filename="done.mp4").json()["id"]
    succeeded_job = create_job(client, succeeded_file).json()["id"]
    assert wait_for_status(client, succeeded_job)["status"] == "succeeded"

    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 1.0, "text": "failed"}],
        fail={"stage": "transcribe", "code": "MODEL_LOAD_FAILED", "message": "boom"},
    )
    failed_file = upload(client, filename="failed.mp4").json()["id"]
    failed_source = _source_path(tmp_path, failed_file)
    failed_job = create_job(client, failed_file).json()["id"]
    assert wait_for_status(client, failed_job)["status"] == "failed"
    assert failed_source.is_file()

    failed_export = tmp_path / "exports" / f"{failed_job}.txt"
    failed_export.parent.mkdir(parents=True, exist_ok=True)
    failed_export.write_text("stale export", encoding="utf-8")

    later = datetime.now(UTC) + timedelta(days=31)
    service = RetentionService(clock=lambda: later)
    summary = service.run_once()

    assert summary["retention_days"] == 30
    assert summary["deleted_jobs"] == 2
    assert set(summary["expired_jobs"]) == {succeeded_job, failed_job}

    for job_id in (succeeded_job, failed_job):
        assert client.get(f"/v1/jobs/{job_id}").status_code == 404
        assert client.get(f"/v1/jobs/{job_id}/result").status_code == 404
        assert client.get(f"/v1/jobs/{job_id}/exports/txt").status_code == 404

    assert client.get(f"/v1/files/{failed_file}").status_code == 404
    assert not failed_source.exists()
    assert not failed_export.exists()
    assert not (tmp_path / "derived-audio" / f"{failed_job}.wav").exists()
    assert not (tmp_path / "derived-audio" / f"{failed_job}_chunks").exists()

    db = _db()
    try:
        for job_id in (succeeded_job, failed_job):
            assert db.get(TranscriptionTask, job_id) is None
            assert db.query(DBSegment).filter(DBSegment.task_id == job_id).count() == 0
            assert db.query(TranscriptionChunk).filter(TranscriptionChunk.task_id == job_id).count() == 0
            assert db.query(TaskLog).filter(TaskLog.task_id == job_id).count() == 0
    finally:
        db.close()


def test_expired_managed_job_removes_upload_derived_chunks_and_exports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(tmp_path, monkeypatch)
    task_id = "expired-managed"
    upload_path = tmp_path / "uploads" / f"{task_id}.wav"
    normalized = tmp_path / "derived-audio" / f"{task_id}.wav"
    chunk_dir = tmp_path / "derived-audio" / f"{task_id}_chunks"
    chunk_path = chunk_dir / "chunk-0001.wav"
    export_path = tmp_path / "exports" / f"{task_id}.txt"
    for path in (upload_path, normalized, chunk_path, export_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")

    old = datetime.now(UTC) - timedelta(days=31)
    db = _db()
    try:
        db.add(
            TranscriptionTask(
                id=task_id,
                filename="old.wav",
                source="local",
                audio_path=f"uploads/{task_id}.wav",
                normalized_audio_path=f"derived-audio/{task_id}.wav",
                status="completed",
                progress=100,
                text="old result",
                options_json=json.dumps({"output_formats": ["txt"]}),
                created_at=old,
                updated_at=old,
                completed_at=old,
            )
        )
        db.add(DBSegment(task_id=task_id, idx=1, start_ms=0, end_ms=1000, text="old result"))
        db.add(
            TranscriptionChunk(
                task_id=task_id,
                idx=1,
                audio_path=f"derived-audio/{task_id}_chunks/chunk-0001.wav",
                start_ms=0,
                end_ms=1000,
                status="completed",
            )
        )
        db.commit()
    finally:
        db.close()

    summary = RetentionService(clock=lambda: datetime.now(UTC)).run_once()

    assert task_id in summary["expired_jobs"]
    assert client.get(f"/v1/jobs/{task_id}").status_code == 404
    assert not upload_path.exists()
    assert not normalized.exists()
    assert not chunk_dir.exists()
    assert not export_path.exists()


def test_retention_days_control_when_a_job_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEEBOX_RETENTION_DAYS", "7")
    client = make_client(tmp_path, monkeypatch)
    file_id = upload(client).json()["id"]
    job_id = create_job(client, file_id).json()["id"]
    assert wait_for_status(client, job_id)["status"] == "succeeded"

    inside = datetime.now(UTC) + timedelta(days=6)
    RetentionService(clock=lambda: inside).run_once()
    assert client.get(f"/v1/jobs/{job_id}").status_code == 200

    outside = datetime.now(UTC) + timedelta(days=8)
    summary = RetentionService(clock=lambda: outside).run_once()
    assert job_id in summary["expired_jobs"]
    assert client.get(f"/v1/jobs/{job_id}").status_code == 404


def test_retained_job_keeps_every_result_file_available_until_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEEBOX_RETENTION_DAYS", "30")
    client = make_client(tmp_path, monkeypatch)
    file_id = upload(client).json()["id"]
    job_id = create_job(client, file_id).json()["id"]
    assert wait_for_status(client, job_id)["status"] == "succeeded"

    inside = datetime.now(UTC) + timedelta(days=29)
    RetentionService(clock=lambda: inside).run_once()

    assert client.get(f"/v1/jobs/{job_id}").status_code == 200
    assert client.get(f"/v1/jobs/{job_id}/result").status_code == 200
    assert client.get(f"/v1/jobs/{job_id}/exports/srt").status_code == 200


# ---------------------------------------------------------------------------
# Orphaned v1 files
# ---------------------------------------------------------------------------


def test_orphaned_upload_is_cleaned_only_after_the_retention_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEEBOX_RETENTION_DAYS", "30")
    client = make_client(tmp_path, monkeypatch)
    old_file = upload(client, filename="old.mp4").json()["id"]
    fresh_file = upload(client, filename="fresh.mp4").json()["id"]
    old_path = _source_path(tmp_path, old_file)
    fresh_path = _source_path(tmp_path, fresh_file)

    db = _db()
    try:
        row = db.get(V1File, old_file)
        assert row is not None
        row.created_at = datetime.now(UTC) - timedelta(days=31)
        db.commit()
    finally:
        db.close()

    summary = RetentionService(clock=lambda: datetime.now(UTC)).run_once()

    assert summary["orphan_files_removed"] == 1
    assert client.get(f"/v1/files/{old_file}").status_code == 404
    assert not old_path.exists()
    assert client.get(f"/v1/files/{fresh_file}").status_code == 200
    assert fresh_path.is_file()


def test_orphaned_blob_without_a_file_row_is_removed_after_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEEBOX_RETENTION_DAYS", "30")
    client = make_client(tmp_path, monkeypatch)
    # Touch the data directory so the app knows it.
    assert client.get("/v1/health").status_code == 200

    orphan_dir = tmp_path / "uploads" / "v1"
    orphan_dir.mkdir(parents=True, exist_ok=True)
    orphan = orphan_dir / "00000000-0000-0000-0000-000000000000.mp4"
    orphan.write_bytes(b"orphan media")
    old = (datetime.now(UTC) - timedelta(days=31)).timestamp()
    os.utime(orphan, (old, old))

    summary = RetentionService(clock=lambda: datetime.now(UTC)).run_once()

    assert summary["orphan_blobs_removed"] == 1
    assert not orphan.exists()


# ---------------------------------------------------------------------------
# In-process scheduling: startup run + periodic ticks
# ---------------------------------------------------------------------------


def _seed_completed_task(tmp_path: Path, task_id: str) -> Path:
    db = _db()
    try:
        db.add(
            TranscriptionTask(
                id=task_id,
                filename="old.wav",
                source="local",
                audio_path="uploads/external-old.wav",
                status="completed",
                progress=100,
                text="old result",
                options_json=json.dumps({"_source_kind": "external"}),
                created_at=datetime.now(UTC) - timedelta(days=40),
                updated_at=datetime.now(UTC) - timedelta(days=40),
                completed_at=datetime.now(UTC) - timedelta(days=40),
            )
        )
        db.commit()
    finally:
        db.close()
    return tmp_path / "uploads" / "external-old.wav"


def test_start_runs_cleanup_immediately_without_waiting_for_the_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(tmp_path, monkeypatch)
    task_id = "expired-on-start"
    _seed_completed_task(tmp_path, task_id)

    # A one-hour interval means only the immediate startup run can delete it.
    later = datetime.now(UTC) + timedelta(days=41)
    service = RetentionService(clock=lambda: later, interval_seconds=3600)
    service.start()
    try:
        wait_until(
            lambda: client.get(f"/v1/jobs/{task_id}").status_code == 404,
            message="startup cleanup did not run immediately",
        )
    finally:
        service.stop()


def test_periodic_tick_deletes_a_job_that_becomes_expired_while_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(tmp_path, monkeypatch)
    file_id = upload(client).json()["id"]
    job_id = create_job(client, file_id).json()["id"]
    assert wait_for_status(client, job_id)["status"] == "succeeded"

    service = RetentionService(interval_seconds=0.05)
    service.start()
    try:
        # Fresh job survives the startup tick.
        time.sleep(0.2)
        assert client.get(f"/v1/jobs/{job_id}").status_code == 200

        # Age the row while the service keeps ticking.
        db = _db()
        try:
            row = db.get(TranscriptionTask, job_id)
            assert row is not None
            row.updated_at = datetime.now(UTC) - timedelta(days=31)
            row.created_at = datetime.now(UTC) - timedelta(days=31)
            db.commit()
        finally:
            db.close()

        wait_until(
            lambda: client.get(f"/v1/jobs/{job_id}").status_code == 404,
            message="periodic cleanup did not delete the aged job",
        )
    finally:
        service.stop()


def test_application_startup_runs_the_cleanup_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Zero retention makes a just-created completed row expired; only the app
    # lifespan's startup cleanup can remove it.
    monkeypatch.setenv("BEEBOX_RETENTION_DAYS", "0")
    client = make_client(tmp_path, monkeypatch)
    task_id = "expired-at-app-start"
    _seed_completed_task(tmp_path, task_id)

    from backend.app import create_app

    with TestClient(create_app()) as started:
        wait_until(
            lambda: started.get(f"/v1/jobs/{task_id}").status_code == 404,
            message="app lifespan did not run the retention service",
        )

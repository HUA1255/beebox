from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.database import session as db_session
from backend.database.models import TranscriptionChunk, TranscriptionTask
from backend.services import task_runtime
from backend.services import tasks as task_service
from backend.services.local_task_worker import run_local_task_worker


def _init_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    db_session.init_db()


def _add_task(db, task_id: str, status: str, *, audio_path: str = "uploads/a.wav") -> TranscriptionTask:
    row = TranscriptionTask(
        id=task_id,
        filename="a.wav",
        source="local",
        audio_path=audio_path,
        status=status,
        progress=50.0,
        options_json="{}",
    )
    db.add(row)
    db.commit()
    return row


def test_retry_retranscribe_delete_reject_active_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_db(tmp_path, monkeypatch)
    monkeypatch.setattr(task_service, "start_task_in_background", lambda task_id: None)
    db = db_session.SessionLocal()
    try:
        _add_task(db, "active-1", "transcribing")
        for action in (
            lambda: task_service.retry_task(db, "active-1"),
            lambda: task_service.retranscribe_task(db, "active-1"),
            lambda: task_service.delete_task(db, "active-1"),
        ):
            with pytest.raises(task_service.TaskActiveError):
                action()
        assert db.get(TranscriptionTask, "active-1").status == "transcribing"
    finally:
        db.close()


def test_retry_terminal_task_resets_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_db(tmp_path, monkeypatch)
    monkeypatch.setattr(task_service, "start_task_in_background", lambda task_id: None)
    db = db_session.SessionLocal()
    try:
        row = _add_task(db, "failed-1", "failed")
        row.error = "boom"
        row.completed_at = task_service._utc_now()
        db.commit()

        response = task_service.retry_task(db, "failed-1")

        assert response is not None
        assert response.status == "queued"
        assert response.error is None
        assert response.completed_at is None
    finally:
        db.close()


def test_delete_all_tasks_skips_active(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_db(tmp_path, monkeypatch)
    db = db_session.SessionLocal()
    try:
        _add_task(db, "active-2", "queued")
        _add_task(db, "done-2", "completed")

        deleted = task_service.delete_all_tasks(db)

        assert deleted == 1
        assert db.get(TranscriptionTask, "active-2") is not None
        assert db.get(TranscriptionTask, "done-2") is None
    finally:
        db.close()


def test_reconcile_orphan_tasks_leaves_queued_rows_for_the_new_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queued row has no orphaned worker; only running rows are interrupted.

    The GPU queue is in-memory, so rejecting queued rows here would undo the
    restart adoption from ticket #64 via the legacy ``active_tasks`` surface.
    """
    _init_db(tmp_path, monkeypatch)
    db = db_session.SessionLocal()
    try:
        _add_task(db, "orphan-running", "transcribing")
        _add_task(db, "surviving-queued", "queued")

        orphans = task_service.reconcile_orphan_tasks(db)

        assert [row.id for row in orphans] == ["orphan-running"]
        running = db.get(TranscriptionTask, "orphan-running")
        assert running.status == "interrupted"
        assert running.error_code == "TASK_INTERRUPTED"
        assert running.progress == 50
        queued = db.get(TranscriptionTask, "surviving-queued")
        assert queued.status == "queued"
        assert queued.progress == 50
        assert queued.error is None
        assert queued.error_code is None
    finally:
        db.close()


def test_active_task_endpoints_return_409(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_db(tmp_path, monkeypatch)
    from backend.app import create_app

    client = TestClient(create_app())
    db = db_session.SessionLocal()
    try:
        _add_task(db, "active-3", "transcribing")
    finally:
        db.close()

    assert client.post("/v1/jobs/active-3/retry").status_code == 409
    assert client.delete("/v1/jobs/active-3").status_code == 409

    db = db_session.SessionLocal()
    try:
        row = db.get(TranscriptionTask, "active-3")
        assert row is not None and row.status == "transcribing"
    finally:
        db.close()


def test_run_task_failure_handler_survives_deleted_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_db(tmp_path, monkeypatch)
    db = db_session.SessionLocal()
    try:
        row = _add_task(db, "ghost-1", "transcribing")
        task_id = row.id
        db.delete(row)
        db.commit()

        ghost = db.get(TranscriptionTask, task_id)
        assert ghost is None
        # row is detached/deleted; run_task must return without raising.
        task_service.run_task(db, row)
    finally:
        db.close()


def test_fail_task_after_escape_marks_active_task_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_db(tmp_path, monkeypatch)
    db = db_session.SessionLocal()
    try:
        _add_task(db, "escape-1", "transcribing")
        task_service._fail_task_after_escape("escape-1")

        row = db.get(TranscriptionTask, "escape-1")
        assert row.status == "failed"
        assert row.error_code == "WORKER_RUNTIME_ERROR"
    finally:
        db.close()


def test_fail_task_after_escape_leaves_terminal_task_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_db(tmp_path, monkeypatch)
    db = db_session.SessionLocal()
    try:
        _add_task(db, "escape-2", "completed")
        task_service._fail_task_after_escape("escape-2")
        assert db.get(TranscriptionTask, "escape-2").status == "completed"
    finally:
        db.close()


def test_request_cancel_ignores_untracked_task() -> None:
    assert task_runtime.request_cancel("untracked-xyz") is None
    assert "untracked-xyz" not in task_runtime.snapshot()["cancelled_task_ids"]

    task_runtime.mark_queued("tracked-xyz")
    assert task_runtime.request_cancel("tracked-xyz") == "queued"
    assert task_runtime.is_cancelled("tracked-xyz")
    task_runtime.mark_finished("tracked-xyz")
    assert "tracked-xyz" not in task_runtime.snapshot()["cancelled_task_ids"]


def test_local_task_worker_missing_request_writes_failed_result(tmp_path: Path) -> None:
    result = tmp_path / "result.json"
    exit_code = run_local_task_worker(tmp_path / "missing-request.json", result)
    assert exit_code == 1
    payload = json.loads(result.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["error"]


def test_cancel_requested_during_postprocessing_stays_cancelled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.services import postprocess as postprocess_service
    from support import fake_speech_env, make_wav_bytes

    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 1.0, "text": "hello"}],
        text="hello",
        duration=1.0,
    )
    real_postprocess = postprocess_service.process_segments

    def slow_postprocess(segments, **kwargs):
        import time

        time.sleep(0.3)
        return real_postprocess(segments, **kwargs)

    monkeypatch.setattr("backend.services.tasks.postprocess.process_segments", slow_postprocess)
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BEEBOX_INLINE_LOCAL_TRANSCRIPTION", "1")
    from backend.app import create_app

    client = TestClient(create_app())

    uploaded = client.post(
        "/v1/files",
        files={"file": ("sample.wav", make_wav_bytes(), "audio/wav")},
    )
    assert uploaded.status_code == 201, uploaded.text
    response = client.post(
        "/v1/jobs",
        json={
            "source": {"type": "file", "file_id": uploaded.json()["id"]},
            "options": {"model": "whisper-base", "diarization": False},
        },
    )
    assert response.status_code == 201, response.text
    task_id = response.json()["id"]

    cancelled = None
    for _ in range(100):
        task = client.get(f"/v1/jobs/{task_id}").json()
        if task["stage"] == "postprocess" and cancelled is None:
            cancelled = client.post(f"/v1/jobs/{task_id}/cancel")
        if task["status"] in {"succeeded", "failed", "cancelled"}:
            break
        import time

        time.sleep(0.05)
    assert cancelled is not None and cancelled.status_code == 200
    assert task["status"] == "cancelled"
    assert client.get(f"/v1/jobs/{task_id}/result").status_code == 409
    task_runtime.mark_finished(task_id)

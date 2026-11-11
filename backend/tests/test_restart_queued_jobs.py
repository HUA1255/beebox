"""Ticket #64: a restart keeps queued jobs and does not fake their progress.

The deploy flow pauses the GPU queue, restarts the service and resumes the
queue. Callers keep submitting while the queue is paused, so those jobs sit in
``queued`` when the old process exits. The GPU queue is in-memory; the startup
recovery in the lifespan must re-adopt those rows in submission order instead
of failing them. Only jobs a worker had actually picked up are interrupted.

Every assertion goes through the public ``/v1`` surface. A restart is
simulated in-process: the in-memory queue is reset (as a new process would
start with an empty one) and then the same startup functions the lifespan
calls run against the database.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.database import session as db_session
from backend.database.models import TranscriptionTask
from backend.services import gpu_queue
from backend.services.tasks import mark_interrupted_tasks, resume_queued_tasks
from support import fake_speech_env, make_wav_bytes

JOB_DELAY_SECONDS = 0.4
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
# Slow CI runners need headroom; local runs still settle quickly.
WAIT_TIMEOUT_SECONDS = 60.0


def make_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BEEBOX_MODEL_STORAGE_ROOT", str(tmp_path / "models-root"))
    monkeypatch.setenv("BEEBOX_INLINE_LOCAL_TRANSCRIPTION", "1")
    monkeypatch.setenv("BEEBOX_DELETE_DERIVED_ON_COMPLETE", "1")
    monkeypatch.delenv("BEEBOX_API_TOKEN", raising=False)
    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 1.0, "text": "restart transcript"}],
        text="restart transcript",
        duration=1.0,
        delay_seconds=JOB_DELAY_SECONDS,
    )
    from backend.app import create_app

    return TestClient(create_app())


def simulate_restart() -> list[str]:
    """Run the startup recovery the lifespan runs, without a second process."""
    db_session.init_db()
    db = db_session.SessionLocal()
    try:
        mark_interrupted_tasks(db)
        return resume_queued_tasks(db)
    finally:
        db.close()


def upload(client: TestClient) -> str:
    response = client.post(
        "/v1/files",
        files={"file": ("restart.wav", make_wav_bytes(), "audio/wav")},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def create_job(client: TestClient, file_id: str) -> dict:
    response = client.post(
        "/v1/jobs",
        json={"kind": "transcription", "source": {"type": "file", "file_id": file_id}},
    )
    assert response.status_code == 201, response.text
    return response.json()


def get_job(client: TestClient, job_id: str) -> dict:
    response = client.get(f"/v1/jobs/{job_id}")
    assert response.status_code == 200, response.text
    return response.json()


def queue(client: TestClient) -> dict:
    response = client.get("/v1/queue")
    assert response.status_code == 200, response.text
    return response.json()


def test_queued_jobs_survive_restart_and_run_in_submission_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(tmp_path, monkeypatch)
    file_id = upload(client)

    # The deploy flow pauses the queue before restarting; callers keep
    # submitting and those jobs are supposed to stay queued.
    assert client.post("/v1/queue/pause").json()["paused"] is True
    job_ids: list[str] = []
    for _ in range(3):
        created = create_job(client, file_id)
        assert created["status"] == "queued"
        job_ids.append(created["id"])
        # Distinct created_at values make the original submission order
        # unambiguous even on a coarse clock.
        time.sleep(0.01)

    assert [get_job(client, job_id)["status"] for job_id in job_ids] == ["queued"] * 3
    assert [get_job(client, job_id)["progress"] for job_id in job_ids] == [0.0] * 3

    # Restart: the old process loses the in-memory queue and its pause state.
    gpu_queue.reset()
    adopted = simulate_restart()
    assert adopted == job_ids

    # The new process starts unpaused, so the queue drains the adopted jobs in
    # their original submission order.
    seen_order: list[str] = []
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        current = queue(client)["current_job_id"]
        if current is not None and (not seen_order or seen_order[-1] != current):
            seen_order.append(current)
        if all(get_job(client, job_id)["status"] in TERMINAL_STATUSES for job_id in job_ids):
            break
        time.sleep(0.02)
    else:  # pragma: no cover - only on a hung queue
        pytest.fail(f"adopted jobs did not finish: {[get_job(client, i)['status'] for i in job_ids]}")

    assert seen_order == job_ids
    assert [get_job(client, job_id)["status"] for job_id in job_ids] == ["succeeded"] * 3
    assert [get_job(client, job_id)["progress"] for job_id in job_ids] == [1.0] * 3
    assert queue(client)["length"] == 0


def test_one_unresumable_row_does_not_stop_startup_or_the_other_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken queued row must not crash the lifespan or lose its siblings."""
    from backend.services import tasks as task_service

    client = make_client(tmp_path, monkeypatch)
    file_id = upload(client)
    assert client.post("/v1/queue/pause").json()["paused"] is True
    broken = create_job(client, file_id)["id"]
    # Distinct created_at values keep the adoption order unambiguous.
    time.sleep(0.01)
    healthy = create_job(client, file_id)["id"]

    gpu_queue.reset()
    original = task_service.start_task_in_background

    def flaky(task_id: str) -> None:
        if task_id == broken:
            raise RuntimeError("corrupt options")
        original(task_id)

    monkeypatch.setattr(task_service, "start_task_in_background", flaky)

    # Startup completes despite the broken row; both ids are reported as adopted
    # attempts so an operator log shows what was looked at.
    adopted = simulate_restart()
    assert adopted == [broken, healthy]

    failed = get_job(client, broken)
    assert failed["status"] == "failed", failed
    assert failed["error_code"] == "TASK_RESUME_FAILED", failed
    assert failed["retryable"] is True, failed
    assert "could not be resumed" in failed["error"]

    finished = wait_until_terminal(client, healthy)
    assert finished["status"] == "succeeded", finished


def wait_until_terminal(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    body: dict = {}
    while time.monotonic() < deadline:
        body = get_job(client, job_id)
        if body["status"] in TERMINAL_STATUSES:
            return body
        time.sleep(0.02)
    pytest.fail(f"job {job_id} did not reach a terminal status: {body}")


def test_running_job_is_interrupted_without_claiming_full_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(tmp_path, monkeypatch)
    db_session.init_db()
    db = db_session.SessionLocal()
    try:
        db.add(
            TranscriptionTask(
                id="restart-running",
                filename="running.wav",
                source="local",
                audio_path="uploads/running.wav",
                status="transcribing",
                progress=42,
                model_name="whisper-base",
                options_json=json.dumps({"_source_kind": "external"}),
            )
        )
        db.commit()
    finally:
        db.close()

    simulate_restart()

    interrupted = get_job(client, "restart-running")
    assert interrupted["status"] == "failed"
    assert interrupted["stage"] == "failed"
    assert interrupted["retryable"] is True
    assert interrupted["error_code"] == "TASK_INTERRUPTED"
    # The last progress the caller observed, not a synthetic 1.0.
    assert interrupted["progress"] == 0.42

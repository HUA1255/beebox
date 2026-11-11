"""The GPU queue through the public ``/v1`` surface (ticket #39).

Every assertion is HTTP: job creation and status, the ``/v1/queue`` admin
endpoints and ``/v1/health``. The only setup seam is the documented fake speech
engine, scripted with a delay so jobs overlap in time and ordering is
observable from outside.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from support import fake_speech_env, make_wav_bytes

JOB_DELAY_SECONDS = 0.4
TERMINAL = {"succeeded", "failed", "cancelled"}
# Slow CI runners need headroom; local runs still finish in milliseconds.
WAIT_TIMEOUT_SECONDS = 60.0


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    return make_client(tmp_path, monkeypatch)


def make_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    api_token: str | None = None,
) -> TestClient:
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BEEBOX_MODEL_STORAGE_ROOT", str(tmp_path / "models-root"))
    if api_token is None:
        monkeypatch.delenv("BEEBOX_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("BEEBOX_API_TOKEN", api_token)
    # Real worker/pipeline in-process on the fake engine: every job spends
    # JOB_DELAY_SECONDS inside the engine, which is what makes a second
    # concurrent job observable if the queue ever allowed one.
    monkeypatch.setenv("BEEBOX_INLINE_LOCAL_TRANSCRIPTION", "1")
    monkeypatch.setenv("BEEBOX_DELETE_DERIVED_ON_COMPLETE", "1")
    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 1.0, "text": "queued transcript"}],
        text="queued transcript",
        duration=1.0,
        delay_seconds=JOB_DELAY_SECONDS,
    )
    from backend.app import create_app

    return TestClient(create_app())


def upload(client: TestClient) -> str:
    response = client.post(
        "/v1/files",
        files={"file": ("queue.wav", make_wav_bytes(), "audio/wav")},
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


def job(client: TestClient, job_id: str) -> dict:
    response = client.get(f"/v1/jobs/{job_id}")
    assert response.status_code == 200, response.text
    return response.json()


def queue(client: TestClient) -> dict:
    response = client.get("/v1/queue")
    assert response.status_code == 200, response.text
    return response.json()


def health(client: TestClient) -> dict:
    response = client.get("/v1/health")
    assert response.status_code == 200, response.text
    return response.json()


def wait_until(predicate, *, timeout: float = WAIT_TIMEOUT_SECONDS, message: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail(message)


def wait_job(client: TestClient, job_id: str, *, timeout: float = WAIT_TIMEOUT_SECONDS) -> dict:
    wait_until(
        lambda: job(client, job_id)["status"] in TERMINAL,
        timeout=timeout,
        message=f"job {job_id} did not finish",
    )
    return job(client, job_id)


def wait_current(client: TestClient, job_id: str) -> None:
    wait_until(
        lambda: queue(client)["current_job_id"] == job_id,
        message=f"job {job_id} never became the queue's current job",
    )


def test_gpu_jobs_run_one_at_a_time_in_submission_order(client: TestClient) -> None:
    file_id = upload(client)
    job_ids = [create_job(client, file_id)["id"] for _ in range(3)]

    seen_order: list[str] = []
    observed_running: list[list[str]] = []
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        current = queue(client)["current_job_id"]
        if current is not None and (not seen_order or seen_order[-1] != current):
            seen_order.append(current)
        running = client.get("/v1/jobs", params={"status": "running"}).json()["items"]
        observed_running.append([item["id"] for item in running])
        if all(job(client, job_id)["status"] in TERMINAL for job_id in job_ids):
            break
        time.sleep(0.02)
    else:  # pragma: no cover - only on a hung queue
        pytest.fail(f"jobs did not finish: {[job(client, i)['status'] for i in job_ids]}")

    assert seen_order == job_ids
    assert observed_running, "the running-jobs endpoint was never sampled"
    assert all(len(ids) <= 1 for ids in observed_running), observed_running
    assert max(len(ids) for ids in observed_running) == 1
    assert [job(client, job_id)["status"] for job_id in job_ids] == ["succeeded"] * 3


def test_pause_finishes_the_running_job_and_holds_new_submissions(client: TestClient) -> None:
    file_id = upload(client)
    first = create_job(client, file_id)["id"]
    wait_current(client, first)

    paused = client.post("/v1/queue/pause")
    assert paused.status_code == 200, paused.text
    assert paused.json()["paused"] is True
    assert paused.json()["current_job_id"] == first

    second = create_job(client, file_id)
    third = create_job(client, file_id)
    assert second["status"] == "queued"
    assert third["status"] == "queued"

    # The job already on the GPU finishes normally while the queue is paused.
    assert wait_job(client, first)["status"] == "succeeded"
    time.sleep(JOB_DELAY_SECONDS)
    assert job(client, second["id"])["status"] == "queued"
    assert job(client, third["id"])["status"] == "queued"

    state = queue(client)
    assert state["paused"] is True
    assert state["length"] == 2
    assert state["current_job_id"] is None
    assert [entry["job_id"] for entry in state["queued"]] == [second["id"], third["id"]]

    resumed = client.post("/v1/queue/resume")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["paused"] is False

    submitted = [second["id"], third["id"]]
    seen_order: list[str] = []
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        current = queue(client)["current_job_id"]
        if current is not None and (not seen_order or seen_order[-1] != current):
            seen_order.append(current)
        if all(job(client, job_id)["status"] in TERMINAL for job_id in submitted):
            break
        time.sleep(0.02)
    else:  # pragma: no cover - only on a hung queue
        pytest.fail("paused jobs did not run after resume")

    assert seen_order == submitted
    assert [job(client, job_id)["status"] for job_id in submitted] == ["succeeded", "succeeded"]
    assert queue(client)["length"] == 0


def test_resume_from_paused_drains_in_fifo_order(client: TestClient) -> None:
    file_id = upload(client)
    assert client.post("/v1/queue/pause").json()["paused"] is True

    job_ids = [create_job(client, file_id)["id"] for _ in range(2)]
    assert queue(client)["length"] == 2
    assert [entry["job_id"] for entry in queue(client)["queued"]] == job_ids

    seen_order: list[str] = []
    client.post("/v1/queue/resume")
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        current = queue(client)["current_job_id"]
        if current is not None and (not seen_order or seen_order[-1] != current):
            seen_order.append(current)
        if all(job(client, job_id)["status"] in TERMINAL for job_id in job_ids):
            break
        time.sleep(0.02)
    else:  # pragma: no cover - only on a hung queue
        pytest.fail("paused jobs did not run after resume")

    assert seen_order == job_ids


def test_cancelling_a_queued_job_while_paused_removes_it_from_the_queue(client: TestClient) -> None:
    assert client.post("/v1/queue/pause").json()["paused"] is True

    job_id = create_job(client, upload(client))["id"]
    assert queue(client)["length"] == 1

    # The queue is paused, so no worker owns the job yet; /v1 cancel must
    # remove it from the queue instead of leaving a token that runs later.
    cancelled = client.post(f"/v1/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert queue(client)["length"] == 0

    client.post("/v1/queue/resume")
    time.sleep(JOB_DELAY_SECONDS)
    assert job(client, job_id)["status"] == "cancelled"


def test_health_reports_queue_length_current_job_and_paused_state(client: TestClient) -> None:
    idle = health(client)["queue"]
    assert idle == {
        "paused": False,
        "length": 0,
        "current_job_id": None,
        "current_job_kind": None,
    }

    file_id = upload(client)
    running_job = create_job(client, file_id)["id"]
    wait_current(client, running_job)
    running = health(client)["queue"]
    assert running["current_job_id"] == running_job
    assert running["current_job_kind"] == "transcription"
    assert running["paused"] is False

    assert wait_job(client, running_job)["status"] == "succeeded"
    assert client.post("/v1/queue/pause").json()["paused"] is True
    # The successful job dropped its source upload (ADR-0011), so queueing a
    # second job needs a fresh file.
    create_job(client, upload(client))

    paused = health(client)["queue"]
    assert paused == {
        "paused": True,
        "length": 1,
        "current_job_id": None,
        "current_job_kind": None,
    }


def test_pause_and_resume_are_idempotent(client: TestClient) -> None:
    first = client.post("/v1/queue/pause")
    second = client.post("/v1/queue/pause")
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["paused"] is True and second.json()["paused"] is True

    resumed = client.post("/v1/queue/resume")
    resumed_again = client.post("/v1/queue/resume")
    assert resumed.json()["paused"] is False
    assert resumed_again.json()["paused"] is False
    assert queue(client)["paused"] is False


def test_pause_and_resume_require_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(tmp_path, monkeypatch, api_token="queue-admin-token")

    assert client.post("/v1/queue/pause").status_code == 401
    assert client.post("/v1/queue/resume").status_code == 401
    assert client.get("/v1/queue").status_code == 401

    authorized = {"Authorization": "Bearer queue-admin-token"}
    paused = client.post("/v1/queue/pause", headers=authorized)
    assert paused.status_code == 200, paused.text
    assert paused.json()["paused"] is True
    resumed = client.post("/v1/queue/resume", headers=authorized)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["paused"] is False


def test_openapi_snapshot_documents_the_queue_surface() -> None:
    snapshot = json.loads(
        (Path(__file__).resolve().parents[2] / "openapi.json").read_text(encoding="utf-8")
    )

    paths = snapshot["paths"]
    assert "get" in paths["/v1/queue"]
    assert "post" in paths["/v1/queue/pause"]
    assert "post" in paths["/v1/queue/resume"]

    # Resolve the health response schema by reference so the check does not
    # depend on the component name.
    health_ref = paths["/v1/health"]["get"]["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ]
    health_schema = snapshot["components"]["schemas"][health_ref.rsplit("/", 1)[-1]]
    assert "queue" in health_schema["properties"]

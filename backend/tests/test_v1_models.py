"""``/v1/models``: catalog, download lifecycle and VRAM release (ticket #41).

Every assertion goes through the public HTTP contract. The only setup seams
are the documented environment contracts:

* ``BEEBOX_SPEECH_ENGINE=fake`` (ticket #35) makes transcription observable
  without model files and tracks which models the engine has loaded.
* ``BEEBOX_MODEL_SOURCE_DIR`` points the download path at a local model
  directory, so no test touches the network.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from support import fake_speech_env, make_wav_bytes

MODEL_ID = "whisper-base"
TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled"}

SMALL_MODEL_FILES = {
    "config.json": b'{"model_type": "whisper"}',
    "tokenizer.json": b'{"version": "1.0"}',
    "preprocessor_config.json": b"{}",
    "model.safetensors": b"weights" * 4096,
}


def seed_local_source(tmp_path: Path, files: dict[str, bytes], model_id: str = MODEL_ID) -> int:
    """Write a model archive under ``BEEBOX_MODEL_SOURCE_DIR/<model_id>``."""
    source = tmp_path / "model-source" / model_id
    source.mkdir(parents=True, exist_ok=True)
    total = 0
    for name, content in files.items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        total += len(content)
    return total


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BEEBOX_MODEL_STORAGE_ROOT", str(tmp_path / "models-root"))
    monkeypatch.setenv("BEEBOX_MODEL_SOURCE_DIR", str(tmp_path / "model-source"))
    # A configured storage root is expected to exist (the installer creates it);
    # downloads refuse to invent a missing volume.
    (tmp_path / "models-root").mkdir(parents=True)
    monkeypatch.delenv("BEEBOX_API_TOKEN", raising=False)
    monkeypatch.setenv("BEEBOX_INLINE_LOCAL_TRANSCRIPTION", "1")
    monkeypatch.setenv("BEEBOX_DELETE_DERIVED_ON_COMPLETE", "1")
    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 1.0, "text": "model lifecycle"}],
        text="model lifecycle",
        duration=1.0,
    )
    from backend.app import create_app

    return TestClient(create_app())


def get_model(client: TestClient, model_id: str = MODEL_ID) -> dict:
    response = client.get(f"/v1/models/{model_id}")
    assert response.status_code == 200, response.text
    return response.json()


def wait_model(client: TestClient, predicate, *, timeout: float = 30.0, description: str = "state") -> dict:
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        body = get_model(client)
        if predicate(body):
            return body
        time.sleep(0.02)
    pytest.fail(f"{MODEL_ID} never reached {description}; last={body}")


def upload(client: TestClient) -> str:
    response = client.post(
        "/v1/files",
        files={"file": ("models.wav", make_wav_bytes(), "audio/wav")},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def create_job(client: TestClient, file_id: str) -> dict:
    response = client.post(
        "/v1/jobs",
        json={
            "kind": "transcription",
            "source": {"type": "file", "file_id": file_id},
            "options": {"model": MODEL_ID},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def wait_job(client: TestClient, job_id: str, *, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/v1/jobs/{job_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] in TERMINAL_JOB_STATUSES:
            return body
        time.sleep(0.02)
    pytest.fail(f"job {job_id} never finished; last={body}")


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def test_model_list_reports_downloaded_complete_loaded_and_disk_usage(client: TestClient) -> None:
    response = client.get("/v1/models")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == len(body["items"])
    by_id = {item["id"]: item for item in body["items"]}
    assert MODEL_ID in by_id
    assert "qwen3-asr-1.7b" in by_id

    item = by_id[MODEL_ID]
    assert item["name"] == "Whisper Base"
    assert item["engine"] == "whisper_transformers"
    assert item["downloaded"] is False
    assert item["complete"] is False
    assert item["loaded"] is False
    assert item["size_bytes"] == 0
    assert item["missing_files"]
    assert item["download"]["state"] == "idle"
    assert item["download"]["progress"] == 0.0


def test_unknown_model_is_a_structured_404_for_every_operation(client: TestClient) -> None:
    for method, path in (
        ("GET", "/v1/models/no-such-model"),
        ("POST", "/v1/models/no-such-model/download"),
        ("POST", "/v1/models/no-such-model/download/cancel"),
        ("POST", "/v1/models/no-such-model/download/retry"),
        ("POST", "/v1/models/no-such-model/unload"),
        ("DELETE", "/v1/models/no-such-model"),
    ):
        response = client.request(method, path)
        assert response.status_code == 404, f"{method} {path}: {response.status_code} {response.text}"
        assert response.json()["error_code"] == "MODEL_NOT_FOUND"


# ---------------------------------------------------------------------------
# Download lifecycle
# ---------------------------------------------------------------------------


def test_download_from_the_local_source_reports_progress_and_completes(
    client: TestClient, tmp_path: Path
) -> None:
    total = seed_local_source(tmp_path, SMALL_MODEL_FILES)

    started = client.post(f"/v1/models/{MODEL_ID}/download")

    assert started.status_code == 202, started.text
    assert started.json()["id"] == MODEL_ID
    assert started.json()["download"]["state"] in {"downloading", "succeeded"}

    completed = wait_model(client, lambda body: body["complete"], description="complete")
    assert completed["downloaded"] is True
    assert completed["complete"] is True
    assert completed["missing_files"] == []
    assert completed["size_bytes"] >= total
    assert completed["download"]["state"] == "succeeded"
    assert completed["download"]["progress"] == 1.0
    assert completed["download"]["bytes_downloaded"] == total
    assert completed["download"]["total_bytes"] == total

    again = client.post(f"/v1/models/{MODEL_ID}/download")
    assert again.status_code == 409
    assert again.json()["error_code"] == "MODEL_ALREADY_DOWNLOADED"


def test_failed_download_reports_the_error_and_retry_completes(
    client: TestClient, tmp_path: Path
) -> None:
    # A source without weights cannot produce a usable install.
    seed_local_source(tmp_path, {"config.json": b"{}"})

    assert client.post(f"/v1/models/{MODEL_ID}/download").status_code == 202
    failed = wait_model(
        client,
        lambda body: body["download"]["state"] == "failed",
        description="a failed download",
    )
    assert failed["downloaded"] is False
    assert failed["complete"] is False
    assert "weight" in failed["download"]["error"].lower()

    seed_local_source(tmp_path, SMALL_MODEL_FILES)
    retry = client.post(f"/v1/models/{MODEL_ID}/download/retry")

    assert retry.status_code == 202, retry.text
    completed = wait_model(client, lambda body: body["complete"], description="complete")
    assert completed["download"]["state"] == "succeeded"
    assert completed["size_bytes"] >= sum(len(value) for value in SMALL_MODEL_FILES.values())


def test_cancel_stops_an_active_download_and_retry_finishes(
    client: TestClient, tmp_path: Path
) -> None:
    # Large enough that the copy loop is still running when the cancel request
    # lands; cancellation is checked between 1 MiB chunks.
    files = dict(SMALL_MODEL_FILES)
    files["model.safetensors"] = b"\0" * (64 * 1024 * 1024)
    seed_local_source(tmp_path, files)

    assert client.post(f"/v1/models/{MODEL_ID}/download").status_code == 202
    in_progress = wait_model(
        client,
        lambda body: body["download"]["state"] == "downloading"
        and body["download"]["bytes_downloaded"] > 0,
        description="a download in progress",
    )
    assert 0.0 < in_progress["download"]["progress"] <= 1.0

    cancelled = client.post(f"/v1/models/{MODEL_ID}/download/cancel")

    assert cancelled.status_code == 200, cancelled.text
    # Cancellation is asynchronous: the reply shows the request being honoured,
    # and polling settles on the final state.
    assert cancelled.json()["download"]["state"] in {"cancelling", "cancelled"}

    settled = wait_model(
        client,
        lambda body: body["download"]["state"] == "cancelled",
        description="a settled cancellation",
    )
    assert settled["downloaded"] is False
    assert settled["complete"] is False
    assert settled["size_bytes"] > 0  # the copied prefix stays until delete/retry

    retry = client.post(f"/v1/models/{MODEL_ID}/download/retry")
    assert retry.status_code == 202, retry.text
    completed = wait_model(client, lambda body: body["complete"], timeout=60, description="complete")
    assert completed["download"]["state"] == "succeeded"

    not_running = client.post(f"/v1/models/{MODEL_ID}/download/cancel")
    assert not_running.status_code == 409
    assert not_running.json()["error_code"] == "MODEL_NOT_DOWNLOADING"


def test_delete_removes_an_incomplete_install_and_its_files(
    client: TestClient, tmp_path: Path
) -> None:
    # Weights alone are enough for the download to be recorded, but the install
    # stays incomplete: the engine's required files are missing.
    weight = b"w" * (1024 * 1024)
    seed_local_source(tmp_path, {"model.safetensors": weight})

    assert client.post(f"/v1/models/{MODEL_ID}/download").status_code == 202
    incomplete = wait_model(client, lambda body: body["downloaded"], description="a recorded install")
    assert incomplete["complete"] is False
    assert "config.json" in incomplete["missing_files"]
    assert incomplete["size_bytes"] >= len(weight)

    deleted = client.delete(f"/v1/models/{MODEL_ID}")

    assert deleted.status_code == 204, deleted.text
    removed = get_model(client)
    assert removed["downloaded"] is False
    assert removed["complete"] is False
    assert removed["size_bytes"] == 0
    assert removed["download"]["state"] == "idle"


# ---------------------------------------------------------------------------
# Loaded state and unload
# ---------------------------------------------------------------------------


def test_unload_drops_the_loaded_model_through_the_speech_engine(client: TestClient) -> None:
    file_id = upload(client)
    job = create_job(client, file_id)
    finished = wait_job(client, job["id"])
    assert finished["status"] == "succeeded", finished

    loaded = get_model(client)
    assert loaded["loaded"] is True

    unloaded = client.post(f"/v1/models/{MODEL_ID}/unload")

    assert unloaded.status_code == 200, unloaded.text
    assert unloaded.json()["loaded"] is False
    assert get_model(client)["loaded"] is False

    # The next transcription loads it again: the fake engine observed both calls.
    # ADR-0011 deletes a successful job's upload, so use a fresh file.
    second = create_job(client, upload(client))
    assert wait_job(client, second["id"])["status"] == "succeeded"
    assert get_model(client)["loaded"] is True

    # Unloading twice is a no-op that still reports the truth.
    assert client.post(f"/v1/models/{MODEL_ID}/unload").json()["loaded"] is False

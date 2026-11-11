"""Manual real-model acceptance suite for the ``/v1`` contract.

Opt in with ``BEEBOX_RUN_REAL_MODELS=1`` and point ``BEEBOX_REAL_MEDIA_DIR`` at
real media. The suite talks HTTP to a running dev instance
(``BEEBOX_BASE_URL``, default ``http://127.0.0.1:17495``) using the dev Bearer
token; run-real-tests.ps1 starts it with that environment. Every step goes
through the public API:

``POST /v1/files`` -> ``POST /v1/jobs`` -> ``GET /v1/jobs/{id}`` ->
``GET /v1/jobs/{id}/result`` -> ``GET /v1/jobs/{id}/exports/{format}``.

A missing model is downloaded through ``/v1/models`` on a production run and
must already be provisioned on the dev instance, which shares production
weights read-only (ADR-0010). Results are written to ``results/`` for the
ticket report.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest

from backend import config
from backend.backends.registry import get_all_model_configs

SUPPORTED_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".mp3", ".wav", ".m4a", ".flac", ".ogg"}
# The v1 export surface is txt/md/srt; "markdown" exercises the alias.
EXPORT_FORMATS = ["txt", "markdown", "srt"]
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
RESULTS: list[dict] = []


def _results_dir() -> Path:
    path = Path(__file__).resolve().parent / "results"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_results() -> None:
    if not RESULTS:
        return
    timestamp = os.environ.get("BEEBOX_REAL_TEST_RESULTS_TIMESTAMP")
    if not timestamp:
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        os.environ["BEEBOX_REAL_TEST_RESULTS_TIMESTAMP"] = timestamp
    output_dir = _results_dir()
    payload = {
        "created_at": datetime.now(UTC).isoformat(),
        "total": len(RESULTS),
        "passed": sum(1 for item in RESULTS if item.get("ok")),
        "failed": sum(1 for item in RESULTS if not item.get("ok")),
        "items": RESULTS,
    }
    (output_dir / f"beebox-real-models-{timestamp}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = [
        "# BeeBox Real Model Results",
        "",
        f"- created_at: `{payload['created_at']}`",
        f"- total: `{payload['total']}`",
        f"- passed: `{payload['passed']}`",
        f"- failed: `{payload['failed']}`",
        "",
        "| model | engine | source | repo | status | text | segments | exports | deleted |",
        "| --- | --- | --- | --- | --- | ---: | ---: | --- | --- |",
    ]
    for item in RESULTS:
        lines.append(
            "| {model} | {engine} | {source} | {repo} | {status} | {text_len} | {segments} | {exports} | {deleted} |".format(
                model=item["model_name"],
                engine=item.get("engine") or "",
                source=item.get("source") or "",
                repo=item.get("repo_id") or "",
                status="pass" if item.get("ok") else "fail",
                text_len=item.get("text_length") or 0,
                segments=item.get("segments_count") or 0,
                exports=",".join(item.get("exports") or []),
                deleted="yes" if item.get("deleted") else "no",
            )
        )
    (output_dir / f"beebox-real-models-{timestamp}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def pytest_sessionfinish(session, exitstatus) -> None:
    _write_results()


@pytest.fixture(scope="session", autouse=True)
def _real_model_results_writer():
    yield
    _write_results()


def _client() -> httpx2.Client:
    """HTTP client for the running dev instance, carrying its Bearer token."""
    base_url = os.environ.get("BEEBOX_BASE_URL", "http://127.0.0.1:17495").rstrip("/")
    token = os.environ.get("BEEBOX_API_TOKEN", "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx2.Client(base_url=base_url, headers=headers, timeout=120.0)


def _media_file() -> Path:
    media_dir = os.environ.get("BEEBOX_REAL_MEDIA_DIR")
    if not media_dir:
        raise AssertionError("BEEBOX_REAL_MEDIA_DIR is required for real model tests")
    root = Path(media_dir).expanduser()
    if not root.exists():
        raise AssertionError(f"BEEBOX_REAL_MEDIA_DIR does not exist: {root}")
    candidates = sorted(path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES)
    if not candidates:
        raise AssertionError(f"No supported media files found in {root}")
    return candidates[0]


def _model_info(client: httpx2.Client, model_name: str) -> dict:
    response = client.get(f"/v1/models/{model_name}")
    assert response.status_code == 200, response.text
    return response.json()


def _wait_for_model(client: httpx2.Client, model_name: str, timeout_s: int) -> dict:
    deadline = time.time() + timeout_s
    last: dict = {}
    while time.time() < deadline:
        last = _model_info(client, model_name)
        download = last.get("download") or {}
        if last["downloaded"] and last["complete"]:
            return last
        if download.get("state") == "failed":
            raise AssertionError(
                f"{model_name} download failed: "
                f"{download.get('error') or download.get('error_code')}"
            )
        time.sleep(2)
    raise AssertionError(f"{model_name} did not become downloaded and complete before timeout; last={last}")


def _ensure_model(client: httpx2.Client, model_name: str, timeout_s: int) -> dict:
    status = _model_info(client, model_name)
    if status["downloaded"] and status["complete"]:
        return status
    if config.is_dev_instance():
        # The dev instance shares production weights read-only (ADR-0010);
        # a missing model must be provisioned on production first.
        raise AssertionError(
            f"{model_name} is not downloaded. The dev instance shares production "
            "models read-only; download it on the production instance first."
        )
    response = client.post(f"/v1/models/{model_name}/download")
    if response.status_code == 202:
        return _wait_for_model(client, model_name, timeout_s)
    if response.status_code == 409:
        body = response.json()
        code = body.get("error_code")
        if code == "MODEL_ALREADY_DOWNLOADED":
            return _model_info(client, model_name)
        if code == "MODEL_DOWNLOAD_IN_PROGRESS":
            return _wait_for_model(client, model_name, timeout_s)
        raise AssertionError(f"{model_name} cannot be downloaded: {body.get('detail')} [{code}]")
    raise AssertionError(f"{model_name} download request failed: {response.status_code} {response.text}")


def _wait_for_job(client: httpx2.Client, job_id: str, model_name: str, timeout_s: int) -> dict:
    deadline = time.time() + timeout_s
    last: dict = {}
    while time.time() < deadline:
        response = client.get(f"/v1/jobs/{job_id}")
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] == "succeeded":
            return last
        if last["status"] in TERMINAL_STATUSES:
            raise AssertionError(
                f"Job {job_id} for {model_name} ended as {last['status']}: "
                f"{last.get('error')} [{last.get('error_code')}]"
            )
        time.sleep(2)
    raise AssertionError(f"Job {job_id} for {model_name} did not settle before timeout; last={last}")


@pytest.mark.parametrize("model_config", get_all_model_configs(), ids=lambda item: item.model_name)
def test_every_local_model_real_transcription(model_config) -> None:
    if os.environ.get("BEEBOX_RUN_REAL_MODELS") != "1":
        raise AssertionError("BEEBOX_RUN_REAL_MODELS=1 is required; real model tests must not be skipped")

    media_path = _media_file()
    download_timeout = int(os.environ.get("BEEBOX_REAL_MODEL_DOWNLOAD_TIMEOUT", "3600"))
    task_timeout = int(os.environ.get("BEEBOX_REAL_MODEL_TASK_TIMEOUT", "1800"))

    result_record = {
        "model_name": model_config.model_name,
        "ok": False,
        "engine": model_config.engine,
        "source": None,
        "repo_id": None,
        "exports": [],
        "deleted": False,
    }
    try:
        with _client() as client:
            status = _ensure_model(client, model_config.model_name, download_timeout)
            result_record["source"] = status.get("source")
            result_record["repo_id"] = status.get("repo_id")

            with media_path.open("rb") as handle:
                upload = client.post(
                    "/v1/files",
                    files={"file": (media_path.name, handle, "application/octet-stream")},
                )
            assert upload.status_code == 201, upload.text
            file_id = upload.json()["id"]

            created = client.post(
                "/v1/jobs",
                json={
                    "kind": "transcription",
                    "source": {"type": "file", "file_id": file_id},
                    "options": {"model": model_config.model_name, "language": "auto"},
                },
            )
            if created.status_code == 409:
                body = created.json()
                raise AssertionError(
                    f"{model_config.model_name} cannot run on this runtime: "
                    f"{body.get('detail')} [{body.get('error_code')}]"
                )
            assert created.status_code == 201, created.text
            job_id = created.json()["id"]

            job = _wait_for_job(client, job_id, model_config.model_name, task_timeout)

            result_response = client.get(f"/v1/jobs/{job_id}/result")
            assert result_response.status_code == 200, result_response.text
            result = result_response.json()
            assert result["status"] == "succeeded"
            assert result["text"]
            assert result["segments"]
            result_record["job_id"] = job_id
            result_record["text_length"] = len(result["text"])
            result_record["segments_count"] = len(result["segments"])

            for fmt in EXPORT_FORMATS:
                export = client.get(f"/v1/jobs/{job_id}/exports/{fmt}")
                assert export.status_code == 200, (fmt, export.text)
                assert export.text.strip()
                result_record["exports"].append(fmt)

            unload = client.post(f"/v1/models/{model_config.model_name}/unload")
            assert unload.status_code == 200, unload.text
            model_dir = config.get_models_dir() / model_config.model_name
            if config.is_dev_instance():
                # Never delete weights that production shares with the dev
                # instance (ADR-0010/ticket #55): the delete contract is
                # covered by the fake-engine suite, not by real weights.
                assert model_dir.exists()
                result_record["deleted"] = False
            else:
                delete = client.delete(f"/v1/models/{model_config.model_name}")
                assert delete.status_code == 204, delete.text
                deleted_status = _model_info(client, model_config.model_name)
                assert deleted_status["downloaded"] is False
                assert not model_dir.exists()
                result_record["deleted"] = True
            result_record["ok"] = True
    except Exception as exc:
        result_record["error"] = str(exc)
        raise
    finally:
        RESULTS.append(result_record)

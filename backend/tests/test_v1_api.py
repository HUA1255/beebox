"""End-to-end tests for the ``/v1`` surface (ticket #36).

Every assertion goes through the public HTTP contract. The only setup seam is
the documented fake speech engine (``BEEBOX_SPEECH_ENGINE=fake``), the real
tiny-WAV helper from :mod:`support`, and direct row seeding for internal
statuses that cannot otherwise be driven deterministically over HTTP.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import config
from support import fake_speech_env, make_wav_bytes

PUBLIC_STATUSES = {"queued", "running", "succeeded", "failed", "cancelled"}
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
V1_TOKEN = "v1-api-test-token"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BEEBOX_MODEL_STORAGE_ROOT", str(tmp_path / "models-root"))
    # Every /v1 call carries the Bearer token, exactly like a real client.
    monkeypatch.setenv("BEEBOX_API_TOKEN", V1_TOKEN)
    # Run the real worker/pipeline in-process (no subprocess, no GPU, no model
    # files) and keep the fake engine deterministic.
    monkeypatch.setenv("BEEBOX_INLINE_LOCAL_TRANSCRIPTION", "1")
    monkeypatch.setenv("BEEBOX_DELETE_DERIVED_ON_COMPLETE", "1")
    monkeypatch.setenv("BEEBOX_GIT_COMMIT", "0123456789abcdef0123456789abcdef01234567")
    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 1.0, "text": "default transcript"}],
        text="default transcript",
        duration=1.0,
        aligned=True,
    )
    from backend.app import create_app

    return TestClient(create_app(), headers={"Authorization": f"Bearer {V1_TOKEN}"})


def upload(
    client: TestClient,
    *,
    filename: str = "clip.wav",
    content: bytes | None = None,
    content_type: str = "audio/wav",
):
    return client.post(
        "/v1/files",
        files={"file": (filename, content if content is not None else make_wav_bytes(), content_type)},
    )


def create_job(client: TestClient, file_id: str, **options):
    return client.post(
        "/v1/jobs",
        json={
            "kind": "transcription",
            "source": {"type": "file", "file_id": file_id},
            "options": options,
        },
    )


def wait_for(client: TestClient, job_id: str, *, timeout: float = 30.0) -> dict:
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


def wait_for_status(
    client: TestClient, job_id: str, statuses: set[str], *, timeout: float = 30.0
) -> dict:
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/v1/jobs/{job_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] in statuses:
            return body
        time.sleep(0.02)
    pytest.fail(f"job {job_id} never reached {statuses}: {body}")


def seed_task(
    tmp_path: Path,
    *,
    status: str,
    chunks: list[tuple[int, str, str | None]] | None = None,
    progress: float = 60,
    duration_ms: int = 300_000,
    error: str | None = None,
    error_code: str | None = None,
) -> str:
    """Seed a task row plus real chunk audio files.

    Some lifecycle states (a running job at restart, a chunk-level failure)
    cannot be produced deterministically over HTTP, so the tests seed the row
    exactly like ticket #36 does and then assert only through ``/v1``.
    """
    from backend.database import session as db_session
    from backend.database.models import TranscriptionChunk, TranscriptionTask

    db_session.init_db()
    job_id = f"seed-{uuid.uuid4().hex[:12]}"
    media_path = tmp_path / f"{job_id}.wav"
    media_path.write_bytes(make_wav_bytes())
    db = db_session.SessionLocal()
    try:
        db.add(
            TranscriptionTask(
                id=job_id,
                filename="long.wav",
                source="local",
                audio_path=config.to_storage_path(media_path),
                status=status,
                progress=progress,
                model_name="qwen3-asr-1.7b",
                duration_ms=duration_ms,
                error=error,
                error_code=error_code,
                options_json=json.dumps(
                    {
                        "output_formats": ["txt", "md", "srt"],
                        "diarization": False,
                        "chunk_window_ms": 120_000,
                        "chunk_overlap_ms": 2_000,
                        "_source_kind": "external",
                    }
                ),
            )
        )
        for idx, chunk_status, text in chunks or []:
            chunk_path = tmp_path / f"{job_id}-chunk-{idx}.wav"
            chunk_path.write_bytes(make_wav_bytes())
            db.add(
                TranscriptionChunk(
                    task_id=job_id,
                    idx=idx,
                    audio_path=config.to_storage_path(chunk_path),
                    start_ms=(idx - 1) * 120_000,
                    end_ms=min(duration_ms, idx * 120_000),
                    status=chunk_status,
                    progress=100 if chunk_status == "completed" else 0,
                    text=text,
                    error="boom" if chunk_status == "failed" else None,
                    error_code="CHUNK_FAILED" if chunk_status == "failed" else None,
                )
            )
        db.commit()
    finally:
        db.close()
    return job_id


def chunk_state(job_id: str) -> list[tuple[int, str, str | None]]:
    from backend.database import session as db_session
    from backend.database.models import TranscriptionChunk

    db = db_session.SessionLocal()
    try:
        rows = (
            db.query(TranscriptionChunk)
            .filter(TranscriptionChunk.task_id == job_id)
            .order_by(TranscriptionChunk.idx.asc())
            .all()
        )
        return [(row.idx, row.status, row.text) for row in rows]
    finally:
        db.close()


def settle_worker(client: TestClient, job_id: str, *, timeout: float = 15.0) -> None:
    """Delete a terminal job, waiting out a cooperative cancel.

    The GPU queue is process-global and stays "current" until the cancelled
    worker's in-flight inference returns. Tests must not leave that slot held
    for the next test file, so poll the delete guard until it releases.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.delete(f"/v1/jobs/{job_id}")
        if response.status_code == 204:
            return
        assert response.status_code == 409, response.text
        assert response.json()["error_code"] == "JOB_ACTIVE"
        time.sleep(0.05)
    pytest.fail(f"job {job_id} worker did not settle before {timeout}s")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def test_health_reports_online_readiness_version_and_commit(client: TestClient) -> None:
    response = client.get("/v1/health")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["online"] is True
    assert body["ready"] is True
    assert body["version"] == "1.0.1"
    assert body["commit"] == "0123456789abcdef0123456789abcdef01234567"


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def test_streaming_upload_stores_large_body_and_round_trips_content(client: TestClient) -> None:
    # Larger than Starlette's 1 MiB in-memory spool threshold, so the multipart
    # parser must spool to disk and the route must copy in chunks.
    content = bytes(range(256)) * (3 * 4096)  # 3 MiB
    response = upload(client, filename="large.wav", content=content)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["filename"] == "large.wav"
    assert body["size_bytes"] == len(content)
    assert body["sha256"] == hashlib.sha256(content).hexdigest()
    assert body["content_type"] == "audio/wav"
    assert body["download_url"] == f"/v1/files/{body['id']}/content"

    fetched = client.get(body["download_url"])
    assert fetched.status_code == 200
    assert fetched.content == content


def test_file_content_supports_byte_ranges_for_headerless_playback(client: TestClient) -> None:
    content = b"0123456789"
    created = upload(client, filename="range.wav", content=content).json()

    partial = client.get(
        f"/v1/files/{created['id']}/content",
        headers={"Range": "bytes=2-5"},
    )

    assert partial.status_code == 206
    assert partial.content == b"2345"
    assert partial.headers["content-range"] == "bytes 2-5/10"

    unsatisfiable = client.get(
        f"/v1/files/{created['id']}/content",
        headers={"Range": "bytes=100-200"},
    )
    assert unsatisfiable.status_code == 416


def test_file_metadata_listing_and_delete(client: TestClient) -> None:
    created = upload(client).json()

    listed = client.get("/v1/files")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert [item["id"] for item in listed.json()["items"]] == [created["id"]]

    fetched = client.get(f"/v1/files/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["sha256"] == created["sha256"]

    deleted = client.delete(f"/v1/files/{created['id']}")
    assert deleted.status_code == 204
    assert client.get(f"/v1/files/{created['id']}").status_code == 404
    assert client.get(f"/v1/files/{created['id']}/content").status_code == 404
    assert client.get("/v1/files").json()["total"] == 0


def test_upload_limit_is_enforced_while_streaming(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEEBOX_MAX_UPLOAD_BYTES", str(1024 * 1024))

    response = upload(client, content=b"x" * (1024 * 1024 + 1))

    assert response.status_code == 413
    body = response.json()
    assert body["error_code"] == "FILE_TOO_LARGE"
    assert body["stage"] == "file"
    assert client.get("/v1/files").json()["total"] == 0


def test_empty_upload_is_rejected(client: TestClient) -> None:
    response = upload(client, content=b"")

    assert response.status_code == 400
    assert response.json()["error_code"] == "EMPTY_FILE"


def test_unknown_file_errors_use_the_v1_error_shape(client: TestClient) -> None:
    response = client.get("/v1/files/does-not-exist")

    assert response.status_code == 404
    body = response.json()
    assert body["error_code"] == "FILE_NOT_FOUND"
    assert body["stage"] == "file"
    assert isinstance(body["detail"], str)


# ---------------------------------------------------------------------------
# Jobs: creation, defaults and public status
# ---------------------------------------------------------------------------


def test_create_job_without_options_uses_server_defaults_and_runs(client: TestClient) -> None:
    file_id = upload(client).json()["id"]

    created = create_job(client, file_id)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["kind"] == "transcription"
    assert body["status"] == "queued"
    assert body["stage"] == "queued"
    assert body["progress"] == 0.0
    assert body["options"] == {
        "language": None,
        "diarization": True,
        "num_speakers": None,
        "model": "qwen3-asr-1.7b",
        "subtitle_language": None,
        "prefer": None,
    }
    assert body["source"] == {
        "type": "file",
        "file_id": file_id,
        "filename": "clip.wav",
        "text_source": "asr",
        "language": None,
        "duration_ms": None,
        "url": None,
        "title": None,
        "author": None,
        "part": None,
        "part_title": None,
        "subtitle_language": None,
    }

    finished = wait_for(client, body["id"])
    assert finished["status"] == "succeeded"
    assert finished["stage"] == "done"
    assert finished["progress"] == 1.0
    assert finished["error"] is None
    assert finished["error_code"] is None
    assert finished["retryable"] is False
    assert finished["source"]["duration_ms"] is not None


def test_create_job_with_explicit_options_persists_them(client: TestClient) -> None:
    file_id = upload(client).json()["id"]

    created = create_job(
        client,
        file_id,
        language="zh",
        diarization=False,
        num_speakers=2,
        model="qwen3-asr-1.7b",
    )
    assert created.status_code == 201, created.text
    assert created.json()["options"] == {
        "language": "zh",
        "diarization": False,
        "num_speakers": 2,
        "model": "qwen3-asr-1.7b",
        "subtitle_language": None,
        "prefer": None,
    }

    finished = wait_for(client, created.json()["id"])
    assert finished["status"] == "succeeded"
    assert finished["options"]["language"] == "zh"
    assert finished["options"]["diarization"] is False
    assert finished["options"]["num_speakers"] == 2


def test_create_job_rejects_unknown_file_and_unsupported_requests(client: TestClient) -> None:
    missing = create_job(client, "no-such-file")
    assert missing.status_code == 404
    assert missing.json()["error_code"] == "FILE_NOT_FOUND"

    file_id = upload(client).json()["id"]
    bad_kind = client.post(
        "/v1/jobs",
        json={"kind": "speech", "source": {"type": "file", "file_id": file_id}},
    )
    assert bad_kind.status_code == 400
    assert bad_kind.json()["error_code"] == "UNSUPPORTED_KIND"

    bad_source = client.post(
        "/v1/jobs",
        json={"kind": "transcription", "source": {"type": "stream", "file_id": file_id}},
    )
    assert bad_source.status_code == 400
    assert bad_source.json()["error_code"] == "UNSUPPORTED_SOURCE_TYPE"

    missing_id = client.post(
        "/v1/jobs",
        json={"kind": "transcription", "source": {"type": "file"}},
    )
    assert missing_id.status_code == 400
    assert missing_id.json()["error_code"] == "INVALID_SOURCE"


def test_create_job_rejects_unknown_model(client: TestClient) -> None:
    file_id = upload(client).json()["id"]

    response = create_job(client, file_id, model="no-such-model")

    assert response.status_code == 400
    assert response.json()["error_code"] == "MODEL_NOT_FOUND"
    assert response.json()["stage"] == "job"


def test_create_job_validation_errors_use_unified_shape(client: TestClient) -> None:
    file_id = upload(client).json()["id"]

    response = create_job(client, file_id, num_speakers=0)

    assert response.status_code == 422
    body = response.json()
    assert isinstance(body["detail"], str)
    assert body["error_code"] == "INVALID_REQUEST"
    assert body["stage"] == "validation"


@pytest.mark.parametrize(
    ("internal", "progress", "expected_status", "expected_stage", "expected_progress", "expected_code", "retryable"),
    [
        ("created", 0, "queued", "queued", 0.0, None, False),
        ("queued", 0, "queued", "queued", 0.0, None, False),
        ("importing", 5, "running", "intake", 0.05, None, False),
        ("preprocessing", 10, "running", "intake", 0.1, None, False),
        ("waiting_model", 20, "running", "model", 0.2, None, False),
        ("downloading_model", 25, "running", "model", 0.25, None, False),
        ("transcribing", 60, "running", "transcribe", 0.6, None, False),
        ("postprocessing", 90, "running", "postprocess", 0.9, None, False),
        ("exporting", 95, "running", "export", 0.95, None, False),
        ("completed", 100, "succeeded", "done", 1.0, None, False),
        ("failed", 100, "failed", "failed", 1.0, "MODEL_LOAD_FAILED", True),
        ("failed_resumable", 100, "failed", "failed", 1.0, "CHUNK_FAILED", True),
        ("failed", 100, "failed", "failed", 1.0, "LOCAL_TOOL_MISSING", False),
        ("interrupted", 100, "failed", "failed", 1.0, "TASK_INTERRUPTED", True),
        ("cancelled", 100, "cancelled", "cancelled", 1.0, "TASK_CANCELLED", False),
    ],
)
def test_internal_states_are_projected_onto_the_five_public_statuses(
    client: TestClient,
    tmp_path: Path,
    internal: str,
    progress: int,
    expected_status: str,
    expected_stage: str,
    expected_progress: float,
    expected_code: str | None,
    retryable: bool,
) -> None:
    from backend.database import session as db_session
    from backend.database.models import TranscriptionTask

    db_session.init_db()
    job_id = f"status-{internal}"
    db = db_session.SessionLocal()
    try:
        db.add(
            TranscriptionTask(
                id=job_id,
                filename="clip.wav",
                source="local",
                audio_path=str(tmp_path / "clip.wav"),
                status=internal,
                progress=progress,
                model_name="qwen3-asr-1.7b",
                error="boom" if internal.startswith("failed") or internal == "interrupted" else None,
                error_code=expected_code if internal.startswith("failed") or internal == "interrupted" else None,
                options_json=json.dumps(
                    {"output_formats": ["txt", "md", "srt"], "diarization": False, "_source_kind": "external"}
                ),
            )
        )
        db.commit()
    finally:
        db.close()

    response = client.get(f"/v1/jobs/{job_id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == expected_status
    assert body["status"] in PUBLIC_STATUSES
    assert body["stage"] == expected_stage
    assert body["progress"] == expected_progress
    assert body["error_code"] == expected_code
    assert body["retryable"] is retryable


def test_job_list_filters_by_public_status(client: TestClient) -> None:
    file_id = upload(client).json()["id"]
    job_id = create_job(client, file_id).json()["id"]
    wait_for(client, job_id)

    succeeded = client.get("/v1/jobs", params={"status": "succeeded"})
    assert succeeded.status_code == 200
    assert [item["id"] for item in succeeded.json()["items"]] == [job_id]

    queued = client.get("/v1/jobs", params={"status": "queued"})
    assert queued.status_code == 200
    assert queued.json()["items"] == []
    assert queued.json()["total"] == 0


def test_file_cannot_be_deleted_while_a_job_is_using_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Keep the job active long enough to observe the in-use guard.
    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 1.0, "text": "slow"}],
        text="slow",
        duration=1.0,
        delay_seconds=1.5,
    )
    file_id = upload(client).json()["id"]
    job_id = create_job(client, file_id).json()["id"]

    blocked = client.delete(f"/v1/files/{file_id}")
    assert blocked.status_code == 409
    assert blocked.json()["error_code"] == "FILE_IN_USE"

    wait_for(client, job_id)
    # ADR-0011: a successful job drops its source media, so the upload row and
    # blob disappear once the post-completion cleanup finishes.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and client.get(f"/v1/files/{file_id}").status_code == 200:
        time.sleep(0.05)
    assert client.get(f"/v1/files/{file_id}").status_code == 404
    assert client.delete(f"/v1/files/{file_id}").status_code == 404


# ---------------------------------------------------------------------------
# Result and exports
# ---------------------------------------------------------------------------

PARAGRAPH_SEGMENTS = [
    {"start": 0.0, "end": 4.0, "text": "第一句话。", "speaker": "1"},
    {"start": 4.2, "end": 8.0, "text": "第二句话。", "speaker": "1"},
    {"start": 9.5, "end": 13.0, "text": "Hello there.", "speaker": "2"},
]


def _completed_paragraph_job(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> dict:
    fake_speech_env(
        monkeypatch,
        segments=PARAGRAPH_SEGMENTS,
        duration=13.0,
        aligned=True,
    )
    file_id = upload(client, filename="clip.wav").json()["id"]
    created = create_job(client, file_id).json()
    finished = wait_for(client, created["id"])
    assert finished["status"] == "succeeded", finished
    return finished


def test_result_contains_text_segments_paragraphs_speakers_and_source(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _completed_paragraph_job(client, monkeypatch)

    response = client.get(f"/v1/jobs/{job['id']}/result")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job_id"] == job["id"]
    assert body["status"] == "succeeded"
    assert body["text"]
    assert body["language"] is None
    # The task duration comes from the real uploaded WAV (1 s), not the
    # scripted segment timestamps.
    assert body["duration_ms"] == 1000

    assert [segment["id"] for segment in body["segments"]] == [1, 2, 3]
    assert [segment["paragraph"] for segment in body["segments"]] == [1, 1, 2]
    assert [segment["speaker"] for segment in body["segments"]] == ["1", "1", "2"]
    assert body["segments"][2]["text"] == "Hello there."

    assert body["paragraphs"] == [
        {
            "index": 1,
            "start": 0.0,
            "end": 8.0,
            "speaker": "1",
            "text": "第一句话。第二句话。",
            "segment_ids": [1, 2],
        },
        {
            "index": 2,
            "start": 9.5,
            "end": 13.0,
            "speaker": "2",
            "text": "Hello there.",
            "segment_ids": [3],
        },
    ]
    assert body["speakers"] == [
        {"speaker": "1", "display_name": "说话人 1", "segment_count": 2, "duration_ms": 7800},
        {"speaker": "2", "display_name": "说话人 2", "segment_count": 1, "duration_ms": 3500},
    ]
    source = body["source"]
    assert source["type"] == "file"
    assert source["file_id"] == job["source"]["file_id"]
    assert source["filename"] == "clip.wav"
    assert source["text_source"] == "asr"
    assert source["duration_ms"] == 1000


def test_exports_follow_the_paragraph_contract(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _completed_paragraph_job(client, monkeypatch)

    txt = client.get(f"/v1/jobs/{job['id']}/exports/txt")
    assert txt.status_code == 200, txt.text
    assert txt.text == (
        "[00:00:00] 说话人 1\n"
        "第一句话。第二句话。\n"
        "\n"
        "[00:00:09] 说话人 2\n"
        "Hello there.\n"
    )
    assert "clip.txt" in txt.headers["content-disposition"]

    markdown = client.get(f"/v1/jobs/{job['id']}/exports/md")
    assert markdown.status_code == 200, markdown.text
    assert markdown.text == (
        "# clip\n"
        "\n"
        "## `00:00:00` 说话人 1\n"
        "\n"
        "第一句话。第二句话。\n"
        "\n"
        "## `00:00:09` 说话人 2\n"
        "\n"
        "Hello there.\n"
    )
    assert "clip.md" in markdown.headers["content-disposition"]

    srt = client.get(f"/v1/jobs/{job['id']}/exports/srt")
    assert srt.status_code == 200, srt.text
    assert srt.text == (
        "1\n"
        "00:00:00,000 --> 00:00:04,000\n"
        "说话人 1: 第一句话。\n"
        "\n"
        "2\n"
        "00:00:04,200 --> 00:00:08,000\n"
        "说话人 1: 第二句话。\n"
        "\n"
        "3\n"
        "00:00:09,500 --> 00:00:13,000\n"
        "说话人 2: Hello there.\n"
    )
    assert "clip.srt" in srt.headers["content-disposition"]


def test_result_and_exports_require_a_succeeded_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 1.0, "text": "slow"}],
        duration=1.0,
        delay_seconds=1.5,
    )
    file_id = upload(client).json()["id"]
    job_id = create_job(client, file_id).json()["id"]

    result = client.get(f"/v1/jobs/{job_id}/result")
    assert result.status_code == 409
    assert result.json()["error_code"] == "JOB_NOT_SUCCEEDED"
    assert result.json()["stage"] == "result"

    exported = client.get(f"/v1/jobs/{job_id}/exports/txt")
    assert exported.status_code == 409
    assert exported.json()["error_code"] == "JOB_NOT_SUCCEEDED"
    assert exported.json()["stage"] == "export"

    wait_for(client, job_id)


def test_unknown_job_and_export_format_errors(client: TestClient) -> None:
    missing = client.get("/v1/jobs/no-such-job/result")
    assert missing.status_code == 404
    assert missing.json()["error_code"] == "JOB_NOT_FOUND"

    file_id = upload(client).json()["id"]
    job_id = create_job(client, file_id).json()["id"]
    wait_for(client, job_id)

    unsupported = client.get(f"/v1/jobs/{job_id}/exports/pdf")
    assert unsupported.status_code == 400
    assert unsupported.json()["error_code"] == "UNSUPPORTED_FORMAT"
    assert unsupported.json()["stage"] == "export"




# ---------------------------------------------------------------------------
# Job operations: cancel (ticket #37)
# ---------------------------------------------------------------------------


def test_cancel_queued_job_is_immediate_and_a_second_cancel_conflicts(
    client: TestClient, tmp_path: Path
) -> None:
    job_id = seed_task(tmp_path, status="queued", progress=0)

    cancelled = client.post(f"/v1/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    body = cancelled.json()
    assert body["status"] == "cancelled"
    assert body["stage"] == "cancelled"
    assert body["progress"] == 1.0
    assert body["error_code"] == "TASK_CANCELLED"
    assert body["retryable"] is False

    again = client.post(f"/v1/jobs/{job_id}/cancel")
    assert again.status_code == 409
    assert again.json()["error_code"] == "JOB_NOT_CANCELLABLE"
    assert again.json()["stage"] == "cancel"


def test_cancel_running_job_stops_the_worker_and_blocks_result_and_exports(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 1.0, "text": "slow transcript"}],
        text="slow transcript",
        duration=1.0,
        delay_seconds=1.5,
    )
    file_id = upload(client).json()["id"]
    job_id = create_job(client, file_id).json()["id"]
    wait_for_status(client, job_id, {"running"})

    cancelled = client.post(f"/v1/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["error_code"] == "TASK_CANCELLED"

    # The in-flight inference is still running; once it returns the worker must
    # not publish a transcript or a success status.
    time.sleep(1.8)
    settled = client.get(f"/v1/jobs/{job_id}")
    assert settled.status_code == 200
    body = settled.json()
    assert body["status"] == "cancelled"
    assert body["stage"] == "cancelled"
    assert body["progress"] == 1.0
    assert body["error_code"] == "TASK_CANCELLED"

    result = client.get(f"/v1/jobs/{job_id}/result")
    assert result.status_code == 409
    assert result.json()["error_code"] == "JOB_NOT_SUCCEEDED"
    exported = client.get(f"/v1/jobs/{job_id}/exports/txt")
    assert exported.status_code == 409
    assert exported.json()["error_code"] == "JOB_NOT_SUCCEEDED"


def test_cancel_succeeded_failed_and_unknown_jobs_use_the_unified_conflict(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_id = upload(client).json()["id"]
    succeeded_id = create_job(client, file_id).json()["id"]
    assert wait_for(client, succeeded_id)["status"] == "succeeded"

    succeeded_cancel = client.post(f"/v1/jobs/{succeeded_id}/cancel")
    assert succeeded_cancel.status_code == 409
    assert succeeded_cancel.json() == {
        "detail": succeeded_cancel.json()["detail"],
        "error_code": "JOB_NOT_CANCELLABLE",
        "stage": "cancel",
    }
    assert isinstance(succeeded_cancel.json()["detail"], str)

    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 1.0, "text": "boom"}],
        text="boom",
        duration=1.0,
        fail={"stage": "transcribe", "code": "MODEL_LOAD_FAILED"},
    )
    failed_file_id = upload(client).json()["id"]
    failed_id = create_job(client, failed_file_id).json()["id"]
    assert wait_for(client, failed_id)["status"] == "failed"

    failed_cancel = client.post(f"/v1/jobs/{failed_id}/cancel")
    assert failed_cancel.status_code == 409
    assert failed_cancel.json()["error_code"] == "JOB_NOT_CANCELLABLE"

    missing = client.post("/v1/jobs/no-such-job/cancel")
    assert missing.status_code == 404
    assert missing.json()["error_code"] == "JOB_NOT_FOUND"


# ---------------------------------------------------------------------------
# Job operations: retry (ticket #37)
# ---------------------------------------------------------------------------


def test_retry_only_accepts_retryable_failed_jobs(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    file_id = upload(client).json()["id"]
    succeeded_id = create_job(client, file_id).json()["id"]
    assert wait_for(client, succeeded_id)["status"] == "succeeded"

    succeeded_retry = client.post(f"/v1/jobs/{succeeded_id}/retry")
    assert succeeded_retry.status_code == 409
    assert succeeded_retry.json()["error_code"] == "JOB_NOT_RETRYABLE"
    assert succeeded_retry.json()["stage"] == "retry"

    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 1.0, "text": "slow"}],
        text="slow",
        duration=1.0,
        delay_seconds=1.5,
    )
    running_file_id = upload(client).json()["id"]
    running_id = create_job(client, running_file_id).json()["id"]
    wait_for_status(client, running_id, {"running"})

    running_retry = client.post(f"/v1/jobs/{running_id}/retry")
    assert running_retry.status_code == 409
    assert running_retry.json()["error_code"] == "JOB_NOT_RETRYABLE"
    client.post(f"/v1/jobs/{running_id}/cancel")
    settle_worker(client, running_id)

    cancelled_id = seed_task(tmp_path, status="cancelled", progress=100)
    cancelled_retry = client.post(f"/v1/jobs/{cancelled_id}/retry")
    assert cancelled_retry.status_code == 409
    assert cancelled_retry.json()["error_code"] == "JOB_NOT_RETRYABLE"

    queued_id = seed_task(tmp_path, status="queued", progress=0)
    queued_retry = client.post(f"/v1/jobs/{queued_id}/retry")
    assert queued_retry.status_code == 409
    assert queued_retry.json()["error_code"] == "JOB_NOT_RETRYABLE"

    missing = client.post("/v1/jobs/no-such-job/retry")
    assert missing.status_code == 404
    assert missing.json()["error_code"] == "JOB_NOT_FOUND"


def test_retry_rejects_a_failure_that_is_not_retryable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 1.0, "text": "bad input"}],
        text="bad input",
        duration=1.0,
        fail={"stage": "transcribe", "code": "UNSUPPORTED_FORMAT"},
    )
    file_id = upload(client).json()["id"]
    job_id = create_job(client, file_id).json()["id"]
    failed = wait_for(client, job_id)
    assert failed["status"] == "failed"
    assert failed["error_code"] == "UNSUPPORTED_FORMAT"
    assert failed["retryable"] is False

    response = client.post(f"/v1/jobs/{job_id}/retry")

    assert response.status_code == 409
    assert response.json()["error_code"] == "JOB_NOT_RETRYABLE"
    assert response.json()["stage"] == "retry"


def test_retry_failed_job_resumes_from_completed_chunks(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    job_id = seed_task(
        tmp_path,
        status="failed_resumable",
        chunks=[
            (1, "completed", "first block"),
            (2, "failed", None),
            (3, "failed", None),
        ],
        error="Chunk 2/3 failed",
        error_code="CHUNK_FAILED",
    )
    # If the retry re-runs chunk 1 the fake engine fails the whole job, so a
    # success proves completed chunks were reused instead of transcribed again.
    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 1.0, "text": "chunk {chunk_index}"}],
        text="chunk {chunk_index}",
        duration=1.0,
        aligned=True,
        fail={"stage": "transcribe", "chunk_index": 1, "code": "SPEECH_ENGINE_FAILED"},
    )

    retried = client.post(f"/v1/jobs/{job_id}/retry")

    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "queued"
    assert retried.json()["stage"] == "queued"
    assert retried.json()["progress"] == 0.0

    finished = wait_for(client, job_id)
    assert finished["status"] == "succeeded", finished
    assert finished["error"] is None
    assert finished["error_code"] is None

    assert chunk_state(job_id) == [
        (1, "completed", "first block"),
        (2, "completed", "chunk 2"),
        (3, "completed", "chunk 3"),
    ]

    result = client.get(f"/v1/jobs/{job_id}/result")
    assert result.status_code == 200, result.text
    text = result.json()["text"]
    assert "first block" in text
    assert "chunk 2" in text
    assert "chunk 3" in text


# ---------------------------------------------------------------------------
# Job operations: delete (ticket #37)
# ---------------------------------------------------------------------------


def test_delete_job_removes_result_exports_and_source_media(
    client: TestClient,
) -> None:
    file_id = upload(client).json()["id"]
    job_id = create_job(client, file_id).json()["id"]
    assert wait_for(client, job_id)["status"] == "succeeded"
    assert client.get(f"/v1/jobs/{job_id}/result").status_code == 200
    assert client.get(f"/v1/jobs/{job_id}/exports/txt").status_code == 200

    deleted = client.delete(f"/v1/jobs/{job_id}")

    assert deleted.status_code == 204
    assert deleted.content == b""
    assert client.get(f"/v1/jobs/{job_id}").status_code == 404
    assert client.get(f"/v1/jobs/{job_id}").json()["error_code"] == "JOB_NOT_FOUND"
    assert client.get(f"/v1/jobs/{job_id}/result").status_code == 404
    assert client.get(f"/v1/jobs/{job_id}/exports/txt").status_code == 404
    assert client.get("/v1/jobs").json()["total"] == 0
    # Explicit deletion ends the media lifecycle immediately (#40): the source
    # upload is unreferenced and must be gone with the job.
    assert client.get(f"/v1/files/{file_id}").status_code == 404
    assert client.get(f"/v1/files/{file_id}/content").status_code == 404


def test_delete_failed_job_purges_the_retained_upload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 1.0, "text": "boom"}],
        text="boom",
        duration=1.0,
        fail={"stage": "transcribe", "code": "MODEL_LOAD_FAILED"},
    )
    file_id = upload(client).json()["id"]
    job_id = create_job(client, file_id).json()["id"]
    assert wait_for(client, job_id)["status"] == "failed"
    # A failed job keeps its upload so a retry can resume (#40).
    assert client.get(f"/v1/files/{file_id}").status_code == 200

    assert client.delete(f"/v1/jobs/{job_id}").status_code == 204

    assert client.get(f"/v1/jobs/{job_id}").status_code == 404
    assert client.get(f"/v1/jobs/{job_id}/result").status_code == 404
    assert client.get(f"/v1/files/{file_id}").status_code == 404


def test_delete_active_job_conflicts_until_it_is_cancelled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 1.0, "text": "slow"}],
        text="slow",
        duration=1.0,
        delay_seconds=1.5,
    )
    file_id = upload(client).json()["id"]
    job_id = create_job(client, file_id).json()["id"]
    wait_for_status(client, job_id, {"running"})

    blocked = client.delete(f"/v1/jobs/{job_id}")
    assert blocked.status_code == 409
    assert blocked.json()["error_code"] == "JOB_ACTIVE"
    assert blocked.json()["stage"] == "delete"

    assert client.post(f"/v1/jobs/{job_id}/cancel").status_code == 200
    # Cancel is cooperative: the row is terminal immediately, but the worker
    # may still be winding down its in-flight inference. Deletion waits for it.
    deadline = time.monotonic() + 15
    deleted = None
    while time.monotonic() < deadline:
        deleted = client.delete(f"/v1/jobs/{job_id}")
        if deleted.status_code == 204:
            break
        assert deleted.status_code == 409
        assert deleted.json()["error_code"] == "JOB_ACTIVE"
        time.sleep(0.05)
    assert deleted is not None and deleted.status_code == 204
    assert client.get(f"/v1/jobs/{job_id}").status_code == 404
    assert client.get(f"/v1/files/{file_id}").status_code == 404

    missing = client.delete("/v1/jobs/no-such-job")
    assert missing.status_code == 404
    assert missing.json()["error_code"] == "JOB_NOT_FOUND"

    queued_id = seed_task(tmp_path, status="queued", progress=0)
    queued_delete = client.delete(f"/v1/jobs/{queued_id}")
    assert queued_delete.status_code == 409
    assert queued_delete.json()["error_code"] == "JOB_ACTIVE"

    failed_id = seed_task(
        tmp_path,
        status="failed",
        progress=100,
        error="boom",
        error_code="MODEL_LOAD_FAILED",
    )
    assert client.delete(f"/v1/jobs/{failed_id}").status_code == 204
    assert client.get(f"/v1/jobs/{failed_id}").status_code == 404


# ---------------------------------------------------------------------------
# Restart recovery (ticket #37)
# ---------------------------------------------------------------------------


def test_restart_marks_running_job_failed_retryable_and_retry_resumes_completed_chunks(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    job_id = seed_task(
        tmp_path,
        status="transcribing",
        progress=42,
        chunks=[(1, "completed", "first block"), (2, "transcribing", None)],
    )
    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 1.0, "text": "chunk {chunk_index}"}],
        text="chunk {chunk_index}",
        duration=1.0,
        aligned=True,
        fail={"stage": "transcribe", "chunk_index": 1, "code": "SPEECH_ENGINE_FAILED"},
    )

    from backend.app import create_app

    # Entering the lifespan is the app restart: interrupted active rows are
    # reconciled before the first request is served.
    with TestClient(create_app(), headers={"Authorization": f"Bearer {V1_TOKEN}"}) as restarted:
        interrupted = restarted.get(f"/v1/jobs/{job_id}")
        assert interrupted.status_code == 200, interrupted.text
        body = interrupted.json()
        assert body["status"] == "failed"
        assert body["stage"] == "failed"
        # The interrupted run's last reported progress is kept; a restart must
        # not claim the job reached 100% (ticket #64).
        assert body["progress"] == 0.42
        assert body["error_code"] == "TASK_INTERRUPTED"
        assert body["retryable"] is True

        retried = restarted.post(f"/v1/jobs/{job_id}/retry")
        assert retried.status_code == 200, retried.text
        assert retried.json()["status"] == "queued"

        finished = wait_for(restarted, job_id)
        assert finished["status"] == "succeeded", finished

    # Chunk 1 was completed before the restart and must not run again.
    assert chunk_state(job_id) == [
        (1, "completed", "first block"),
        (2, "completed", "chunk 2"),
    ]


# ---------------------------------------------------------------------------
# SSE event stream (ticket #37)
# ---------------------------------------------------------------------------


def read_job_events(client: TestClient, job_id: str, *, limit: int = 500) -> list[dict]:
    events: list[dict] = []
    with client.stream("GET", f"/v1/jobs/{job_id}/events") as response:
        assert response.status_code == 200, response.read()
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            payload = json.loads(line[len("data:"):].strip())
            events.append(payload)
            if len(events) >= limit or payload.get("status") in TERMINAL_STATUSES:
                break
    return events


def test_events_stream_reports_status_stage_and_progress_until_terminal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 1.0, "text": "streamed"}],
        text="streamed",
        duration=1.0,
        delay_seconds=1.0,
    )
    file_id = upload(client).json()["id"]
    job_id = create_job(client, file_id).json()["id"]

    events = read_job_events(client, job_id)

    assert events, "the stream produced no job events"
    assert events[0]["status"] in {"queued", "running"}
    assert events[-1]["status"] == "succeeded"
    assert events[-1]["stage"] == "done"
    assert events[-1]["progress"] == 1.0
    assert any(
        event["status"] == "running"
        and event["stage"] == "transcribe"
        and 0.0 < event["progress"] < 1.0
        for event in events
    ), events
    for event in events:
        assert {"id", "status", "stage", "progress"} <= set(event)
        assert event["id"] == job_id
    settle_worker(client, job_id)


def test_events_stream_closes_after_a_cancelled_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 1.0, "text": "slow"}],
        text="slow",
        duration=1.0,
        delay_seconds=1.5,
    )
    file_id = upload(client).json()["id"]
    job_id = create_job(client, file_id).json()["id"]
    wait_for_status(client, job_id, {"running"})
    assert client.post(f"/v1/jobs/{job_id}/cancel").status_code == 200

    events = read_job_events(client, job_id)

    assert events
    assert events[0]["status"] == "cancelled"
    assert events[-1]["status"] == "cancelled"
    assert events[-1]["stage"] == "cancelled"
    assert events[-1]["error_code"] == "TASK_CANCELLED"
    settle_worker(client, job_id)


def test_events_for_unknown_job_is_a_unified_404(client: TestClient) -> None:
    response = client.get("/v1/jobs/no-such-job/events")

    assert response.status_code == 404
    assert response.json()["error_code"] == "JOB_NOT_FOUND"

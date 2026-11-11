"""Ticket #38: one error shape for `/v1`, mandatory Bearer auth, tickets.

Everything here goes through the public HTTP contract: the fake speech engine
selects scripted output, the real tiny WAV is uploaded, and assertions read
only response bodies, status codes and headers.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from support import fake_speech_env, make_wav_bytes

TOKEN = "ticket-38-token"
UPPER_SNAKE = re.compile(r"^[A-Z][A-Z0-9_]*$")
REPO_ROOT = Path(__file__).resolve().parents[2]
AUTHORIZED = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BEEBOX_MODEL_STORAGE_ROOT", str(tmp_path / "models-root"))
    monkeypatch.setenv("BEEBOX_API_TOKEN", TOKEN)
    monkeypatch.setenv("BEEBOX_INLINE_LOCAL_TRANSCRIPTION", "1")
    monkeypatch.setenv("BEEBOX_DELETE_DERIVED_ON_COMPLETE", "1")
    monkeypatch.setenv("BEEBOX_GIT_COMMIT", "0123456789abcdef0123456789abcdef01234567")
    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 1.0, "text": "ticket 38 transcript"}],
        text="ticket 38 transcript",
        duration=1.0,
        aligned=True,
    )
    from backend.app import create_app

    return TestClient(create_app(), headers=AUTHORIZED)


def upload(client: TestClient, *, filename: str = "clip.wav") -> dict:
    response = client.post(
        "/v1/files",
        files={"file": (filename, make_wav_bytes(), "audio/wav")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_job(client: TestClient, file_id: str, **options) -> dict:
    response = client.post(
        "/v1/jobs",
        json={
            "kind": "transcription",
            "source": {"type": "file", "file_id": file_id},
            "options": options,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


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
    pytest.fail(f"job {job_id} did not settle: {body}")


def catalog(client: TestClient) -> dict[str, dict]:
    response = client.get("/v1/errors")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == len(body["items"])
    return {item["code"]: item for item in body["items"]}


def declared_api_error_codes() -> set[str]:
    """Every literal ``ApiError(..., "CODE", ...)`` code in the /v1 modules.

    Collecting the source of truth from the route modules keeps this test
    honest when a new route raises a new code: a hand-picked request list
    silently stops covering whatever was added last.
    """
    codes: set[str] = set()
    for path in sorted((REPO_ROOT / "backend" / "v1").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name != "ApiError":
                continue
            candidates: list[ast.expr] = []
            if len(node.args) >= 3:
                candidates.append(node.args[2])
            candidates.extend(
                keyword.value for keyword in node.keywords if keyword.arg == "error_code"
            )
            for candidate in candidates:
                if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
                    codes.add(candidate.value)
    return codes


def assert_unified_error(response, *, status: int, code: str, stage: str) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"detail", "error_code", "stage"}, body
    assert isinstance(body["detail"], str) and body["detail"], body
    assert body["error_code"] == code, body
    assert UPPER_SNAKE.match(body["error_code"]), body
    assert body["stage"] == stage, body
    return body


# ---------------------------------------------------------------------------
# Error-code catalog
# ---------------------------------------------------------------------------


def test_error_catalog_is_public_contract_with_meanings(client: TestClient) -> None:
    codes = catalog(client)

    assert codes, "the error catalog must not be empty"
    for code, entry in codes.items():
        assert UPPER_SNAKE.match(code), code
        assert set(entry) == {"code", "http_status", "meaning"}, entry
        assert isinstance(entry["meaning"], str) and entry["meaning"].strip(), entry
        assert entry["http_status"] is None or 400 <= entry["http_status"] <= 599, entry


def test_error_catalog_covers_every_code_the_api_raises(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    raised: set[str] = set()
    bare = TestClient(client.app)

    # 401 auth codes (header missing/wrong, query token, bad ticket).
    raised.add(assert_unified_error(
        bare.get("/v1/files"),
        status=401,
        code="AUTH_REQUIRED",
        stage="auth",
    )["error_code"])
    raised.add(assert_unified_error(
        bare.get("/v1/files", headers={"Authorization": "Bearer nope"}),
        status=401,
        code="AUTH_INVALID_TOKEN",
        stage="auth",
    )["error_code"])
    raised.add(assert_unified_error(
        client.get(f"/v1/files?api_token={TOKEN}"),
        status=401,
        code="AUTH_QUERY_TOKEN_REJECTED",
        stage="auth",
    )["error_code"])
    raised.add(assert_unified_error(
        bare.get("/v1/files?resource_ticket=not-a-ticket"),
        status=401,
        code="AUTH_INVALID_RESOURCE_TICKET",
        stage="auth",
    )["error_code"])

    # 404 / 405 from routing.
    raised.add(assert_unified_error(
        client.get("/v1/no-such-route"),
        status=404,
        code="NOT_FOUND",
        stage="routing",
    )["error_code"])
    raised.add(assert_unified_error(
        client.patch("/v1/files"),
        status=405,
        code="METHOD_NOT_ALLOWED",
        stage="routing",
    )["error_code"])

    # 400 / 413 / 422 from the file and job routes.
    raised.add(assert_unified_error(
        client.post(
            "/v1/files",
            files={"file": ("empty.wav", b"", "audio/wav")},
        ),
        status=400,
        code="EMPTY_FILE",
        stage="file",
    )["error_code"])
    monkeypatch.setenv("BEEBOX_MAX_UPLOAD_BYTES", "1024")
    raised.add(assert_unified_error(
        client.post(
            "/v1/files",
            files={"file": ("big.wav", b"x" * 2048, "audio/wav")},
        ),
        status=413,
        code="FILE_TOO_LARGE",
        stage="file",
    )["error_code"])
    monkeypatch.delenv("BEEBOX_MAX_UPLOAD_BYTES")
    raised.add(assert_unified_error(
        client.get("/v1/files/does-not-exist"),
        status=404,
        code="FILE_NOT_FOUND",
        stage="file",
    )["error_code"])
    raised.add(assert_unified_error(
        client.post(
            "/v1/jobs",
            json={"kind": "transcription", "source": {"type": "file", "file_id": "nope"}},
        ),
        status=404,
        code="FILE_NOT_FOUND",
        stage="job",
    )["error_code"])
    file_id = upload(client)["id"]
    raised.add(assert_unified_error(
        client.post(
            "/v1/jobs",
            json={"kind": "speech", "source": {"type": "file", "file_id": file_id}},
        ),
        status=400,
        code="UNSUPPORTED_KIND",
        stage="job",
    )["error_code"])
    raised.add(assert_unified_error(
        client.post(
            "/v1/jobs",
            json={"kind": "transcription", "source": {"type": "stream", "file_id": file_id}},
        ),
        status=400,
        code="UNSUPPORTED_SOURCE_TYPE",
        stage="job",
    )["error_code"])
    raised.add(assert_unified_error(
        client.post(
            "/v1/jobs",
            json={"kind": "transcription", "source": {"type": "file"}},
        ),
        status=400,
        code="INVALID_SOURCE",
        stage="job",
    )["error_code"])
    raised.add(assert_unified_error(
        client.post(
            "/v1/jobs",
            json={
                "kind": "transcription",
                "source": {"type": "file", "file_id": file_id},
                "options": {"model": "no-such-model"},
            },
        ),
        status=400,
        code="MODEL_NOT_FOUND",
        stage="job",
    )["error_code"])
    raised.add(assert_unified_error(
        client.post(
            "/v1/jobs",
            json={
                "kind": "transcription",
                "source": {"type": "file", "file_id": file_id},
                "options": {"num_speakers": 0},
            },
        ),
        status=422,
        code="INVALID_REQUEST",
        stage="validation",
    )["error_code"])

    # 409 while a job is still running.
    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 1.0, "text": "slow"}],
        text="slow",
        duration=1.0,
        delay_seconds=1.5,
    )
    job_id = create_job(client, file_id)["id"]
    raised.add(assert_unified_error(
        client.get(f"/v1/jobs/{job_id}/result"),
        status=409,
        code="JOB_NOT_SUCCEEDED",
        stage="result",
    )["error_code"])
    raised.add(assert_unified_error(
        client.delete(f"/v1/files/{file_id}"),
        status=409,
        code="FILE_IN_USE",
        stage="file",
    )["error_code"])
    assert_unified_error(
        client.get("/v1/jobs/no-such-job/result"),
        status=404,
        code="JOB_NOT_FOUND",
        stage="job",
    )
    raised.add("JOB_NOT_FOUND")
    wait_for(client, job_id)
    raised.add(assert_unified_error(
        client.get(f"/v1/jobs/{job_id}/exports/pdf"),
        status=400,
        code="UNSUPPORTED_FORMAT",
        stage="export",
    )["error_code"])

    # 500 from an unexpected failure is still the unified shape.
    app = client.app

    @app.get("/v1/test-boom")
    def boom() -> None:
        raise RuntimeError("secret internal detail")

    failing = TestClient(app, headers=AUTHORIZED, raise_server_exceptions=False)
    raised.add(assert_unified_error(
        failing.get("/v1/test-boom"),
        status=500,
        code="INTERNAL_ERROR",
        stage="server",
    )["error_code"])

    # Job-level failure codes travel in a job payload, not an HTTP error.
    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 1.0, "text": "will fail"}],
        text="will fail",
        duration=1.0,
        fail={"stage": "transcribe", "code": "SPEECH_ENGINE_FAILED"},
    )
    failed_file = upload(client, filename="failing.wav")["id"]
    failed_job = wait_for(client, create_job(client, failed_file)["id"])
    assert failed_job["status"] == "failed"
    raised.add(failed_job["error_code"])

    codes = catalog(client)
    declared = declared_api_error_codes()
    # Sanity: the AST walk must actually see the routers' error codes.
    assert len(declared) >= 20, sorted(declared)
    missing = sorted((raised | declared) - set(codes))
    assert not missing, (
        "error codes the /v1 modules can raise are absent from /v1/errors: "
        f"{missing}"
    )

    # The lifecycle codes added for cancel/retry/delete and the model download
    # state machine are HTTP 409s, not job-level failures.
    for code in (
        "JOB_ACTIVE",
        "JOB_NOT_CANCELLABLE",
        "JOB_NOT_RETRYABLE",
        "MODEL_ALREADY_DOWNLOADED",
        "MODEL_DOWNLOAD_IN_PROGRESS",
        "MODEL_NOT_DOWNLOADABLE",
        "MODEL_NOT_DOWNLOADING",
    ):
        assert code in codes, f"{code} is missing from the error catalog"
        assert codes[code]["http_status"] == 409, (code, codes[code])


def test_error_catalog_lists_pipeline_failure_codes(client: TestClient) -> None:
    codes = catalog(client)

    # These are emitted through a job's error_code by the pipeline itself.
    for code in ("TASK_FAILED", "TASK_CANCELLED", "CHUNK_FAILED", "MODEL_LOAD_FAILED"):
        assert code in codes, f"{code} is missing from the error catalog"
        assert codes[code]["http_status"] is None


# ---------------------------------------------------------------------------
# Bearer authentication
# ---------------------------------------------------------------------------


def test_v1_requires_a_bearer_token_except_health(client: TestClient) -> None:
    bare = TestClient(client.app)

    assert bare.get("/v1/health").status_code == 200

    for method, path in (
        ("GET", "/v1/errors"),
        ("GET", "/v1/files"),
        ("GET", "/v1/jobs"),
        ("POST", "/v1/jobs"),
        ("POST", "/v1/resource-tickets"),
        ("GET", "/v1/queue"),
        ("POST", "/v1/queue/pause"),
    ):
        response = bare.request(method, path)
        body = assert_unified_error(response, status=401, code="AUTH_REQUIRED", stage="auth")
        assert body["detail"]
        assert response.headers["www-authenticate"] == "Bearer"


def test_wrong_bearer_token_is_rejected_in_the_unified_shape(client: TestClient) -> None:
    bare = TestClient(client.app)

    response = bare.get("/v1/files", headers={"Authorization": "Bearer wrong-token"})

    assert_unified_error(response, status=401, code="AUTH_INVALID_TOKEN", stage="auth")
    assert client.get("/v1/files").status_code == 200


def test_authentication_precedes_request_validation(client: TestClient) -> None:
    bare = TestClient(client.app)

    # An unauthenticated caller learns nothing about the body schema.
    response = bare.post("/v1/jobs", json={"nonsense": True})

    assert_unified_error(response, status=401, code="AUTH_REQUIRED", stage="auth")


def test_query_string_tokens_are_rejected_on_v1(client: TestClient) -> None:
    bare = TestClient(client.app)

    for path in (
        f"/v1/files?api_token={TOKEN}",
        "/v1/files?api_token=wrong-token",
        f"/v1/health?api_token={TOKEN}",
    ):
        assert_unified_error(
            bare.get(path),
            status=401,
            code="AUTH_QUERY_TOKEN_REJECTED",
            stage="auth",
        )

    # Not honoured even when a valid Bearer header is present as well.
    assert_unified_error(
        client.get(f"/v1/errors?api_token={TOKEN}"),
        status=401,
        code="AUTH_QUERY_TOKEN_REJECTED",
        stage="auth",
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def test_health_does_not_expose_sensitive_information(
    client: TestClient, tmp_path: Path
) -> None:
    bare = TestClient(client.app)

    v1 = bare.get("/v1/health")
    assert v1.status_code == 200
    body = v1.json()
    # The contract fields stay; queue health from #39 is operational metadata.
    assert {"online", "ready", "version", "commit"} <= set(body)
    for leaked in ("data_dir", "models_dir", "token", "storage_path"):
        assert leaked not in body
    assert TOKEN not in v1.text
    assert str(tmp_path) not in v1.text


def test_health_hides_job_details_without_a_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Warm the cached runtime probe: the first /v1/health in a process can
    # spend seconds probing the GPU, long enough for the scripted job below to
    # finish before the assertions read the queue.
    assert client.get("/v1/health").status_code == 200

    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 1.0, "text": "slow health check"}],
        text="slow health check",
        duration=1.0,
        delay_seconds=3.0,
    )
    job_id = create_job(client, upload(client)["id"])["id"]

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if client.get("/v1/health").json()["queue"]["current_job_id"] == job_id:
            break
        time.sleep(0.02)
    else:
        pytest.fail("the job never became the running queue entry")

    bare = TestClient(client.app)
    public_response = bare.get("/v1/health")
    assert public_response.status_code == 200, public_response.text
    public = public_response.json()
    assert public["queue"]["current_job_id"] is None
    assert public["queue"]["current_job_kind"] is None
    # Aggregate queue state is not a job detail and stays visible.
    assert public["queue"]["paused"] is False
    assert public["queue"]["length"] == 0
    assert job_id not in public_response.text

    authenticated = client.get("/v1/health").json()
    assert authenticated["queue"]["current_job_id"] == job_id
    assert authenticated["queue"]["current_job_kind"] == "transcription"

    wait_for(client, job_id)


def test_health_reports_gpu_state_without_requiring_a_gpu(client: TestClient) -> None:
    bare = TestClient(client.app)

    response = bare.get("/v1/health")
    assert response.status_code == 200, response.text
    gpu = response.json()["gpu"]

    assert set(gpu) == {"available", "name", "memory_total_mb", "memory_used_mb"}
    assert isinstance(gpu["available"], bool)
    if gpu["available"]:
        assert isinstance(gpu["name"], str) and gpu["name"]
        for key in ("memory_total_mb", "memory_used_mb"):
            assert gpu[key] is None or (isinstance(gpu[key], int) and gpu[key] >= 0)
    else:
        # No CUDA device, torch or nvidia-smi: the shape must still be stable.
        assert gpu["name"] is None
        assert gpu["memory_total_mb"] is None
        assert gpu["memory_used_mb"] is None


# ---------------------------------------------------------------------------
# Resource tickets for headerless downloads
# ---------------------------------------------------------------------------


def _issue_ticket(client: TestClient, path: str, method: str = "GET") -> dict:
    response = client.post("/v1/resource-tickets", json={"path": path, "method": method})
    assert response.status_code == 200, response.text
    return response.json()


def test_resource_ticket_allows_exactly_one_headerless_download(client: TestClient) -> None:
    file = upload(client)
    content = make_wav_bytes()

    grant = _issue_ticket(client, file["download_url"])
    assert set(grant) == {"ticket", "path", "method", "expires_at"}
    assert grant["path"] == file["download_url"]
    assert grant["method"] == "GET"
    assert grant["ticket"] != TOKEN
    ttl = datetime.fromisoformat(grant["expires_at"]) - datetime.now(UTC)
    assert 0 < ttl.total_seconds() <= 60
    assert TOKEN not in grant["ticket"]

    bare = TestClient(client.app)
    url = f"{file['download_url']}?resource_ticket={grant['ticket']}"
    first = bare.get(url)
    assert first.status_code == 200
    assert first.content == content

    # A ticket is single use: replaying it is rejected like any wrong credential.
    assert_unified_error(
        bare.get(url),
        status=401,
        code="AUTH_INVALID_RESOURCE_TICKET",
        stage="auth",
    )


def test_resource_ticket_is_scoped_to_one_path_and_method(client: TestClient) -> None:
    first = upload(client, filename="first.wav")
    second = upload(client, filename="second.wav")

    get_ticket = _issue_ticket(client, first["download_url"])
    bare = TestClient(client.app)
    assert_unified_error(
        bare.get(f"{second['download_url']}?resource_ticket={get_ticket['ticket']}"),
        status=401,
        code="AUTH_INVALID_RESOURCE_TICKET",
        stage="auth",
    )
    # The failed attempt did not consume the ticket.
    assert bare.get(f"{first['download_url']}?resource_ticket={get_ticket['ticket']}").status_code == 200

    head_ticket = _issue_ticket(client, second["download_url"], "HEAD")
    assert_unified_error(
        bare.get(f"{second['download_url']}?resource_ticket={head_ticket['ticket']}"),
        status=401,
        code="AUTH_INVALID_RESOURCE_TICKET",
        stage="auth",
    )
    assert bare.head(f"{second['download_url']}?resource_ticket={head_ticket['ticket']}").status_code == 200


def test_resource_tickets_cannot_be_issued_without_auth_or_for_other_paths(
    client: TestClient,
) -> None:
    file = upload(client)
    bare = TestClient(client.app)

    assert_unified_error(
        bare.post("/v1/resource-tickets", json={"path": file["download_url"]}),
        status=401,
        code="AUTH_REQUIRED",
        stage="auth",
    )

    for path in ("/tasks/some-task/audio", "/v1/files/../secrets", "https://example.com/file"):
        assert_unified_error(
            client.post("/v1/resource-tickets", json={"path": path}),
            status=400,
            code="RESOURCE_TICKET_PATH_INVALID",
            stage="auth",
        )


# ---------------------------------------------------------------------------
# Startup guard
# ---------------------------------------------------------------------------


def test_server_refuses_a_non_loopback_bind_without_a_token(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["BEEBOX_DATA_DIR"] = str(tmp_path)
    environment.pop("BEEBOX_API_TOKEN", None)
    environment.pop("BEEBOX_PUBLIC_BIND_ADDRESS", None)

    completed = subprocess.run(
        [sys.executable, "-m", "backend.server", "--host", "0.0.0.0", "--port", "0"],
        cwd=str(REPO_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode != 0, completed.stdout
    assert "BEEBOX_API_TOKEN" in completed.stderr
    assert "0.0.0.0" in completed.stderr


def test_loopback_binds_do_not_require_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend import config

    monkeypatch.delenv("BEEBOX_API_TOKEN", raising=False)
    for host in ("127.0.0.1", "localhost", "::1", "[::1]"):
        config.validate_bind_security(host)

    for host in ("0.0.0.0", "192.168.1.10", "::"):
        with pytest.raises(RuntimeError, match="BEEBOX_API_TOKEN"):
            config.validate_bind_security(host)

    monkeypatch.setenv("BEEBOX_API_TOKEN", "configured")
    config.validate_bind_security("0.0.0.0")

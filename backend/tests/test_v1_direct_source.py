"""End-to-end tests for ``source.type = "direct"`` (ticket #32).

Every case drives the public ``/v1`` surface. Both HTTP seams are scripted
(``direct_media.resolve_host`` and ``direct_media.get_http_transport``), so no
test resolves a real name, opens a socket or touches a real host. The fake
speech engine stands in for the GPU, exactly like the video-link tests.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from support import (
    FakeHttpResponse,
    FakeHttpTransport,
    fake_speech_env,
    make_wav_bytes,
)

TERMINAL = {"succeeded", "failed", "cancelled"}
GLOBAL_ADDRESS = "93.184.216.34"
MEDIA_URL = "https://media.example/clip.wav?signature=sentinel-value"
SIGNED_QUERY = "signature=sentinel-value"


def wav_response(duration_seconds: float = 2.0) -> FakeHttpResponse:
    body = make_wav_bytes(duration_seconds)
    return FakeHttpResponse(
        headers={"Content-Type": "audio/wav"},
        chunks=(body[:1024], body[1024:]),
    )


class Resolver:
    """A scripted host resolver that records the lookups it answered."""

    def __init__(self, *addresses: str) -> None:
        self.addresses = tuple(addresses) or (GLOBAL_ADDRESS,)
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> tuple[str, ...]:
        self.calls.append((host, port))
        return self.addresses


def install_transport(
    monkeypatch: pytest.MonkeyPatch,
    *responses,
    addresses: tuple[str, ...] = (GLOBAL_ADDRESS,),
) -> tuple[FakeHttpTransport, Resolver]:
    from backend.services import direct_media

    transport = FakeHttpTransport(*responses)
    resolver = Resolver(*addresses)
    monkeypatch.setattr(direct_media, "get_http_transport", lambda: transport)
    monkeypatch.setattr(direct_media, "resolve_host", resolver)
    return transport, resolver


def configure_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BEEBOX_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv("BEEBOX_MODEL_STORAGE_ROOT", str(tmp_path / "models-root"))
    monkeypatch.delenv("BEEBOX_API_TOKEN", raising=False)
    monkeypatch.setenv("BEEBOX_GIT_COMMIT", "0123456789abcdef0123456789abcdef01234567")
    monkeypatch.setenv("BEEBOX_INLINE_LOCAL_TRANSCRIPTION", "1")
    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 2.0, "text": "直链语音识别得到的句子。"}],
        text="直链语音识别得到的句子。",
        duration=2.0,
        language="zh",
        aligned=True,
    )
    from backend.app import create_app

    return TestClient(create_app())


def create_direct_job(client: TestClient, url: str = MEDIA_URL, **source):
    body: dict = {"kind": "transcription", "source": {"type": "direct", "url": url}}
    body["source"].update(source)
    return client.post("/v1/jobs", json=body)


def wait_for(client: TestClient, job_id: str, *, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/v1/jobs/{job_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] in TERMINAL:
            return body
        time.sleep(0.02)
    pytest.fail(f"job {job_id} did not reach a terminal status: {body}")


def uploads_for(uploads_dir: Path, job_id: str) -> list[Path]:
    if not uploads_dir.exists():
        return []
    return [path for path in uploads_dir.iterdir() if job_id in path.name]


def wait_for_media_cleanup(
    uploads_dir: Path, job_id: str, *, timeout: float = 10.0
) -> None:
    """ADR-0011 cleanup runs right after the job row is marked succeeded."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not uploads_for(uploads_dir, job_id):
            return
        time.sleep(0.02)
    pytest.fail(
        f"temporary media for {job_id} was not cleaned up: "
        f"{uploads_for(uploads_dir, job_id)}"
    )


def json_text(payload: object) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)


def assert_job_rejected(client: TestClient, body: dict, code: str) -> dict:
    before = client.get("/v1/jobs").json()["total"]
    rejected = client.post("/v1/jobs", json=body)

    assert rejected.status_code == 400, rejected.text
    payload = rejected.json()
    assert payload["error_code"] == code, payload
    assert payload["stage"] == "job", payload
    assert payload["detail"], payload
    assert client.get("/v1/jobs").json()["total"] == before
    return payload


# ---------------------------------------------------------------------------
# Submitting and completing a direct job
# ---------------------------------------------------------------------------


def test_a_direct_media_link_produces_a_transcript_and_echoes_its_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = configure_client(tmp_path, monkeypatch)
    transport, resolver = install_transport(monkeypatch, wav_response())
    source = {
        "title": "直接音频示例",
        "author": "BeeBox 测试",
        "platform": "example",
        "page_url": "https://example.com/episode/1",
    }

    created = create_direct_job(client, **source)
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]
    body = created.json()
    assert body["source"]["type"] == "direct"
    assert body["source"]["url"] == MEDIA_URL
    for key, value in source.items():
        assert body["source"][key] == value, body["source"]
    assert body["source"]["text_source"] == "asr"
    assert body["source"]["subtitle_language"] is None

    finished = wait_for(client, job_id)
    assert finished["status"] == "succeeded", finished
    for key, value in source.items():
        assert finished["source"][key] == value, finished["source"]
    assert finished["source"]["url"] == MEDIA_URL
    assert finished["source"]["text_source"] == "asr"
    assert finished["source"]["subtitle_language"] is None
    # The ASR media supplies the duration the link itself did not declare.
    assert finished["source"]["duration_ms"] == 2000
    assert transport.requests == [{"url": MEDIA_URL, "timeout": 30}]
    assert resolver.calls == [("media.example", 443)]

    result = client.get(f"/v1/jobs/{job_id}/result").json()
    assert result["source"] == finished["source"]
    assert result["text"] == "直链语音识别得到的句子。"
    assert result["segments"][0]["text"] == "直链语音识别得到的句子。"
    # ADR-0011: the downloaded media is temporary and disappears on success.
    wait_for_media_cleanup(tmp_path / "uploads", job_id)


def test_optional_source_fields_are_null_when_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = configure_client(tmp_path, monkeypatch)
    install_transport(monkeypatch, wav_response())

    created = create_direct_job(client)
    assert created.status_code == 201, created.text
    source = created.json()["source"]
    assert source["title"] is None
    assert source["author"] is None
    assert source["platform"] is None
    assert source["page_url"] is None
    # The filename falls back to the URL's last path segment, never the query.
    assert source["filename"] == "clip.wav"
    assert SIGNED_QUERY not in source["filename"]

    finished = wait_for(client, created.json()["id"])
    assert finished["status"] == "succeeded", finished
    assert finished["source"]["title"] is None
    assert finished["source"]["author"] is None
    assert finished["source"]["platform"] is None
    assert finished["source"]["page_url"] is None


@pytest.mark.parametrize("url", [None, "", "   \t"])
def test_a_missing_or_blank_url_is_invalid_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str | None
) -> None:
    client = configure_client(tmp_path, monkeypatch)
    install_transport(monkeypatch)
    body: dict = {"kind": "transcription", "source": {"type": "direct"}}
    if url is not None:
        body["source"]["url"] = url

    payload = assert_job_rejected(client, body, "INVALID_SOURCE")
    assert "url" in payload["detail"]


@pytest.mark.parametrize(
    "url",
    [
        "ftp://media.example/clip.wav",
        "file:///tmp/clip.wav",
        "not a url",
        "https://",
        # urlsplit() itself raises ValueError for these.
        "http://[::1",
        "https://[bad/",
    ],
)
def test_a_non_http_link_is_rejected_with_invalid_url_and_no_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    client = configure_client(tmp_path, monkeypatch)
    transport, _ = install_transport(monkeypatch)

    payload = assert_job_rejected(
        client, {"kind": "transcription", "source": {"type": "direct", "url": url}}, "INVALID_URL"
    )
    # A signed link must never be echoed by an error message.
    assert SIGNED_QUERY not in payload["detail"]
    assert transport.requests == []


# ---------------------------------------------------------------------------
# The security baseline: public addresses only, no redirects
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("addresses"),
    [
        ("127.0.0.1",),
        ("10.0.0.1",),
        ("192.168.1.5",),
        ("169.254.1.1",),
        ("192.0.2.7",),
        ("100.64.0.1",),
        ("::1",),
        ("fc00::1",),
        ("224.0.0.1",),
        # One public answer is not enough: every answer must be public.
        (GLOBAL_ADDRESS, "10.0.0.1"),
    ],
)
def test_loopback_private_and_reserved_targets_fail_without_a_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, addresses: tuple[str, ...]
) -> None:
    client = configure_client(tmp_path, monkeypatch)
    transport, resolver = install_transport(monkeypatch, addresses=addresses)

    created = create_direct_job(client)
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]
    finished = wait_for(client, job_id)

    assert finished["status"] == "failed", finished
    assert finished["error_code"] == "DIRECT_URL_NOT_ALLOWED", finished
    assert finished["retryable"] is False, finished
    assert finished["error"], finished
    assert transport.requests == []
    assert resolver.calls == [("media.example", 443)]
    assert uploads_for(tmp_path / "uploads", job_id) == []
    assert client.get(f"/v1/jobs/{job_id}/result").status_code == 409


def test_a_literal_loopback_host_is_refused_by_the_real_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.services import direct_media

    client = configure_client(tmp_path, monkeypatch)
    transport = FakeHttpTransport()
    # Only the transport is scripted: the real resolver handles a literal
    # address without DNS, proving the job-level check before any request.
    monkeypatch.setattr(direct_media, "get_http_transport", lambda: transport)

    created = create_direct_job(client, "http://127.0.0.1:8080/clip.wav")
    assert created.status_code == 201, created.text
    finished = wait_for(client, created.json()["id"])

    assert finished["status"] == "failed", finished
    assert finished["error_code"] == "DIRECT_URL_NOT_ALLOWED", finished
    assert transport.requests == []


def test_a_redirect_is_refused_and_the_target_is_never_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = configure_client(tmp_path, monkeypatch)
    transport, _ = install_transport(
        monkeypatch,
        FakeHttpResponse(302, headers={"Location": "https://elsewhere.example/payload"}),
    )

    created = create_direct_job(client)
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]
    finished = wait_for(client, job_id)

    assert finished["status"] == "failed", finished
    assert finished["error_code"] == "DIRECT_URL_NOT_ALLOWED", finished
    assert finished["retryable"] is False, finished
    assert "redirect" in finished["error"].lower(), finished
    assert transport.requests == [{"url": MEDIA_URL, "timeout": 30}]
    assert MEDIA_URL not in finished["error"]
    assert uploads_for(tmp_path / "uploads", job_id) == []


# ---------------------------------------------------------------------------
# Media and size validation
# ---------------------------------------------------------------------------


def test_an_html_page_is_reported_as_unsupported_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = configure_client(tmp_path, monkeypatch)
    install_transport(
        monkeypatch,
        FakeHttpResponse(
            headers={"Content-Type": "text/html; charset=utf-8"},
            chunks=(b"<!doctype html><title>not media</title>",),
        ),
    )

    created = create_direct_job(client)
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]
    finished = wait_for(client, job_id)

    assert finished["status"] == "failed", finished
    assert finished["error_code"] == "UNSUPPORTED_MEDIA_FORMAT", finished
    assert finished["retryable"] is False, finished
    assert uploads_for(tmp_path / "uploads", job_id) == []


@pytest.mark.parametrize(
    "response",
    [
        # Declared up front.
        FakeHttpResponse(
            headers={"Content-Type": "audio/wav", "Content-Length": "999999"},
            chunks=(b"x" * 999999,),
        ),
        # Only discovered while streaming: no Content-Length header.
        FakeHttpResponse(
            headers={"Content-Type": "audio/wav"},
            chunks=(b"x" * 800, b"x" * 800),
        ),
    ],
    ids=["content_length", "streaming"],
)
def test_responses_over_the_upload_cap_fail_as_file_too_large(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response: FakeHttpResponse
) -> None:
    monkeypatch.setenv("BEEBOX_MAX_UPLOAD_BYTES", "1024")
    client = configure_client(tmp_path, monkeypatch)
    install_transport(monkeypatch, response)

    created = create_direct_job(client)
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]
    finished = wait_for(client, job_id)

    assert finished["status"] == "failed", finished
    assert finished["error_code"] == "FILE_TOO_LARGE", finished
    assert finished["retryable"] is False, finished
    assert uploads_for(tmp_path / "uploads", job_id) == []


# ---------------------------------------------------------------------------
# Retry: download failures re-download, ASR failures stay on the GPU path
# ---------------------------------------------------------------------------


def test_a_download_failure_is_retryable_and_the_retry_downloads_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = configure_client(tmp_path, monkeypatch)
    transport, _ = install_transport(
        monkeypatch, FakeHttpResponse(404), wav_response()
    )

    created = create_direct_job(client)
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]
    failed = wait_for(client, job_id)

    assert failed["status"] == "failed", failed
    assert failed["error_code"] == "DOWNLOAD_NETWORK_ERROR", failed
    assert failed["retryable"] is True, failed
    assert len(transport.requests) == 1
    assert uploads_for(tmp_path / "uploads", job_id) == []

    retried = client.post(f"/v1/jobs/{job_id}/retry")
    assert retried.status_code == 200, retried.text
    finished = wait_for(client, job_id)

    assert finished["status"] == "succeeded", finished
    assert len(transport.requests) == 2
    assert transport.requests[1]["url"] == MEDIA_URL
    wait_for_media_cleanup(tmp_path / "uploads", job_id)


def test_a_timeout_is_reported_as_a_retryable_download_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.services import direct_media

    client = configure_client(tmp_path, monkeypatch)
    transport, _ = install_transport(monkeypatch, direct_media.HttpTimeout("scripted"))

    created = create_direct_job(client)
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]
    finished = wait_for(client, job_id)

    assert finished["status"] == "failed", finished
    assert finished["error_code"] == "DOWNLOAD_TIMEOUT", finished
    assert finished["retryable"] is True, finished
    assert uploads_for(tmp_path / "uploads", job_id) == []


def test_an_asr_failure_retries_on_the_gpu_path_without_downloading_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = configure_client(tmp_path, monkeypatch)
    fake_speech_env(
        monkeypatch,
        fail={"stage": "transcribe", "code": "SPEECH_ENGINE_FAILED"},
    )
    transport, _ = install_transport(monkeypatch, wav_response())

    created = create_direct_job(client)
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]
    failed = wait_for(client, job_id)

    assert failed["status"] == "failed", failed
    assert failed["error_code"] == "SPEECH_ENGINE_FAILED", failed
    assert failed["retryable"] is True, failed
    assert len(transport.requests) == 1

    # The media was already downloaded and validated; a retry must stay on the
    # GPU/ASR path instead of downloading the link again.
    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 2.0, "text": "重试后的句子。"}],
        text="重试后的句子。",
        duration=2.0,
        language="zh",
        aligned=True,
    )
    retried = client.post(f"/v1/jobs/{job_id}/retry")
    assert retried.status_code == 200, retried.text
    finished = wait_for(client, job_id)

    assert finished["status"] == "succeeded", finished
    assert finished["source"]["text_source"] == "asr"
    assert len(transport.requests) == 1


# ---------------------------------------------------------------------------
# Queue hand-off and cleanup
# ---------------------------------------------------------------------------


def test_the_download_completes_before_the_job_joins_the_gpu_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = configure_client(tmp_path, monkeypatch)
    install_transport(monkeypatch, wav_response())
    assert client.post("/v1/queue/pause").status_code == 200

    created = create_direct_job(client)
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]

    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        queue = client.get("/v1/queue").json()
        if queue["length"] == 1:
            break
        time.sleep(0.02)
    else:  # pragma: no cover - only on a very slow machine
        pytest.fail(f"the direct job never joined the GPU queue: {queue}")

    queued = client.get(f"/v1/jobs/{job_id}").json()
    assert queued["status"] == "queued", queued
    # The media exists before the GPU queue takes over.
    assert uploads_for(tmp_path / "uploads", job_id) != []

    assert client.post("/v1/queue/resume").status_code == 200
    finished = wait_for(client, job_id)

    assert finished["status"] == "succeeded", finished
    wait_for_media_cleanup(tmp_path / "uploads", job_id)


def test_task_logs_and_diagnostics_never_contain_the_signed_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.database import session as db_session
    from backend.services import diagnostics, task_logs

    client = configure_client(tmp_path, monkeypatch)
    install_transport(monkeypatch, FakeHttpResponse(403))

    created = create_direct_job(client)
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]
    finished = wait_for(client, job_id)
    assert finished["status"] == "failed", finished

    db = db_session.SessionLocal()
    try:
        logs = task_logs.list_logs(db, job_id)
        items = diagnostics.list_task_diagnostics(db, job_id)
    finally:
        db.close()
    assert logs, "the direct job should log its progress"
    rendered = json_text(
        [
            {"message": log.message, "data": log.data}
            for log in logs
        ]
        + [{"message": item.message, "command": item.command} for item in items]
    )
    assert SIGNED_QUERY not in rendered
    assert MEDIA_URL not in rendered
    # The caller's own job response may carry the submitted URL (it is the
    # retry input), but its error text may not.
    assert MEDIA_URL not in finished["error"]


# ---------------------------------------------------------------------------
# The public error catalog
# ---------------------------------------------------------------------------


def test_direct_source_codes_are_in_the_public_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = configure_client(tmp_path, monkeypatch)

    response = client.get("/v1/errors")
    assert response.status_code == 200, response.text
    items = {item["code"]: item for item in response.json()["items"]}
    assert items["DIRECT_URL_NOT_ALLOWED"]["http_status"] is None
    assert items["DIRECT_URL_NOT_ALLOWED"]["meaning"].strip()
    # The upload cap reuses the documented 413 code.
    assert items["FILE_TOO_LARGE"]["http_status"] == 413

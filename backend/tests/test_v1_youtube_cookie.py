"""YouTube age-restricted videos and the write-only cookie upload (ticket #48).

YouTube normally needs no login: the bgutil PO token provider covers the
"confirm you're not a bot" check. Age-restricted videos are the exception.
These tests drive the public ``/v1`` surface with the fake platform:

* an age-restricted video fails with ``VIDEO_LOGIN_REQUIRED`` until a cookie
  file has been uploaded;
* the admin upload accepts a Netscape cookie file and never echoes its
  contents in the response, the job logs or the application log;
* invalid or oversized uploads are rejected without echoing the payload.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from support import fake_speech_env

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "video_platform"
TERMINAL = {"succeeded", "failed", "cancelled"}

AGE_RESTRICTED_URL = "https://www.youtube.com/watch?v=AgeRestricted48"
COOKIE_MARKER = "SUPER_SECRET_COOKIE_VALUE_48"
COOKIE_CONTENT = (
    "# Netscape HTTP Cookie File\n"
    "# This is a generated file! Do not edit.\n"
    f".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\t{COOKIE_MARKER}\n"
)


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, api_token: str | None = None) -> TestClient:
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BEEBOX_MODEL_STORAGE_ROOT", str(tmp_path / "models-root"))
    if api_token:
        monkeypatch.setenv("BEEBOX_API_TOKEN", api_token)
    else:
        monkeypatch.delenv("BEEBOX_API_TOKEN", raising=False)
    monkeypatch.setenv("BEEBOX_VIDEO_PLATFORM", "fake")
    monkeypatch.setenv("BEEBOX_VIDEO_FAKE_ROOT", str(FIXTURE_ROOT))
    monkeypatch.setenv("BEEBOX_GIT_COMMIT", "0123456789abcdef0123456789abcdef01234567")
    fake_speech_env(
        monkeypatch,
        fail={
            "stage": "any",
            "code": "ASR_SHOULD_NOT_RUN",
            "message": "ASR must not run for the age-restricted subtitle path",
        },
    )
    from backend.app import create_app

    return TestClient(create_app())


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    return _client(tmp_path, monkeypatch)


def create_url_job(client: TestClient, url: str):
    return client.post(
        "/v1/jobs",
        json={"kind": "transcription", "source": {"type": "url", "url": url}},
    )


def wait_for(client: TestClient, job_id: str, *, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/v1/jobs/{job_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] in TERMINAL:
            return body
        time.sleep(0.05)
    pytest.fail(f"job {job_id} did not reach a terminal status: {body}")


def upload_cookie(client: TestClient, content: bytes | str = COOKIE_CONTENT):
    payload = content.encode("utf-8") if isinstance(content, str) else content
    return client.post(
        "/v1/admin/youtube-cookie",
        files={"file": ("youtube-cookies.txt", payload, "text/plain")},
    )


def _assert_no_cookie_leak(client: TestClient, job_id: str, caplog: pytest.LogCaptureFixture) -> None:
    # The legacy /tasks/{id}/logs route is gone (#56); the public job view and
    # captured logs are the remaining surfaces a cookie could leak through.
    job = client.get(f"/v1/jobs/{job_id}")
    assert job.status_code == 200, job.text
    assert COOKIE_MARKER not in job.text
    assert COOKIE_MARKER not in caplog.text


def test_age_restricted_video_without_cookie_fails_with_video_login_required(
    client: TestClient,
) -> None:
    created = create_url_job(client, AGE_RESTRICTED_URL)
    assert created.status_code == 201, created.text

    finished = wait_for(client, created.json()["id"])
    assert finished["status"] == "failed", finished
    assert finished["error_code"] == "VIDEO_LOGIN_REQUIRED"


def test_cookie_upload_is_write_only_and_unlocks_the_age_restricted_video(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    uploaded = upload_cookie(client)
    assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    assert body["configured"] is True
    assert body["size_bytes"] == len(COOKIE_CONTENT.encode("utf-8"))
    assert body["cookie_count"] == 1
    assert COOKIE_MARKER not in uploaded.text
    assert "\t" not in uploaded.text

    status = client.get("/v1/admin/youtube-cookie")
    assert status.status_code == 200, status.text
    assert status.json()["configured"] is True
    assert COOKIE_MARKER not in status.text

    created = create_url_job(client, AGE_RESTRICTED_URL)
    assert created.status_code == 201, created.text
    finished = wait_for(client, created.json()["id"])
    assert finished["status"] == "succeeded", finished
    assert finished["error_code"] is None
    assert finished["source"]["text_source"] == "platform_manual"
    assert "This is the English manual subtitle." in client.get(
        f"/v1/jobs/{created.json()['id']}/result"
    ).json()["text"]

    _assert_no_cookie_leak(client, created.json()["id"], caplog)
    assert COOKIE_MARKER not in caplog.text


def test_cookie_upload_rejects_invalid_and_oversized_files_without_echoing_content(
    client: TestClient,
) -> None:
    invalid = upload_cookie(client, f"not a cookie jar {COOKIE_MARKER}")
    assert invalid.status_code == 400, invalid.text
    assert invalid.json()["error_code"] == "YOUTUBE_COOKIE_INVALID"
    assert COOKIE_MARKER not in invalid.text

    empty = upload_cookie(client, b"")
    assert empty.status_code == 400, empty.text
    assert empty.json()["error_code"] == "YOUTUBE_COOKIE_INVALID"

    oversized = upload_cookie(
        client,
        "# Netscape HTTP Cookie File\n" + ("x" * (1024 * 1024 + 1)),
    )
    assert oversized.status_code == 413, oversized.text
    assert oversized.json()["error_code"] == "YOUTUBE_COOKIE_TOO_LARGE"


def test_cookie_admin_endpoint_is_bearer_protected_when_a_token_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch, api_token="youtube-cookie-admin-token")

    anonymous = upload_cookie(client)
    assert anonymous.status_code == 401, anonymous.text
    assert anonymous.json()["error_code"] == "AUTH_REQUIRED"

    authorized = client.post(
        "/v1/admin/youtube-cookie",
        headers={"Authorization": "Bearer youtube-cookie-admin-token"},
        files={"file": ("youtube-cookies.txt", COOKIE_CONTENT.encode("utf-8"), "text/plain")},
    )
    assert authorized.status_code == 201, authorized.text
    assert authorized.json()["configured"] is True

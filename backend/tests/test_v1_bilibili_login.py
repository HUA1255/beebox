"""QR login for Bilibili and write-only credential handling (ticket #46).

Everything here drives the public ``/v1`` surface with the fake Bilibili login
client and the fake video platform: no network, no GPU and no real account.
The cookie values used by the fake client are the secrets the tests scan every
response and captured log record for; the real implementation never returns
them and never writes them anywhere but the secrets directory.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from urllib.request import Request

import pytest
from fastapi.testclient import TestClient

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "video_platform"
COOKIE_FILENAME = "bilibili-cookies.txt"
TOKEN = "ticket-46-token"

FAKE_SECRETS = {
    "SESSDATA": "t46-sessdata-secret-value",
    "bili_jct": "t46-bili-jct-secret-value",
    "DedeUserID": "10046",
    "ac_time_value": "t46-ac-time-secret-value",
}


def fake_login_config(**overrides) -> dict:
    config = {
        "states": ["pending", "scanned", "confirmed"],
        "cookies": dict(FAKE_SECRETS),
        "qr_url": "https://fake.bilibili.test/qr/t46",
        "failure": "The fake Bilibili QR code expired",
    }
    config.update(overrides)
    return config


def build_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    login_config: dict | None = None,
    token: str | None = None,
) -> TestClient:
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BEEBOX_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv("BEEBOX_MODEL_STORAGE_ROOT", str(tmp_path / "models-root"))
    monkeypatch.setenv("BEEBOX_BILIBILI_LOGIN", "fake")
    monkeypatch.setenv(
        "BEEBOX_BILIBILI_LOGIN_FAKE",
        json.dumps(login_config if login_config is not None else fake_login_config()),
    )
    monkeypatch.setenv("BEEBOX_VIDEO_PLATFORM", "fake")
    monkeypatch.setenv("BEEBOX_VIDEO_FAKE_ROOT", str(FIXTURE_ROOT))
    monkeypatch.setenv("BEEBOX_GIT_COMMIT", "0123456789abcdef0123456789abcdef01234567")
    if token is None:
        monkeypatch.delenv("BEEBOX_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("BEEBOX_API_TOKEN", token)
    from backend.app import create_app

    headers = {"Authorization": f"Bearer {token}"} if token else None
    return TestClient(create_app(), headers=headers)


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    return tmp_path / "secrets"


@pytest.fixture
def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    return build_client(tmp_path, monkeypatch)


def login_status(client: TestClient) -> dict:
    response = client.get("/v1/bilibili/login")
    assert response.status_code == 200, response.text
    return response.json()


def test_qr_login_polling_writes_the_cookie_file_and_health_reports_valid(
    client: TestClient, secrets_dir: Path
) -> None:
    assert client.get("/v1/health").json()["bilibili_login"] == "absent"

    started = client.post("/v1/bilibili/login")
    assert started.status_code == 201, started.text
    body = started.json()
    assert body["status"] == "pending"
    assert body["qr_url"] == "https://fake.bilibili.test/qr/t46"
    assert body["qr_image"].startswith("data:image/png;base64,")
    # The payload is a real base64 PNG, not an opaque blob the console cannot use.
    base64.b64decode(body["qr_image"].split(",", 1)[1])

    assert login_status(client)["status"] == "pending"
    assert login_status(client)["status"] == "scanned"
    confirmed = login_status(client)
    assert confirmed["status"] == "confirmed"
    assert confirmed["detail"] is None

    cookie_path = secrets_dir / COOKIE_FILENAME
    assert cookie_path.is_file()
    assert COOKIE_FILENAME in {path.name for path in secrets_dir.iterdir()}

    assert client.get("/v1/health").json()["bilibili_login"] == "valid"


def test_status_is_idle_before_a_login_is_started(client: TestClient) -> None:
    assert login_status(client) == {"status": "idle", "detail": None}


def test_cancelling_stops_the_pending_login_without_writing_a_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secrets_dir: Path
) -> None:
    client = build_client(
        tmp_path, monkeypatch, login_config=fake_login_config(states=["pending"] * 5)
    )
    assert client.post("/v1/bilibili/login").status_code == 201
    assert login_status(client)["status"] == "pending"

    cancelled = client.post("/v1/bilibili/login/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json() == {"status": "cancelled", "detail": None}
    # Cancelling again is harmless and keeps the state.
    assert client.post("/v1/bilibili/login/cancel").json()["status"] == "cancelled"
    assert login_status(client)["status"] == "cancelled"
    assert not (secrets_dir / COOKIE_FILENAME).exists()
    assert client.get("/v1/health").json()["bilibili_login"] == "absent"


def test_failed_login_reports_a_safe_failure_and_writes_no_cookie(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    secrets_dir: Path,
) -> None:
    client = build_client(
        tmp_path,
        monkeypatch,
        login_config=fake_login_config(
            states=["failed"], failure="The fake Bilibili QR code expired"
        ),
    )
    assert client.post("/v1/bilibili/login").status_code == 201

    failed = login_status(client)
    assert failed["status"] == "failed"
    assert failed["detail"] == "The fake Bilibili QR code expired"
    assert not (secrets_dir / COOKIE_FILENAME).exists()
    assert client.get("/v1/health").json()["bilibili_login"] == "absent"


def test_health_reports_expired_for_a_stale_cookie_file(
    client: TestClient, secrets_dir: Path
) -> None:
    secrets_dir.mkdir(parents=True, exist_ok=True)
    stale = "t46-expired-sessdata-secret"
    (secrets_dir / COOKIE_FILENAME).write_text(
        "# Netscape HTTP Cookie File\n"
        f"#HttpOnly_.bilibili.com\tTRUE\t/\tTRUE\t1000\tSESSDATA\t{stale}\n",
        encoding="utf-8",
    )

    response = client.get("/v1/health")

    assert response.status_code == 200, response.text
    assert response.json()["bilibili_login"] == "expired"
    assert stale not in response.text


def test_confirmed_login_writes_a_cookie_file_yt_dlp_can_read(
    client: TestClient, secrets_dir: Path
) -> None:
    assert client.post("/v1/bilibili/login").status_code == 201
    assert login_status(client)["status"] == "pending"
    assert login_status(client)["status"] == "scanned"
    assert login_status(client)["status"] == "confirmed"

    path = secrets_dir / COOKIE_FILENAME
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line and not line.startswith("# ")]
    cookie_lines = [line for line in lines if not line.startswith("#")]
    assert cookie_lines, "the cookie file has no records"
    for line in cookie_lines:
        fields = line.split("\t")
        assert len(fields) == 7, line
        assert fields[0].endswith(".bilibili.com")
        assert fields[1] == "TRUE"
        assert fields[2] == "/"
        assert fields[3] == "TRUE"
        assert int(fields[4]) > 0

    # yt-dlp reads the same Netscape format through http.cookiejar.
    jar = MozillaCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)
    request = Request("https://www.bilibili.com/video/BV1Gt411c7mD")
    jar.add_cookie_header(request)
    header = request.get_header("Cookie") or ""
    for name, value in FAKE_SECRETS.items():
        assert f"{name}={value}" in header, name


def test_confirmed_login_records_tv_provenance_without_secret_values(
    client: TestClient, secrets_dir: Path
) -> None:
    from backend.services.bilibili_login.cookies import (
        LOGIN_SOURCE_FILENAME,
        LOGIN_SOURCE_TV,
    )

    assert client.post("/v1/bilibili/login").status_code == 201
    assert login_status(client)["status"] == "pending"
    assert login_status(client)["status"] == "scanned"
    assert login_status(client)["status"] == "confirmed"

    marker = secrets_dir / LOGIN_SOURCE_FILENAME
    assert marker.is_file()
    text = marker.read_text(encoding="utf-8")
    # The marker is a channel word only: no cookie name or value.
    assert text.strip() == LOGIN_SOURCE_TV
    for secret in FAKE_SECRETS.values():
        assert secret not in text


def test_no_response_log_or_database_contains_the_cookie(
    client: TestClient, tmp_path: Path, secrets_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    responses = [client.get("/v1/health"), client.post("/v1/bilibili/login")]
    for _ in range(3):
        responses.append(client.get("/v1/bilibili/login"))
    responses.append(client.get("/v1/health"))
    responses.append(client.get("/v1/errors"))
    responses.append(client.get("/v1/bilibili/login"))

    for response in responses:
        for secret in FAKE_SECRETS.values():
            assert secret not in response.text, "a response leaked a cookie value"

    for secret in FAKE_SECRETS.values():
        assert secret not in caplog.text, "a log record leaked a cookie value"

    database_files = [
        path
        for pattern in ("beebox.db", "beebox.db-wal", "beebox.db-shm")
        for path in tmp_path.glob(pattern)
    ]
    assert database_files, "the test database was not created"
    stored = b"".join(path.read_bytes() for path in database_files)
    for secret in FAKE_SECRETS.values():
        assert secret.encode() not in stored, "the database leaked a cookie value"

    # The only place the credential is allowed to exist is the secrets file.
    assert all(
        secret in (secrets_dir / COOKIE_FILENAME).read_text(encoding="utf-8")
        for secret in FAKE_SECRETS.values()
    )


def test_login_endpoints_require_the_bearer_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = build_client(tmp_path, monkeypatch, token=TOKEN)
    bare = TestClient(client.app)

    for method, path in (
        ("POST", "/v1/bilibili/login"),
        ("GET", "/v1/bilibili/login"),
        ("POST", "/v1/bilibili/login/cancel"),
    ):
        response = bare.request(method, path)
        assert response.status_code == 401, response.text
        assert response.json()["error_code"] == "AUTH_REQUIRED"

    # The authenticated console can use the same routes; health stays public
    # and still reports the login state without a token.
    assert client.post("/v1/bilibili/login").status_code == 201
    health = bare.get("/v1/health")
    assert health.status_code == 200
    assert health.json()["bilibili_login"] == "absent"


def test_job_that_needs_a_login_fails_with_video_login_required(
    client: TestClient, secrets_dir: Path
) -> None:
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / COOKIE_FILENAME).write_text(
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_.bilibili.com\tTRUE\t/\tTRUE\t1000\tSESSDATA\texpired-value\n",
        encoding="utf-8",
    )
    assert client.get("/v1/health").json()["bilibili_login"] == "expired"

    created = client.post(
        "/v1/jobs",
        json={
            "kind": "transcription",
            "source": {
                "type": "url",
                "url": "https://www.bilibili.com/video/BV1LoginOnly",
            },
        },
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]

    deadline = time.monotonic() + 20
    body: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/v1/jobs/{job_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.02)

    assert body["status"] == "failed", body
    assert body["error_code"] == "VIDEO_LOGIN_REQUIRED"
    assert "expired-value" not in str(body)


def test_credential_cookies_keep_only_the_canonical_names() -> None:
    """The real QR flow writes the names yt-dlp reads, not duplicate casings."""
    from types import SimpleNamespace

    from backend.services.bilibili_login.client import credential_cookies

    credential = SimpleNamespace(
        sessdata="sess",
        bili_jct="jct",
        dedeuserid="42",
        ac_time_value="ac",
        buvid3=None,
        buvid4="",
    )

    assert credential_cookies(credential) == {
        "SESSDATA": "sess",
        "bili_jct": "jct",
        "DedeUserID": "42",
        "ac_time_value": "ac",
    }

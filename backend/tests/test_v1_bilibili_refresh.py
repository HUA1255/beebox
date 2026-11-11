"""Daily Bilibili credential refresh (ticket #47, ADR-0012).

Every test drives a public seam:

* :class:`backend.services.bilibili_login.BilibiliRefreshService` with an
  injected clock and the fake Bilibili refresh client (the ticket's explicit
  test hook), so "a day passes" costs no wall-clock time;
* ``/v1/health`` for the operator-visible login state;
* the cookie file on disk, which is what yt-dlp actually reads;
* ``/v1/jobs`` for a video that needs the login.

The fake refresh values below are the secrets every test scans responses and
logs for; the real implementation never returns or logs them.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.services.bilibili_login import (
    BilibiliRefreshService,
    FakeBilibiliRefreshClient,
    cookie_file_path,
    login_status,
)
from backend.services.bilibili_login.cookies import (
    LOGIN_SOURCE_TV,
    LOGIN_SOURCE_WEB,
    login_source_path,
    write_login_source,
)

COOKIE_FILENAME = "bilibili-cookies.txt"
DAY_SECONDS = 24 * 60 * 60

STALE_SECRETS = {
    "SESSDATA": "t47-stale-sessdata-secret",
    "bili_jct": "t47-stale-bili-jct-secret",
    "DedeUserID": "10047",
    "ac_time_value": "t47-stale-ac-time-secret",
}
REFRESHED_SECRETS = {
    "SESSDATA": "t47-refreshed-sessdata-secret",
    "bili_jct": "t47-refreshed-bili-jct-secret",
    "DedeUserID": "10047",
    "ac_time_value": "t47-refreshed-ac-time-secret",
}


def seed_cookie_file(secrets_dir: Path, cookies: dict[str, str], *, expires_at: int) -> Path:
    """Write the Netscape cookie file yt-dlp reads, bypassing the service."""
    secrets_dir.mkdir(parents=True, exist_ok=True)
    lines = ["# Netscape HTTP Cookie File\n"]
    for name, value in cookies.items():
        domain = "#HttpOnly_.bilibili.com" if name == "SESSDATA" else ".bilibili.com"
        lines.append(f"{domain}\tTRUE\t/\tTRUE\t{expires_at}\t{name}\t{value}\n")
    path = secrets_dir / COOKIE_FILENAME
    path.write_text("".join(lines), encoding="utf-8")
    return path


def build_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    secrets_dir: Path | None = None,
    fake_refresh: dict | None = None,
) -> TestClient:
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BEEBOX_SECRETS_DIR", str(secrets_dir or tmp_path / "secrets"))
    monkeypatch.setenv("BEEBOX_MODEL_STORAGE_ROOT", str(tmp_path / "models-root"))
    monkeypatch.setenv("BEEBOX_BILIBILI_LOGIN", "fake")
    monkeypatch.setenv("BEEBOX_BILIBILI_REFRESH", "fake")
    monkeypatch.setenv(
        "BEEBOX_BILIBILI_REFRESH_FAKE",
        json.dumps(fake_refresh if fake_refresh is not None else {"outcome": "valid"}),
    )
    monkeypatch.setenv("BEEBOX_GIT_COMMIT", "0123456789abcdef0123456789abcdef01234567")
    monkeypatch.delenv("BEEBOX_API_TOKEN", raising=False)
    from backend.app import create_app

    return TestClient(create_app())


def test_checks_once_a_day_with_an_injected_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = tmp_path / "secrets"
    monkeypatch.setenv("BEEBOX_SECRETS_DIR", str(secrets))
    seed_cookie_file(secrets, STALE_SECRETS, expires_at=int(time.time()) + DAY_SECONDS)

    now = [1_700_000_000.0]
    client = FakeBilibiliRefreshClient(outcome="valid")
    service = BilibiliRefreshService(
        refresh_client_factory=lambda: client,
        clock=lambda: now[0],
        interval_seconds=DAY_SECONDS,
    )

    first = service.tick()
    assert first is not None and first.state == "valid"
    assert client.call_count == 1

    # Most of a day later nothing runs; after a full day the next check is due.
    now[0] += DAY_SECONDS - 60
    assert service.tick() is None
    assert client.call_count == 1

    now[0] += 61
    second = service.tick()
    assert second is not None and second.state == "valid"
    assert client.call_count == 2


def test_successful_refresh_updates_the_cookie_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = tmp_path / "secrets"
    monkeypatch.setenv("BEEBOX_SECRETS_DIR", str(secrets))
    seed_cookie_file(secrets, STALE_SECRETS, expires_at=int(time.time()) + 3600)

    client = FakeBilibiliRefreshClient(
        outcome="refreshed", cookies=dict(REFRESHED_SECRETS)
    )
    service = BilibiliRefreshService(
        refresh_client_factory=lambda: client,
        clock=lambda: 1_700_000_000.0,
        interval_seconds=DAY_SECONDS,
    )

    outcome = service.run_once()

    assert outcome.state == "refreshed"
    assert outcome.refreshed is True
    stored = cookie_file_path().read_text(encoding="utf-8")
    for name, value in REFRESHED_SECRETS.items():
        assert f"{name}\t{value}" in stored, name
    for name in ("SESSDATA", "bili_jct", "ac_time_value"):
        assert STALE_SECRETS[name] not in stored, name
    assert login_status().state == "valid"


def test_refresh_interval_is_configurable_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend import config

    monkeypatch.delenv("BEEBOX_BILIBILI_REFRESH_INTERVAL_SECONDS", raising=False)
    assert config.get_bilibili_refresh_interval_seconds() == DAY_SECONDS

    monkeypatch.setenv("BEEBOX_BILIBILI_REFRESH_INTERVAL_SECONDS", "120")
    assert config.get_bilibili_refresh_interval_seconds() == 120.0

    monkeypatch.setenv("BEEBOX_BILIBILI_REFRESH_INTERVAL_SECONDS", "not-a-number")
    assert config.get_bilibili_refresh_interval_seconds() == DAY_SECONDS


@pytest.mark.parametrize("outcome", ["failed", "expired"])
def test_failed_or_invalid_refresh_flips_health_to_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    secrets = tmp_path / "secrets"
    seed_cookie_file(secrets, STALE_SECRETS, expires_at=int(time.time()) + 30 * DAY_SECONDS)
    client = build_client(
        tmp_path,
        monkeypatch,
        secrets_dir=secrets,
        fake_refresh={"outcome": outcome, "failure": "The platform rejected the refresh"},
    )
    assert client.get("/v1/health").json()["bilibili_login"] == "valid"

    refresh = BilibiliRefreshService(
        refresh_client_factory=lambda: FakeBilibiliRefreshClient(outcome=outcome),
        clock=lambda: 1_700_000_000.0,
        interval_seconds=DAY_SECONDS,
    )
    result = refresh.run_once()

    assert result.state == outcome
    health = client.get("/v1/health")
    assert health.status_code == 200, health.text
    assert health.json()["bilibili_login"] == "expired"
    assert "The platform rejected the refresh" not in health.text


def _build_video_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, secrets_dir: Path
) -> TestClient:
    """A client whose Bilibili video has no platform subtitle track."""
    from support import fake_speech_env

    video_root = tmp_path / "video-fixtures"
    video_root.mkdir()
    (video_root / "videos.json").write_text(
        json.dumps(
            {
                "videos": [
                    {
                        "platform": "bilibili",
                        "id": "BV1RefreshGate",
                        "urls": ["https://www.bilibili.com/video/BV1RefreshGate"],
                        "title": "Login-gated refresh sample",
                        "duration_seconds": 2.0,
                        "audio_seconds": 2.0,
                        "tracks": [],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BEEBOX_VIDEO_PLATFORM", "fake")
    monkeypatch.setenv("BEEBOX_VIDEO_FAKE_ROOT", str(video_root))
    monkeypatch.setenv("BEEBOX_INLINE_LOCAL_TRANSCRIPTION", "1")
    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 2.0, "text": "降级后的语音识别结果。"}],
        text="降级后的语音识别结果。",
        duration=2.0,
        language="zh",
        aligned=True,
    )
    return build_client(tmp_path, monkeypatch, secrets_dir=secrets_dir)


def _create_and_wait(client: TestClient, url: str) -> dict:
    created = client.post(
        "/v1/jobs", json={"kind": "transcription", "source": {"type": "url", "url": url}}
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]
    deadline = time.monotonic() + 60
    body: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/v1/jobs/{job_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] in {"succeeded", "failed", "cancelled"}:
            return body
        time.sleep(0.02)
    pytest.fail(f"job {job_id} did not reach a terminal status: {body}")


def test_bilibili_video_without_login_fails_instead_of_using_anonymous_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0012: no login means no job, never a silent anonymous downgrade."""
    secrets = tmp_path / "secrets"
    client = _build_video_client(tmp_path, monkeypatch, secrets_dir=secrets)
    assert client.get("/v1/health").json()["bilibili_login"] == "absent"

    finished = _create_and_wait(
        client, "https://www.bilibili.com/video/BV1RefreshGate"
    )

    assert finished["status"] == "failed", finished
    assert finished["error_code"] == "VIDEO_LOGIN_REQUIRED", finished
    assert finished["retryable"] is True, finished
    assert "absent" in finished["error"], finished
    assert client.get(f"/v1/jobs/{finished['id']}/result").status_code == 409


def test_job_that_needs_login_fails_instead_of_degrading_to_asr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = tmp_path / "secrets"
    seed_cookie_file(secrets, STALE_SECRETS, expires_at=int(time.time()) + 30 * DAY_SECONDS)
    client = _build_video_client(tmp_path, monkeypatch, secrets_dir=secrets)
    assert client.get("/v1/health").json()["bilibili_login"] == "valid"

    # A failed daily refresh makes the stored login invalid.
    refresh = BilibiliRefreshService(
        refresh_client_factory=lambda: FakeBilibiliRefreshClient(outcome="failed"),
        clock=lambda: 1_700_000_000.0,
        interval_seconds=DAY_SECONDS,
    )
    assert refresh.run_once().state == "failed"
    assert client.get("/v1/health").json()["bilibili_login"] == "expired"

    finished = _create_and_wait(
        client, "https://www.bilibili.com/video/BV1RefreshGate"
    )

    assert finished["status"] == "failed", finished
    assert finished["error_code"] == "VIDEO_LOGIN_REQUIRED"
    # The speech-recognition downgrade would have "succeeded"; it must not run.
    assert client.get(f"/v1/jobs/{finished['id']}/result").status_code == 409


def test_login_gate_and_failure_classification_use_different_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent refuses the job up front; only expired explains a failed download."""
    from backend.services.bilibili_login import (
        login_expired,
        login_unavailable,
    )
    from backend.services.video_platform.models import (
        LOGIN_ABSENT,
        LOGIN_EXPIRED,
        LOGIN_VALID,
        LoginStatus,
    )

    assert login_unavailable(LoginStatus(state=LOGIN_ABSENT)) is True
    assert login_unavailable(LoginStatus(state=LOGIN_EXPIRED)) is True
    assert login_unavailable(LoginStatus(state=LOGIN_VALID)) is False

    assert login_expired(LoginStatus(state=LOGIN_EXPIRED)) is True
    assert login_expired(LoginStatus(state=LOGIN_ABSENT)) is False
    assert login_expired(LoginStatus(state=LOGIN_VALID)) is False


class ScriptedCredential:
    """Stand-in for ``bilibili_api.Credential`` (the library's own seam)."""

    instances: list["ScriptedCredential"] = []
    valid = True
    needs_refresh = False
    fail: str | None = None

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.refreshed = False
        self.check_refresh_calls = 0
        self.refresh_calls = 0
        self.sessdata = kwargs.get("sessdata")
        self.bili_jct = kwargs.get("bili_jct")
        self.dedeuserid = kwargs.get("dedeuserid")
        self.ac_time_value = kwargs.get("ac_time_value")
        self.__class__.instances.append(self)

    async def check_valid(self) -> bool:
        if self.fail:
            raise RuntimeError(self.fail)
        return self.valid

    async def check_refresh(self) -> bool:
        self.check_refresh_calls += 1
        return self.needs_refresh

    async def refresh(self) -> None:
        self.refresh_calls += 1
        self.refreshed = True
        self.sessdata = "api-refreshed-sessdata"
        self.bili_jct = "api-refreshed-bili-jct"
        self.ac_time_value = "api-refreshed-ac-time"

    def get_cookies(self) -> dict:
        return {
            "SESSDATA": self.sessdata,
            "bili_jct": self.bili_jct,
            "DedeUserID": self.dedeuserid,
            "ac_time_value": self.ac_time_value,
            "buvid3": "",
        }


def install_bilibili_api(monkeypatch: pytest.MonkeyPatch, valid: bool, needs_refresh: bool) -> None:
    class Credential(ScriptedCredential):
        pass

    Credential.valid = valid
    Credential.needs_refresh = needs_refresh
    module = types.ModuleType("bilibili_api")
    module.Credential = Credential
    monkeypatch.setitem(sys.modules, "bilibili_api", module)


def test_real_refresh_client_refreshes_only_when_the_platform_asks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.services.bilibili_login.refresh_api import BilibiliApiRefreshClient

    monkeypatch.setenv("BEEBOX_SECRETS_DIR", str(tmp_path / "secrets"))
    write_login_source(LOGIN_SOURCE_WEB)

    install_bilibili_api(monkeypatch, valid=True, needs_refresh=True)
    poll = BilibiliApiRefreshClient().refresh(dict(STALE_SECRETS))

    assert poll.status == "refreshed"
    credential = ScriptedCredential.instances[-1]
    assert credential.refreshed is True
    assert credential.kwargs["sessdata"] == STALE_SECRETS["SESSDATA"]
    assert credential.kwargs["dedeuserid"] == STALE_SECRETS["DedeUserID"]
    assert poll.cookies is not None
    assert poll.cookies["SESSDATA"] == "api-refreshed-sessdata"
    assert poll.cookies["bili_jct"] == "api-refreshed-bili-jct"
    assert "buvid3" not in poll.cookies

    install_bilibili_api(monkeypatch, valid=True, needs_refresh=False)
    poll = BilibiliApiRefreshClient().refresh(dict(STALE_SECRETS))
    assert poll.status == "valid"
    assert ScriptedCredential.instances[-1].refreshed is False


def test_real_refresh_client_reports_an_invalid_login_and_hides_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.services.bilibili_login.refresh_api import BilibiliApiRefreshClient

    monkeypatch.setenv("BEEBOX_SECRETS_DIR", str(tmp_path / "secrets"))
    write_login_source(LOGIN_SOURCE_WEB)

    install_bilibili_api(monkeypatch, valid=False, needs_refresh=False)
    assert BilibiliApiRefreshClient().refresh(dict(STALE_SECRETS)).status == "expired"

    class FailingCredential(ScriptedCredential):
        fail = "the response contained the credential"

    module = types.ModuleType("bilibili_api")
    module.Credential = FailingCredential
    monkeypatch.setitem(sys.modules, "bilibili_api", module)
    poll = BilibiliApiRefreshClient().refresh(dict(STALE_SECRETS))
    assert poll.status == "failed"
    assert "contained the credential" not in (poll.detail or "")


def test_real_refresh_client_never_web_refreshes_a_tv_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TV provenance permits validity detection only (ticket #68, ADR-0012)."""
    from backend.services.bilibili_login.refresh_api import BilibiliApiRefreshClient

    monkeypatch.setenv("BEEBOX_SECRETS_DIR", str(tmp_path / "secrets"))
    write_login_source(LOGIN_SOURCE_TV)

    # The platform would ask for a web refresh; a TV credential must not take it.
    install_bilibili_api(monkeypatch, valid=True, needs_refresh=True)
    poll = BilibiliApiRefreshClient().refresh(dict(STALE_SECRETS))

    assert poll.status == "valid"
    assert poll.cookies is None
    credential = ScriptedCredential.instances[-1]
    assert credential.check_refresh_calls == 0
    assert credential.refresh_calls == 0
    assert credential.refreshed is False

    # An unreadable/missing marker is the pre-provenance TV case: same result.
    login_source_path().unlink()
    poll = BilibiliApiRefreshClient().refresh(dict(STALE_SECRETS))
    assert poll.status == "valid"
    assert ScriptedCredential.instances[-1].check_refresh_calls == 0
    assert ScriptedCredential.instances[-1].refresh_calls == 0


def seed_tv_login(client: TestClient) -> Path:
    """Run the fake TV QR login and return the credential file it wrote."""
    assert client.post("/v1/bilibili/login").status_code == 201
    state = ""
    for _ in range(5):
        state = client.get("/v1/bilibili/login").json()["status"]
        if state == "confirmed":
            break
    assert state == "confirmed", state
    assert login_source_path().read_text(encoding="utf-8").strip() == LOGIN_SOURCE_TV
    return cookie_file_path()


def test_tv_credential_daily_cycle_keeps_it_valid_and_the_file_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.services.bilibili_login.refresh_api import BilibiliApiRefreshClient

    secrets = tmp_path / "secrets"
    client = build_client(tmp_path, monkeypatch, secrets_dir=secrets)
    path = seed_tv_login(client)
    before = path.read_text(encoding="utf-8")
    before_mtime = path.stat().st_mtime_ns
    assert client.get("/v1/health").json()["bilibili_login"] == "valid"

    install_bilibili_api(monkeypatch, valid=True, needs_refresh=True)
    now = [1_700_000_000.0]
    refresh = BilibiliRefreshService(
        refresh_client_factory=lambda: BilibiliApiRefreshClient(),
        clock=lambda: now[0],
        interval_seconds=DAY_SECONDS,
    )

    first = refresh.tick()
    assert first is not None and first.state == "valid"
    assert first.refreshed is False

    # The next day's pass still finds the login valid; nothing is rewritten.
    now[0] += DAY_SECONDS
    second = refresh.tick()
    assert second is not None and second.state == "valid"
    assert second.refreshed is False

    assert path.read_text(encoding="utf-8") == before
    assert path.stat().st_mtime_ns == before_mtime
    credential = ScriptedCredential.instances[-1]
    assert credential.check_refresh_calls == 0
    assert credential.refresh_calls == 0
    assert login_status().state == "valid"
    assert client.get("/v1/health").json()["bilibili_login"] == "valid"


def test_tv_credential_reported_invalid_expires_without_a_web_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.services.bilibili_login.refresh_api import BilibiliApiRefreshClient

    secrets = tmp_path / "secrets"
    client = build_client(tmp_path, monkeypatch, secrets_dir=secrets)
    path = seed_tv_login(client)
    before = path.read_text(encoding="utf-8")
    assert client.get("/v1/health").json()["bilibili_login"] == "valid"

    install_bilibili_api(monkeypatch, valid=False, needs_refresh=False)
    refresh = BilibiliRefreshService(
        refresh_client_factory=lambda: BilibiliApiRefreshClient(),
        clock=lambda: 1_700_000_000.0,
        interval_seconds=DAY_SECONDS,
    )

    outcome = refresh.run_once()

    assert outcome.state == "expired"
    credential = ScriptedCredential.instances[-1]
    assert credential.check_refresh_calls == 0
    assert credential.refresh_calls == 0
    # Expiry is recorded without touching the credential or the provenance.
    assert path.read_text(encoding="utf-8") == before
    assert login_source_path().read_text(encoding="utf-8").strip() == LOGIN_SOURCE_TV
    health = client.get("/v1/health")
    assert health.status_code == 200, health.text
    assert health.json()["bilibili_login"] == "expired"


def test_web_sourced_credential_still_takes_the_web_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provenance only gates the TV case; a web login keeps renewing."""
    from backend.services.bilibili_login.refresh_api import BilibiliApiRefreshClient

    secrets = tmp_path / "secrets"
    monkeypatch.setenv("BEEBOX_SECRETS_DIR", str(secrets))
    path = seed_cookie_file(
        secrets, STALE_SECRETS, expires_at=int(time.time()) + 30 * DAY_SECONDS
    )
    write_login_source(LOGIN_SOURCE_WEB)
    client = build_client(tmp_path, monkeypatch, secrets_dir=secrets)

    install_bilibili_api(monkeypatch, valid=True, needs_refresh=True)
    refresh = BilibiliRefreshService(
        refresh_client_factory=lambda: BilibiliApiRefreshClient(),
        clock=lambda: 1_700_000_000.0,
        interval_seconds=DAY_SECONDS,
    )

    outcome = refresh.run_once()

    assert outcome.state == "refreshed"
    assert outcome.refreshed is True
    credential = ScriptedCredential.instances[-1]
    assert credential.check_refresh_calls == 1
    assert credential.refresh_calls == 1
    stored = path.read_text(encoding="utf-8")
    assert "api-refreshed-sessdata" in stored
    assert STALE_SECRETS["SESSDATA"] not in stored
    assert login_source_path().read_text(encoding="utf-8").strip() == LOGIN_SOURCE_WEB
    assert client.get("/v1/health").json()["bilibili_login"] == "valid"


def test_refresh_never_logs_or_returns_the_cookie(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    secrets = tmp_path / "secrets"
    seed_cookie_file(secrets, STALE_SECRETS, expires_at=int(time.time()) + 30 * DAY_SECONDS)
    client = build_client(
        tmp_path,
        monkeypatch,
        secrets_dir=secrets,
        fake_refresh={"outcome": "refreshed", "cookies": dict(REFRESHED_SECRETS)},
    )
    refresh = BilibiliRefreshService(
        refresh_client_factory=lambda: FakeBilibiliRefreshClient(
            outcome="refreshed", cookies=dict(REFRESHED_SECRETS)
        ),
        clock=lambda: 1_700_000_000.0,
        interval_seconds=DAY_SECONDS,
    )

    assert refresh.run_once().state == "refreshed"

    for path in ("/v1/health", "/v1/bilibili/login", "/v1/errors"):
        response = client.get(path)
        for secret in REFRESHED_SECRETS.values():
            assert secret not in response.text, f"{path} leaked a cookie value"
    for secret in REFRESHED_SECRETS.values():
        assert secret not in caplog.text, "a log record leaked a cookie value"

    database_files = [
        path
        for pattern in ("beebox.db", "beebox.db-wal", "beebox.db-shm")
        for path in tmp_path.glob(pattern)
    ]
    for path in database_files:
        for secret in REFRESHED_SECRETS.values():
            assert secret.encode() not in path.read_bytes(), "the database leaked a cookie value"

    # The only place the refreshed credential may exist is the secrets file.
    stored = cookie_file_path().read_text(encoding="utf-8")
    assert all(secret in stored for secret in REFRESHED_SECRETS.values())


def test_app_lifespan_runs_the_daily_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = tmp_path / "secrets"
    seed_cookie_file(secrets, STALE_SECRETS, expires_at=int(time.time()) + 30 * DAY_SECONDS)
    build_client(
        tmp_path,
        monkeypatch,
        secrets_dir=secrets,
        fake_refresh={"outcome": "failed"},
    )
    monkeypatch.setenv("BEEBOX_BILIBILI_REFRESH_INTERVAL_SECONDS", "0.05")

    from backend.app import create_app

    with TestClient(create_app()) as started:
        deadline = time.monotonic() + 30
        state = started.get("/v1/health").json()["bilibili_login"]
        while state != "expired" and time.monotonic() < deadline:
            time.sleep(0.05)
            state = started.get("/v1/health").json()["bilibili_login"]
        assert state == "expired", "the app lifespan did not run the refresh service"

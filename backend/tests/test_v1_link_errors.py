"""End-to-end link-boundary behaviour through ``/v1`` (ticket #45).

Every case drives the public HTTP surface with the fake platform: it injects
each failure mode, scripts Bilibili multi-part metadata and records the proxy
it receives. No test touches the network, a GPU or the speech engine.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from support import fake_speech_env, seed_bilibili_login

TERMINAL = {"succeeded", "failed", "cancelled"}
DEFAULT_SUBTITLE = "1\n00:00:00,500 --> 00:00:02,000\n通用字幕。\n"
FIRST_PART_SUBTITLE = "1\n00:00:00,000 --> 00:00:01,000\n第一分 P 的内容。\n"
SECOND_PART_SUBTITLE = "1\n00:00:00,000 --> 00:00:02,000\n第二分 P 的内容。\n"
MULTI_PART_URL = "https://www.bilibili.com/video/BV1Multi45"
PROXY = "http://127.0.0.1:3128"
LINK_ERROR_CODES = {
    "INVALID_URL",
    "UNSUPPORTED_URL",
    "VIDEO_NOT_ACCESSIBLE",
    "VIDEO_LOGIN_REQUIRED",
    "DOWNLOAD_NETWORK_ERROR",
    "DOWNLOAD_TIMEOUT",
}


def fake_video(video_id: str, *, url: str | None = None, tracks: bool = True, **extra) -> dict:
    entry = {
        "platform": "bilibili",
        "id": video_id,
        "urls": [url or f"https://www.bilibili.com/video/{video_id}"],
        "title": f"Video {video_id}",
        "author": "BeeBox 测试频道",
        "duration_seconds": 2.0,
        "tracks": (
            [{"language": "zh-Hans", "kind": "manual", "format": "srt", "path": "manual.srt"}]
            if tracks
            else []
        ),
    }
    entry.update(extra)
    return entry


def configure_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    videos: list[dict],
    *,
    proxy: str | None = None,
    subtitle_files: dict[str, str] | None = None,
    asr: bool = False,
    bilibili_login: str = "valid",
) -> tuple[TestClient, Path]:
    root = tmp_path / "fake-platform"
    root.mkdir(parents=True, exist_ok=True)
    files = {"manual.srt": DEFAULT_SUBTITLE, **(subtitle_files or {})}
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8")
    config = {
        "tool_version": "fake-2026.10.26",
        "videos": videos,
        "record_calls": "calls.jsonl",
    }
    (root / "videos.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    secrets = tmp_path / "secrets"
    monkeypatch.setenv("BEEBOX_SECRETS_DIR", str(secrets))
    monkeypatch.setenv("BEEBOX_MODEL_STORAGE_ROOT", str(tmp_path / "models-root"))
    monkeypatch.delenv("BEEBOX_API_TOKEN", raising=False)
    monkeypatch.setenv("BEEBOX_VIDEO_PLATFORM", "fake")
    monkeypatch.setenv("BEEBOX_VIDEO_FAKE_ROOT", str(root))
    monkeypatch.setenv("BEEBOX_GIT_COMMIT", "0123456789abcdef0123456789abcdef01234567")
    monkeypatch.delenv("BEEBOX_DOWNLOAD_PROXY", raising=False)
    # Ticket #66: Bilibili links only proceed with a usable stored login. An
    # expired file and no file at all are the two states the gate refuses.
    if bilibili_login == "valid":
        seed_bilibili_login(secrets)
    elif bilibili_login == "expired":
        seed_bilibili_login(secrets, expires_at=1000)
    elif bilibili_login != "absent":  # pragma: no cover - test-helper misuse
        raise ValueError(f"Unsupported bilibili_login fixture state: {bilibili_login}")
    if proxy is not None:
        monkeypatch.setenv("BEEBOX_DOWNLOAD_PROXY", proxy)
    if asr:
        # The no-subtitle path downloads audio and then runs ASR inline.
        monkeypatch.setenv("BEEBOX_INLINE_LOCAL_TRANSCRIPTION", "1")
        fake_speech_env(
            monkeypatch,
            segments=[{"start": 0.0, "end": 1.0, "text": "语音识别得到的句子。"}],
            text="语音识别得到的句子。",
            duration=1.0,
            language="zh",
            aligned=True,
        )
    else:
        fake_speech_env(
            monkeypatch,
            fail={
                "stage": "any",
                "code": "ASR_SHOULD_NOT_RUN",
                "message": "ASR must not run for platform-subtitle jobs",
            },
        )
    from backend.app import create_app

    return TestClient(create_app()), root


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


def read_calls(root: Path) -> list[dict]:
    path = root / "calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def assert_link_rejected(client: TestClient, url: str, code: str) -> dict:
    """A link the boundary refuses is a 400 and never becomes a job."""
    before = client.get("/v1/jobs").json()["total"]
    rejected = create_url_job(client, url)

    assert rejected.status_code == 400, rejected.text
    body = rejected.json()
    assert body["error_code"] == code, body
    assert body["stage"] == "job", body
    assert body["detail"], body
    assert client.get("/v1/jobs").json()["total"] == before
    queue = client.get("/v1/queue").json()
    assert queue["length"] == 0
    assert queue["current_job_id"] is None
    return body


def assert_background_link_failure(client: TestClient, job_id: str, code: str) -> dict:
    finished = wait_for(client, job_id)

    assert finished["status"] == "failed", finished
    assert finished["error_code"] == code, finished
    assert finished["error"], finished
    assert client.get(f"/v1/jobs/{job_id}/result").status_code == 409
    queue = client.get("/v1/queue").json()
    assert queue["length"] == 0
    assert queue["current_job_id"] is None
    return finished


# ---------------------------------------------------------------------------
# Each failure class has its own public code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["not a video url", "/video/BV1Gt411c7mD", "ftp://x/video/1"])
def test_invalid_url_is_not_reported_as_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    client, _ = configure_client(tmp_path, monkeypatch, [fake_video("BV1Placeholder45")])

    body = assert_link_rejected(client, value, "INVALID_URL")
    assert value in body["detail"]


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/video/BV1Gt411c7mD",
        "https://vimeo.com/123456",
        "https://www.bilibili.com/video/",
    ],
)
def test_unsupported_urls_are_reported_as_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    client, _ = configure_client(tmp_path, monkeypatch, [fake_video("BV1Placeholder45")])

    assert_link_rejected(client, url, "UNSUPPORTED_URL")


def test_rejected_links_leave_the_task_list_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = configure_client(tmp_path, monkeypatch, [fake_video("BV1Placeholder45")])

    for url, code in (
        ("https://example.com/x", "UNSUPPORTED_URL"),
        ("not a url", "INVALID_URL"),
        ("https://vimeo.com/123456", "UNSUPPORTED_URL"),
    ):
        assert_link_rejected(client, url, code)

    jobs = client.get("/v1/jobs").json()
    assert jobs["total"] == 0
    assert jobs["items"] == []


@pytest.mark.parametrize("reason", ["private", "region", "paid", "removed"])
def test_inaccessible_videos_share_one_public_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    url = f"https://www.bilibili.com/video/BV1Gone{reason}"
    client, _ = configure_client(
        tmp_path,
        monkeypatch,
        [fake_video(f"BV1Gone{reason}", url=url, unavailable=reason)],
    )

    created = create_url_job(client, url)
    assert created.status_code == 201, created.text
    finished = assert_background_link_failure(
        client, created.json()["id"], "VIDEO_NOT_ACCESSIBLE"
    )
    assert reason in finished["error"]


def test_login_required_video_reports_video_login_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://www.bilibili.com/video/BV1Login45"
    client, _ = configure_client(
        tmp_path, monkeypatch, [fake_video("BV1Login45", url=url, login_required=True)]
    )

    created = create_url_job(client, url)
    assert created.status_code == 201, created.text
    assert_background_link_failure(client, created.json()["id"], "VIDEO_LOGIN_REQUIRED")


def test_network_failure_reports_download_network_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://www.bilibili.com/video/BV1Net45"
    client, _ = configure_client(
        tmp_path,
        monkeypatch,
        [
            fake_video(
                "BV1Net45",
                url=url,
                fail={"stage": "metadata", "error_code": "DOWNLOAD_NETWORK_ERROR"},
            )
        ],
    )

    created = create_url_job(client, url)
    assert created.status_code == 201, created.text
    assert_background_link_failure(client, created.json()["id"], "DOWNLOAD_NETWORK_ERROR")


def test_timeout_reports_download_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://www.bilibili.com/video/BV1Slow45"
    client, _ = configure_client(
        tmp_path,
        monkeypatch,
        [
            fake_video(
                "BV1Slow45",
                url=url,
                fail={"stage": "metadata", "error_code": "DOWNLOAD_TIMEOUT"},
            )
        ],
    )

    created = create_url_job(client, url)
    assert created.status_code == 201, created.text
    assert_background_link_failure(client, created.json()["id"], "DOWNLOAD_TIMEOUT")


def test_a_failure_during_the_subtitle_download_is_never_a_fake_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://www.bilibili.com/video/BV1Late45"
    client, _ = configure_client(
        tmp_path,
        monkeypatch,
        [
            fake_video(
                "BV1Late45",
                url=url,
                fail={"stage": "subtitle", "error_code": "DOWNLOAD_NETWORK_ERROR"},
            )
        ],
    )

    created = create_url_job(client, url)
    assert created.status_code == 201, created.text
    finished = assert_background_link_failure(
        client, created.json()["id"], "DOWNLOAD_NETWORK_ERROR"
    )
    # Metadata was fetched before the download failed; the job still fails.
    assert finished["source"]["title"] == "Video BV1Late45"
    assert finished["stage"] == "failed"


def test_an_unreachable_proxy_is_a_download_network_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://www.bilibili.com/video/BV1Proxy45"
    client, root = configure_client(
        tmp_path,
        monkeypatch,
        [
            fake_video(
                "BV1Proxy45",
                url=url,
                fail={"stage": "metadata", "error_code": "DOWNLOAD_NETWORK_ERROR"},
            )
        ],
        proxy="http://127.0.0.1:9",
    )

    created = create_url_job(client, url)
    assert created.status_code == 201, created.text
    assert_background_link_failure(client, created.json()["id"], "DOWNLOAD_NETWORK_ERROR")
    [metadata_call] = read_calls(root)
    assert metadata_call["proxy"] == "http://127.0.0.1:9"


# ---------------------------------------------------------------------------
# Playlists, collections and live streams are refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/playlist?list=PL123456",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123456",
        "https://www.bilibili.com/list/123456",
        "https://www.bilibili.com/medialist/play/123456",
        "https://space.bilibili.com/123456/channel/collectiondetail?sid=789",
        "https://live.bilibili.com/123456",
        "https://www.youtube.com/live/dQw4w9WgXcQ",
    ],
)
def test_playlist_collection_and_live_links_are_refused_without_downloading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    client, root = configure_client(tmp_path, monkeypatch, [fake_video("BV1Placeholder45")])

    assert_link_rejected(client, url, "UNSUPPORTED_URL")
    assert read_calls(root) == []


@pytest.mark.parametrize("flag", ["live", "playlist"])
def test_platform_reported_live_or_playlist_metadata_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    url = "https://www.bilibili.com/video/BV1Flag45"
    client, _ = configure_client(
        tmp_path,
        monkeypatch,
        [fake_video("BV1Flag45", url=url, **{flag: True})],
    )

    created = create_url_job(client, url)
    assert created.status_code == 201, created.text
    assert_background_link_failure(client, created.json()["id"], "UNSUPPORTED_URL")


# ---------------------------------------------------------------------------
# Bilibili multi-part selection
# ---------------------------------------------------------------------------


def multi_part_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, Path]:
    return configure_client(
        tmp_path,
        monkeypatch,
        [
            {
                "platform": "bilibili",
                "id": "BV1Multi45",
                "urls": [MULTI_PART_URL],
                "title": "多 P 示例视频",
                "author": "BeeBox 测试频道",
                "parts": [
                    {
                        "title": "第一集",
                        "duration_seconds": 1.0,
                        "tracks": [
                            {
                                "language": "zh-Hans",
                                "kind": "manual",
                                "format": "srt",
                                "path": "first.srt",
                            }
                        ],
                    },
                    {
                        "title": "第二集",
                        "duration_seconds": 2.0,
                        "tracks": [
                            {
                                "language": "zh-Hans",
                                "kind": "manual",
                                "format": "srt",
                                "path": "second.srt",
                            }
                        ],
                    },
                ],
            }
        ],
        subtitle_files={"first.srt": FIRST_PART_SUBTITLE, "second.srt": SECOND_PART_SUBTITLE},
    )


def test_multi_part_link_without_p_transcribes_only_the_first_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = multi_part_client(tmp_path, monkeypatch)

    created = create_url_job(client, MULTI_PART_URL)
    assert created.status_code == 201, created.text
    finished = wait_for(client, created.json()["id"])

    assert finished["status"] == "succeeded", finished
    source = finished["source"]
    assert source["part"] == 1
    assert source["part_title"] == "第一集"
    assert source["title"] == "多 P 示例视频"
    assert source["duration_ms"] == 1000

    result = client.get(f"/v1/jobs/{finished['id']}/result").json()
    assert "第一分 P 的内容。" in result["text"]
    assert "第二分 P 的内容。" not in result["text"]
    assert [(segment["start"], segment["end"]) for segment in result["segments"]] == [
        (0.0, 1.0)
    ]


def test_multi_part_link_transcribes_only_the_selected_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = multi_part_client(tmp_path, monkeypatch)

    created = create_url_job(client, f"{MULTI_PART_URL}?p=2&vd_source=abc")
    assert created.status_code == 201, created.text
    finished = wait_for(client, created.json()["id"])

    assert finished["status"] == "succeeded", finished
    source = finished["source"]
    assert source["url"] == f"{MULTI_PART_URL}?p=2&vd_source=abc"
    assert source["part"] == 2
    assert source["part_title"] == "第二集"
    assert source["duration_ms"] == 2000

    result = client.get(f"/v1/jobs/{finished['id']}/result").json()
    assert "第二分 P 的内容。" in result["text"]
    assert "第一分 P 的内容。" not in result["text"]
    srt = client.get(f"/v1/jobs/{finished['id']}/exports/srt")
    assert srt.status_code == 200, srt.text
    assert "00:00:00,000 --> 00:00:02,000" in srt.text
    assert "第二分 P 的内容。" in srt.text


def test_a_missing_part_is_not_accessible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = multi_part_client(tmp_path, monkeypatch)

    created = create_url_job(client, f"{MULTI_PART_URL}?p=3")
    assert created.status_code == 201, created.text
    assert_background_link_failure(
        client, created.json()["id"], "VIDEO_NOT_ACCESSIBLE"
    )


def test_an_invalid_part_number_is_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = multi_part_client(tmp_path, monkeypatch)

    assert_link_rejected(client, f"{MULTI_PART_URL}?p=abc", "UNSUPPORTED_URL")


# ---------------------------------------------------------------------------
# Download proxy
# ---------------------------------------------------------------------------


def test_the_configured_proxy_reaches_every_platform_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://www.bilibili.com/video/BV1ProxyOk45"
    client, root = configure_client(
        tmp_path,
        monkeypatch,
        [fake_video("BV1ProxyOk45", url=url)],
        proxy=PROXY,
    )

    created = create_url_job(client, url)
    assert created.status_code == 201, created.text
    finished = wait_for(client, created.json()["id"])
    assert finished["status"] == "succeeded", finished

    calls = read_calls(root)
    assert [call["stage"] for call in calls] == ["metadata", "subtitle"]
    assert all(call["proxy"] == PROXY for call in calls)
    # The proxy is configuration, never part of the public result.
    assert PROXY not in json.dumps(
        client.get(f"/v1/jobs/{finished['id']}/result").json(), ensure_ascii=False
    )


def test_the_proxy_also_reaches_the_audio_download_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://www.bilibili.com/video/BV1AudioProxy45"
    client, root = configure_client(
        tmp_path,
        monkeypatch,
        [
            fake_video(
                "BV1AudioProxy45",
                url=url,
                tracks=False,
                audio_seconds=1.0,
            )
        ],
        proxy=PROXY,
        asr=True,
    )

    created = create_url_job(client, url)
    assert created.status_code == 201, created.text
    finished = wait_for(client, created.json()["id"])

    assert finished["status"] == "succeeded", finished
    assert finished["source"]["text_source"] == "asr"
    calls = read_calls(root)
    assert [call["stage"] for call in calls] == ["metadata", "audio"]
    assert all(call["proxy"] == PROXY for call in calls)


def test_no_proxy_is_sent_when_none_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://www.bilibili.com/video/BV1NoProxy45"
    client, root = configure_client(
        tmp_path, monkeypatch, [fake_video("BV1NoProxy45", url=url)]
    )

    created = create_url_job(client, url)
    assert created.status_code == 201, created.text
    finished = wait_for(client, created.json()["id"])

    assert finished["status"] == "succeeded", finished
    calls = read_calls(root)
    assert calls and all(call["proxy"] is None for call in calls)


# ---------------------------------------------------------------------------
# Duplicate submissions and the public catalog
# ---------------------------------------------------------------------------


def test_submitting_the_same_link_twice_creates_two_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://www.bilibili.com/video/BV1Twice45"
    client, root = configure_client(
        tmp_path, monkeypatch, [fake_video("BV1Twice45", url=url)]
    )

    first = create_url_job(client, url)
    second = create_url_job(client, url)
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    first_id = first.json()["id"]
    second_id = second.json()["id"]
    assert first_id != second_id

    first_finished = wait_for(client, first_id)
    second_finished = wait_for(client, second_id)
    assert first_finished["status"] == "succeeded", first_finished
    assert second_finished["status"] == "succeeded", second_finished
    assert first_finished["source"]["url"] == second_finished["source"]["url"] == url
    assert client.get(f"/v1/jobs/{first_id}/result").status_code == 200
    assert client.get(f"/v1/jobs/{second_id}/result").status_code == 200

    succeeded = client.get("/v1/jobs", params={"status": "succeeded"}).json()
    assert succeeded["total"] == 2
    assert {item["id"] for item in succeeded["items"]} == {first_id, second_id}
    assert [call["stage"] for call in read_calls(root)].count("metadata") == 2


def test_every_link_error_code_is_in_the_public_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = configure_client(tmp_path, monkeypatch, [])

    response = client.get("/v1/errors")
    assert response.status_code == 200, response.text
    items = {item["code"]: item for item in response.json()["items"]}

    # Both link codes are rejected at creation time now, so they are HTTP 400s
    # rather than job-level failures; the other boundary codes still travel in
    # a failed job's error_code.
    http_statuses = {"INVALID_URL": 400, "UNSUPPORTED_URL": 400}
    for code in sorted(LINK_ERROR_CODES):
        assert code in items, f"{code} is missing from /v1/errors"
        assert items[code]["http_status"] == http_statuses.get(code), items[code]
        assert items[code]["meaning"].strip(), items[code]


# ---------------------------------------------------------------------------
# Ticket #66: a Bilibili link never degrades to anonymous access
# ---------------------------------------------------------------------------


def test_bilibili_link_fails_when_no_login_is_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://www.bilibili.com/video/BV1NoLogin66"
    client, root = configure_client(
        tmp_path,
        monkeypatch,
        [fake_video("BV1NoLogin66", url=url)],
        bilibili_login="absent",
    )

    created = create_url_job(client, url)
    assert created.status_code == 201, created.text
    finished = assert_background_link_failure(
        client, created.json()["id"], "VIDEO_LOGIN_REQUIRED"
    )
    assert finished["retryable"] is True, finished
    assert "absent" in finished["error"], finished
    # The gate runs before any platform request: nothing was fetched.
    assert read_calls(root) == []


def test_bilibili_link_fails_when_the_stored_login_is_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://www.bilibili.com/video/BV1Expired66"
    client, root = configure_client(
        tmp_path,
        monkeypatch,
        [fake_video("BV1Expired66", url=url)],
        bilibili_login="expired",
    )
    assert client.get("/v1/health").json()["bilibili_login"] == "expired"

    created = create_url_job(client, url)
    assert created.status_code == 201, created.text
    finished = assert_background_link_failure(
        client, created.json()["id"], "VIDEO_LOGIN_REQUIRED"
    )
    assert finished["retryable"] is True, finished
    assert "expired" in finished["error"], finished
    assert read_calls(root) == []


def test_prefer_asr_allows_a_bilibili_job_without_a_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://www.bilibili.com/video/BV1PreferAsr66"
    client, root = configure_client(
        tmp_path,
        monkeypatch,
        [fake_video("BV1PreferAsr66", url=url, audio_seconds=2.0)],
        asr=True,
        bilibili_login="absent",
    )

    created = client.post(
        "/v1/jobs",
        json={
            "kind": "transcription",
            "source": {"type": "url", "url": url},
            "options": {"prefer": "asr"},
        },
    )
    assert created.status_code == 201, created.text
    finished = wait_for(client, created.json()["id"])

    assert finished["status"] == "succeeded", finished
    assert finished["source"]["text_source"] == "asr"
    assert [call["stage"] for call in read_calls(root)] == ["metadata", "audio"]


def test_youtube_link_is_not_gated_by_the_bilibili_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://www.youtube.com/watch?v=NoBilibiliGate66"
    entry = {
        "platform": "youtube",
        "id": "NoBilibiliGate66",
        "urls": [url],
        "title": "YouTube link without a Bilibili login",
        "duration_seconds": 2.0,
        "tracks": [
            {"language": "en", "kind": "manual", "format": "srt", "path": "manual.srt"}
        ],
    }
    client, _ = configure_client(
        tmp_path, monkeypatch, [entry], bilibili_login="absent"
    )

    created = create_url_job(client, url)
    assert created.status_code == 201, created.text
    finished = wait_for(client, created.json()["id"])

    assert finished["status"] == "succeeded", finished
    assert finished["source"]["text_source"] == "platform_manual"


def test_retry_after_signing_in_completes_the_blocked_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://www.bilibili.com/video/BV1RetryLogin66"
    client, root = configure_client(
        tmp_path,
        monkeypatch,
        [fake_video("BV1RetryLogin66", url=url)],
        bilibili_login="absent",
    )

    created = create_url_job(client, url)
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]
    blocked = assert_background_link_failure(client, job_id, "VIDEO_LOGIN_REQUIRED")

    # Signing in stores a valid credential; the retryable failure now completes.
    seed_bilibili_login(root.parent / "secrets")
    retried = client.post(f"/v1/jobs/{blocked['id']}/retry")
    assert retried.status_code == 200, retried.text
    finished = wait_for(client, job_id)

    assert finished["status"] == "succeeded", finished
    assert finished["source"]["text_source"] == "platform_manual"

"""End-to-end tests for video-link jobs whose text comes from platform subtitles.

Ticket #42 adds the video platform boundary. Every test here drives the public
``/v1`` surface with the documented fake platform: local sample metadata and
subtitle files, no network, no GPU and no speech engine. The fake speech engine
is configured to fail if ASR is ever called, so a succeeded subtitle job proves
the text came from the platform.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from support import fake_speech_env, seed_bilibili_login

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "video_platform"
TERMINAL = {"succeeded", "failed", "cancelled"}

BILIBILI_URL = "https://www.bilibili.com/video/BV1Gt411c7mD"
B23_URL = "https://b23.tv/BeeBox42"
YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
YOUTU_BE_URL = "https://youtu.be/dQw4w9WgXcQ"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BEEBOX_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv("BEEBOX_MODEL_STORAGE_ROOT", str(tmp_path / "models-root"))
    monkeypatch.delenv("BEEBOX_API_TOKEN", raising=False)
    monkeypatch.setenv("BEEBOX_VIDEO_PLATFORM", "fake")
    monkeypatch.setenv("BEEBOX_VIDEO_FAKE_ROOT", str(FIXTURE_ROOT))
    monkeypatch.setenv("BEEBOX_GIT_COMMIT", "0123456789abcdef0123456789abcdef01234567")
    # Ticket #66: a Bilibili job needs a usable login before it may proceed.
    seed_bilibili_login(tmp_path / "secrets")
    # Any ASR call would fail the job: the subtitle path must not invoke it.
    fake_speech_env(
        monkeypatch,
        fail={
            "stage": "any",
            "code": "ASR_SHOULD_NOT_RUN",
            "message": "ASR must not run for platform-subtitle jobs",
        },
    )
    from backend.app import create_app

    return TestClient(create_app())


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


# ---------------------------------------------------------------------------
# Subtitle path
# ---------------------------------------------------------------------------


def test_bilibili_link_transcribes_from_manual_subtitles_without_the_gpu_queue(client: TestClient) -> None:
    # A paused GPU queue must not delay a subtitle job: nothing may enter it.
    assert client.post("/v1/queue/pause").status_code == 200

    created = create_url_job(client, BILIBILI_URL)
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]

    finished = wait_for(client, job_id)
    assert finished["status"] == "succeeded", finished
    assert finished["stage"] == "done"
    assert finished["progress"] == 1.0
    assert finished["error_code"] is None

    source = finished["source"]
    assert source["type"] == "url"
    assert source["url"] == BILIBILI_URL
    assert source["title"] == "示例视频：字幕路径"
    assert source["filename"] == "示例视频：字幕路径"
    assert source["author"] == "BeeBox 测试频道"
    assert source["duration_ms"] == 75250
    assert source["language"] == "zh-Hans"
    assert source["subtitle_language"] == "zh-Hans"
    assert source["text_source"] == "platform_manual"
    assert finished["options"]["diarization"] is False

    queue = client.get("/v1/queue").json()
    assert queue["paused"] is True
    assert queue["length"] == 0
    assert queue["current_job_id"] is None
    assert client.post("/v1/queue/resume").status_code == 200

    result = client.get(f"/v1/jobs/{job_id}/result")
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["source"] == source
    assert body["duration_ms"] == 75250
    assert "欢迎来到 BeeBox。" in body["text"]
    assert "自动字幕不应被选中。" not in body["text"]
    assert [segment["text"] for segment in body["segments"]] == [
        "欢迎来到 BeeBox。",
        "这是平台人工字幕。",
        "最后一句。",
    ]
    assert [(segment["start"], segment["end"]) for segment in body["segments"]] == [
        (1.0, 3.5),
        (4.0, 7.25),
        (62.5, 64.0),
    ]
    assert [paragraph["index"] for paragraph in body["paragraphs"]] == [1]
    # Subtitle jobs do not run diarization, so no speaker is ever assigned.
    assert [speaker["speaker"] for speaker in body["speakers"]] == [None]

    srt = client.get(f"/v1/jobs/{job_id}/exports/srt")
    assert srt.status_code == 200, srt.text
    assert srt.text == (
        "1\n"
        "00:00:01,000 --> 00:00:03,500\n"
        "欢迎来到 BeeBox。\n"
        "\n"
        "2\n"
        "00:00:04,000 --> 00:00:07,250\n"
        "这是平台人工字幕。\n"
        "\n"
        "3\n"
        "00:01:02,500 --> 00:01:04,000\n"
        "最后一句。\n"
    )
    assert quote("示例视频：字幕路径.srt") in srt.headers["content-disposition"]

    listed = client.get("/v1/jobs", params={"status": "succeeded"}).json()
    assert [item["id"] for item in listed["items"]] == [job_id]


@pytest.mark.parametrize(
    ("url", "expected_language", "expected_text_source"),
    [
        (B23_URL, "zh-Hans", "platform_manual"),
        (YOUTU_BE_URL, "en", "platform_auto"),
    ],
)
def test_short_links_are_recognized(
    client: TestClient, url: str, expected_language: str, expected_text_source: str
) -> None:
    created = create_url_job(client, url)
    assert created.status_code == 201, created.text
    assert created.json()["source"]["type"] == "url"

    finished = wait_for(client, created.json()["id"])

    assert finished["status"] == "succeeded", finished
    assert finished["source"]["url"] == url
    assert finished["source"]["language"] == expected_language
    assert finished["source"]["subtitle_language"] == expected_language
    assert finished["source"]["text_source"] == expected_text_source


def test_machine_translated_subtitles_are_never_selected(client: TestClient) -> None:
    created = create_url_job(client, YOUTUBE_URL)
    assert created.status_code == 201, created.text

    finished = wait_for(client, created.json()["id"])

    assert finished["status"] == "succeeded", finished
    source = finished["source"]
    assert source["title"] == "YouTube Subtitle Sample"
    assert source["author"] == "Sample Channel"
    assert source["duration_ms"] == 42500
    assert source["subtitle_language"] == "en"
    assert source["text_source"] == "platform_auto"

    result = client.get(f"/v1/jobs/{created.json()['id']}/result").json()
    assert "机器翻译不应被选中。" not in result["text"]
    assert "Welcome to the sample video." in result["text"]

    srt = client.get(f"/v1/jobs/{created.json()['id']}/exports/srt")
    assert srt.text.startswith(
        "1\n"
        "00:00:00,500 --> 00:00:02,000\n"
        "Welcome to the sample video.\n"
    )
    assert "00:00:40,000 --> 00:00:42,000" in srt.text


# ---------------------------------------------------------------------------
# Unsupported URLs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "error_code"),
    [
        # Ticket #45 splits malformed values out of UNSUPPORTED_URL.
        ("https://example.com/video/42", "UNSUPPORTED_URL"),
        ("not a video url", "INVALID_URL"),
    ],
)
def test_unusable_url_is_rejected_without_queue_work(
    client: TestClient, url: str, error_code: str
) -> None:
    rejected = create_url_job(client, url)

    assert rejected.status_code == 400, rejected.text
    body = rejected.json()
    assert body["error_code"] == error_code
    assert body["stage"] == "job"
    assert body["detail"]

    # Nothing was queued and no job resource was created for the refused link.
    queue = client.get("/v1/queue").json()
    assert queue["length"] == 0
    assert queue["current_job_id"] is None
    assert client.get("/v1/jobs").json()["total"] == 0


def test_url_source_requires_a_url(client: TestClient) -> None:
    response = client.post("/v1/jobs", json={"kind": "transcription", "source": {"type": "url"}})

    assert response.status_code == 400
    assert response.json()["error_code"] == "INVALID_SOURCE"
    assert response.json()["stage"] == "job"


# ---------------------------------------------------------------------------
# Health and events
# ---------------------------------------------------------------------------


def test_health_reports_the_downloader_version(client: TestClient) -> None:
    response = client.get("/v1/health")

    assert response.status_code == 200, response.text
    assert response.json()["yt_dlp_version"] == "fake-2026.10.26"


def test_video_job_publishes_the_same_task_events_as_file_jobs(client: TestClient) -> None:
    from backend.utils.events import event_bus

    events: list[tuple[str, dict]] = []
    ready = threading.Event()
    finished = threading.Event()
    failure: list[BaseException] = []

    def reader() -> None:
        async def consume() -> None:
            stream = event_bus.subscribe(heartbeat_seconds=0.05)
            try:
                async for event in stream:
                    ready.set()
                    if event["event"] in {"connected", "ping"}:
                        continue
                    payload = json.loads(event["data"]).get("payload") or {}
                    events.append((event["event"], payload))
                    if event["event"] in {"task.completed", "task.failed"}:
                        break
            finally:
                await stream.aclose()

        try:
            asyncio.run(asyncio.wait_for(consume(), 20))
        except BaseException as exc:  # pragma: no cover - surfaces in the assertion
            failure.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    assert ready.wait(10), "event subscriber did not connect"

    created = create_url_job(client, BILIBILI_URL)
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]
    assert finished.wait(20), "no terminal task event was published"
    thread.join(timeout=5)
    assert not failure, failure

    event_types = [event_type for event_type, payload in events if payload.get("id") == job_id]
    assert "task.updated" in event_types
    assert "task.completed" in event_types
    completed = next(payload for event_type, payload in events if event_type == "task.completed")
    assert completed["id"] == job_id
    assert completed["status"] == "completed"

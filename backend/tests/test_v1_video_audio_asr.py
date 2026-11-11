"""End-to-end tests for video-link jobs whose text comes from ASR (ticket #44).

A link without a usable platform subtitle track, or one submitted with
``options.prefer=asr``, must download only the audio stream, join the GPU
queue like a normal transcription, produce the transcript with the speech
engine, and drop the downloaded audio when the job succeeds. Every test drives
the public ``/v1`` surface with the fake video platform (local, generated
audio sample) and the fake speech engine: no network and no GPU.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from support import fake_speech_env, seed_bilibili_login

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "video_platform"
TERMINAL = {"succeeded", "failed", "cancelled"}

BILIBILI_URL = "https://www.bilibili.com/video/BV1Gt411c7mD"
NO_SUBTITLE_URL = "https://www.youtube.com/watch?v=NoSubtitle42"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BEEBOX_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv("BEEBOX_MODEL_STORAGE_ROOT", str(tmp_path / "models-root"))
    monkeypatch.delenv("BEEBOX_API_TOKEN", raising=False)
    monkeypatch.setenv("BEEBOX_VIDEO_PLATFORM", "fake")
    monkeypatch.setenv("BEEBOX_VIDEO_FAKE_ROOT", str(FIXTURE_ROOT))
    monkeypatch.setenv("BEEBOX_INLINE_LOCAL_TRANSCRIPTION", "1")
    monkeypatch.setenv("BEEBOX_GIT_COMMIT", "0123456789abcdef0123456789abcdef01234567")
    # Ticket #66: a Bilibili job needs a usable login before it may proceed.
    seed_bilibili_login(tmp_path / "secrets")
    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 2.0, "text": "语音识别得到的句子。"}],
        text="语音识别得到的句子。",
        duration=2.0,
        language="zh",
        aligned=True,
    )
    from backend.app import create_app

    return TestClient(create_app())


def create_url_job(client: TestClient, url: str, **options):
    body: dict = {"kind": "transcription", "source": {"type": "url", "url": url}}
    if options:
        body["options"] = options
    return client.post("/v1/jobs", json=body)


def wait_for(client: TestClient, job_id: str, *, timeout: float = 60.0) -> dict:
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


def wait_for_gpu_queue_entry(client: TestClient, *, timeout: float = 30.0) -> None:
    """Block until a job waits in the (paused) GPU queue."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        queue = client.get("/v1/queue").json()
        if queue["length"] == 1:
            return
        time.sleep(0.02)
    pytest.fail(f"no job entered the GPU queue: {client.get('/v1/queue').json()}")


def test_video_without_subtitles_downloads_audio_and_joins_the_gpu_queue(
    client: TestClient, tmp_path: Path
) -> None:
    # A paused queue proves the job takes the GPU path: it cannot complete
    # until the queue is resumed.
    assert client.post("/v1/queue/pause").status_code == 200

    created = create_url_job(client, NO_SUBTITLE_URL, language="zh")
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]

    wait_for_gpu_queue_entry(client)
    queued = client.get(f"/v1/jobs/{job_id}").json()
    assert queued["status"] == "queued", queued

    assert client.post("/v1/queue/resume").status_code == 200
    finished = wait_for(client, job_id)

    assert finished["status"] == "succeeded", finished
    assert finished["error_code"] is None
    source = finished["source"]
    assert source["type"] == "url"
    assert source["url"] == NO_SUBTITLE_URL
    assert source["title"] == "No Subtitles Video"
    assert source["filename"] == "No Subtitles Video"
    assert source["author"] == "Sample Channel"
    assert source["duration_ms"] == 12000
    assert source["language"] == "zh"
    assert source["subtitle_language"] is None
    assert source["text_source"] == "asr"

    result = client.get(f"/v1/jobs/{job_id}/result")
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["source"] == source
    assert body["text"] == "语音识别得到的句子。"
    assert [(segment["start"], segment["end"], segment["text"]) for segment in body["segments"]] == [
        (0.0, 2.0, "语音识别得到的句子。")
    ]

    # The downloaded temp audio and every derived file are gone (ADR-0011).
    uploads = tmp_path / "uploads"
    assert not uploads.exists() or list(uploads.iterdir()) == []
    derived = tmp_path / "derived_audio"
    assert not derived.exists() or list(derived.iterdir()) == []

    queue = client.get("/v1/queue").json()
    assert queue["length"] == 0
    assert queue["current_job_id"] is None


def test_prefer_asr_ignores_available_platform_subtitles(client: TestClient) -> None:
    assert client.post("/v1/queue/pause").status_code == 200

    created = create_url_job(client, BILIBILI_URL, prefer="asr")
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]

    wait_for_gpu_queue_entry(client)
    assert client.post("/v1/queue/resume").status_code == 200
    finished = wait_for(client, job_id)

    assert finished["status"] == "succeeded", finished
    source = finished["source"]
    # The video has a usable manual track, but prefer=asr must skip it.
    assert source["text_source"] == "asr"
    assert source["subtitle_language"] is None
    assert source["title"] == "示例视频：字幕路径"
    assert source["author"] == "BeeBox 测试频道"
    assert source["duration_ms"] == 75250
    assert source["url"] == BILIBILI_URL

    result = client.get(f"/v1/jobs/{job_id}/result").json()
    assert result["text"] == "语音识别得到的句子。"
    assert "欢迎来到 BeeBox。" not in result["text"]

"""HTTP contracts for Zhihu zvideo jobs (ticket #30).

A Zhihu zvideo page carries no usable platform subtitles, so the job must
always take the audio/ASR path even when the downloader reports subtitle
tracks, and a caller's ``subtitle_language`` must fail instead of silently
falling back. Answers, articles, columns and questions are refused at submit
with no job row. Every test drives ``/v1`` with the fake video platform and the
fake speech engine: no network and no GPU.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from support import fake_speech_env

TERMINAL = {"succeeded", "failed", "cancelled"}
ZVIDEO_ID = "1234567890123456789"
ZVIDEO_URL = f"https://www.zhihu.com/zvideo/{ZVIDEO_ID}"
ASR_TEXT = "语音识别得到的句子。"
SUBTITLE_TEXT = "平台字幕不应被使用。"


def configure_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tracks: bool = True,
    asr_succeeds: bool = True,
) -> tuple[TestClient, Path]:
    """A fake Zhihu video scripted with a subtitle track and an audio sample."""
    root = tmp_path / "fake-platform"
    root.mkdir(parents=True, exist_ok=True)
    (root / "manual.srt").write_text(
        f"1\n00:00:00,500 --> 00:00:02,000\n{SUBTITLE_TEXT}\n", encoding="utf-8"
    )
    entry = {
        "platform": "zhihu",
        "id": ZVIDEO_ID,
        "urls": [ZVIDEO_URL],
        "title": "知乎视频示例",
        "author": "BeeBox 测试频道",
        "duration_seconds": 2.0,
        "audio_seconds": 1.0,
        "tracks": (
            [{"language": "zh-Hans", "kind": "manual", "format": "srt", "path": "manual.srt"}]
            if tracks
            else []
        ),
    }
    config = {
        "tool_version": "fake-2026.10.26",
        "videos": [entry],
        "record_calls": "calls.jsonl",
    }
    (root / "videos.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BEEBOX_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv("BEEBOX_MODEL_STORAGE_ROOT", str(tmp_path / "models-root"))
    monkeypatch.delenv("BEEBOX_API_TOKEN", raising=False)
    monkeypatch.setenv("BEEBOX_VIDEO_PLATFORM", "fake")
    monkeypatch.setenv("BEEBOX_VIDEO_FAKE_ROOT", str(root))
    monkeypatch.setenv("BEEBOX_INLINE_LOCAL_TRANSCRIPTION", "1")
    monkeypatch.setenv("BEEBOX_GIT_COMMIT", "0123456789abcdef0123456789abcdef01234567")
    if asr_succeeds:
        fake_speech_env(
            monkeypatch,
            segments=[{"start": 0.0, "end": 1.0, "text": ASR_TEXT}],
            text=ASR_TEXT,
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
                "message": "ASR must not run for this job",
            },
        )
    from backend.app import create_app

    return TestClient(create_app()), root


def create_url_job(client: TestClient, url: str, **options):
    body: dict = {"kind": "transcription", "source": {"type": "url", "url": url}}
    if options:
        body["options"] = options
    return client.post("/v1/jobs", json=body)


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


# ---------------------------------------------------------------------------
# A zvideo job always transcribes the audio
# ---------------------------------------------------------------------------


def test_zvideo_with_reported_subtitle_tracks_still_uses_asr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, root = configure_client(tmp_path, monkeypatch)

    created = create_url_job(client, ZVIDEO_URL)
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]
    finished = wait_for(client, job_id)

    assert finished["status"] == "succeeded", finished
    source = finished["source"]
    assert source["platform"] == "zhihu"
    assert source["url"] == ZVIDEO_URL
    assert source["text_source"] == "asr"
    assert source["subtitle_language"] is None
    assert source["title"] == "知乎视频示例"
    assert source["author"] == "BeeBox 测试频道"
    assert source["duration_ms"] == 2000

    result = client.get(f"/v1/jobs/{job_id}/result").json()
    assert result["text"] == ASR_TEXT
    assert SUBTITLE_TEXT not in result["text"]
    # The platform reports a manual track, but the job only ever downloads the
    # audio stream.
    assert [call["stage"] for call in read_calls(root)] == ["metadata", "audio"]


def test_zvideo_job_without_reported_tracks_also_uses_asr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, root = configure_client(tmp_path, monkeypatch, tracks=False)

    created = create_url_job(client, ZVIDEO_URL)
    assert created.status_code == 201, created.text
    finished = wait_for(client, created.json()["id"])

    assert finished["status"] == "succeeded", finished
    assert finished["source"]["text_source"] == "asr"
    assert [call["stage"] for call in read_calls(root)] == ["metadata", "audio"]


def test_subtitle_language_on_a_zvideo_job_fails_instead_of_falling_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, root = configure_client(tmp_path, monkeypatch, asr_succeeds=False)

    created = create_url_job(client, ZVIDEO_URL, subtitle_language="zh-Hans")
    assert created.status_code == 201, created.text
    finished = wait_for(client, created.json()["id"])

    assert finished["status"] == "failed", finished
    assert finished["error_code"] == "VIDEO_SUBTITLE_UNAVAILABLE"
    assert finished["retryable"] is False
    assert finished["source"]["url"] == ZVIDEO_URL
    # The platform has no subtitles at all, so the request is refused before
    # any metadata or audio request (no silent fallback to ASR).
    assert read_calls(root) == []


# ---------------------------------------------------------------------------
# Only /zvideo/<id> pages are single videos
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.zhihu.com/question/12345678",
        "https://www.zhihu.com/question/12345678/answer/87654321",
        "https://www.zhihu.com/p/123456789",
        "https://zhuanlan.zhihu.com/p/123456789",
        "https://www.zhihu.com/column/c_123456789",
        "https://www.zhihu.com/video/1234567890123456789",
        "https://www.zhihu.com/",
    ],
)
def test_non_zvideo_zhihu_links_are_rejected_at_submit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    client, root = configure_client(tmp_path, monkeypatch, asr_succeeds=False)

    assert_link_rejected(client, url, "UNSUPPORTED_URL")
    assert read_calls(root) == []
    assert client.get("/v1/jobs").json()["total"] == 0

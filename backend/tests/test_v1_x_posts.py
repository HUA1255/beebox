"""X single-post jobs through ``/v1`` (ticket #28).

X publishes no usable platform subtitles, so an X post always takes the
audio/ASR path even when the downloader reports subtitle tracks. These cases
drive the fake video platform and the fake speech engine, so no test touches
the network or a GPU.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.services.video_platform import PLATFORM_X
from test_v1_link_errors import (
    assert_link_rejected,
    configure_client,
    create_url_job,
    read_calls,
    wait_for,
)

X_POST_URL = "https://x.com/BeeBoxTest/status/1234567890123456789"
X_POST_ID = "1234567890123456789"


def fake_x_post(*, url: str = X_POST_URL, **extra) -> dict:
    """An X post whose downloader metadata still offers a subtitle track."""
    entry = {
        "platform": PLATFORM_X,
        "id": X_POST_ID,
        "urls": [url],
        "title": "Spoken X post",
        "author": "BeeBoxTest",
        "duration_seconds": 1.5,
        "tracks": [
            {"language": "en", "kind": "manual", "format": "srt", "path": "manual.srt"}
        ],
        "audio_seconds": 1.5,
    }
    entry.update(extra)
    return entry


def test_x_post_uses_speech_recognition_even_when_tracks_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, root = configure_client(
        tmp_path,
        monkeypatch,
        [fake_x_post()],
        asr=True,
        # An X post needs no Bilibili login; the gate must not apply to it.
        bilibili_login="absent",
    )

    created = create_url_job(client, X_POST_URL)
    assert created.status_code == 201, created.text
    assert created.json()["source"]["platform"] == PLATFORM_X
    assert created.json()["source"]["url"] == X_POST_URL

    finished = wait_for(client, created.json()["id"])
    assert finished["status"] == "succeeded", finished
    source = finished["source"]
    assert source["url"] == X_POST_URL
    assert source["platform"] == PLATFORM_X
    assert source["text_source"] == "asr"
    assert source["subtitle_language"] is None
    assert source["title"] == "Spoken X post"

    # The reported track is never downloaded; only metadata and audio.
    assert [call["stage"] for call in read_calls(root)] == ["metadata", "audio"]

    result = client.get(f"/v1/jobs/{finished['id']}/result").json()
    assert "语音识别得到的句子。" in result["text"]


def test_twitter_com_link_keeps_the_submitted_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://twitter.com/BeeBoxTest/status/1234567890123456789?s=20"
    client, _ = configure_client(
        tmp_path, monkeypatch, [fake_x_post(url=url)], asr=True
    )

    created = create_url_job(client, url)
    assert created.status_code == 201, created.text
    finished = wait_for(client, created.json()["id"])

    assert finished["status"] == "succeeded", finished
    assert finished["source"]["url"] == url
    assert finished["source"]["platform"] == PLATFORM_X
    assert finished["source"]["text_source"] == "asr"


def test_x_post_with_a_requested_subtitle_language_fails_without_falling_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, root = configure_client(tmp_path, monkeypatch, [fake_x_post()], asr=True)

    created = client.post(
        "/v1/jobs",
        json={
            "kind": "transcription",
            "source": {"type": "url", "url": X_POST_URL},
            "options": {"subtitle_language": "en"},
        },
    )
    assert created.status_code == 201, created.text
    finished = wait_for(client, created.json()["id"])

    assert finished["status"] == "failed", finished
    assert finished["error_code"] == "VIDEO_SUBTITLE_UNAVAILABLE", finished
    # A caller-specified language cannot succeed on retry either.
    assert finished["retryable"] is False, finished
    assert finished["source"]["text_source"] != "platform_manual"
    # The capability is known offline: nothing was fetched or downloaded.
    assert read_calls(root) == []


def test_x_post_without_video_fails_as_non_retryable_video_not_accessible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ticket #39: a text-only or image-only post has no video to retry."""
    client, root = configure_client(
        tmp_path,
        monkeypatch,
        [fake_x_post(unavailable="no video")],
        asr=True,
    )

    created = create_url_job(client, X_POST_URL)
    assert created.status_code == 201, created.text
    finished = wait_for(client, created.json()["id"])

    assert finished["status"] == "failed", finished
    assert finished["error_code"] == "VIDEO_NOT_ACCESSIBLE", finished
    assert finished["retryable"] is False, finished
    assert "no video" in finished["error"], finished
    # The metadata fetch is the first request and already failed.
    assert [call["stage"] for call in read_calls(root)] == ["metadata"]


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/",
        "https://x.com/home",
        "https://x.com/search?q=BeeBox",
        "https://x.com/i/lists/123456",
        "https://x.com/BeeBoxTest",
        "https://x.com/BeeBoxTest/likes",
        "https://twitter.com/i/flow/login",
    ],
)
def test_a_non_post_x_link_is_refused_at_submit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    client, root = configure_client(tmp_path, monkeypatch, [fake_x_post()], asr=True)

    assert_link_rejected(client, url, "UNSUPPORTED_URL")
    assert read_calls(root) == []

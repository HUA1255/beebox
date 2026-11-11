"""Fake-platform contracts for ticket #45.

The fake platform is the test double for Bilibili/YouTube at the same seam
the job pipeline uses. These tests pin its scripted failure modes, proxy
recording and Bilibili multi-part selection, all without a network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.services.video_platform import (
    AudioDownloadError,
    DownloadNetworkError,
    DownloadOptions,
    DownloadTimeoutError,
    FakeVideoPlatform,
    InvalidUrlError,
    LoginRequiredError,
    SubtitleUnavailableError,
    UnsupportedUrlError,
    VideoUnavailableError,
    parse_video_url,
)
from support import make_wav_bytes

SUBTITLE = "1\n00:00:00,500 --> 00:00:01,500\nFake platform subtitle.\n"
VIDEO_URL = "https://www.bilibili.com/video/BV1Fake45"
PARTS_URL = "https://www.bilibili.com/video/BV1Parts45"
FAIL_URL = "https://www.bilibili.com/video/BV1Fail45"
LIVE_URL = "https://www.bilibili.com/video/BV1Live45"
PLAYLIST_URL = "https://www.bilibili.com/video/BV1List45"
UNAVAILABLE_URL = "https://www.bilibili.com/video/BV1Gone45"

_ERROR_TYPES = {
    "INVALID_URL": InvalidUrlError,
    "UNSUPPORTED_URL": UnsupportedUrlError,
    "VIDEO_NOT_ACCESSIBLE": VideoUnavailableError,
    "VIDEO_LOGIN_REQUIRED": LoginRequiredError,
    "DOWNLOAD_NETWORK_ERROR": DownloadNetworkError,
    "DOWNLOAD_TIMEOUT": DownloadTimeoutError,
    "VIDEO_SUBTITLE_UNAVAILABLE": SubtitleUnavailableError,
    "VIDEO_AUDIO_DOWNLOAD_FAILED": AudioDownloadError,
}


def write_fake_root(tmp_path: Path, videos: list[dict], **top_level) -> Path:
    """Write a fake platform root with a shared manual subtitle sample."""
    root = tmp_path / "fake-platform"
    root.mkdir(parents=True, exist_ok=True)
    (root / "subtitle.srt").write_text(SUBTITLE, encoding="utf-8")
    (root / "audio.wav").write_bytes(make_wav_bytes(0.2))
    config = {"tool_version": "fake-ticket-45", "videos": videos, **top_level}
    (root / "videos.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return root


def single_video(**extra) -> dict:
    payload = {
        "platform": "bilibili",
        "id": "BV1Fake45",
        "urls": [VIDEO_URL],
        "title": "Ticket 45 Video",
        "author": "BeeBox 测试频道",
        "duration_seconds": 12.5,
        "audio": "audio.wav",
        "tracks": [{"language": "zh-Hans", "kind": "manual", "format": "srt", "path": "subtitle.srt"}],
    }
    payload.update(extra)
    return payload


@pytest.mark.parametrize("code", sorted(_ERROR_TYPES))
def test_fake_platform_injects_each_failure_code_at_metadata(
    tmp_path: Path, code: str
) -> None:
    root = write_fake_root(
        tmp_path,
        [
            single_video(
                fail={"stage": "metadata", "error_code": code, "message": f"injected {code}"}
            )
        ],
    )
    platform = FakeVideoPlatform(root)
    ref = parse_video_url(VIDEO_URL)

    with pytest.raises(_ERROR_TYPES[code]) as error:
        platform.fetch_video_info(ref)

    assert error.value.code == code
    assert f"injected {code}" in error.value.message
    assert platform.calls[0]["stage"] == "metadata"
    assert platform.calls[0]["video_id"] == "BV1Fake45"


@pytest.mark.parametrize("code", ["DOWNLOAD_NETWORK_ERROR", "DOWNLOAD_TIMEOUT"])
def test_fake_platform_injects_failures_in_the_subtitle_download(
    tmp_path: Path, code: str
) -> None:
    root = write_fake_root(
        tmp_path,
        [single_video(fail={"stage": "subtitle", "error_code": code})],
    )
    platform = FakeVideoPlatform(root)
    ref = parse_video_url(VIDEO_URL)
    info = platform.fetch_video_info(ref)

    with pytest.raises(_ERROR_TYPES[code]) as error:
        platform.download_subtitle(info.tracks[0])

    assert error.value.code == code
    assert [call["stage"] for call in platform.calls] == ["metadata", "subtitle"]


def test_fake_platform_injects_audio_download_failures(tmp_path: Path) -> None:
    root = write_fake_root(
        tmp_path,
        [single_video(fail={"stage": "audio", "error_code": "VIDEO_AUDIO_DOWNLOAD_FAILED"})],
    )
    platform = FakeVideoPlatform(root)
    ref = parse_video_url(VIDEO_URL)

    with pytest.raises(AudioDownloadError) as error:
        platform.download_audio(ref, tmp_path / "audio.wav")

    assert error.value.code == "VIDEO_AUDIO_DOWNLOAD_FAILED"
    assert platform.calls[-1]["stage"] == "audio"


@pytest.mark.parametrize(
    ("url", "flag"),
    [(LIVE_URL, "live"), (PLAYLIST_URL, "playlist")],
)
def test_fake_platform_rejects_live_streams_and_playlists(
    tmp_path: Path, url: str, flag: str
) -> None:
    root = write_fake_root(
        tmp_path,
        [
            single_video(
                id=url.rsplit("/", 1)[-1],
                urls=[url],
                **{flag: True},
            )
        ],
    )
    platform = FakeVideoPlatform(root)

    with pytest.raises(UnsupportedUrlError) as error:
        platform.fetch_video_info(parse_video_url(url))

    assert error.value.code == "UNSUPPORTED_URL"


@pytest.mark.parametrize("reason", ["private", "region", "paid", "removed"])
def test_fake_platform_reports_unavailable_videos_with_one_code(
    tmp_path: Path, reason: str
) -> None:
    root = write_fake_root(
        tmp_path,
        [
            single_video(
                id="BV1Gone45",
                urls=[UNAVAILABLE_URL],
                unavailable=reason,
            )
        ],
    )
    platform = FakeVideoPlatform(root)

    with pytest.raises(VideoUnavailableError) as error:
        platform.fetch_video_info(parse_video_url(UNAVAILABLE_URL))

    assert error.value.code == "VIDEO_NOT_ACCESSIBLE"
    assert reason in error.value.message


def test_fake_platform_records_the_proxy_for_every_call(tmp_path: Path) -> None:
    root = write_fake_root(tmp_path, [single_video()], record_calls="calls.jsonl")
    platform = FakeVideoPlatform(root)
    ref = parse_video_url(VIDEO_URL)
    proxy = "http://127.0.0.1:3128"
    options = DownloadOptions(proxy=proxy)

    info = platform.fetch_video_info(ref, options=options)
    platform.download_subtitle(info.tracks[0], options=options)
    destination = platform.download_audio(ref, tmp_path / "audio.wav", options=options)

    assert destination.is_file()
    recorded = [
        json.loads(line)
        for line in (root / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [call["stage"] for call in recorded] == ["metadata", "subtitle", "audio"]
    assert all(call["proxy"] == proxy for call in recorded)
    assert platform.calls == recorded


def test_fake_platform_records_no_proxy_when_unconfigured(tmp_path: Path) -> None:
    root = write_fake_root(tmp_path, [single_video()], record_calls="calls.jsonl")
    platform = FakeVideoPlatform(root)

    platform.fetch_video_info(parse_video_url(VIDEO_URL))

    [call] = platform.calls
    assert call["proxy"] is None


def test_fake_platform_returns_only_the_requested_bilibili_part(tmp_path: Path) -> None:
    root = tmp_path / "fake-platform"
    root.mkdir()
    (root / "first.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n第一分 P。\n", encoding="utf-8"
    )
    (root / "second.srt").write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n第二分 P。\n", encoding="utf-8"
    )
    (root / "videos.json").write_text(
        json.dumps(
            {
                "videos": [
                    {
                        "platform": "bilibili",
                        "id": "BV1Parts45",
                        "urls": [PARTS_URL],
                        "title": "多 P 示例",
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
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    platform = FakeVideoPlatform(root)

    first = platform.fetch_video_info(parse_video_url(PARTS_URL))
    second = platform.fetch_video_info(parse_video_url(f"{PARTS_URL}?p=2"))

    assert first.title == "多 P 示例"
    assert first.part_title == "第一集"
    assert first.duration_ms == 1000
    assert first.tracks[0].path == "first.srt"
    assert second.part_title == "第二集"
    assert second.duration_ms == 2000
    assert second.tracks[0].path == "second.srt"

    with pytest.raises(VideoUnavailableError):
        platform.fetch_video_info(parse_video_url(f"{PARTS_URL}?p=3"))

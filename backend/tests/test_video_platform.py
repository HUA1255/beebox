"""Unit contracts of the video platform boundary itself (ticket #42).

These tests exercise the public seam the job pipeline uses (URL recognition,
track selection, subtitle parsing and the fake platform's downloads) without
an HTTP server. No network is used.
"""

from __future__ import annotations

import json
import shutil
import wave
from pathlib import Path

import pytest

from backend.services.video_platform import (
    FakeVideoPlatform,
    LoginRequiredError,
    UnsupportedUrlError,
    VideoRef,
    parse_subtitle,
    parse_video_url,
)
from support import make_wav_bytes

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "video_platform"


@pytest.mark.parametrize(
    ("url", "platform", "video_id"),
    [
        ("https://www.bilibili.com/video/BV1Gt411c7mD", "bilibili", "BV1Gt411c7mD"),
        ("https://m.bilibili.com/video/BV1Gt411c7mD?p=2", "bilibili", "BV1Gt411c7mD"),
        ("https://b23.tv/BeeBox42", "bilibili", "BeeBox42"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"),
        ("https://youtube.com/shorts/dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"),
    ],
)
def test_parse_video_url_recognizes_supported_single_video_links(
    url: str, platform: str, video_id: str
) -> None:
    ref = parse_video_url(url)

    assert ref.platform == platform
    assert ref.video_id == video_id
    assert ref.original_url == url
    assert ref.canonical_url.startswith("https://")


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/watch?v=1",
        "https://www.youtube.com/playlist?list=PL123",
        "https://www.youtube.com/@channel",
        "https://www.bilibili.com/",
        "not a url",
    ],
)
def test_parse_video_url_rejects_everything_else(url: str) -> None:
    with pytest.raises(Exception) as error:
        parse_video_url(url)
    assert error.value.code in {"INVALID_URL", "UNSUPPORTED_URL"}


def test_subtitle_parsers_convert_srt_vtt_and_json_cues() -> None:
    srt = "1\n00:00:01,250 --> 00:00:02,500\n第一句。\n"
    vtt = "WEBVTT\n\n00:00:03.000 --> 00:00:04.000\nSecond line.\n"
    bilibili = json.dumps({"body": [{"from": 5.5, "to": 7.0, "content": "第三句"}]})

    assert [(cue.start_ms, cue.end_ms, cue.text) for cue in parse_subtitle(srt, "srt")] == [
        (1250, 2500, "第一句。")
    ]
    assert [(cue.start_ms, cue.end_ms, cue.text) for cue in parse_subtitle(vtt, "vtt")] == [
        (3000, 4000, "Second line.")
    ]
    assert [(cue.start_ms, cue.end_ms, cue.text) for cue in parse_subtitle(bilibili, "json")] == [
        (5500, 7000, "第三句")
    ]


def test_fake_platform_downloads_audio_and_surfaces_login_requirement(tmp_path: Path) -> None:
    root = tmp_path / "samples"
    root.mkdir()
    shutil.copyfile(FIXTURE_ROOT / "bilibili-manual-zh.srt", root / "manual.srt")
    (root / "sample.wav").write_bytes(make_wav_bytes(0.2))
    (root / "videos.json").write_text(
        json.dumps(
            {
                "tool_version": "fake-test",
                "login": {"state": "not_required"},
                "videos": [
                    {
                        "platform": "bilibili",
                        "id": "BV1",
                        "urls": ["https://www.bilibili.com/video/BV1"],
                        "title": "Sample",
                        "author": "Author",
                        "duration_seconds": 1.0,
                        "audio": "sample.wav",
                        "tracks": [
                            {
                                "language": "zh-Hans",
                                "kind": "manual",
                                "format": "srt",
                                "path": "manual.srt",
                            }
                        ],
                    },
                    {
                        "platform": "bilibili",
                        "id": "BV2",
                        "urls": ["https://www.bilibili.com/video/BV2"],
                        "title": "Private",
                        "login_required": True,
                        "tracks": [],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    platform = FakeVideoPlatform(root)
    ref = parse_video_url("https://www.bilibili.com/video/BV1")
    info = platform.fetch_video_info(ref)

    destination = platform.download_audio(ref, tmp_path / "audio.wav")
    assert destination.read_bytes() == (root / "sample.wav").read_bytes()
    assert platform.check_login().state == "not_required"
    assert platform.tool_version() == "fake-test"
    assert [track.language for track in info.tracks] == ["zh-Hans"]

    private = parse_video_url("https://www.bilibili.com/video/BV2")
    with pytest.raises(LoginRequiredError):
        platform.fetch_video_info(private)

    with pytest.raises(UnsupportedUrlError):
        parse_video_url("https://vimeo.com/123")


def test_fake_platform_generates_a_local_audio_sample_without_network(tmp_path: Path) -> None:
    root = tmp_path / "generated"
    root.mkdir()
    url = "https://www.youtube.com/watch?v=gen1"
    (root / "videos.json").write_text(
        json.dumps(
            {
                "videos": [
                    {
                        "platform": "youtube",
                        "id": "gen1",
                        "urls": [url],
                        "title": "Generated audio",
                        "duration_seconds": 3.0,
                        "audio_seconds": 3.0,
                        "tracks": [],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    platform = FakeVideoPlatform(root)
    ref = parse_video_url(url)

    destination = platform.download_audio(ref, tmp_path / "download.wav")

    with wave.open(str(destination), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 16_000
        assert handle.getnframes() == 48_000


def test_yt_dlp_platform_sees_the_bilibili_cookie_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    from backend.services.bilibili_login import cookie_file_path, write_cookie_file
    from backend.services.video_platform.yt_dlp import YtDlpVideoPlatform

    monkeypatch.setenv("BEEBOX_SECRETS_DIR", str(tmp_path / "secrets"))
    platform = YtDlpVideoPlatform()

    assert platform.cookie_file() == cookie_file_path()
    assert platform.check_login().state == "absent"
    assert "cookiefile" not in platform.yt_dlp_options()

    write_cookie_file({"SESSDATA": "valid-secret"}, expires_at=int(time.time()) + 3600)
    assert platform.check_login().state == "valid"
    # The download path is configured with the same file yt-dlp reads.
    assert platform.yt_dlp_options()["cookiefile"] == str(cookie_file_path())

    write_cookie_file({"SESSDATA": "expired-secret"}, expires_at=int(time.time()) - 60)
    assert platform.check_login().state == "expired"


def test_yt_dlp_platform_classifies_failures_as_login_required_when_the_cookie_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    from backend.services.bilibili_login import write_cookie_file
    from backend.services.video_platform.errors import DownloadNetworkError, LoginRequiredError
    from backend.services.video_platform.yt_dlp import _classify_download_error

    monkeypatch.setenv("BEEBOX_SECRETS_DIR", str(tmp_path / "secrets"))
    write_cookie_file({"SESSDATA": "expired-secret"}, expires_at=int(time.time()) - 60)

    assert isinstance(
        _classify_download_error(RuntimeError("Something unexpected broke")),
        DownloadNetworkError,
    )
    expired = _classify_download_error(
        RuntimeError("Something unexpected broke"), login_expired=True
    )
    assert isinstance(expired, LoginRequiredError)
    assert expired.code == "VIDEO_LOGIN_REQUIRED"


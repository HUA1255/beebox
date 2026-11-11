"""URL boundary cases for video links (ticket #45).

Recognition is pure and offline: a link either yields a :class:`VideoRef`, is
a caller mistake (:class:`InvalidUrlError`) or is valid but not a supported
single video (:class:`UnsupportedUrlError`). Bilibili multi-part links keep the
selected part, and playlists, collections and live streams are rejected before
any download is attempted.
"""

from __future__ import annotations

import pytest

from backend.services.video_platform import (
    InvalidUrlError,
    UnsupportedUrlError,
    parse_video_url,
)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "not a url",
        "/video/BV1Gt411c7mD",
        "ftp://www.bilibili.com/video/BV1Gt411c7mD",
        "https:///video/BV1Gt411c7mD",
    ],
)
def test_values_that_are_not_absolute_http_urls_are_invalid(value: str) -> None:
    with pytest.raises(InvalidUrlError) as error:
        parse_video_url(value)
    assert error.value.code == "INVALID_URL"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/video/BV1Gt411c7mD",
        "https://www.bilibili.com/",
        "https://www.bilibili.com/list/123456",
        "https://www.bilibili.com/medialist/play/123456",
        "https://space.bilibili.com/123456/channel/collectiondetail?sid=789",
        "https://live.bilibili.com/123456",
        "https://www.youtube.com/playlist?list=PL123456",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123456",
        "https://www.youtube.com/live/dQw4w9WgXcQ",
        "https://www.youtube.com/@somechannel",
    ],
)
def test_playlists_collections_and_live_links_are_unsupported(url: str) -> None:
    with pytest.raises(UnsupportedUrlError) as error:
        parse_video_url(url)
    assert error.value.code == "UNSUPPORTED_URL"


def test_bilibili_multi_part_links_default_to_the_first_part() -> None:
    ref = parse_video_url("https://www.bilibili.com/video/BV1Gt411c7mD")

    assert ref.part == 1
    assert ref.video_id == "BV1Gt411c7mD"
    assert ref.canonical_url == "https://www.bilibili.com/video/BV1Gt411c7mD"


def test_bilibili_multi_part_links_keep_the_selected_part() -> None:
    ref = parse_video_url(
        "https://m.bilibili.com/video/BV1Gt411c7mD?p=2&vd_source=abcdef"
    )

    assert ref.part == 2
    assert ref.video_id == "BV1Gt411c7mD"
    assert ref.canonical_url == "https://www.bilibili.com/video/BV1Gt411c7mD?p=2"
    assert ref.original_url == "https://m.bilibili.com/video/BV1Gt411c7mD?p=2&vd_source=abcdef"


def test_bilibili_explicit_first_part_is_normalized_to_part_one() -> None:
    ref = parse_video_url("https://www.bilibili.com/video/BV1Gt411c7mD?p=1")

    assert ref.part == 1
    assert ref.canonical_url == "https://www.bilibili.com/video/BV1Gt411c7mD"


@pytest.mark.parametrize("value", ["0", "-1", "abc", ""])
def test_bilibili_part_must_be_a_positive_integer(value: str) -> None:
    with pytest.raises(UnsupportedUrlError):
        parse_video_url(f"https://www.bilibili.com/video/BV1Gt411c7mD?p={value}")


def test_short_and_youtube_links_have_no_bilibili_part() -> None:
    short = parse_video_url("https://b23.tv/BeeBox42")
    assert short.part == 1
    # The short link is resolved by yt-dlp; it must not be rewritten with the
    # short code treated as a BV id.
    assert short.canonical_url == "https://b23.tv/BeeBox42"
    assert parse_video_url("https://youtu.be/dQw4w9WgXcQ").part is None
    assert parse_video_url("https://www.youtube.com/shorts/dQw4w9WgXcQ").part is None

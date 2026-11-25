"""URL boundary cases for video links (ticket #45).

Recognition is pure and offline: a link either yields a :class:`VideoRef`, is
a caller mistake (:class:`InvalidUrlError`) or is valid but not a supported
single video (:class:`UnsupportedUrlError`). Bilibili multi-part links keep the
selected part, and playlists, collections and live streams are rejected before
any download is attempted.
"""

from __future__ import annotations

import re

import pytest

from backend.services.video_platform import (
    PLATFORM_BILIBILI,
    PLATFORM_X,
    PLATFORM_YOUTUBE,
    PLATFORM_ZHIHU,
    SUPPORTED_PLATFORMS,
    InvalidUrlError,
    UnsupportedUrlError,
    parse_video_url,
    platform_rule,
)
from backend.services.video_platform import platforms
from backend.services.video_platform.platforms import (
    HostRule,
    PlatformCapabilities,
    PlatformRule,
    UrlForm,
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


def test_short_link_part_rules_follow_the_video_form() -> None:
    with pytest.raises(UnsupportedUrlError):
        parse_video_url("https://b23.tv/BeeBox42?p=0")
    assert parse_video_url("https://b23.tv/BeeBox42?p=3").part == 3
    # Bangumi episodes are not multi-part videos; ?p= is ignored.
    assert parse_video_url("https://www.bilibili.com/bangumi/play/ep123456?p=2").part is None


def test_youtube_short_links_reject_playlists() -> None:
    with pytest.raises(UnsupportedUrlError) as error:
        parse_video_url("https://youtu.be/dQw4w9WgXcQ?list=PL123456")
    assert error.value.code == "UNSUPPORTED_URL"


@pytest.mark.parametrize(
    ("url", "platform", "video_id", "canonical"),
    [
        (
            "https://www.bilibili.com/bangumi/play/ep123456",
            PLATFORM_BILIBILI,
            "ep123456",
            "https://www.bilibili.com/bangumi/play/ep123456",
        ),
        (
            "https://youtu.be/dQw4w9WgXcQ",
            PLATFORM_YOUTUBE,
            "dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            PLATFORM_YOUTUBE,
            "dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://www.youtube.com/embed/dQw4w9WgXcQ",
            PLATFORM_YOUTUBE,
            "dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            PLATFORM_YOUTUBE,
            "dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://www.youtube.com/watch/?v=dQw4w9WgXcQ",
            PLATFORM_YOUTUBE,
            "dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
    ],
)
def test_each_link_form_maps_to_its_stable_canonical_url(
    url: str, platform: str, video_id: str, canonical: str
) -> None:
    ref = parse_video_url(url)

    assert ref.platform == platform
    assert ref.video_id == video_id
    assert ref.canonical_url == canonical
    assert ref.original_url == url


def test_platform_capabilities_describe_what_each_platform_offers() -> None:
    bilibili = platform_rule(PLATFORM_BILIBILI)
    youtube = platform_rule(PLATFORM_YOUTUBE)

    assert bilibili is not None
    assert bilibili.capabilities.has_platform_subtitles is True
    assert bilibili.capabilities.requires_login is True
    assert bilibili.capabilities.has_parts is True
    assert bilibili.capabilities.has_short_links is True
    assert youtube is not None
    assert youtube.capabilities.has_platform_subtitles is True
    assert youtube.capabilities.requires_login is False
    assert youtube.capabilities.has_parts is False
    assert youtube.capabilities.has_short_links is True
    assert platform_rule("example") is None


def test_supported_platforms_are_derived_from_the_rule_table() -> None:
    assert SUPPORTED_PLATFORMS == (
        PLATFORM_BILIBILI,
        PLATFORM_YOUTUBE,
        PLATFORM_X,
        PLATFORM_ZHIHU,
    )
    assert tuple(rule.platform for rule in platforms.PLATFORM_RULES) == SUPPORTED_PLATFORMS


# ---------------------------------------------------------------------------
# Zhihu zvideo links (ticket #30)
# ---------------------------------------------------------------------------


def test_zhihu_zvideo_links_map_to_their_canonical_url() -> None:
    ref = parse_video_url("https://www.zhihu.com/zvideo/1234567890123456789")

    assert ref.platform == PLATFORM_ZHIHU
    assert ref.video_id == "1234567890123456789"
    assert ref.canonical_url == "https://www.zhihu.com/zvideo/1234567890123456789"
    assert ref.original_url == "https://www.zhihu.com/zvideo/1234567890123456789"
    assert ref.part is None


@pytest.mark.parametrize(
    "url",
    [
        "https://zhihu.com/zvideo/1234567890123456789",
        "https://www.zhihu.com/zvideo/1234567890123456789/",
        "https://www.zhihu.com/zvideo/1234567890123456789?share_code=abc",
        "https://m.zhihu.com/zvideo/1234567890123456789",
    ],
)
def test_zhihu_host_variants_share_one_canonical_url(url: str) -> None:
    ref = parse_video_url(url)

    assert ref.platform == PLATFORM_ZHIHU
    assert ref.canonical_url == "https://www.zhihu.com/zvideo/1234567890123456789"
    # The submitted link is never rewritten; it is result metadata.
    assert ref.original_url == url


@pytest.mark.parametrize(
    "url",
    [
        "https://www.zhihu.com/question/12345678",
        "https://www.zhihu.com/question/12345678/answer/87654321",
        "https://www.zhihu.com/p/123456789",
        "https://zhuanlan.zhihu.com/p/123456789",
        "https://www.zhihu.com/column/c_123456789",
        "https://www.zhihu.com/zvideo/",
        "https://www.zhihu.com/zvideo/123abc",
        "https://www.zhihu.com/video/1234567890123456789",
        "https://www.zhihu.com/",
    ],
)
def test_zhihu_pages_that_are_not_a_single_zvideo_are_unsupported(url: str) -> None:
    with pytest.raises(UnsupportedUrlError) as error:
        parse_video_url(url)
    assert error.value.code == "UNSUPPORTED_URL"


def test_zhihu_has_no_platform_subtitles_and_needs_no_login() -> None:
    zhihu = platform_rule(PLATFORM_ZHIHU)

    assert zhihu is not None
    assert zhihu.capabilities.has_platform_subtitles is False
    assert zhihu.capabilities.requires_login is False
    assert zhihu.capabilities.has_parts is False
    assert zhihu.capabilities.has_short_links is False


def test_a_new_platform_needs_only_one_rule_table_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synthetic = PlatformRule(
        platform="example",
        capabilities=PlatformCapabilities(
            has_platform_subtitles=False,
            requires_login=False,
            has_parts=False,
            has_short_links=False,
        ),
        hosts=(
            HostRule(
                host="media.example",
                forms=(
                    UrlForm(
                        path=re.compile(r"^/watch/(?P<id>[A-Za-z0-9]+)"),
                        canonical="https://media.example/watch/{id}",
                    ),
                ),
                unsupported_message="The example link does not point to a single video",
            ),
        ),
    )
    monkeypatch.setattr(platforms, "PLATFORM_RULES", (*platforms.PLATFORM_RULES, synthetic))

    ref = parse_video_url("https://media.example/watch/abc123")

    assert ref.platform == "example"
    assert ref.video_id == "abc123"
    assert ref.canonical_url == "https://media.example/watch/abc123"
    with pytest.raises(UnsupportedUrlError):
        parse_video_url("https://media.example/profile/abc123")

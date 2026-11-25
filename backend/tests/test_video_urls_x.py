"""X single-post link recognition (tickets #28 and #29).

Recognition is pure and offline: only ``/<user>/status/<id>`` is a post, and
everything else on x.com/twitter.com (home, search, lists, profiles, non-status
paths and the site's own ``/i/...`` pages) is unsupported. A ``t.co`` short
link is accepted offline and marked so the job expands it at task start. X
posts offer no usable platform subtitles, which the rule table records as a
capability.
"""

from __future__ import annotations

import pytest

from backend.services.video_platform import (
    PLATFORM_X,
    UnsupportedUrlError,
    parse_video_url,
    platform_rule,
)

X_POST_URL = "https://x.com/BeeBoxTest/status/1234567890123456789"
X_POST_ID = "1234567890123456789"
TCO_URL = "https://t.co/BeeBoxFake29"


@pytest.mark.parametrize(
    ("url", "canonical"),
    [
        (X_POST_URL, X_POST_URL),
        (
            "https://www.x.com/BeeBoxTest/status/1234567890123456789?s=20",
            X_POST_URL,
        ),
        (
            "https://twitter.com/BeeBoxTest/status/1234567890123456789",
            X_POST_URL,
        ),
        (
            # Subdomains and the legacy host are normalized to the x.com post.
            "https://mobile.twitter.com/BeeBoxTest/status/1234567890123456789/?s=20&t=abc",
            X_POST_URL,
        ),
    ],
)
def test_a_single_x_post_link_is_recognized(url: str, canonical: str) -> None:
    ref = parse_video_url(url)

    assert ref.platform == PLATFORM_X
    assert ref.video_id == X_POST_ID
    assert ref.canonical_url == canonical
    assert ref.original_url == url
    assert ref.part is None


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/",
        "https://x.com/home",
        "https://x.com/search?q=BeeBox",
        "https://x.com/hashtag/BeeBox",
        "https://x.com/BeeBoxTest",
        "https://x.com/BeeBoxTest/likes",
        "https://x.com/BeeBoxTest/status/1234567890123456789/photo/1",
        "https://x.com/BeeBoxTest/status/not-a-number",
        "https://x.com/BeeBoxTest/status/",
        "https://x.com/i/lists/123456",
        "https://x.com/i/flow/login",
        "https://x.com/i/web/status/1234567890123456789",
        "https://twitter.com/search?q=BeeBox",
        "https://twitter.com/i/flow/signup",
    ],
)
def test_anything_that_is_not_a_single_post_is_unsupported(url: str) -> None:
    with pytest.raises(UnsupportedUrlError) as error:
        parse_video_url(url)

    assert error.value.code == "UNSUPPORTED_URL"


def test_x_capabilities_describe_a_single_post_without_platform_subtitles() -> None:
    rule = platform_rule(PLATFORM_X)

    assert rule is not None
    assert rule.capabilities.has_platform_subtitles is False
    assert rule.capabilities.requires_login is False
    assert rule.capabilities.has_parts is False
    # t.co short links are accepted offline and expanded by the job (ticket #29).
    assert rule.capabilities.has_short_links is True


# ---------------------------------------------------------------------------
# t.co short links (ticket #29)
# ---------------------------------------------------------------------------


def test_a_tco_short_link_is_an_x_short_link_without_a_known_id() -> None:
    ref = parse_video_url(TCO_URL)

    assert ref.platform == PLATFORM_X
    assert ref.short_link is True
    # The token is opaque; only expansion at task start can reveal the post.
    assert ref.video_id == "BeeBoxFake29"
    assert ref.canonical_url == TCO_URL
    assert ref.original_url == TCO_URL
    assert ref.part is None


@pytest.mark.parametrize(
    "url",
    [
        "https://t.co/",
        "https://t.co/BeeBoxFake29/extra",
    ],
)
def test_a_tco_url_that_is_not_a_short_link_is_unsupported(url: str) -> None:
    with pytest.raises(UnsupportedUrlError):
        parse_video_url(url)


def test_the_short_link_marker_is_only_set_for_tco() -> None:
    # A plain post is known offline, and b23.tv/youtu.be links are resolved by
    # the downloader, so neither is a short link the job must expand.
    assert parse_video_url(X_POST_URL).short_link is False
    assert parse_video_url("https://b23.tv/BV1Gt411c7mD").short_link is False
    assert parse_video_url("https://youtu.be/dQw4w9WgXcQ").short_link is False

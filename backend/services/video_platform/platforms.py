"""Declarative rules for every video platform BeeBox recognizes (ticket #27).

This module is the single place that knows which hosts belong to a platform,
which URL shapes are single videos, how each shape builds its canonical URL and
what the platform can do (:class:`PlatformCapabilities`).
:func:`backend.services.video_platform.urls.parse_video_url` only validates the
value, finds the rule for its host and applies the first matching form; it holds
no host-specific branches.

Adding a platform means adding one :class:`PlatformRule` entry here: its hosts,
their accepted forms and its capabilities. A future form whose shape a path
regex cannot express should add a field to :class:`UrlForm` or one small
extractor hook referenced from its rule, never a new branch in the parser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from backend.services.video_platform.models import (
    PLATFORM_BILIBILI,
    PLATFORM_X,
    PLATFORM_YOUTUBE,
)


@dataclass(frozen=True)
class PlatformCapabilities:
    """What a platform offers; later tickets branch on these flags."""

    has_platform_subtitles: bool
    requires_login: bool
    has_parts: bool
    has_short_links: bool


@dataclass(frozen=True)
class UrlForm:
    """One accepted single-video URL shape.

    ``path`` matches the URL path. A named ``id`` group carries the video id
    unless ``query_id`` names the query parameter that does (``watch?v=``).
    ``canonical`` is a format template with ``{id}`` and, when ``part_query``
    is set, ``{part}`` (empty for part 1, ``?p=N`` otherwise); any other named
    group the path pattern captures is available to the template too (an X
    post's ``{user}``). ``None`` keeps the submitted URL because the short
    link is resolved by the downloader.
    ``part_query`` names the query parameter that selects a multi-part index
    and ``part_error`` is the unsupported-link message for a bad value.
    """

    path: re.Pattern[str]
    canonical: str | None
    query_id: str | None = None
    part_query: str | None = None
    part_error: str = "Invalid part number: {value!r}"
    video_id_pattern: re.Pattern[str] | None = None


@dataclass(frozen=True)
class HostRule:
    """The forms a platform accepts on one host and its subdomains.

    ``reject_path`` refuses a host's non-video page shapes (a YouTube live
    link) before any form is tried, with ``reject_path_message``.
    ``unsupported_message`` is the fallback for a host that matched but no form.
    """

    host: str
    forms: tuple[UrlForm, ...]
    unsupported_message: str
    reject_path: re.Pattern[str] | None = None
    reject_path_message: str | None = None


@dataclass(frozen=True)
class PlatformRule:
    """Everything BeeBox knows about one platform.

    ``reject_query`` names query parameters that turn any link on the platform
    into a list download (a YouTube playlist ``list=``); presence is refused
    with ``reject_query_message``.
    """

    platform: str
    hosts: tuple[HostRule, ...]
    capabilities: PlatformCapabilities
    reject_query: tuple[str, ...] = ()
    reject_query_message: str | None = None


_PLAYLIST_MESSAGE = "Playlist links are not supported; submit a single video"

_SHORT_LINK_PATH = re.compile(r"^/+(?P<id>[^/]+)")
_BILIBILI_VIDEO_PATH = re.compile(r"^/video/(?P<id>[A-Za-z0-9]+)")
_BILIBILI_BANGUMI_PATH = re.compile(r"^/bangumi/play/(?P<id>[A-Za-z0-9]+)")
_BILIBILI_PART_ERROR = "Invalid Bilibili part number: {value!r}"

_YOUTUBE_VIDEO_PATH = re.compile(r"^/(?:shorts|embed)/(?P<id>[A-Za-z0-9_-]+)")
_YOUTUBE_ANY_PATH = re.compile(r".*")
_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_YOUTUBE_LIVE_PATH = re.compile(r"^/live(?:/|$)")

# A single post is /<user>/status/<numeric id>; nothing may follow the id
# except a trailing slash, and the query (tracking parameters) is ignored
# because it never selects different content.
_X_POST_PATH = re.compile(r"^/(?P<user>[A-Za-z0-9_]+)/status/(?P<id>[0-9]+)/?$")
# /i/... holds the site's own pages (login flow, lists, signed-in web
# status links); none of them is a post to transcribe.
_X_INTERNAL_PATH = re.compile(r"^/i(?:/|$)")
_X_UNSUPPORTED_MESSAGE = "The X link does not point to a single post"


PLATFORM_RULES: tuple[PlatformRule, ...] = (
    PlatformRule(
        platform=PLATFORM_BILIBILI,
        capabilities=PlatformCapabilities(
            has_platform_subtitles=True,
            # ADR-0012: no job proceeds without a usable stored login.
            requires_login=True,
            has_parts=True,
            has_short_links=True,
        ),
        hosts=(
            HostRule(
                host="b23.tv",
                forms=(
                    UrlForm(
                        path=_SHORT_LINK_PATH,
                        # yt-dlp resolves the short code; keep it verbatim.
                        canonical=None,
                        part_query="p",
                        part_error=_BILIBILI_PART_ERROR,
                    ),
                ),
                unsupported_message="The b23.tv link does not point to a video",
            ),
            HostRule(
                host="bilibili.com",
                forms=(
                    UrlForm(
                        path=_BILIBILI_VIDEO_PATH,
                        canonical="https://www.bilibili.com/video/{id}{part}",
                        part_query="p",
                        part_error=_BILIBILI_PART_ERROR,
                    ),
                    UrlForm(
                        # Bangumi episodes have no multi-part index.
                        path=_BILIBILI_BANGUMI_PATH,
                        canonical="https://www.bilibili.com/bangumi/play/{id}",
                    ),
                ),
                unsupported_message="The Bilibili link does not point to a single video",
            ),
        ),
    ),
    PlatformRule(
        platform=PLATFORM_YOUTUBE,
        capabilities=PlatformCapabilities(
            has_platform_subtitles=True,
            requires_login=False,
            has_parts=False,
            has_short_links=True,
        ),
        reject_query=("list",),
        reject_query_message=_PLAYLIST_MESSAGE,
        hosts=(
            HostRule(
                host="youtu.be",
                forms=(
                    UrlForm(
                        path=_SHORT_LINK_PATH,
                        canonical="https://www.youtube.com/watch?v={id}",
                        video_id_pattern=_YOUTUBE_ID,
                    ),
                ),
                unsupported_message="The youtu.be link does not point to a video",
            ),
            HostRule(
                host="youtube.com",
                forms=(
                    UrlForm(
                        path=_YOUTUBE_VIDEO_PATH,
                        canonical="https://www.youtube.com/watch?v={id}",
                        video_id_pattern=_YOUTUBE_ID,
                    ),
                    UrlForm(
                        # watch?v=... (and any page carrying a v= id).
                        path=_YOUTUBE_ANY_PATH,
                        canonical="https://www.youtube.com/watch?v={id}",
                        query_id="v",
                        video_id_pattern=_YOUTUBE_ID,
                    ),
                ),
                unsupported_message="The YouTube link does not point to a single video",
                # A /live link is always a stream, even with a video id.
                reject_path=_YOUTUBE_LIVE_PATH,
                reject_path_message="Live streams are not supported",
            ),
        ),
    ),
    PlatformRule(
        platform=PLATFORM_X,
        capabilities=PlatformCapabilities(
            # X reports caption tracks, but they are not usable platform
            # subtitles (ticket #28): every X job is transcribed from audio.
            has_platform_subtitles=False,
            # Public posts need no cookie; age-restricted posts are refused.
            requires_login=False,
            has_parts=False,
            # t.co short links are ticket #29, not part of this rule.
            has_short_links=False,
        ),
        hosts=tuple(
            HostRule(
                host=host,
                forms=(
                    UrlForm(
                        path=_X_POST_PATH,
                        # Normalize both hosts (and their subdomains) to the
                        # x.com post; the submitted query never selects content.
                        canonical="https://x.com/{user}/status/{id}",
                    ),
                ),
                unsupported_message=_X_UNSUPPORTED_MESSAGE,
                reject_path=_X_INTERNAL_PATH,
                reject_path_message="X internal pages are not posts",
            )
            for host in ("x.com", "twitter.com")
        ),
    ),
)

SUPPORTED_PLATFORMS: tuple[str, ...] = tuple(rule.platform for rule in PLATFORM_RULES)


def platform_rule(platform: str) -> PlatformRule | None:
    """The rule for ``platform``, or ``None`` when it is not in the table."""
    for rule in PLATFORM_RULES:
        if rule.platform == platform:
            return rule
    return None


__all__ = [
    "PLATFORM_RULES",
    "SUPPORTED_PLATFORMS",
    "HostRule",
    "PlatformCapabilities",
    "PlatformRule",
    "UrlForm",
    "platform_rule",
]

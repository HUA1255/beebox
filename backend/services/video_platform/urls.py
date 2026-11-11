"""Recognize the video links BeeBox accepts (tickets #42 and #45).

Only single videos on Bilibili and YouTube are supported. Recognition is pure
and offline: the caller gets a :class:`VideoRef` or a typed
:class:`UnsupportedUrlError`/``InvalidUrlError`` without any network request.
Playlists, collections and live streams are refused here, before any
download; Bilibili multi-part links keep only the requested part (``?p=N``,
part 1 when absent) so a job never turns into a list download.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from backend.services.video_platform.errors import InvalidUrlError, UnsupportedUrlError
from backend.services.video_platform.models import (
    PLATFORM_BILIBILI,
    PLATFORM_YOUTUBE,
    SUPPORTED_PLATFORMS,
    VideoRef,
)

_BILIBILI_VIDEO_PATH = re.compile(r"^/video/(?P<id>[A-Za-z0-9]+)")
_BILIBILI_BANGUMI_PATH = re.compile(r"^/bangumi/play/(?P<id>[A-Za-z0-9]+)")
_YOUTUBE_VIDEO_PATH = re.compile(r"^/(?:shorts|embed)/(?P<id>[A-Za-z0-9_-]+)")
_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def _hostname(parts) -> str:
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise InvalidUrlError("The URL has no host")
    return host


def _query(parts) -> dict[str, list[str]]:
    return parse_qs(parts.query or "", keep_blank_values=True)


def _youtube_id(parts) -> str | None:
    path = parts.path or ""
    if path == "/live" or path.startswith("/live/"):
        # A /live link is always a stream, even when it carries a video id.
        raise UnsupportedUrlError("Live streams are not supported")
    match = _YOUTUBE_VIDEO_PATH.match(path)
    if match:
        return match.group("id")
    for value in _query(parts).get("v", []):
        if value:
            return value
    return None


def _reject_playlist(parts) -> None:
    if "list" in _query(parts):
        raise UnsupportedUrlError("Playlist links are not supported; submit a single video")


def _bilibili_part(parts) -> int:
    """The requested part index; absent or ``p=1`` means the first part."""
    values = _query(parts).get("p")
    if not values:
        return 1
    raw = (values[0] or "").strip()
    if not raw.isdigit() or int(raw) < 1:
        raise UnsupportedUrlError(f"Invalid Bilibili part number: {raw!r}")
    return int(raw)


def _bilibili_ref(
    url: str, *, video_id: str, part: int, canonical: str | None = None
) -> VideoRef:
    if canonical is None:
        canonical = f"https://www.bilibili.com/video/{video_id}"
        if part > 1:
            canonical = f"{canonical}?p={part}"
    return VideoRef(
        platform=PLATFORM_BILIBILI,
        video_id=video_id,
        canonical_url=canonical,
        original_url=url,
        part=part,
    )


def parse_video_url(url: str) -> VideoRef:
    """Return the platform reference for a supported single-video URL.

    Raises :class:`InvalidUrlError` when the value is not an absolute http(s)
    URL and :class:`UnsupportedUrlError` for every other host or non-video
    page (playlists, collections, live streams, channels). The original string
    is kept verbatim for result metadata.
    """
    original = (url or "").strip()
    try:
        parts = urlsplit(original)
    except ValueError as exc:
        raise InvalidUrlError(f"Not a valid URL: {url!r}") from exc
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise InvalidUrlError(f"Not an absolute http(s) URL: {url!r}")
    host = _hostname(parts)
    path = parts.path or "/"

    if host == "b23.tv" or host.endswith(".b23.tv"):
        video_id = path.strip("/").split("/", 1)[0]
        if not video_id:
            raise UnsupportedUrlError("The b23.tv link does not point to a video")
        return _bilibili_ref(
            original,
            video_id=video_id,
            part=_bilibili_part(parts),
            canonical=original,
        )

    if host == "bilibili.com" or host.endswith(".bilibili.com"):
        video_match = _BILIBILI_VIDEO_PATH.match(path)
        if video_match:
            return _bilibili_ref(
                original,
                video_id=video_match.group("id"),
                part=_bilibili_part(parts),
            )
        bangumi_match = _BILIBILI_BANGUMI_PATH.match(path)
        if bangumi_match:
            return VideoRef(
                platform=PLATFORM_BILIBILI,
                video_id=bangumi_match.group("id"),
                canonical_url=(
                    f"https://www.bilibili.com/bangumi/play/{bangumi_match.group('id')}"
                ),
                original_url=original,
            )
        raise UnsupportedUrlError("The Bilibili link does not point to a single video")

    if host == "youtu.be" or host.endswith(".youtu.be"):
        _reject_playlist(parts)
        video_id = path.strip("/").split("/", 1)[0]
        if not video_id or not _YOUTUBE_ID.match(video_id):
            raise UnsupportedUrlError("The youtu.be link does not point to a video")
        return VideoRef(
            platform=PLATFORM_YOUTUBE,
            video_id=video_id,
            canonical_url=f"https://www.youtube.com/watch?v={video_id}",
            original_url=original,
        )

    if host == "youtube.com" or host.endswith(".youtube.com"):
        _reject_playlist(parts)
        video_id = _youtube_id(parts)
        if not video_id or not _YOUTUBE_ID.match(video_id):
            raise UnsupportedUrlError("The YouTube link does not point to a single video")
        return VideoRef(
            platform=PLATFORM_YOUTUBE,
            video_id=video_id,
            canonical_url=f"https://www.youtube.com/watch?v={video_id}",
            original_url=original,
        )

    raise UnsupportedUrlError(f"Unsupported video platform: {host}")


__all__ = [
    "PLATFORM_BILIBILI",
    "PLATFORM_YOUTUBE",
    "SUPPORTED_PLATFORMS",
    "VideoRef",
    "parse_video_url",
]

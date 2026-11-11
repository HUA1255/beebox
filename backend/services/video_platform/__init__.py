"""The video platform boundary (ticket #42).

Everything that talks to Bilibili or YouTube goes through this package:
recognizing a link, fetching video metadata and the subtitle track list,
downloading one subtitle track, downloading only the audio stream (ticket
#44) and checking login state (tickets #46/#47).

Two implementations ship:

``fake``
    Reads local sample metadata and subtitle files. Tests and smoke runs use
    it, so nothing requires a network or a GPU.
``yt-dlp`` (default)
    The production implementation; yt-dlp is a pinned dependency and imported
    lazily.

Failures are typed (:mod:`backend.services.video_platform.errors`), so ticket
#45 can map them onto public error codes without parsing downloader messages.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from backend.services.video_platform.cleaning import clean_auto_captions
from backend.services.video_platform.config import (
    DEFAULT_TIMEOUT_SECONDS,
    DOWNLOAD_PROXY_ENV,
    DownloadOptions,
    download_options,
)
from backend.services.video_platform.errors import (
    AudioDownloadError,
    DownloadNetworkError,
    DownloadTimeoutError,
    InvalidUrlError,
    LocalToolMissingError,
    LoginRequiredError,
    SubtitleUnavailableError,
    UnsupportedUrlError,
    VideoPlatformConfigError,
    VideoPlatformError,
    VideoUnavailableError,
)
from backend.services.video_platform.fake import (
    FAKE_PLATFORM_NAME,
    FAKE_PLATFORM_ROOT_ENV,
    FakeVideoPlatform,
)
from backend.services.video_platform.models import (
    LOGIN_ABSENT,
    LOGIN_EXPIRED,
    LOGIN_NOT_REQUIRED,
    LOGIN_UNKNOWN,
    LOGIN_VALID,
    PLATFORM_BILIBILI,
    PLATFORM_YOUTUBE,
    SUPPORTED_PLATFORMS,
    TEXT_SOURCE_ASR,
    TEXT_SOURCE_AUTO,
    TEXT_SOURCE_MANUAL,
    TEXT_SOURCE_PENDING,
    TRACK_KIND_AUTO,
    TRACK_KIND_MANUAL,
    LoginStatus,
    SubtitleCue,
    SubtitleDocument,
    SubtitleTrack,
    VideoInfo,
    VideoRef,
)
from backend.services.video_platform.subtitles import SUPPORTED_SUBTITLE_FORMATS, parse_subtitle
from backend.services.video_platform.selection import (
    REASON_AUTO_CHINESE,
    REASON_AUTO_ORIGINAL,
    REASON_AUTO_REQUESTED,
    REASON_MANUAL_CHINESE,
    REASON_MANUAL_ORIGINAL,
    REASON_MANUAL_REQUESTED,
    REASON_NO_USABLE_SUBTITLE,
    REASON_PREFER_ASR,
    REASON_SUBTITLE_LANGUAGE_UNAVAILABLE,
    TEXT_PATH_ASR,
    TEXT_PATH_SUBTITLE,
    SubtitleDecision,
    decide_subtitle,
)
from backend.services.video_platform.urls import parse_video_url

VIDEO_PLATFORM_ENV = "BEEBOX_VIDEO_PLATFORM"
PLATFORM_REAL = "real"

_REAL_NAMES = {PLATFORM_REAL, "yt-dlp", "ytdlp", "local"}


class VideoPlatform(Protocol):
    """The seam every Bilibili/YouTube interaction crosses."""

    name: str

    def fetch_video_info(
        self, ref: VideoRef, *, options: DownloadOptions | None = None
    ) -> VideoInfo:
        """Metadata plus the subtitle tracks offered for the video."""

    def download_subtitle(
        self, track: SubtitleTrack, *, options: DownloadOptions | None = None
    ) -> SubtitleDocument:
        """Download and parse one subtitle track."""

    def download_audio(
        self, ref: VideoRef, destination: Path, *, options: DownloadOptions | None = None
    ) -> Path:
        """Download only the audio stream (the ticket #44 ASR path)."""

    def check_login(self) -> LoginStatus:
        """Report the Bilibili login state (tickets #46/#47 own the lifecycle)."""

    def tool_version(self) -> str | None:
        """The downloader version reported by ``/v1/health``."""


def video_platform_name() -> str:
    value = os.environ.get(VIDEO_PLATFORM_ENV, "").strip().lower()
    return value or PLATFORM_REAL


def get_video_platform() -> VideoPlatform:
    """Build the configured platform implementation."""
    name = video_platform_name()
    from backend.services import youtube_cookies

    cookie_file = youtube_cookies.cookie_file()
    if name == FAKE_PLATFORM_NAME:
        root = os.environ.get(FAKE_PLATFORM_ROOT_ENV, "").strip()
        if not root:
            raise VideoPlatformConfigError(
                f"{VIDEO_PLATFORM_ENV}=fake requires {FAKE_PLATFORM_ROOT_ENV}"
            )
        return FakeVideoPlatform(Path(root), cookie_file=cookie_file)
    if name in _REAL_NAMES:
        from backend.services.video_platform.yt_dlp import YtDlpVideoPlatform

        return YtDlpVideoPlatform(cookie_file=cookie_file)
    raise VideoPlatformConfigError(
        f"Unsupported video platform {name!r}; expected one of "
        f"{', '.join(sorted(_REAL_NAMES | {FAKE_PLATFORM_NAME}))}"
    )


def yt_dlp_version() -> str | None:
    """The downloader version for the health endpoint; never raises."""
    try:
        return get_video_platform().tool_version()
    except Exception:
        return None


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "DOWNLOAD_PROXY_ENV",
    "FAKE_PLATFORM_NAME",
    "FAKE_PLATFORM_ROOT_ENV",
    "LOGIN_ABSENT",
    "LOGIN_EXPIRED",
    "LOGIN_NOT_REQUIRED",
    "LOGIN_UNKNOWN",
    "LOGIN_VALID",
    "PLATFORM_BILIBILI",
    "PLATFORM_REAL",
    "PLATFORM_YOUTUBE",
    "REASON_AUTO_CHINESE",
    "REASON_AUTO_ORIGINAL",
    "REASON_AUTO_REQUESTED",
    "REASON_MANUAL_CHINESE",
    "REASON_MANUAL_ORIGINAL",
    "REASON_MANUAL_REQUESTED",
    "REASON_NO_USABLE_SUBTITLE",
    "REASON_PREFER_ASR",
    "REASON_SUBTITLE_LANGUAGE_UNAVAILABLE",
    "SUPPORTED_PLATFORMS",
    "SUPPORTED_SUBTITLE_FORMATS",
    "TEXT_PATH_ASR",
    "TEXT_PATH_SUBTITLE",
    "TEXT_SOURCE_ASR",
    "TEXT_SOURCE_AUTO",
    "TEXT_SOURCE_MANUAL",
    "TEXT_SOURCE_PENDING",
    "TRACK_KIND_AUTO",
    "TRACK_KIND_MANUAL",
    "VIDEO_PLATFORM_ENV",
    "AudioDownloadError",
    "DownloadNetworkError",
    "DownloadOptions",
    "DownloadTimeoutError",
    "FakeVideoPlatform",
    "InvalidUrlError",
    "LocalToolMissingError",
    "LoginRequiredError",
    "LoginStatus",
    "SubtitleCue",
    "SubtitleDecision",
    "SubtitleDocument",
    "SubtitleTrack",
    "SubtitleUnavailableError",
    "UnsupportedUrlError",
    "VideoInfo",
    "VideoPlatform",
    "VideoPlatformConfigError",
    "VideoPlatformError",
    "VideoRef",
    "VideoUnavailableError",
    "clean_auto_captions",
    "decide_subtitle",
    "download_options",
    "get_video_platform",
    "parse_subtitle",
    "parse_video_url",
    "video_platform_name",
    "yt_dlp_version",
]

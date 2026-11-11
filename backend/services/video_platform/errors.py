"""Typed failures raised by the video platform boundary (ticket #42).

The pipeline never parses downloader messages: every failure already carries a
stable ``error_code``. Ticket #45 maps these codes onto the remaining public
URL boundary cases (playlists, live streams, proxies) and ticket #46/#47 own
the Bilibili login lifecycle.
"""

from __future__ import annotations

from backend.services.errors import BeeBoxError


class VideoPlatformError(BeeBoxError):
    """Base class for every platform-side failure."""

    code = "VIDEO_PLATFORM_FAILED"

    def __init__(self, message: str, *, code: str | None = None, stage: str = "video") -> None:
        super().__init__(code or self.code, message, stage=stage)


class InvalidUrlError(VideoPlatformError):
    """The value is not an absolute, parseable URL."""

    code = "INVALID_URL"


class UnsupportedUrlError(VideoPlatformError):
    """The URL is not a supported single Bilibili/YouTube video."""

    code = "UNSUPPORTED_URL"


class VideoUnavailableError(VideoPlatformError):
    """The video exists but cannot be fetched (private, removed, region-locked)."""

    code = "VIDEO_NOT_ACCESSIBLE"


class LoginRequiredError(VideoPlatformError):
    """The platform requires a valid login for this video."""

    code = "VIDEO_LOGIN_REQUIRED"


class LocalToolMissingError(VideoPlatformError):
    """A local helper binary needed for the download is missing (ticket #85)."""

    code = "LOCAL_TOOL_MISSING"


class DownloadNetworkError(VideoPlatformError):
    """The download failed for a network or transport reason."""

    code = "DOWNLOAD_NETWORK_ERROR"


class DownloadTimeoutError(VideoPlatformError):
    """The download exceeded its deadline."""

    code = "DOWNLOAD_TIMEOUT"


class SubtitleUnavailableError(VideoPlatformError):
    """No usable platform subtitle track exists for the video."""

    code = "VIDEO_SUBTITLE_UNAVAILABLE"


class AudioDownloadError(VideoPlatformError):
    """The audio-only download failed (used by the ticket #44 ASR path)."""

    code = "VIDEO_AUDIO_DOWNLOAD_FAILED"


class VideoPlatformConfigError(VideoPlatformError):
    """The platform implementation is misconfigured."""

    code = "VIDEO_PLATFORM_CONFIG_INVALID"


__all__ = [
    "AudioDownloadError",
    "DownloadNetworkError",
    "DownloadTimeoutError",
    "InvalidUrlError",
    "LocalToolMissingError",
    "LoginRequiredError",
    "SubtitleUnavailableError",
    "UnsupportedUrlError",
    "VideoPlatformConfigError",
    "VideoPlatformError",
    "VideoUnavailableError",
]

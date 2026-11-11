"""Download settings shared by every video platform implementation.

``BEEBOX_DOWNLOAD_PROXY`` is resolved once per job and handed to every
platform call (metadata fetch, subtitle download, audio download) so a proxy
can never cover only part of a job. Keeping the value out of result metadata
and logs matches the write-only credential rule (ADR-0011).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DOWNLOAD_PROXY_ENV = "BEEBOX_DOWNLOAD_PROXY"
DEFAULT_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class DownloadOptions:
    """Per-job transport settings passed across the video platform boundary."""

    proxy: str | None = None
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS


def download_options(*, timeout_seconds: int | None = None) -> DownloadOptions:
    """Read the download settings from the environment for one job."""
    proxy = os.environ.get(DOWNLOAD_PROXY_ENV, "").strip() or None
    return DownloadOptions(
        proxy=proxy,
        timeout_seconds=timeout_seconds or DEFAULT_TIMEOUT_SECONDS,
    )


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "DOWNLOAD_PROXY_ENV",
    "DownloadOptions",
    "download_options",
]

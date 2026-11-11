"""The write-only YouTube cookie store (ticket #48).

YouTube works without login by default (the bgutil PO token provider), but an
age-restricted video needs a signed-in cookie jar. An operator uploads a
Netscape-format ``cookies.txt`` once; this module validates it, writes it to
the secrets directory with restrictive permissions, and hands the path to the
yt-dlp adapter.

The cookie contents are never returned by the API, never written to the
database and never logged. Only non-secret metadata (size, cookie count and
modification time) is exposed for operations.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from backend import config

COOKIE_FILE_ENV = "BEEBOX_YOUTUBE_COOKIE_FILE"
DEFAULT_COOKIE_FILENAME = "youtube-cookies.txt"
MAX_COOKIE_BYTES = 1024 * 1024
_COOKIE_HEADERS = ("# Netscape HTTP Cookie File", "# HTTP Cookie File")


class YouTubeCookieError(Exception):
    """Base class for cookie upload failures; messages never include content."""


class InvalidYouTubeCookie(YouTubeCookieError):
    """The uploaded bytes are not a usable Netscape cookie jar."""


class YouTubeCookieTooLarge(YouTubeCookieError):
    """The upload exceeds the cookie file limit."""


@dataclass(frozen=True)
class YouTubeCookieInfo:
    """Non-secret metadata about the stored cookie file."""

    configured: bool
    size_bytes: int
    cookie_count: int
    updated_at: datetime | None


def cookie_file_path() -> Path:
    """Where the cookie jar lives; ``BEEBOX_YOUTUBE_COOKIE_FILE`` overrides it."""
    explicit = os.environ.get(COOKIE_FILE_ENV, "").strip()
    if explicit:
        return Path(explicit).expanduser().absolute()
    return config.resolve_secrets_dir() / DEFAULT_COOKIE_FILENAME


def cookie_file() -> Path | None:
    """The stored cookie jar when one exists, otherwise ``None``."""
    path = cookie_file_path()
    try:
        if path.is_file() and path.stat().st_size > 0:
            return path
    except OSError:
        return None
    return None


def count_cookies(content: bytes) -> int:
    """Validate Netscape cookie syntax and return the number of cookie lines.

    Browser extensions write a header line, comment lines and one tab-separated
    line per cookie; ``#HttpOnly_`` prefixes are part of the Netscape dialect.
    """
    if not content or len(content) > MAX_COOKIE_BYTES:
        raise InvalidYouTubeCookie("The uploaded file is empty or too large")
    if b"\x00" in content:
        raise InvalidYouTubeCookie("The uploaded file is not a text cookie jar")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InvalidYouTubeCookie("The uploaded file is not UTF-8 text") from exc

    lines = text.splitlines()
    header = next((line.strip() for line in lines if line.strip()), "")
    if not header.startswith(_COOKIE_HEADERS):
        raise InvalidYouTubeCookie(
            "The uploaded file is not a Netscape cookies.txt export"
        )

    count = 0
    for line in lines:
        stripped = line.strip("\r")
        if not stripped:
            continue
        if stripped.startswith("#HttpOnly_"):
            fields = stripped.split("\t")
        elif stripped.startswith("#"):
            continue
        else:
            fields = stripped.split("\t")
        if len(fields) != 7:
            raise InvalidYouTubeCookie(
                "The uploaded file has a malformed Netscape cookie line"
            )
        count += 1
    if count == 0:
        raise InvalidYouTubeCookie("The uploaded file contains no cookies")
    return count


def store_cookie(content: bytes) -> YouTubeCookieInfo:
    """Validate and atomically replace the stored cookie jar."""
    if len(content) > MAX_COOKIE_BYTES:
        raise YouTubeCookieTooLarge(
            f"The cookie file exceeds the {MAX_COOKIE_BYTES} byte limit"
        )
    count = count_cookies(content)

    path = cookie_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # Best effort on Windows; the installer already restricts the
            # secrets directory ACL to SYSTEM and Administrators.
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return cookie_status()


def cookie_status() -> YouTubeCookieInfo:
    """The stored cookie jar's non-secret metadata."""
    path = cookie_file_path()
    try:
        stat = path.stat()
    except OSError:
        return YouTubeCookieInfo(False, 0, 0, None)
    if not path.is_file() or stat.st_size <= 0:
        return YouTubeCookieInfo(False, 0, 0, None)

    count = 0
    if stat.st_size <= MAX_COOKIE_BYTES:
        try:
            count = count_cookies(path.read_bytes())
        except (OSError, YouTubeCookieError):
            count = 0
    return YouTubeCookieInfo(
        configured=True,
        size_bytes=stat.st_size,
        cookie_count=count,
        updated_at=datetime.fromtimestamp(stat.st_mtime, UTC),
    )


__all__ = [
    "COOKIE_FILE_ENV",
    "DEFAULT_COOKIE_FILENAME",
    "MAX_COOKIE_BYTES",
    "InvalidYouTubeCookie",
    "YouTubeCookieError",
    "YouTubeCookieInfo",
    "YouTubeCookieTooLarge",
    "cookie_file",
    "cookie_file_path",
    "cookie_status",
    "count_cookies",
    "store_cookie",
]

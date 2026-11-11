"""Write-only-to-callers Bilibili credential file (ticket #46, ADR-0011/ADR-0012).

The credential is write-only from the outside: the login service stores what a
confirmed QR login returns in the secrets directory in the Netscape cookie
format yt-dlp reads, and nothing here logs cookie names or values, nor builds
an API response from them. The one internal reader is the daily refresh loop
(ADR-0012), which parses the file to renew the login and reports only
``valid``/``expired``/``absent`` through ``/v1/health``.

The file lives at ``<secrets dir>/bilibili-cookies.txt``. ``BEEBOX_SECRETS_DIR``
overrides the directory; otherwise the install root's ``secrets`` directory is
used (``C:\\BeeBox\\secrets`` in the Windows deployment), falling back to
``<data dir>/secrets`` for a developer checkout.

A sibling ``bilibili-login-source`` marker records whether the credential came
from the TV QR channel or a web login; the daily refresh uses it to decide
whether the web refresh endpoint may be called (ticket #68, ADR-0012).
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

from backend import config
from backend.services.video_platform.models import (
    LOGIN_ABSENT,
    LOGIN_EXPIRED,
    LOGIN_VALID,
    LoginStatus,
)

COOKIE_FILENAME = "bilibili-cookies.txt"
# Written by a failed daily refresh (ticket #47): ``login_status`` then reports
# ``expired`` even while the cookie file's own timestamp is still in range. The
# marker holds no credential.
INVALID_MARKER_FILENAME = "bilibili-login-invalid"
# Which login channel produced the stored credential (ticket #68). The console
# QR login uses the TV channel, whose refresh token is app-scoped and cannot be
# renewed through the web cookie endpoint (ADR-0012). The marker holds one word
# and no credential.
LOGIN_SOURCE_FILENAME = "bilibili-login-source"
LOGIN_SOURCE_TV = "tv"
LOGIN_SOURCE_WEB = "web"
LOGIN_SOURCE_CHANNELS = (LOGIN_SOURCE_TV, LOGIN_SOURCE_WEB)
#: Credentials written before provenance was recorded came from the TV channel,
#: so a missing marker means "non-renewable" rather than "try the web refresh".
DEFAULT_LOGIN_SOURCE = LOGIN_SOURCE_TV
COOKIE_DOMAIN = ".bilibili.com"
# Bilibili's web login cookies are long-lived; the refresh ticket (#47) renews
# them daily before this deadline. The stamp only drives health reporting.
COOKIE_TTL_SECONDS = 180 * 24 * 3600
# Stable ordering keeps the file diff-friendly and puts SESSDATA first.
_COOKIE_ORDER = ("SESSDATA", "bili_jct", "DedeUserID", "ac_time_value", "buvid3", "buvid4")


def cookie_file_path() -> Path:
    """The yt-dlp cookie file path; never creates the secrets directory."""
    return config.resolve_secrets_dir() / COOKIE_FILENAME


def invalid_marker_path() -> Path:
    """The marker a failed refresh leaves behind; it contains no credential."""
    return config.resolve_secrets_dir() / INVALID_MARKER_FILENAME


def login_source_path() -> Path:
    """The provenance marker for the stored credential; contains no secret."""
    return config.resolve_secrets_dir() / LOGIN_SOURCE_FILENAME


def write_login_source(source: str) -> Path:
    """Record which channel produced the stored credential.

    The marker is a single non-secret word (``tv`` or ``web``). Only the daily
    refresh reads it, to decide whether the web refresh endpoint applies. The
    write is atomic (same ``mkstemp`` + ``os.replace`` pattern as the cookie
    file) so a concurrent reader never sees a half-written marker.
    """
    value = (source or "").strip().lower()
    if value not in LOGIN_SOURCE_CHANNELS:
        raise ValueError("Unsupported Bilibili login source")
    path = login_source_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".bilibili-login-source.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{value}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def read_login_source() -> str:
    """Return the channel that produced the stored credential.

    A missing or unreadable marker means the credential predates provenance
    recording, which is the TV channel: the safe answer is to skip the web
    refresh rather than send an app-scoped refresh token to it.
    """
    try:
        value = login_source_path().read_text(encoding="utf-8").strip().lower()
    except OSError:
        return DEFAULT_LOGIN_SOURCE
    return value or DEFAULT_LOGIN_SOURCE


def mark_login_invalid() -> None:
    """Record that the platform rejected or could not refresh the login."""
    path = invalid_marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("invalid\n", encoding="utf-8")


def clear_login_invalid() -> None:
    """Forget a previous refresh failure; a new credential supersedes it."""
    invalid_marker_path().unlink(missing_ok=True)


def _cookie_line(name: str, value: str, expires_at: int) -> str:
    if any(character in value for character in "\t\r\n"):
        # The message deliberately omits the value: it is a credential.
        raise ValueError(f"The {name!r} login cookie cannot be stored in Netscape format")
    domain = f"#HttpOnly_{COOKIE_DOMAIN}" if name == "SESSDATA" else COOKIE_DOMAIN
    return f"{domain}\tTRUE\t/\tTRUE\t{expires_at}\t{name}\t{value}\n"


def _ordered_cookies(cookies: dict[str, str]) -> list[tuple[str, str]]:
    ordered: list[tuple[str, str]] = []
    for name in _COOKIE_ORDER:
        value = cookies.get(name)
        if isinstance(value, str) and value:
            ordered.append((name, value))
    seen = {name for name, _value in ordered}
    for name, value in cookies.items():
        if name not in seen and isinstance(value, str) and value:
            ordered.append((name, value))
    return ordered


def write_cookie_file(
    cookies: dict[str, str],
    *,
    expires_at: int | None = None,
    source: str | None = None,
) -> Path:
    """Atomically write the credentials as a Netscape cookie file.

    ``source`` records the login channel in the provenance marker; omit it when
    the credential was already stored by that channel (a refresh keeps it).
    Returns the file path. The secret values are deliberately absent from every
    exception, log record and return value.
    """
    if source is not None:
        # Record provenance before committing the credential so a credential on
        # disk always has the marker that explains how to renew it.
        write_login_source(source)
    path = cookie_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    expiry = int(expires_at if expires_at is not None else time.time() + COOKIE_TTL_SECONDS)
    lines = [
        "# Netscape HTTP Cookie File\n",
        "# Generated by BeeBox from a confirmed Bilibili QR login. Do not edit.\n",
        "\n",
    ]
    lines.extend(_cookie_line(name, value, expiry) for name, value in _ordered_cookies(cookies))
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".bilibili-cookies.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.writelines(lines)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            # Windows may not map POSIX modes; the secrets directory ACL is the
            # real boundary there.
            pass
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    # A credential that just came from the platform is authoritative again.
    clear_login_invalid()
    return path


def _cookie_records(path: Path) -> list[tuple[str, str, str, str, str, str, str]]:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    records: list[tuple[str, str, str, str, str, str, str]] = []
    for line in text.splitlines():
        if not line or line.startswith("# "):
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        elif line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            continue
        records.append(tuple(fields))  # type: ignore[arg-type]
    return records


def read_cookies() -> dict[str, str]:
    """Return the stored cookies for an internal refresh request.

    This is the one reader of the write-only file: ticket #47 needs the
    current credential to ask the platform for a fresh one. The values never
    leave the refresh service, an API response or a log record.
    """
    records = _cookie_records(cookie_file_path())
    return {name: value for _domain, _sub, _path, _secure, _expiry, name, value in records if value}


#: States in which a Bilibili job must not continue anonymously (ADR-0012).
LOGIN_UNAVAILABLE_STATES = frozenset({LOGIN_ABSENT, LOGIN_EXPIRED})


def login_unavailable(status: LoginStatus | None = None) -> bool:
    """Whether a Bilibili job must stop instead of falling back to anonymous access.

    ADR-0012 refuses a silent downgrade: without a usable login the platform
    may hide subtitles and the higher-quality audio, so a job fails with
    ``VIDEO_LOGIN_REQUIRED`` and the operator signs in again. This is the one
    place that decides the state is unusable; the video job executor calls it
    before touching the network.
    """
    return (status or login_status()).state in LOGIN_UNAVAILABLE_STATES


def login_expired(status: LoginStatus | None = None) -> bool:
    """Whether a stored credential exists but is stale.

    Used when a download already failed: only a stale credential is evidence
    that the login caused the failure. An absent credential is either refused
    by the job gate beforehand, or the caller explicitly chose the anonymous
    path (``prefer=asr``) and the failure must be classified on its own merits.
    """
    return (status or login_status()).state == LOGIN_EXPIRED


def login_status() -> LoginStatus:
    """Report ``valid``, ``expired`` or ``absent`` from local state only.

    No network is used, so ``/v1/health`` stays fast and side-effect free. The
    file is inspected in memory; no value ever leaves this function. A failed
    or invalidating daily refresh (ticket #47) writes a marker file, which
    reports ``expired`` even while the cookie file's own timestamp is in range.
    """
    path = cookie_file_path()
    if not path.is_file():
        return LoginStatus(state=LOGIN_ABSENT, detail="No Bilibili login is stored")
    if invalid_marker_path().is_file():
        return LoginStatus(
            state=LOGIN_EXPIRED,
            detail="The stored Bilibili login was invalidated by a failed refresh",
        )
    now = int(time.time())
    for domain, _include_subdomains, _cookie_path, _secure, expiry, name, value in _cookie_records(path):
        if name != "SESSDATA" or not value or "bilibili.com" not in domain:
            continue
        try:
            expires_at = int(expiry)
        except ValueError:
            return LoginStatus(state=LOGIN_EXPIRED, detail="The stored Bilibili login is unreadable")
        if expires_at and expires_at <= now:
            return LoginStatus(state=LOGIN_EXPIRED, detail="The stored Bilibili login has expired")
        return LoginStatus(state=LOGIN_VALID)
    return LoginStatus(state=LOGIN_ABSENT, detail="No Bilibili login is stored")


__all__ = [
    "COOKIE_DOMAIN",
    "COOKIE_FILENAME",
    "COOKIE_TTL_SECONDS",
    "DEFAULT_LOGIN_SOURCE",
    "INVALID_MARKER_FILENAME",
    "LOGIN_SOURCE_CHANNELS",
    "LOGIN_SOURCE_FILENAME",
    "LOGIN_SOURCE_TV",
    "LOGIN_SOURCE_WEB",
    "LOGIN_UNAVAILABLE_STATES",
    "clear_login_invalid",
    "cookie_file_path",
    "invalid_marker_path",
    "login_expired",
    "login_source_path",
    "login_status",
    "login_unavailable",
    "mark_login_invalid",
    "read_cookies",
    "read_login_source",
    "write_cookie_file",
    "write_login_source",
]

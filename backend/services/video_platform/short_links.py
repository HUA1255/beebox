"""Expand short links at task start (ticket #29).

A ``t.co`` link hides its target behind an opaque token, so submit stays
offline and only marks the reference as a short link. The job then expands it
before the login gate, metadata fetch and every download, with the same
:class:`~backend.services.video_platform.config.DownloadOptions` as the
platform calls, which means ``BEEBOX_DOWNLOAD_PROXY`` covers both steps.

Expansion never saves cookies and never reads a response body: it only follows
the redirect chain (bounded by :data:`MAX_REDIRECTS`) and returns the final
address for the job to recognize again. Transport failures become the usual
retryable download errors.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import requests

from backend.services.video_platform.config import (
    DEFAULT_TIMEOUT_SECONDS,
    DownloadOptions,
)
from backend.services.video_platform.errors import (
    DownloadNetworkError,
    DownloadTimeoutError,
)

MAX_REDIRECTS = 10
_USER_AGENT = "BeeBox/1.0 (short-link expansion)"

# X answers an expired or refused session with a login or verification page
# instead of the post; the job asks for a sign-in rather than reporting the
# link as unsupported.
_LOGIN_HOSTS = ("x.com", "twitter.com")
_LOGIN_PATHS = (
    re.compile(r"^/i/flow/(?:login|signup)(?:/|$)"),
    re.compile(r"^/login(?:/|$)"),
    re.compile(r"^/account/access(?:/|$)"),
)


def is_login_page(url: str) -> bool:
    """True when ``url`` is an X login or verification page."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not any(host == item or host.endswith(f".{item}") for item in _LOGIN_HOSTS):
        return False
    path = parts.path or "/"
    return any(pattern.match(path) for pattern in _LOGIN_PATHS)


def expand_short_link(url: str, *, options: DownloadOptions | None = None) -> str:
    """Return the address ``url`` finally points at.

    Redirects are followed through the job's proxy, if one is configured.
    Raises :class:`DownloadTimeoutError` when the deadline expires and
    :class:`DownloadNetworkError` for any other transport or HTTP failure, so
    the caller reports the retryable codes the ticket defines.
    """
    settings = options or DownloadOptions()
    proxy = settings.proxy
    proxies = {"http": proxy, "https": proxy} if proxy else None
    timeout = settings.timeout_seconds or DEFAULT_TIMEOUT_SECONDS
    try:
        with requests.Session() as session:
            session.max_redirects = MAX_REDIRECTS
            response = session.get(
                url,
                allow_redirects=True,
                timeout=timeout,
                proxies=proxies,
                headers={"User-Agent": _USER_AGENT},
                stream=True,
            )
    except requests.exceptions.Timeout as exc:
        raise DownloadTimeoutError(
            f"Timed out while expanding the short link {url}"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise DownloadNetworkError(
            f"Network error while expanding the short link {url}"
        ) from exc

    try:
        if response.status_code >= 400:
            raise DownloadNetworkError(
                f"The short link {url} returned HTTP {response.status_code}"
            )
        return response.url or url
    finally:
        response.close()


__all__ = ["MAX_REDIRECTS", "expand_short_link", "is_login_page"]

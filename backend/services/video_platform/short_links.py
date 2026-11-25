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

Which expansion targets are login or verification walls is platform knowledge,
so it lives in the rule table (:attr:`PlatformRule.login_paths`) and this
module only applies that rule.
"""

from __future__ import annotations

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
from backend.services.video_platform.platforms import PlatformRule, platform_rule

MAX_REDIRECTS = 10
_USER_AGENT = "BeeBox/1.0 (short-link expansion)"


def _host_matches(rule: PlatformRule, host: str) -> bool:
    return any(
        host == host_rule.host or host.endswith(f".{host_rule.host}")
        for host_rule in rule.hosts
    )


def is_login_page(url: str, *, platform: str) -> bool:
    """True when ``url`` is a login or verification page on ``platform``.

    Only a platform whose rule declares ``login_paths`` can answer yes; every
    other platform (and every unknown one) has no login wall to detect here.
    """
    rule = platform_rule(platform)
    if rule is None or not rule.login_paths:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host or not _host_matches(rule, host):
        return False
    path = parts.path or "/"
    return any(pattern.match(path) for pattern in rule.login_paths)


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

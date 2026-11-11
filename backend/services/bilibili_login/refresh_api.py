"""Production refresh adapter over the pinned ``bilibili-api-python`` (ADR-0012).

The library exposes exactly what the daily check needs: ``check_valid`` says
whether the stored cookies still identify a login, ``check_refresh`` says
whether the platform wants them renewed, and ``refresh`` rotates them. All
three are async, so they run on the same private event loop the QR login uses.

Only web-sourced credentials may take the refresh path (ticket #68). The TV QR
login returns web cookies plus an app-scoped OAuth refresh token, while the
library's ``refresh()`` posts ``source=main_web`` to the web cookie endpoint;
its token is not interchangeable with the web ``refresh_token``. A TV-sourced
credential is therefore only checked for validity, and the operator re-scans
when the platform says it is gone.

Failures are returned as ``RefreshPoll(status="failed")`` with a generic
detail: an exception body may contain a platform response and never reaches a
log record or an API caller.
"""

from __future__ import annotations

import os

from backend.services.bilibili_login.client import (
    DEFAULT_LOGIN_TIMEOUT_SECONDS,
    _AsyncRunner,
)
from backend.services.bilibili_login.cookies import (
    LOGIN_SOURCE_WEB,
    read_login_source,
)
from backend.services.bilibili_login.refresh import (
    REFRESH_EXPIRED,
    REFRESH_FAILED,
    REFRESH_REFRESHED,
    REFRESH_VALID,
    RefreshPoll,
)
from backend.services.video_platform.config import DOWNLOAD_PROXY_ENV


class BilibiliApiRefreshClient:
    """Daily-check client backed by the pinned ``bilibili-api-python``."""

    name = "bilibili-api"

    def __init__(
        self,
        *,
        timeout_seconds: int = DEFAULT_LOGIN_TIMEOUT_SECONDS,
        proxy: str | None = None,
        source: str | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._proxy = proxy
        # An explicit source wins; otherwise the provenance marker is read at
        # check time, so a re-scan with another channel takes effect at once.
        self._source = source

    def refresh(self, cookies: dict[str, str]) -> RefreshPoll:
        try:
            from bilibili_api import Credential
        except Exception:
            return RefreshPoll(
                status=REFRESH_FAILED,
                detail="bilibili-api is not installed; install the locked dependencies",
            )
        if not cookies.get("SESSDATA"):
            return RefreshPoll(
                status=REFRESH_EXPIRED, detail="No Bilibili login is stored"
            )
        source = self._source or read_login_source()
        runner = _AsyncRunner(self._timeout_seconds)
        try:
            credential = Credential(
                sessdata=cookies.get("SESSDATA"),
                bili_jct=cookies.get("bili_jct"),
                dedeuserid=cookies.get("DedeUserID"),
                ac_time_value=cookies.get("ac_time_value"),
                proxy=self._proxy or os.environ.get(DOWNLOAD_PROXY_ENV, "").strip() or None,
            )
            return runner.run(self._check(credential, source))
        except Exception:
            return RefreshPoll(
                status=REFRESH_FAILED,
                detail="The Bilibili login refresh request failed",
            )
        finally:
            runner.close()

    async def _check(self, credential, source: str) -> RefreshPoll:
        if not await credential.check_valid():
            return RefreshPoll(
                status=REFRESH_EXPIRED,
                detail="The Bilibili login is no longer valid",
            )
        if source != LOGIN_SOURCE_WEB:
            # TV (and unknown) provenance: validity detection only. Sending an
            # app-scoped TV refresh token to the web refresh endpoint is not a
            # supported renewal path (ADR-0012), so never call check_refresh/
            # refresh here; the operator re-scans when the login expires.
            return RefreshPoll(status=REFRESH_VALID)
        if await credential.check_refresh():
            await credential.refresh()
            return RefreshPoll(status=REFRESH_REFRESHED, cookies=self._cookies(credential))
        return RefreshPoll(status=REFRESH_VALID, cookies=self._cookies(credential))

    @staticmethod
    def _cookies(credential) -> dict[str, str]:
        cookies = credential.get_cookies()
        return {
            str(name): value
            for name, value in cookies.items()
            if isinstance(value, str) and value
        }


__all__ = ["BilibiliApiRefreshClient"]

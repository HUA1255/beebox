"""The Bilibili QR-login client boundary (ticket #46).

Two implementations ship, selected by ``BEEBOX_BILIBILI_LOGIN``:

``real`` (default)
    ``bilibili-api-python==17.4.2`` (pinned, ADR-0012). Imported lazily so the
    fake client and the test suite never need it installed.
``fake``
    Scripted states and fixed cookie values from ``BEEBOX_BILIBILI_LOGIN_FAKE``;
    no network. Tests and smoke runs use this.

The client only reports states and the credential needed to write the cookie
file. Neither the protocol nor the implementations log or return cookie values
to HTTP callers.
"""

from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass
from typing import Protocol

from backend.services.errors import BeeBoxError

BILIBILI_LOGIN_ENV = "BEEBOX_BILIBILI_LOGIN"
BILIBILI_LOGIN_FAKE = "fake"
BILIBILI_LOGIN_REAL = "real"

LOGIN_IDLE = "idle"
LOGIN_PENDING = "pending"
LOGIN_SCANNED = "scanned"
LOGIN_CONFIRMED = "confirmed"
LOGIN_FAILED = "failed"
LOGIN_CANCELLED = "cancelled"
LOGIN_STATES = (
    LOGIN_IDLE,
    LOGIN_PENDING,
    LOGIN_SCANNED,
    LOGIN_CONFIRMED,
    LOGIN_FAILED,
    LOGIN_CANCELLED,
)
ACTIVE_LOGIN_STATES = (LOGIN_PENDING, LOGIN_SCANNED)

#: Bilibili's browser QR code is valid for about three minutes.
QR_CODE_TTL_SECONDS = 180
DEFAULT_LOGIN_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class QRCodePayload:
    """What the console needs to show the login QR code."""

    status: str
    qr_url: str | None
    qr_image: str | None
    expires_in: int


@dataclass(frozen=True)
class LoginPoll:
    """One poll result; ``cookies`` is only set when the login is confirmed."""

    status: str
    detail: str | None = None
    cookies: dict[str, str] | None = None


#: Cookie names yt-dlp reads, in Bilibili's canonical capitalisation.
BILIBILI_COOKIE_NAMES = (
    "SESSDATA",
    "bili_jct",
    "DedeUserID",
    "ac_time_value",
    "buvid3",
    "buvid4",
)


def credential_cookies(credential) -> dict[str, str]:
    """Map a bilibili-api credential to the canonical cookie names.

    ``Credential.get_cookies()`` also exposes lowercase duplicates and internal
    fields; the cookie file only needs the standard names.
    """
    values = {
        "SESSDATA": getattr(credential, "sessdata", ""),
        "bili_jct": getattr(credential, "bili_jct", ""),
        "DedeUserID": getattr(credential, "dedeuserid", ""),
        "ac_time_value": getattr(credential, "ac_time_value", ""),
        "buvid3": getattr(credential, "buvid3", ""),
        "buvid4": getattr(credential, "buvid4", ""),
    }
    return {
        name: value
        for name in BILIBILI_COOKIE_NAMES
        if isinstance(value := values[name], str) and value
    }


class BilibiliLoginError(BeeBoxError):
    """Base class for login-client failures with a stable public code."""

    code = "BILIBILI_LOGIN_FAILED"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(code or self.code, message, stage="bilibili_login")


class BilibiliLoginUnavailableError(BilibiliLoginError):
    """The configured login implementation cannot be loaded."""

    code = "BILIBILI_LOGIN_UNAVAILABLE"


class BilibiliLoginStartFailedError(BilibiliLoginError):
    """The platform did not return a QR code."""

    code = "BILIBILI_LOGIN_START_FAILED"


class BilibiliLoginClient(Protocol):
    """The seam the login service depends on."""

    def start(self) -> QRCodePayload:
        """Request a fresh QR code and return the payload to display."""

    def poll(self) -> LoginPoll:
        """Ask the platform for the current state of that QR code."""

    def cancel(self) -> None:
        """Drop the pending QR code and release whatever it holds."""


def bilibili_login_name() -> str:
    value = os.environ.get(BILIBILI_LOGIN_ENV, "").strip().lower()
    return value or BILIBILI_LOGIN_REAL


def get_login_client() -> BilibiliLoginClient:
    """Build the configured client implementation."""
    name = bilibili_login_name()
    if name == BILIBILI_LOGIN_FAKE:
        from backend.services.bilibili_login.fake import FakeBilibiliLoginClient

        return FakeBilibiliLoginClient.from_environment()
    if name in {BILIBILI_LOGIN_REAL, "bilibili-api", "bilibili"}:
        return BilibiliApiLoginClient()
    raise BilibiliLoginUnavailableError(
        f"Unsupported Bilibili login implementation {name!r}; "
        f"expected {BILIBILI_LOGIN_REAL!r} or {BILIBILI_LOGIN_FAKE!r}"
    )


class _AsyncRunner:
    """One event loop on a private thread for the async bilibili-api client.

    bilibili-api keys its aiohttp session pool by event loop, so the whole QR
    login lives on a single loop instead of a new ``asyncio.run`` per call.
    """

    def __init__(self, timeout_seconds: int) -> None:
        self._timeout_seconds = timeout_seconds
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._serve, name="bilibili-login", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=5)

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def run(self, coroutine):
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result(timeout=self._timeout_seconds)

    def close(self) -> None:
        if self._loop.is_closed():
            return
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=2)
        except RuntimeError:
            return
        finally:
            # A hung request keeps the loop alive on its daemon thread; never
            # close a running loop from another thread.
            if not self._thread.is_alive() and not self._loop.is_closed():
                self._loop.close()


class BilibiliApiLoginClient:
    """Production client backed by the pinned ``bilibili-api-python``."""

    name = "bilibili-api"

    def __init__(self, *, timeout_seconds: int = DEFAULT_LOGIN_TIMEOUT_SECONDS) -> None:
        self._timeout_seconds = timeout_seconds
        self._runner: _AsyncRunner | None = None
        self._qr = None
        self._events = None

    def start(self) -> QRCodePayload:
        try:
            from bilibili_api import login_v2
        except Exception as exc:  # pragma: no cover - dependency is pinned
            raise BilibiliLoginUnavailableError(
                "bilibili-api is not installed; install the locked dependencies"
            ) from exc
        try:
            # The web QR flow no longer returns SESSDATA/bili_jct/DedeUserID
            # (observed 2026-11); the TV flow returns the full cookie list.
            qr = login_v2.QrCodeLogin(platform=login_v2.QrCodeLoginChannel.TV)
            self._get_runner().run(qr.generate_qrcode())
            picture = qr.get_qrcode_picture()
            content = getattr(picture, "content", b"") if picture is not None else b""
        except BilibiliLoginError:
            raise
        except Exception as exc:
            self.cancel()
            raise BilibiliLoginStartFailedError(
                "The Bilibili login service did not return a QR code"
            ) from exc
        self._qr = qr
        self._events = login_v2.QrCodeLoginEvents
        image = None
        if content:
            import base64

            image = "data:image/png;base64," + base64.b64encode(content).decode("ascii")
        return QRCodePayload(
            status=LOGIN_PENDING,
            # QrCodeLogin exposes only the terminal rendering of the link.
            qr_url=None,
            qr_image=image,
            expires_in=QR_CODE_TTL_SECONDS,
        )

    def poll(self) -> LoginPoll:
        if self._qr is None or self._events is None:
            return LoginPoll(status=LOGIN_FAILED, detail="No Bilibili login is in progress")
        try:
            event = self._get_runner().run(self._qr.check_state())
        except BilibiliLoginError:
            raise
        except Exception:
            self.cancel()
            return LoginPoll(
                status=LOGIN_FAILED,
                detail="The Bilibili login status could not be checked",
            )
        events = self._events
        if event == events.SCAN:
            return LoginPoll(status=LOGIN_PENDING)
        if event == events.CONF:
            return LoginPoll(status=LOGIN_SCANNED)
        if event == events.TIMEOUT:
            self.cancel()
            return LoginPoll(status=LOGIN_FAILED, detail="The Bilibili QR code expired")
        if event == events.DONE:
            credential = self._qr.get_credential()
            return LoginPoll(
                status=LOGIN_CONFIRMED,
                cookies=credential_cookies(credential),
            )
        return LoginPoll(status=LOGIN_FAILED, detail="Unexpected Bilibili login state")

    def cancel(self) -> None:
        self._qr = None
        self._events = None
        if self._runner is not None:
            self._runner.close()
            self._runner = None

    def _get_runner(self) -> _AsyncRunner:
        if self._runner is None:
            self._runner = _AsyncRunner(self._timeout_seconds)
        return self._runner


__all__ = [
    "ACTIVE_LOGIN_STATES",
    "BILIBILI_LOGIN_ENV",
    "BILIBILI_LOGIN_FAKE",
    "BILIBILI_LOGIN_REAL",
    "DEFAULT_LOGIN_TIMEOUT_SECONDS",
    "LOGIN_CANCELLED",
    "LOGIN_CONFIRMED",
    "LOGIN_FAILED",
    "LOGIN_IDLE",
    "LOGIN_PENDING",
    "LOGIN_SCANNED",
    "LOGIN_STATES",
    "QR_CODE_TTL_SECONDS",
    "BilibiliApiLoginClient",
    "BilibiliLoginClient",
    "BilibiliLoginError",
    "BilibiliLoginStartFailedError",
    "BilibiliLoginUnavailableError",
    "BILIBILI_COOKIE_NAMES",
    "LoginPoll",
    "QRCodePayload",
    "bilibili_login_name",
    "credential_cookies",
    "get_login_client",
]

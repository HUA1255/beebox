"""Daily Bilibili credential check and refresh (tickets #47/#68, ADR-0012).

The service checks the stored login once a day through the platform boundary:
it asks the client whether the credential is still valid and, when the
platform says a refresh is due and the credential is web-sourced, writes the
renewed cookie atomically back to the same write-only file yt-dlp reads. A
login that is still valid is left untouched. TV-sourced credentials (the only
channel the QR login uses now) are validity-checked only, because their
app-scoped refresh token does not work with the web refresh endpoint; when the
platform rejects one the operator re-scans. A rejected login or a failed
refresh is never papered over:
:func:`backend.services.bilibili_login.login_status` then reports ``expired``
and jobs that need the login fail with ``VIDEO_LOGIN_REQUIRED``.

The clock is injectable so tests can advance days instead of sleeping, and the
client boundary has a fake implementation, like the QR login client.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from backend import config
from backend.services.bilibili_login.client import (
    BILIBILI_LOGIN_FAKE,
    BilibiliLoginError,
    bilibili_login_name,
)
from backend.services.bilibili_login.cookies import (
    clear_login_invalid,
    mark_login_invalid,
    read_cookies,
    write_cookie_file,
)

logger = logging.getLogger(__name__)

BILIBILI_REFRESH_ENV = "BEEBOX_BILIBILI_REFRESH"
BILIBILI_REFRESH_FAKE = "fake"
BILIBILI_REFRESH_REAL = "real"

REFRESH_ABSENT = "absent"
REFRESH_VALID = "valid"
REFRESH_REFRESHED = "refreshed"
REFRESH_EXPIRED = "expired"
REFRESH_FAILED = "failed"
REFRESH_STATES = (
    REFRESH_ABSENT,
    REFRESH_VALID,
    REFRESH_REFRESHED,
    REFRESH_EXPIRED,
    REFRESH_FAILED,
)

#: The service wakes this often, but a check only runs when the interval is up.
_MAX_TICK_SECONDS = 60.0
_MIN_TICK_SECONDS = 0.05


class BilibiliRefreshUnavailableError(BilibiliLoginError):
    """The configured refresh implementation cannot be loaded."""

    code = "BILIBILI_REFRESH_UNAVAILABLE"


@dataclass(frozen=True)
class RefreshPoll:
    """One answer from the platform: whether the login is valid and its cookies.

    ``cookies`` is only populated for ``valid`` and ``refreshed``; it is a
    credential and never leaves the refresh service.
    """

    status: str
    cookies: dict[str, str] | None = None
    detail: str | None = None


@dataclass(frozen=True)
class RefreshOutcome:
    """The public result of one daily check, safe to log and assert on."""

    state: str
    refreshed: bool = False
    detail: str | None = None
    checked_at: float = 0.0


class BilibiliRefreshClient(Protocol):
    """The seam the refresh service depends on."""

    def refresh(self, cookies: dict[str, str]) -> RefreshPoll:
        """Validate the stored login and refresh it when the platform says so."""


def bilibili_refresh_name() -> str:
    """Configured refresh implementation.

    ``BEEBOX_BILIBILI_REFRESH`` chooses explicitly; when it is unset the login
    implementation is reused, so ``BEEBOX_BILIBILI_LOGIN=fake`` never mixes a
    fake QR login with a real network refresh.
    """
    value = os.environ.get(BILIBILI_REFRESH_ENV, "").strip().lower()
    return value or bilibili_login_name()


def get_refresh_client() -> BilibiliRefreshClient:
    """Build the configured refresh client implementation."""
    name = bilibili_refresh_name()
    if name == BILIBILI_REFRESH_FAKE or name == BILIBILI_LOGIN_FAKE:
        from backend.services.bilibili_login.fake import FakeBilibiliRefreshClient

        return FakeBilibiliRefreshClient.from_environment()
    if name in {BILIBILI_REFRESH_REAL, "bilibili-api", "bilibili"}:
        from backend.services.bilibili_login.refresh_api import BilibiliApiRefreshClient

        return BilibiliApiRefreshClient()
    raise BilibiliRefreshUnavailableError(
        f"Unsupported Bilibili refresh implementation {name!r}; "
        f"expected {BILIBILI_REFRESH_REAL!r} or {BILIBILI_REFRESH_FAKE!r}"
    )


class BilibiliRefreshService:
    """Run one refresh check per interval in a daemon thread."""

    def __init__(
        self,
        *,
        refresh_client_factory: Callable[[], BilibiliRefreshClient] | None = None,
        clock: Callable[[], float] | None = None,
        interval_seconds: float | None = None,
    ) -> None:
        self._client_factory = refresh_client_factory or get_refresh_client
        self._clock = clock or time.time
        self._interval_seconds = max(
            _MIN_TICK_SECONDS,
            float(
                interval_seconds
                if interval_seconds is not None
                else config.get_bilibili_refresh_interval_seconds()
            ),
        )
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_due_at: float | None = None
        self._last: RefreshOutcome | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        """Check once now, then keep checking every interval until stopped."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="beebox-bilibili-refresh",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        tick_seconds = max(_MIN_TICK_SECONDS, min(self._interval_seconds, _MAX_TICK_SECONDS))
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:  # a refresh pass must never kill the server
                logger.exception("Bilibili login refresh pass failed")
            if self._stop_event.wait(tick_seconds):
                return

    # -- scheduled check ---------------------------------------------------
    def tick(self, *, now: float | None = None) -> RefreshOutcome | None:
        """Run a check only when the interval has elapsed; ``None`` otherwise."""
        moment = self._moment(now)
        if self._next_due_at is not None and moment < self._next_due_at:
            return None
        return self.run_once(now=moment)

    def run_once(self, *, now: float | None = None) -> RefreshOutcome:
        """Check the stored login now, regardless of the schedule."""
        moment = self._moment(now)
        with self._lock:
            outcome = self._check(moment)
            self._last = outcome
            self._next_due_at = moment + self._interval_seconds
        return outcome

    @property
    def last_outcome(self) -> RefreshOutcome | None:
        return self._last

    def _moment(self, now: float | None) -> float:
        return float(self._clock() if now is None else now)

    def _check(self, moment: float) -> RefreshOutcome:
        cookies = read_cookies()
        if not cookies:
            return RefreshOutcome(
                state=REFRESH_ABSENT,
                detail="No Bilibili login is stored",
                checked_at=moment,
            )

        poll = self._poll(cookies)
        status = poll.status if poll.status in REFRESH_STATES else REFRESH_FAILED
        if status == REFRESH_VALID:
            # A valid login needs no renewal: leave the credential file exactly
            # as it is, content and mtime. A TV-sourced credential is only ever
            # validity-checked (ADR-0012), and rewriting it on every daily pass
            # would imply a renewal that never happened. The platform did
            # confirm the login, so a stale failed-refresh marker goes away.
            self._clear_invalid()
            return RefreshOutcome(state=REFRESH_VALID, checked_at=moment)
        if status == REFRESH_REFRESHED:
            updated = {
                name: value
                for name, value in (poll.cookies or cookies).items()
                if isinstance(name, str) and isinstance(value, str) and value
            }
            try:
                if not updated:
                    raise ValueError("empty credential")
                write_cookie_file(updated)
            except (OSError, ValueError):
                self._invalidate()
                return RefreshOutcome(
                    state=REFRESH_FAILED,
                    detail="The refreshed Bilibili credential could not be stored",
                    checked_at=moment,
                )
            return RefreshOutcome(
                state=REFRESH_REFRESHED, refreshed=True, checked_at=moment
            )

        self._invalidate()
        detail = poll.detail
        if status == REFRESH_EXPIRED:
            detail = detail or "The Bilibili login is no longer valid"
            logger.warning("Bilibili login refresh reported an invalid login")
        else:
            detail = detail or "The Bilibili login could not be refreshed"
            logger.warning("Bilibili login refresh failed: %s", detail)
        return RefreshOutcome(state=status, detail=detail, checked_at=moment)

    def _poll(self, cookies: dict[str, str]) -> RefreshPoll:
        try:
            client = self._client_factory()
        except BilibiliLoginError as exc:
            return RefreshPoll(status=REFRESH_FAILED, detail=exc.message)
        except Exception:
            return RefreshPoll(
                status=REFRESH_FAILED,
                detail="The Bilibili refresh client could not be created",
            )
        try:
            return client.refresh(dict(cookies))
        except BilibiliLoginError as exc:
            return RefreshPoll(status=REFRESH_FAILED, detail=exc.message)
        except Exception:
            # Never surface an exception body: it may contain a response that
            # includes the credential.
            return RefreshPoll(
                status=REFRESH_FAILED,
                detail="The Bilibili login refresh request failed",
            )

    def _invalidate(self) -> None:
        try:
            mark_login_invalid()
        except OSError:
            logger.error("Could not record the invalid Bilibili login state")

    def _clear_invalid(self) -> None:
        try:
            clear_login_invalid()
        except OSError:
            logger.error("Could not clear the invalid Bilibili login state")


__all__ = [
    "BILIBILI_REFRESH_ENV",
    "BILIBILI_REFRESH_FAKE",
    "BILIBILI_REFRESH_REAL",
    "REFRESH_ABSENT",
    "REFRESH_EXPIRED",
    "REFRESH_FAILED",
    "REFRESH_REFRESHED",
    "REFRESH_STATES",
    "REFRESH_VALID",
    "BilibiliRefreshClient",
    "BilibiliRefreshService",
    "BilibiliRefreshUnavailableError",
    "RefreshOutcome",
    "RefreshPoll",
    "bilibili_refresh_name",
    "get_refresh_client",
]

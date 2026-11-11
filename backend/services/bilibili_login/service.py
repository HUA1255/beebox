"""Single active Bilibili QR-login session (ticket #46).

BeeBox is a single-user service, so at most one QR login is in flight. Starting
again replaces the previous QR code; the console then shows the new one. The
service advances the state when the console polls and writes the credential
file the moment the platform reports ``confirmed``. No response contains the
credential; see :mod:`backend.services.bilibili_login.cookies`.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from backend.services.bilibili_login.client import (
    ACTIVE_LOGIN_STATES,
    LOGIN_CANCELLED,
    LOGIN_CONFIRMED,
    LOGIN_FAILED,
    LOGIN_IDLE,
    LOGIN_STATES,
    BilibiliLoginClient,
    BilibiliLoginError,
    get_login_client,
)
from backend.services.bilibili_login.cookies import LOGIN_SOURCE_TV, write_cookie_file

_COOKIE_WRITE_FAILED_DETAIL = "The Bilibili login credential could not be stored"


@dataclass(frozen=True)
class LoginSnapshot:
    status: str
    detail: str | None = None


class BilibiliLoginService:
    """Own the one in-flight QR login and its state machine."""

    def __init__(self, *, client_factory=get_login_client) -> None:
        self._client_factory = client_factory
        self._lock = threading.Lock()
        self._client: BilibiliLoginClient | None = None
        self._state = LOGIN_IDLE
        self._detail: str | None = None

    def start(self):
        """Generate a fresh QR code, replacing any login already in flight."""
        with self._lock:
            self._discard_client()
            self._state = LOGIN_IDLE
            self._detail = None
            client = self._client_factory()
            try:
                payload = client.start()
            except BaseException:
                client.cancel()
                raise
            self._client = client
            self._state = payload.status
            self._detail = None
            return payload

    def status(self) -> LoginSnapshot:
        """Poll the platform once and project the public state."""
        with self._lock:
            if self._client is None or self._state not in ACTIVE_LOGIN_STATES:
                return self._snapshot()
            try:
                outcome = self._client.poll()
            except BilibiliLoginError as exc:
                self._discard_client()
                self._state = LOGIN_FAILED
                self._detail = exc.message
                return self._snapshot()
            except Exception:
                # Never surface an exception body: it may contain a response
                # that includes the credential.
                self._discard_client()
                self._state = LOGIN_FAILED
                self._detail = "The Bilibili login status could not be checked"
                return self._snapshot()

            if outcome.status in ACTIVE_LOGIN_STATES:
                self._state = outcome.status
                return self._snapshot()
            if outcome.status == LOGIN_CONFIRMED:
                try:
                    # The QR client speaks the TV channel (ADR-0012, ticket
                    # #68); record that provenance so the daily check only
                    # validates this credential instead of sending its
                    # app-scoped refresh token to the web refresh endpoint.
                    write_cookie_file(
                        dict(outcome.cookies or {}), source=LOGIN_SOURCE_TV
                    )
                except (OSError, ValueError):
                    self._discard_client()
                    self._state = LOGIN_FAILED
                    self._detail = _COOKIE_WRITE_FAILED_DETAIL
                    return self._snapshot()
                self._discard_client()
                self._state = LOGIN_CONFIRMED
                self._detail = None
                return self._snapshot()

            self._discard_client()
            self._state = LOGIN_FAILED
            self._detail = outcome.detail or "The Bilibili login failed"
            return self._snapshot()

    def cancel(self) -> LoginSnapshot:
        """Cancel the active login, if any; idempotent."""
        with self._lock:
            if self._state in ACTIVE_LOGIN_STATES or self._client is not None:
                self._discard_client()
                self._state = LOGIN_CANCELLED
                self._detail = None
            return self._snapshot()

    def _snapshot(self) -> LoginSnapshot:
        state = self._state if self._state in LOGIN_STATES else LOGIN_FAILED
        return LoginSnapshot(status=state, detail=self._detail)

    def _discard_client(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.cancel()
        except Exception:
            # Releasing a pending QR code must never fail the request.
            pass


__all__ = [
    "BilibiliLoginService",
    "LoginSnapshot",
]

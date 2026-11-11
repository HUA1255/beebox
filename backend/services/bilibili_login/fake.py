"""Scripted Bilibili login client for tests and smoke runs (ticket #46).

Selection (configuration only, like the fake speech engine and fake video
platform)::

    BEEBOX_BILIBILI_LOGIN=fake
    BEEBOX_BILIBILI_LOGIN_FAKE={"states": ["pending", "scanned", "confirmed"],
                                "cookies": {"SESSDATA": "...", ...}}

``states`` is the sequence of poll results, one per ``GET /v1/bilibili/login``
after the QR code was started; the last entry repeats. ``cookies`` becomes the
credential written to the secrets directory on ``confirmed``. No network is
used. Tests keep these cookie values in the test module that owns them and scan
every response and log record for leaks.
"""

from __future__ import annotations

import json
import os

from backend.services.bilibili_login.client import (
    LOGIN_CONFIRMED,
    LOGIN_FAILED,
    LOGIN_PENDING,
    LOGIN_SCANNED,
    QR_CODE_TTL_SECONDS,
    BilibiliLoginError,
    LoginPoll,
    QRCodePayload,
)
from backend.services.bilibili_login.refresh import (
    REFRESH_EXPIRED,
    REFRESH_FAILED,
    REFRESH_REFRESHED,
    REFRESH_VALID,
    RefreshPoll,
)

FAKE_LOGIN_CONFIG_ENV = "BEEBOX_BILIBILI_LOGIN_FAKE"
FAKE_REFRESH_CONFIG_ENV = "BEEBOX_BILIBILI_REFRESH_FAKE"
FAKE_REFRESH_OUTCOMES = (REFRESH_VALID, REFRESH_REFRESHED, REFRESH_EXPIRED, REFRESH_FAILED)
_FAKE_STATES = (LOGIN_PENDING, LOGIN_SCANNED, LOGIN_CONFIRMED, LOGIN_FAILED)
_DEFAULT_STATES = (LOGIN_PENDING, LOGIN_SCANNED, LOGIN_CONFIRMED)
_DEFAULT_QR_URL = "https://fake.bilibili.test/qr/beebox"
# A real 1x1 PNG so the console receives a displayable image.
_FAKE_QR_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
_DEFAULT_COOKIES = {
    "SESSDATA": "fake-sessdata",
    "bili_jct": "fake-bili-jct",
    "DedeUserID": "10046",
    "ac_time_value": "fake-ac-time-value",
}


class BilibiliLoginConfigError(BilibiliLoginError):
    """The fake login configuration is invalid; it never contains secrets."""

    code = "BILIBILI_LOGIN_UNAVAILABLE"


def _payload_from_environment(env: str = FAKE_LOGIN_CONFIG_ENV) -> dict:
    raw = os.environ.get(env, "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BilibiliLoginConfigError(f"{env} must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise BilibiliLoginConfigError(f"{env} must be a JSON object")
    return payload


class FakeBilibiliLoginClient:
    """Replay a scripted QR login without touching the network."""

    name = "fake"

    def __init__(
        self,
        *,
        states: tuple[str, ...] = _DEFAULT_STATES,
        cookies: dict[str, str] | None = None,
        qr_url: str = _DEFAULT_QR_URL,
        qr_image: str | None = None,
        failure: str = "The fake Bilibili QR code expired",
        expires_in: int = QR_CODE_TTL_SECONDS,
    ) -> None:
        if not states:
            raise BilibiliLoginConfigError("The fake login needs at least one state")
        for state in states:
            if state not in _FAKE_STATES:
                raise BilibiliLoginConfigError(
                    f"Unsupported fake login state {state!r}"
                )
        self._states = tuple(states)
        self._cookies = dict(cookies or _DEFAULT_COOKIES)
        self._qr_url = qr_url
        self._qr_image = qr_image or f"data:image/png;base64,{_FAKE_QR_PNG_BASE64}"
        self._failure = failure
        self._expires_in = expires_in
        self._index = 0

    @classmethod
    def from_environment(cls) -> "FakeBilibiliLoginClient":
        payload = _payload_from_environment()
        states = payload.get("states", list(_DEFAULT_STATES))
        if not isinstance(states, list) or not all(isinstance(item, str) for item in states):
            raise BilibiliLoginConfigError("The fake login 'states' value must be a string list")
        cookies = payload.get("cookies", _DEFAULT_COOKIES)
        if not isinstance(cookies, dict) or not all(
            isinstance(name, str) and isinstance(value, str)
            for name, value in cookies.items()
        ):
            raise BilibiliLoginConfigError("The fake login 'cookies' value must be a string map")
        qr_url = payload.get("qr_url", _DEFAULT_QR_URL)
        qr_image = payload.get("qr_image")
        failure = payload.get("failure", "The fake Bilibili QR code expired")
        expires_in = payload.get("expires_in", QR_CODE_TTL_SECONDS)
        if not isinstance(qr_url, str) or not isinstance(failure, str):
            raise BilibiliLoginConfigError("The fake login URL and failure text must be strings")
        if qr_image is not None and not isinstance(qr_image, str):
            raise BilibiliLoginConfigError("The fake login 'qr_image' value must be a string")
        try:
            expires = int(expires_in)
        except (TypeError, ValueError) as exc:
            raise BilibiliLoginConfigError(
                "The fake login 'expires_in' value must be an integer"
            ) from exc
        return cls(
            states=tuple(str(item).strip().lower() for item in states),
            cookies={str(name): str(value) for name, value in cookies.items()},
            qr_url=qr_url,
            qr_image=qr_image,
            failure=failure,
            expires_in=expires,
        )

    def start(self) -> QRCodePayload:
        self._index = 0
        return QRCodePayload(
            status=LOGIN_PENDING,
            qr_url=self._qr_url,
            qr_image=self._qr_image,
            expires_in=self._expires_in,
        )

    def poll(self) -> LoginPoll:
        state = self._states[min(self._index, len(self._states) - 1)]
        self._index += 1
        if state == LOGIN_CONFIRMED:
            return LoginPoll(status=LOGIN_CONFIRMED, cookies=dict(self._cookies))
        if state == LOGIN_FAILED:
            return LoginPoll(status=LOGIN_FAILED, detail=self._failure)
        return LoginPoll(status=state)

    def cancel(self) -> None:
        self._index = 0


class FakeBilibiliRefreshClient:
    """Replay one daily refresh without touching the network (ticket #47).

    Scripted through ``BEEBOX_BILIBILI_REFRESH_FAKE``::

        {"outcome": "valid" | "refreshed" | "expired" | "failed",
         "cookies": {"SESSDATA": "...", ...},
         "failure": "text shown to the operator"}

    ``valid`` keeps the stored cookies, ``refreshed`` replaces them with the
    configured ones, and ``expired``/``failed`` invalidate the login. The
    default is ``valid`` with the cookies already on disk, so a fake login
    without refresh configuration never rewrites the credential.
    """

    name = "fake"

    def __init__(
        self,
        *,
        outcome: str = REFRESH_VALID,
        cookies: dict[str, str] | None = None,
        failure: str = "The fake Bilibili login refresh failed",
    ) -> None:
        if outcome not in FAKE_REFRESH_OUTCOMES:
            raise BilibiliLoginConfigError(
                f"Unsupported fake refresh outcome {outcome!r}"
            )
        self._outcome = outcome
        self._cookies = dict(cookies) if cookies is not None else None
        self._failure = failure
        self.call_count = 0

    @classmethod
    def from_environment(cls) -> "FakeBilibiliRefreshClient":
        payload = _payload_from_environment(FAKE_REFRESH_CONFIG_ENV)
        outcome = payload.get("outcome", REFRESH_VALID)
        cookies = payload.get("cookies")
        failure = payload.get("failure", "The fake Bilibili login refresh failed")
        if not isinstance(outcome, str):
            raise BilibiliLoginConfigError("The fake refresh 'outcome' value must be a string")
        if cookies is not None and (
            not isinstance(cookies, dict)
            or not all(
                isinstance(name, str) and isinstance(value, str)
                for name, value in cookies.items()
            )
        ):
            raise BilibiliLoginConfigError("The fake refresh 'cookies' value must be a string map")
        if not isinstance(failure, str):
            raise BilibiliLoginConfigError("The fake refresh 'failure' value must be a string")
        return cls(
            outcome=outcome.strip().lower(),
            cookies={str(name): str(value) for name, value in cookies.items()}
            if cookies is not None
            else None,
            failure=failure,
        )

    def refresh(self, cookies: dict[str, str]) -> RefreshPoll:
        self.call_count += 1
        if self._outcome in {REFRESH_VALID, REFRESH_REFRESHED}:
            return RefreshPoll(
                status=self._outcome,
                cookies=dict(self._cookies if self._cookies is not None else cookies),
            )
        if self._outcome == REFRESH_EXPIRED:
            return RefreshPoll(
                status=REFRESH_EXPIRED,
                detail="The fake Bilibili login is no longer valid",
            )
        return RefreshPoll(status=REFRESH_FAILED, detail=self._failure)


__all__ = [
    "FAKE_LOGIN_CONFIG_ENV",
    "FAKE_REFRESH_CONFIG_ENV",
    "FAKE_REFRESH_OUTCOMES",
    "BilibiliLoginConfigError",
    "FakeBilibiliLoginClient",
    "FakeBilibiliRefreshClient",
]

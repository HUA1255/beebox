"""Bearer-token checks shared by the auth middleware and public health.

The middleware in :mod:`backend.app` and the public ``/v1/health`` route must
agree on what "authenticated" means (ADR-0011), so the comparison lives here
once. Resource tickets are a separate, single-use credential and are not part
of this check.
"""

from __future__ import annotations

import os
import secrets

from fastapi import Request

TOKEN_ENV_VAR = "BEEBOX_API_TOKEN"
_BEARER_PREFIX = "Bearer "


def configured_token() -> str:
    """The token the deployment configured, or ``""`` for loopback development."""
    return os.environ.get(TOKEN_ENV_VAR, "").strip()


def supplied_bearer_token(request: Request) -> str:
    """The token from ``Authorization: Bearer ...``, or ``""`` when absent."""
    authorization = request.headers.get("authorization", "")
    if authorization.startswith(_BEARER_PREFIX):
        return authorization[len(_BEARER_PREFIX) :]
    return ""


def authorization_matches(supplied: str, expected: str) -> bool:
    """Constant-time comparison that tolerates non-ASCII header values."""
    return bool(supplied) and secrets.compare_digest(
        supplied.encode("utf-8"), expected.encode("utf-8")
    )


def is_authenticated(request: Request) -> bool:
    """True when the request carries the configured Bearer token.

    Without a configured token the service is loopback development (the
    startup guard refuses a non-loopback bind), so the request counts as
    authenticated exactly as it does in the middleware.
    """
    expected = configured_token()
    if not expected:
        return True
    return authorization_matches(supplied_bearer_token(request), expected)


__all__ = [
    "TOKEN_ENV_VAR",
    "authorization_matches",
    "configured_token",
    "is_authenticated",
    "supplied_bearer_token",
]

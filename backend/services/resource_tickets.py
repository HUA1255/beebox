"""Short-lived, path-scoped tickets for headerless downloads.

A browser or media player that cannot send an ``Authorization`` header asks an
authenticated endpoint (``POST /v1/resource-tickets``) for a ticket, then
presents it as ``resource_ticket`` on the exact path it was issued for. Tickets
are random, kept in memory, single use, valid for 60 seconds and scoped to one
path and method, so they cannot be replayed against another resource
(ADR-0011). Ticket #56 removed the legacy long-lived audio tickets together
with the ``/tasks/{id}/audio`` route.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import secrets
import threading
import time

DOWNLOAD_TICKET_TTL_SECONDS = 60
DOWNLOAD_METHODS = ("GET", "HEAD")
MAX_TICKETS = 256
MAX_PATH_LENGTH = 512


@dataclass(frozen=True)
class ResourceTicketGrant:
    ticket: str
    path: str
    method: str
    expires_at: datetime


@dataclass
class _Ticket:
    path: str
    method: str
    expires_monotonic: float


_lock = threading.Lock()
_tickets: dict[str, _Ticket] = {}


def _purge_expired_locked(now: float) -> None:
    expired = [token for token, item in _tickets.items() if item.expires_monotonic <= now]
    for token in expired:
        _tickets.pop(token, None)


def _validated_scope(path: str, method: str) -> tuple[str, str]:
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or len(path) > MAX_PATH_LENGTH
        or any(character in path for character in "?#")
        or ".." in path
    ):
        raise ValueError("Resource ticket path is invalid")
    if not path.startswith("/v1/"):
        raise ValueError("Download tickets are limited to /v1 paths")
    if method not in DOWNLOAD_METHODS:
        raise ValueError("Download tickets are limited to GET or HEAD")
    return path, method


def issue_download_ticket(path: str, *, method: str = "GET") -> ResourceTicketGrant:
    """Issue the single-use, 60 second ticket used by ``/v1`` downloads."""
    path, method = _validated_scope(path, method)
    token = secrets.token_urlsafe(32)
    now = time.monotonic()
    expires = now + DOWNLOAD_TICKET_TTL_SECONDS
    with _lock:
        _purge_expired_locked(now)
        if len(_tickets) >= MAX_TICKETS:
            oldest = min(_tickets, key=lambda item: _tickets[item].expires_monotonic)
            _tickets.pop(oldest, None)
        _tickets[token] = _Ticket(path=path, method=method, expires_monotonic=expires)
    return ResourceTicketGrant(
        ticket=token,
        path=path,
        method=method,
        expires_at=datetime.now(UTC) + timedelta(seconds=DOWNLOAD_TICKET_TTL_SECONDS),
    )


def consume(ticket: str, path: str, method: str) -> bool:
    """Accept a v1 download ticket exactly once for its path and method."""
    if not ticket:
        return False
    now = time.monotonic()
    with _lock:
        _purge_expired_locked(now)
        item = _tickets.get(ticket)
        if item is None:
            return False
        if item.method != method or item.path != path:
            return False
        _tickets.pop(ticket, None)
        return True


def clear() -> None:
    with _lock:
        _tickets.clear()

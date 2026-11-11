"""``/v1/admin``: write-only operator credentials (ticket #48).

The only credential here is the YouTube cookie jar used for age-restricted
videos. Uploading replaces the stored file; the contents are never returned,
never logged and never written to the database. Reading the status only
reports whether a jar is present plus its size and cookie count.

Every route inherits the ``/v1`` Bearer-token authentication (ADR-0011), so
the same token that operates the queue and models also guards this endpoint.
"""

from __future__ import annotations

from fastapi import APIRouter, File, UploadFile

from backend.services import youtube_cookies
from backend.v1.errors import ApiError, ErrorResponse
from backend.v1.schemas import YouTubeCookieStatus

router = APIRouter(
    prefix="/v1/admin",
    tags=["v1"],
    responses={401: {"model": ErrorResponse}},
)

STAGE = "admin"


def _status() -> YouTubeCookieStatus:
    info = youtube_cookies.cookie_status()
    return YouTubeCookieStatus(
        configured=info.configured,
        size_bytes=info.size_bytes,
        cookie_count=info.cookie_count,
        updated_at=info.updated_at,
    )


@router.post(
    "/youtube-cookie",
    response_model=YouTubeCookieStatus,
    status_code=201,
    responses={400: {"model": ErrorResponse}, 413: {"model": ErrorResponse}},
)
def upload_youtube_cookie(file: UploadFile = File(...)) -> YouTubeCookieStatus:
    """Validate and store a Netscape ``cookies.txt`` without echoing it."""
    content = file.file.read(youtube_cookies.MAX_COOKIE_BYTES + 1)
    try:
        youtube_cookies.store_cookie(content)
    except youtube_cookies.YouTubeCookieTooLarge as exc:
        raise ApiError(413, str(exc), "YOUTUBE_COOKIE_TOO_LARGE", STAGE) from exc
    except youtube_cookies.InvalidYouTubeCookie as exc:
        raise ApiError(400, str(exc), "YOUTUBE_COOKIE_INVALID", STAGE) from exc
    return _status()


@router.get("/youtube-cookie", response_model=YouTubeCookieStatus)
def get_youtube_cookie_status() -> YouTubeCookieStatus:
    """Report whether a cookie jar is stored; never return its contents."""
    return _status()


__all__ = ["router"]

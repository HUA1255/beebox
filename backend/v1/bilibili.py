"""``/v1/bilibili/login``: QR sign-in and the write-only credential (ticket #46).

The console starts a login, renders the returned QR code and polls until the
status is ``confirmed``. The service then writes the cookie file in the secrets
directory; no response here contains a cookie name, value or file content. The
daily refresh loop is ticket #47.

Every route inherits the Bearer authentication applied to the whole ``/v1``
surface, so only the operator can start or cancel a login.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from backend.services.bilibili_login import (
    BilibiliLoginError,
    BilibiliLoginService,
    BilibiliLoginUnavailableError,
)
from backend.v1.errors import ApiError, ErrorResponse
from backend.v1.schemas import BilibiliLoginStartResponse, BilibiliLoginStatusResponse

STAGE_BILIBILI_LOGIN = "bilibili_login"

router = APIRouter(
    prefix="/v1/bilibili",
    tags=["v1"],
    responses={401: {"model": ErrorResponse}},
)


def get_login_service(request: Request) -> BilibiliLoginService:
    """Return the app-scoped login service, creating it on first use."""
    service = getattr(request.app.state, "bilibili_login_service", None)
    if service is None:
        service = BilibiliLoginService()
        request.app.state.bilibili_login_service = service
    return service


@router.post(
    "/login",
    response_model=BilibiliLoginStartResponse,
    status_code=201,
    summary="Start a Bilibili QR login and return the QR code",
)
def start_login(request: Request) -> BilibiliLoginStartResponse:
    try:
        payload = get_login_service(request).start()
    except BilibiliLoginUnavailableError as exc:
        raise ApiError(503, exc.message, exc.code, STAGE_BILIBILI_LOGIN) from exc
    except BilibiliLoginError as exc:
        raise ApiError(502, exc.message, exc.code, STAGE_BILIBILI_LOGIN) from exc
    return BilibiliLoginStartResponse(
        status="pending",
        qr_url=payload.qr_url,
        qr_image=payload.qr_image,
        expires_in=payload.expires_in,
    )


@router.get(
    "/login",
    response_model=BilibiliLoginStatusResponse,
    summary="Poll the Bilibili QR login status",
)
def login_status(request: Request) -> BilibiliLoginStatusResponse:
    snapshot = get_login_service(request).status()
    return BilibiliLoginStatusResponse(status=snapshot.status, detail=snapshot.detail)


@router.post(
    "/login/cancel",
    response_model=BilibiliLoginStatusResponse,
    summary="Cancel the in-flight Bilibili QR login",
)
def cancel_login(request: Request) -> BilibiliLoginStatusResponse:
    snapshot = get_login_service(request).cancel()
    return BilibiliLoginStatusResponse(status=snapshot.status, detail=snapshot.detail)


__all__ = ["get_login_service", "router"]

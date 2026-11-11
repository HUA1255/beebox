"""``/v1/resource-tickets``: credentials for headerless downloads.

Some download clients (a browser tab, an ``<audio>`` element) cannot attach an
``Authorization`` header. They authenticate once, ask for a ticket scoped to
the exact path and method they are about to fetch, and present it as
``?resource_ticket=...``. The ticket lives 60 seconds and is accepted once
(ADR-0011).
"""

from __future__ import annotations

from fastapi import APIRouter

from backend.services import resource_tickets
from backend.v1.errors import STAGE_AUTH, ApiError, ErrorResponse
from backend.v1.schemas import DownloadTicketRequest, DownloadTicketResponse

router = APIRouter(
    prefix="/v1/resource-tickets",
    tags=["v1"],
    responses={401: {"model": ErrorResponse}},
)


@router.post(
    "",
    response_model=DownloadTicketResponse,
    responses={400: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def create_resource_ticket(request: DownloadTicketRequest) -> DownloadTicketResponse:
    try:
        grant = resource_tickets.issue_download_ticket(request.path, method=request.method)
    except ValueError as exc:
        raise ApiError(400, str(exc), "RESOURCE_TICKET_PATH_INVALID", STAGE_AUTH) from exc
    return DownloadTicketResponse(
        ticket=grant.ticket,
        path=grant.path,
        method=grant.method,
        expires_at=grant.expires_at,
    )

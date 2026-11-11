"""Unified error shape and error-code catalog for ``/v1`` (ticket #38).

Every v1 error serialises as ``{detail, error_code, stage}`` with an
uppercase, underscore-separated ``error_code`` (ADR-0009). Request validation,
authentication, routing misses and unexpected failures are rendered here too,
so callers never have to branch on an array or on a bare ``{"detail": ...}``.

``GET /v1/errors`` is the documented catalog: it lists every code the API can
raise, HTTP codes with their status, and pipeline failure codes that travel in
a job's ``error_code`` with ``http_status = null``.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.services.errors import ERROR_CODES

STAGE_AUTH = "auth"
STAGE_VALIDATION = "validation"
STAGE_ROUTING = "routing"
STAGE_SERVER = "server"

# HTTP-level codes: (status, meaning). ``None`` means the code is only ever
# carried inside a job payload rather than returned as an HTTP error.
_HTTP_CODES: dict[str, tuple[int | None, str]] = {
    "INVALID_REQUEST": (422, "The request body, query or path parameters failed validation."),
    "AUTH_REQUIRED": (401, "The request is missing the Authorization: Bearer header."),
    "AUTH_INVALID_TOKEN": (401, "The supplied Bearer token does not match BEEBOX_API_TOKEN."),
    "AUTH_QUERY_TOKEN_REJECTED": (
        401,
        "Tokens are not accepted in the query string; send Authorization: Bearer instead.",
    ),
    "AUTH_INVALID_RESOURCE_TICKET": (
        401,
        "The resource ticket is unknown, expired, already used, or scoped to another path or method.",
    ),
    "RESOURCE_TICKET_PATH_INVALID": (400, "A resource ticket can only be issued for a v1 download path."),
    "NOT_FOUND": (404, "No route matches this path."),
    "METHOD_NOT_ALLOWED": (405, "The route exists but does not accept this HTTP method."),
    "INTERNAL_ERROR": (500, "The server failed unexpectedly; details are in the server log."),
    "FILE_NOT_FOUND": (404, "The requested file does not exist or its content is no longer stored."),
    "FILE_TOO_LARGE": (413, "The upload exceeds the configured maximum file size."),
    "EMPTY_FILE": (400, "The uploaded file contains no bytes."),
    "FILE_IN_USE": (409, "The file is referenced by a queued or running job and cannot be deleted yet."),
    "YOUTUBE_COOKIE_INVALID": (
        400,
        "The uploaded YouTube cookie file is not a Netscape cookies.txt export.",
    ),
    "YOUTUBE_COOKIE_TOO_LARGE": (
        413,
        "The uploaded YouTube cookie file exceeds the 1 MiB limit.",
    ),
    "UNSUPPORTED_KIND": (400, "The requested job kind is not supported in v1."),
    "UNSUPPORTED_SOURCE_TYPE": (400, "The requested source type is not supported in v1."),
    "INVALID_SOURCE": (400, "The source is missing a field the chosen source type requires."),
    "INVALID_URL": (400, "The video link is not a valid absolute URL."),
    "UNSUPPORTED_URL": (
        400,
        "The link is not a supported single Bilibili or YouTube video "
        "(other hosts, playlists, collections and live streams are refused).",
    ),
    "MODEL_NOT_FOUND": (400, "The requested model is not present in the model registry."),
    "MODEL_COMPATIBILITY_FAILED": (409, "The model cannot run on the detected runtime."),
    "MODEL_ALREADY_DOWNLOADED": (409, "The model is already downloaded and complete."),
    "MODEL_DOWNLOAD_IN_PROGRESS": (409, "The model download is already running."),
    "MODEL_NOT_DOWNLOADABLE": (409, "The model cannot be downloaded in this deployment."),
    "MODEL_NOT_DOWNLOADING": (409, "There is no active download for the model to cancel."),
    "JOB_NOT_FOUND": (404, "The requested job does not exist."),
    "JOB_ACTIVE": (409, "The job is queued or running and cannot be deleted yet."),
    "JOB_NOT_CANCELLABLE": (409, "Only queued or running jobs can be cancelled."),
    "JOB_NOT_RETRYABLE": (409, "The job did not fail in a way that can be resumed or retried."),
    "JOB_CREATION_FAILED": (500, "The job row could not be read back after creation."),
    "JOB_NOT_SUCCEEDED": (409, "The job has not succeeded yet, so its result or exports are unavailable."),
    "UNSUPPORTED_FORMAT": (400, "The requested export format is not supported."),
    "BILIBILI_LOGIN_UNAVAILABLE": (
        503,
        "The configured Bilibili login implementation cannot be loaded.",
    ),
    "BILIBILI_LOGIN_START_FAILED": (
        502,
        "The Bilibili login service did not return a QR code.",
    ),
    "BILIBILI_LOGIN_FAILED": (
        502,
        "The Bilibili login service failed for an unclassified reason.",
    ),
}

# Codes carried in a job's ``error_code`` that have no dedicated mapping in
# :mod:`backend.services.errors`.
_JOB_CODES: dict[str, str] = {
    "SPEECH_ENGINE_INVALID": "The configured speech engine name is not supported.",
    "SPEECH_ENGINE_CONFIG_INVALID": "The scripted/config speech engine payload is invalid.",
    "SPEECH_ENGINE_FAILED": "The speech engine failed while transcribing or diarizing.",
    "INVALID_TRANSCRIPTION_BACKEND": "The configured transcription backend is not supported.",
    "TASK_FAILED": "The job failed without a more specific error code.",
    "TASK_INTERRUPTED": "The job was interrupted by a server restart.",
    "TASK_RESUME_FAILED": "The queued job could not be resumed after the server restart.",
    "LOCAL_WORKER_FAILED": "The local transcription worker process failed.",
    "LOCAL_WORKER_EXITED": "The local transcription worker process exited before reporting a result.",
    "LOCAL_WORKER_PROTOCOL_ERROR": "The local transcription worker returned an invalid response.",
    "CHUNK_AUDIO_MISSING": "A chunk's audio file is missing and could not be regenerated.",
    "MEDIA_IMPORT_FAILED": "Media could not be imported for processing.",
    "WORKER_RUNTIME_ERROR": "The task worker raised an unexpected error.",
    "LANGUAGE_REQUIRED": "The model requires an explicit language for this request.",
}

#: ``code -> (http_status, meaning)`` for every code the public API documents.
ERROR_CATALOG: dict[str, tuple[int | None, str]] = {
    code: (None, meaning) for code, meaning in ERROR_CODES.items()
}
ERROR_CATALOG.update({code: (None, meaning) for code, meaning in _JOB_CODES.items()})
ERROR_CATALOG.update(_HTTP_CODES)


class ApiError(Exception):
    """A v1 error rendered as ``{detail, error_code, stage}``."""

    def __init__(self, status_code: int, detail: str, error_code: str, stage: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.error_code = error_code
        self.stage = stage


class ErrorResponse(BaseModel):
    """The documented shape of every v1 error."""

    detail: str
    error_code: str = Field(description="Uppercase, underscore-separated machine code")
    stage: str = Field(description="Operation stage that produced the error")


class ErrorCodeEntry(BaseModel):
    code: str = Field(description="Uppercase, underscore-separated machine code")
    http_status: int | None = Field(
        default=None,
        description="HTTP status when the code is returned as an error; null for job-level failure codes",
    )
    meaning: str = Field(description="Human-readable description of when the code occurs")


class ErrorCatalogResponse(BaseModel):
    items: list[ErrorCodeEntry]
    total: int


def error_response(
    status_code: int,
    detail: str,
    error_code: str,
    stage: str,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Render the one v1 error shape, with optional transport headers."""
    return JSONResponse(
        {"detail": detail, "error_code": error_code, "stage": stage},
        status_code=status_code,
        headers=headers,
    )


router = APIRouter(prefix="/v1", tags=["v1"], responses={401: {"model": ErrorResponse}})


@router.get(
    "/errors",
    response_model=ErrorCatalogResponse,
    summary="List every error code with its meaning",
)
def list_error_codes() -> ErrorCatalogResponse:
    items = [
        ErrorCodeEntry(code=code, http_status=http_status, meaning=meaning)
        for code, (http_status, meaning) in sorted(ERROR_CATALOG.items())
    ]
    return ErrorCatalogResponse(items=items, total=len(items))


__all__ = [
    "ApiError",
    "ERROR_CATALOG",
    "ErrorCodeEntry",
    "ErrorCatalogResponse",
    "ErrorResponse",
    "STAGE_AUTH",
    "STAGE_ROUTING",
    "STAGE_SERVER",
    "STAGE_VALIDATION",
    "error_response",
    "router",
]

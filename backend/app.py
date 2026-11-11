from __future__ import annotations

from contextlib import asynccontextmanager
import math
from urllib.parse import parse_qsl, urlencode

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend import __version__, config
from backend.console import is_console_path, register_console
from backend.database import init_db
from backend.database import session as db_session
from backend.services import bilibili_login
from backend.services import resource_tickets
from backend.services import retention
from backend.services.tasks import mark_interrupted_tasks, resume_queued_tasks
from backend.services.errors import BeeBoxError
from backend.v1 import register_v1_routes
from backend.v1.auth import is_authenticated, supplied_bearer_token
from backend.v1.errors import (
    STAGE_AUTH,
    STAGE_ROUTING,
    STAGE_SERVER,
    STAGE_VALIDATION,
    error_response,
)

# ``/v1/health`` is the only API endpoint that answers without a token.
PUBLIC_PATHS = frozenset({"/v1/health"})


def _is_v1_path(path: str) -> bool:
    return path == "/v1" or path.startswith("/v1/")


def _query_credentials(request: Request) -> tuple[str, str]:
    """Strip ``api_token``/``resource_ticket`` from the query string.

    Removing them before routing keeps credentials out of access logs and out
    of any handler that echoes query parameters (ADR-0011).
    """
    query_items = parse_qsl(
        request.scope.get("query_string", b"").decode("utf-8", errors="replace"),
        keep_blank_values=True,
    )
    supplied_query_token = next((value for key, value in query_items if key == "api_token"), "")
    supplied_resource_ticket = next(
        (value for key, value in query_items if key == "resource_ticket"),
        "",
    )
    request.scope["query_string"] = urlencode(
        [(key, value) for key, value in query_items if key not in {"api_token", "resource_ticket"}],
        doseq=True,
    ).encode("utf-8")
    return supplied_query_token, supplied_resource_ticket


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config.configure_model_cache_environment()
    init_db()
    _mark_interrupted_tasks()
    # The GPU queue is in-memory: adopt the jobs that were waiting when the
    # previous process stopped so they keep their queued order (ticket #64).
    _resume_queued_tasks()
    from backend.services import platform as platform_service

    # Warm the (subprocess-based) runtime probe before serving requests so
    # /v1/health reports the real GPU state immediately after startup.
    try:
        platform_service.runtime_probe_snapshot()
    except Exception:
        pass
    cleanup = retention.RetentionService()
    cleanup.start()
    # ADR-0012: check the Bilibili login once a day and refresh the credential
    # file when the platform says it is due. A failed refresh flips health to
    # expired; silently serving lower-quality/no-subtitle results is not an
    # option.
    bilibili_refresh = bilibili_login.BilibiliRefreshService()
    bilibili_refresh.start()
    _app.state.bilibili_refresh_service = bilibili_refresh
    try:
        yield
    finally:
        bilibili_refresh.stop()
        cleanup.stop()


def create_app() -> FastAPI:
    # The dev instance must not even start when one of its writable paths
    # points at production (ADR-0010, ticket #55).
    config.validate_dev_instance_paths()
    app = FastAPI(
        title="BeeBox API",
        description="BeeBox local voice service API",
        version=__version__,
        lifespan=lifespan,
        # The service exposes /v1 and /console only (ticket #56); the
        # committed openapi.json snapshot is the contract of record.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.exception_handler(BeeBoxError)
    async def handle_beebox_error(_request: Request, exc: BeeBoxError):
        return JSONResponse(
            {"detail": exc.message, "error_code": exc.code, "stage": exc.stage},
            status_code=409,
        )

    from backend.v1.errors import ApiError

    @app.exception_handler(ApiError)
    async def handle_v1_api_error(_request: Request, exc: ApiError):
        return JSONResponse(
            {"detail": exc.detail, "error_code": exc.error_code, "stage": exc.stage},
            status_code=exc.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(_request: Request, exc: RequestValidationError):
        def json_safe(value):
            if isinstance(value, float) and not math.isfinite(value):
                return str(value)
            if isinstance(value, dict):
                return {key: json_safe(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [json_safe(item) for item in value]
            return value

        if _is_v1_path(_request.url.path):
            # Every v1 error is {detail, error_code, stage} with a string
            # detail; callers never branch on a pydantic error array.
            first = exc.errors()[0] if exc.errors() else {}
            location = ".".join(str(item) for item in first.get("loc", ()))
            message = first.get("msg", "Invalid request")
            detail = f"{location}: {message}" if location else message
            return error_response(422, detail, "INVALID_REQUEST", STAGE_VALIDATION)
        return JSONResponse({"detail": json_safe(exc.errors())}, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(_request: Request, exc: StarletteHTTPException):
        if not _is_v1_path(_request.url.path):
            # Missing console assets and any retired path use the default
            # Starlette shape.
            return JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
                headers=exc.headers,
            )
        if exc.status_code == 404:
            error_code, stage = "NOT_FOUND", STAGE_ROUTING
        elif exc.status_code == 405:
            error_code, stage = "METHOD_NOT_ALLOWED", STAGE_ROUTING
        elif exc.status_code == 401:
            error_code, stage = "AUTH_REQUIRED", STAGE_AUTH
        else:
            error_code, stage = "INVALID_REQUEST", STAGE_ROUTING
        detail = exc.detail if isinstance(exc.detail, str) and exc.detail else "Request failed"
        return error_response(exc.status_code, detail, error_code, stage, headers=exc.headers)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_request: Request, exc: Exception):
        if not _is_v1_path(_request.url.path):
            # Same response the default ServerErrorMiddleware produces.
            return PlainTextResponse("Internal Server Error", status_code=500)
        # Never leak the exception message or traceback to an API caller.
        return error_response(500, "Internal server error", "INTERNAL_ERROR", STAGE_SERVER)

    @app.middleware("http")
    async def authenticate_request(request: Request, call_next):
        path = request.url.path
        supplied_query_token, supplied_resource_ticket = _query_credentials(request)

        if request.method == "OPTIONS":
            return await call_next(request)

        if not _is_v1_path(path):
            # Retired API paths and the console are not behind the /v1
            # Bearer check; routing decides between the console shell and 404.
            return await call_next(request)

        if supplied_query_token:
            # Not honoured even when the value is correct: credentials in URLs
            # end up in logs, history and proxies (ADR-0011).
            return error_response(
                401,
                "Query-string tokens are not accepted; send Authorization: Bearer instead",
                "AUTH_QUERY_TOKEN_REJECTED",
                STAGE_AUTH,
                headers={"WWW-Authenticate": "Bearer"},
            )

        if path in PUBLIC_PATHS:
            return await call_next(request)

        if is_authenticated(request):
            # The configured token matched, or loopback development has none:
            # there is nothing to compare (the startup guard refuses
            # non-loopback binds without a token).
            return await call_next(request)

        header_token = supplied_bearer_token(request)
        if not header_token and supplied_resource_ticket:
            if resource_tickets.consume(supplied_resource_ticket, path, request.method):
                return await call_next(request)
            return error_response(
                401,
                "The resource ticket is invalid, expired, already used, "
                "or scoped to another path or method",
                "AUTH_INVALID_RESOURCE_TICKET",
                STAGE_AUTH,
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not header_token:
            return error_response(
                401,
                "Authorization: Bearer <BEEBOX_API_TOKEN> is required",
                "AUTH_REQUIRED",
                STAGE_AUTH,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return error_response(
            401,
            "The supplied Bearer token is invalid",
            "AUTH_INVALID_TOKEN",
            STAGE_AUTH,
            headers={"WWW-Authenticate": "Bearer"},
        )

    register_v1_routes(app)
    register_console(app)

    return app


def _mark_interrupted_tasks() -> None:
    db = db_session.SessionLocal()
    try:
        mark_interrupted_tasks(db)
    finally:
        db.close()


def _resume_queued_tasks() -> None:
    db = db_session.SessionLocal()
    try:
        resume_queued_tasks(db)
    finally:
        db.close()


app = create_app()

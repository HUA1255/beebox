"""Ticket #56: the service exposes only ``/v1`` plus the console.

The legacy BeeBox routers were deleted when the home cutover landed. These
tests pin that surface: no non-``/v1`` API route is registered, the OpenAPI
contract contains only ``/v1`` paths, and every retired path answers 404.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient


def make_client(tmp_path: Path) -> TestClient:
    os.environ["BEEBOX_DATA_DIR"] = str(tmp_path)
    from backend.app import create_app

    return TestClient(create_app())


def _registered_routes(app) -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()

    def visit(items) -> None:
        for route in items:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if path and methods:
                routes.update((method, path) for method in methods)
            nested = getattr(route, "routes", None)
            if nested:
                visit(nested)
            original_router = getattr(route, "original_router", None)
            if original_router is not None:
                visit(original_router.routes)

    visit(app.routes)
    return routes


# The routes the deleted legacy surface used to serve. Every one of them must
# be gone; the paths double as a regression list for accidental re-mounts.
RETIRED_ROUTES = (
    "/",
    "/health",
    "/health/filesystem",
    "/api-info",
    "/auth/resource-ticket",
    "/diagnostics/error-codes",
    "/events",
    "/runtime/status",
    "/runtime/health-report",
    "/runtime/diagnostic-bundle.zip",
    "/models/status",
    "/models/aux-status",
    "/models/cache-dir",
    "/models/download",
    "/models/verify",
    "/models/active-downloads",
    "/models/storage",
    "/models/cleanup-incomplete",
    "/models/progress/whisper-base",
    "/models/whisper-base",
    "/models/whisper-base/compatibility",
    "/models/whisper-base/unload",
    "/models/whisper-base/cancel-download",
    "/models/whisper-base/stop-download",
    "/models/whisper-base/retry-download",
    "/models/whisper-base/redownload",
    "/settings/asr",
    "/settings/media-storage",
    "/tasks",
    "/tasks/active",
    "/tasks/task-1",
    "/tasks/task-1/audio",
    "/tasks/task-1/cancel",
    "/tasks/task-1/chunks",
    "/tasks/task-1/chunks/1/retry",
    "/tasks/task-1/chunks/retry-failed",
    "/tasks/task-1/diagnostics",
    "/tasks/task-1/events",
    "/tasks/task-1/export/txt",
    "/tasks/task-1/logs",
    "/tasks/task-1/retranscribe",
    "/tasks/task-1/retry",
    "/tasks/task-1/speakers",
    "/transcriptions",
    "/transcriptions/batch",
    "/transcriptions/preflight",
    "/transcriptions/readiness",
)


def test_only_v1_and_console_routes_are_registered(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    routes = _registered_routes(client.app)

    leaked = sorted(
        (method, path)
        for method, path in routes
        if not (path == "/v1" or path.startswith("/v1/") or path.startswith("/console"))
    )
    assert leaked == [], f"non-/v1 routes are still registered: {leaked}"


def test_retired_legacy_paths_are_not_served(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    for path in RETIRED_ROUTES:
        response = client.get(path)
        assert response.status_code == 404, f"GET {path} is still served ({response.status_code})"


def test_openapi_contract_contains_only_v1_paths(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    paths = client.app.openapi()["paths"]

    leaked = sorted(path for path in paths if not path.startswith("/v1/"))
    assert leaked == [], f"non-/v1 paths are still documented: {leaked}"


def test_v1_contract_routes_stay_registered(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    routes = _registered_routes(client.app)

    expected = {
        ("GET", "/v1/health"),
        ("GET", "/v1/errors"),
        ("POST", "/v1/admin/youtube-cookie"),
        ("GET", "/v1/admin/youtube-cookie"),
        ("GET", "/v1/bilibili/login"),
        ("POST", "/v1/bilibili/login"),
        ("POST", "/v1/bilibili/login/cancel"),
        ("POST", "/v1/files"),
        ("GET", "/v1/files"),
        ("GET", "/v1/files/{file_id}"),
        ("GET", "/v1/files/{file_id}/content"),
        ("DELETE", "/v1/files/{file_id}"),
        ("POST", "/v1/jobs"),
        ("GET", "/v1/jobs"),
        ("GET", "/v1/jobs/{job_id}"),
        ("DELETE", "/v1/jobs/{job_id}"),
        ("GET", "/v1/jobs/{job_id}/result"),
        ("GET", "/v1/jobs/{job_id}/exports/{format}"),
        ("POST", "/v1/jobs/{job_id}/cancel"),
        ("POST", "/v1/jobs/{job_id}/retry"),
        ("GET", "/v1/jobs/{job_id}/events"),
        ("GET", "/v1/models"),
        ("GET", "/v1/models/{model_id}"),
        ("POST", "/v1/models/{model_id}/download"),
        ("POST", "/v1/models/{model_id}/download/cancel"),
        ("POST", "/v1/models/{model_id}/download/retry"),
        ("POST", "/v1/models/{model_id}/unload"),
        ("DELETE", "/v1/models/{model_id}"),
        ("GET", "/v1/queue"),
        ("POST", "/v1/queue/pause"),
        ("POST", "/v1/queue/resume"),
        ("POST", "/v1/resource-tickets"),
    }

    missing = expected - routes
    assert not missing, f"v1 contract routes are missing: {sorted(missing)}"


def test_fresh_database_contains_only_retained_tables(tmp_path: Path) -> None:
    """An empty data directory must not recreate removed feature tables."""
    make_client(tmp_path)
    from backend.database import init_db

    init_db()

    with sqlite3.connect(tmp_path / "beebox.db") as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

    assert tables == {
        "asr_settings",
        "schema_migrations",
        "task_diagnostics",
        "task_logs",
        "transcript_segments",
        "transcription_chunks",
        "transcription_tasks",
        # Ticket #36: uploaded /v1 file metadata.
        "v1_files",
    }

"""The committed OpenAPI snapshot must match the app (ticket #36).

The snapshot is the contract of record (ADR-0009). Any route, schema or
response-model change must regenerate it; this test is what makes a stale
snapshot fail CI.
"""

from __future__ import annotations

import json
from pathlib import Path

from backend.app import create_app

SNAPSHOT_PATH = Path(__file__).resolve().parents[2] / "openapi.json"
REGENERATE_COMMAND = "python scripts/export_openapi.py"


def load_snapshot() -> dict:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def test_committed_openapi_snapshot_exists() -> None:
    assert SNAPSHOT_PATH.is_file(), (
        f"{SNAPSHOT_PATH} is missing; regenerate it with `{REGENERATE_COMMAND}`"
    )


def test_openapi_snapshot_matches_the_app_contract() -> None:
    snapshot = load_snapshot()

    assert create_app().openapi() == snapshot, (
        "openapi.json is stale: the app contract changed but the snapshot was "
        f"not regenerated. Run `{REGENERATE_COMMAND}` and commit openapi.json."
    )


def test_openapi_snapshot_documents_the_v1_surface() -> None:
    paths = load_snapshot()["paths"]

    expected = {
        "/v1/health",
        "/v1/errors",
        "/v1/admin/youtube-cookie",
        "/v1/bilibili/login",
        "/v1/bilibili/login/cancel",
        "/v1/files",
        "/v1/files/{file_id}",
        "/v1/files/{file_id}/content",
        "/v1/jobs",
        "/v1/jobs/{job_id}",
        "/v1/jobs/{job_id}/result",
        "/v1/jobs/{job_id}/exports/{format}",
        "/v1/jobs/{job_id}/cancel",
        "/v1/jobs/{job_id}/retry",
        "/v1/jobs/{job_id}/events",
        "/v1/models",
        "/v1/models/{model_id}",
        "/v1/models/{model_id}/download",
        "/v1/models/{model_id}/download/cancel",
        "/v1/models/{model_id}/download/retry",
        "/v1/models/{model_id}/unload",
        "/v1/resource-tickets",
    }
    assert expected <= set(paths)
    assert {"delete"} <= set(paths["/v1/jobs/{job_id}"])


def test_openapi_snapshot_contains_only_v1_paths() -> None:
    paths = load_snapshot()["paths"]

    leaked = sorted(path for path in paths if not path.startswith("/v1/"))
    assert leaked == [], f"the legacy surface is still documented: {leaked}"

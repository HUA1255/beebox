#!/usr/bin/env python3
"""Regenerate the committed ``openapi.json`` contract snapshot.

Usage (from the repository root)::

    uv run python scripts/export_openapi.py
    # or, with the repository venv:
    .venv/Scripts/python.exe scripts/export_openapi.py

The snapshot is the contract of record (ADR-0009). ``backend/tests/
test_openapi_snapshot.py`` fails whenever the running app's schema differs
from this file, so any route/schema change must run this script and commit the
result. The output is deterministic: sorted keys, UTF-8, two-space indent and
exactly one trailing newline.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app import create_app  # noqa: E402  (path bootstrap must run first)

SNAPSHOT_PATH = REPO_ROOT / "openapi.json"


def render_snapshot(schema: dict) -> str:
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    SNAPSHOT_PATH.write_text(
        render_snapshot(create_app().openapi()),
        encoding="utf-8",
        # Keep the committed contract LF regardless of the host OS.
        newline="\n",
    )
    print(f"Wrote {SNAPSHOT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

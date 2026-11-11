"""Serve the operations console from BeeBox itself (ticket #49).

The console is a separate Vite/React single-page application in ``console/``.
Its built assets are mounted at ``/console`` on the same port as ``/v1``
(ADR-0010), so an operator never needs a second web server.

Two paths are served:

* ``/console`` and ``/console/<asset>`` return the matching file from the
  build directory;
* every other ``/console/...`` path falls back to ``index.html`` so client-side
  routes such as ``/console/jobs/<id>`` work after a reload.

``BEEBOX_CONSOLE_DIR`` overrides the build directory (the Windows installer
copies ``console/dist`` next to the app and points at it); a checkout uses
``<repository>/console/dist``. Console assets are public: the token is entered
in the browser, and every ``/v1`` call still requires it. Without a build the
root path answers 404 with the build command instead of a stack trace.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, PlainTextResponse, Response

CONSOLE_DIR_ENV = "BEEBOX_CONSOLE_DIR"
CONSOLE_PATH_PREFIX = "/console"

_BUILD_HINT = (
    "BeeBox console is not built. Run `npm ci && npm run build` in the "
    "console/ directory, or point BEEBOX_CONSOLE_DIR at a build."
)


def is_console_path(path: str) -> bool:
    """True for ``/console`` and everything below it (public static assets)."""
    return path == CONSOLE_PATH_PREFIX or path.startswith(f"{CONSOLE_PATH_PREFIX}/")


def console_dist_dir() -> Path | None:
    """Return the console build directory, or ``None`` when it is absent."""
    override = os.environ.get(CONSOLE_DIR_ENV, "").strip()
    candidate = (
        Path(override).expanduser()
        if override
        else Path(__file__).resolve().parents[1] / "console" / "dist"
    )
    try:
        return candidate.resolve() if candidate.is_dir() else None
    except OSError:
        return None


def _not_built() -> Response:
    return PlainTextResponse(_BUILD_HINT, status_code=404)


def _safe_asset(root: Path, relative: str) -> Path | None:
    """Resolve ``relative`` below ``root``; ``None`` when it escapes."""
    try:
        candidate = (root / relative).resolve()
    except (OSError, ValueError):
        return None
    if candidate != root and root not in candidate.parents:
        return None
    return candidate


def _console_response(relative: str) -> Response:
    root = console_dist_dir()
    if root is None:
        return _not_built()

    if relative:
        candidate = _safe_asset(root, relative)
        if candidate is None:
            return Response(status_code=404)
        if candidate.is_file():
            return FileResponse(candidate)

    index = root / "index.html"
    if not index.is_file():
        return _not_built()
    return FileResponse(
        index,
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


def register_console(app: FastAPI) -> None:
    """Mount ``/console``; called once from :func:`backend.app.create_app`."""

    @app.get(CONSOLE_PATH_PREFIX, include_in_schema=False)
    def console_root() -> Response:
        return _console_response("")

    @app.get(f"{CONSOLE_PATH_PREFIX}/{{relative_path:path}}", include_in_schema=False)
    def console_asset(relative_path: str) -> Response:
        return _console_response(relative_path)


__all__ = [
    "CONSOLE_DIR_ENV",
    "CONSOLE_PATH_PREFIX",
    "console_dist_dir",
    "is_console_path",
    "register_console",
]

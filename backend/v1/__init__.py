"""The public ``/v1`` API surface.

The v1 surface is a thin, stable contract over the existing task pipeline:
files are stored once, jobs reference them, and every job is projected through
:mod:`backend.v1.status` so internal task states never leak. Ticket #56 removed
the legacy routers, so ``/v1`` (plus the ``/console`` static build) is the
entire service surface.
"""

from __future__ import annotations

from fastapi import FastAPI


def register_v1_routes(app: FastAPI) -> None:
    """Mount the complete ``/v1`` surface."""
    from backend.v1.admin import router as admin_router
    from backend.v1.bilibili import router as bilibili_router
    from backend.v1.errors import router as errors_router
    from backend.v1.files import router as files_router
    from backend.v1.health import router as health_router
    from backend.v1.jobs import router as jobs_router
    from backend.v1.models import router as models_router
    from backend.v1.queue import router as queue_router
    from backend.v1.tickets import router as tickets_router

    app.include_router(health_router)
    app.include_router(bilibili_router)
    app.include_router(errors_router)
    app.include_router(admin_router)
    app.include_router(files_router)
    app.include_router(jobs_router)
    app.include_router(models_router)
    app.include_router(queue_router)
    app.include_router(tickets_router)

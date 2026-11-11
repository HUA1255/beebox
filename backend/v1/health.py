"""``/v1/health``: liveness, readiness, version and build commit.

The commit comes from ``BEEBOX_GIT_COMMIT`` when the deployment sets it (the
Windows service does) and otherwise from ``git rev-parse HEAD``. A checkout
without ``.git`` must still answer, so every failure path degrades to ``None``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from fastapi import APIRouter, Request
from sqlalchemy import text

from backend import __version__, config
from backend.database import session as db_session
from backend.services import bilibili_login
from backend.services import gpu_queue
from backend.services import platform as platform_service
from backend.services import video_platform
from backend.services.process_utils import no_window_kwargs
from backend.v1.auth import is_authenticated
from backend.v1.schemas import GpuState, HealthResponse, QueueState

router = APIRouter(prefix="/v1", tags=["v1"])

COMMIT_ENV_VARS = ("BEEBOX_GIT_COMMIT", "GIT_COMMIT")
_GIT_TIMEOUT_SECONDS = 2


def resolve_commit() -> str | None:
    """Return the deployment commit, or ``None`` when it cannot be determined."""
    for name in COMMIT_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    repository_root = Path(__file__).resolve().parents[2]
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            **no_window_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _database_ready() -> bool:
    try:
        db_session.init_db()
        db = db_session.SessionLocal()
        try:
            db.execute(text("SELECT 1"))
        finally:
            db.close()
        return True
    except Exception:
        return False


def _storage_ready() -> bool:
    for directory in (config.get_data_dir(), config.get_uploads_dir()):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / ".v1-health"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except OSError:
            return False
    return True


def _gpu_state() -> GpuState:
    """Project the runtime probe onto the non-sensitive GPU contract.

    The probe runs in a child process (or from a frozen executable) and is
    cached; a machine without CUDA, torch or nvidia-smi must still answer with
    ``available=false`` and ``None`` values rather than failing health.
    """
    try:
        snapshot = platform_service.runtime_probe_snapshot()
    except Exception:
        return GpuState(available=False)
    available = bool(snapshot.get("torch_cuda_available"))
    name = snapshot.get("cuda_device_name") if available else None
    total = snapshot.get("cuda_memory_total_mb") if available else None
    used = snapshot.get("cuda_memory_used_mb") if available else None
    return GpuState(
        available=available,
        name=name if isinstance(name, str) and name else None,
        memory_total_mb=int(total) if isinstance(total, (int, float)) and total >= 0 else None,
        memory_used_mb=int(used) if isinstance(used, (int, float)) and used >= 0 else None,
    )


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    """Report liveness and readiness; job details need the Bearer token.

    ``/v1/health`` is the only endpoint that answers without a token, so an
    unauthenticated caller gets the operational summary only: the running
    job's id and kind are withheld. Queue ``paused``/``length`` are aggregate
    state, not job details, and stay visible.
    """
    queue = gpu_queue.snapshot()
    authenticated = is_authenticated(request)
    return HealthResponse(
        online=True,
        ready=_database_ready() and _storage_ready(),
        version=__version__,
        commit=resolve_commit(),
        queue=QueueState(
            paused=queue.paused,
            length=queue.length,
            current_job_id=queue.current_job_id if authenticated else None,
            current_job_kind=queue.current_job_kind if authenticated else None,
        ),
        gpu=_gpu_state(),
        yt_dlp_version=video_platform.yt_dlp_version(),
        bilibili_login=bilibili_login.login_status().state,
    )

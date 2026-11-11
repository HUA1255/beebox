"""``/v1/queue``: inspect and pause the single GPU queue (ticket #39).

ADR-0009 makes one first-in-first-out queue with a concurrency of one the only
way jobs reach the GPU. Operators pause it before maintenance (ADR-0010 also
uses pause/wait/resume around deployments); the running job is never
interrupted, new submissions keep queueing, and resume continues in order.

Pause and resume are idempotent. Every route here inherits the API
authentication applied to the whole ``/v1`` surface (ticket #38).
"""

from __future__ import annotations

from fastapi import APIRouter

from backend.services import gpu_queue
from backend.v1.schemas import QueuedJob, QueueStatusResponse

router = APIRouter(prefix="/v1/queue", tags=["v1"])


def queue_status() -> QueueStatusResponse:
    """Project the queue service state onto the public contract."""
    snapshot = gpu_queue.snapshot()
    return QueueStatusResponse(
        paused=snapshot.paused,
        length=snapshot.length,
        current_job_id=snapshot.current_job_id,
        current_job_kind=snapshot.current_job_kind,
        queued=[QueuedJob(job_id=entry.job_id, kind=entry.kind) for entry in snapshot.queued],
    )


@router.get("", response_model=QueueStatusResponse)
def get_queue() -> QueueStatusResponse:
    return queue_status()


@router.post("/pause", response_model=QueueStatusResponse)
def pause_queue() -> QueueStatusResponse:
    gpu_queue.pause()
    return queue_status()


@router.post("/resume", response_model=QueueStatusResponse)
def resume_queue() -> QueueStatusResponse:
    gpu_queue.resume()
    return queue_status()


__all__ = ["router", "queue_status"]

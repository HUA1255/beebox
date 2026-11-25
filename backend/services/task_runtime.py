from __future__ import annotations

import threading
import time
from typing import Callable

# A condition (backed by an RLock) lets a caller wait for a worker to release
# its marker, which is what makes an immediate retry safe.
_lock = threading.Condition()
_queued_task_ids: set[str] = set()
_running_task_ids: set[str] = set()
_cancelled_task_ids: set[str] = set()


def mark_queued(task_id: str) -> None:
    with _lock:
        _queued_task_ids.add(task_id)
        _cancelled_task_ids.discard(task_id)


def mark_running(task_id: str) -> None:
    with _lock:
        _queued_task_ids.discard(task_id)
        _running_task_ids.add(task_id)


def mark_finished(task_id: str) -> None:
    with _lock:
        _queued_task_ids.discard(task_id)
        _running_task_ids.discard(task_id)
        _cancelled_task_ids.discard(task_id)
        _lock.notify_all()


def wait_finished(task_id: str, *, timeout: float = 2.0) -> bool:
    """Wait for a worker to release ``task_id``; return whether it did.

    The terminal row state is written before the executor reaches its
    ``mark_finished`` call, so a retry issued immediately after a failure can
    otherwise observe a stale active marker and be refused. A bounded wait
    closes that window without blocking a genuinely active job forever.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    with _lock:
        while task_id in _queued_task_ids or task_id in _running_task_ids:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _lock.wait(remaining)
        return True


def request_cancel(task_id: str) -> str | None:
    with _lock:
        if task_id in _queued_task_ids:
            _queued_task_ids.discard(task_id)
            _cancelled_task_ids.add(task_id)
            return "queued"
        if task_id in _running_task_ids:
            _cancelled_task_ids.add(task_id)
            return "running"
        # Untracked ids have no worker to notify; do not pin them in the set forever.
        return None


def is_cancelled(task_id: str) -> bool:
    with _lock:
        return task_id in _cancelled_task_ids


def cancellation_checker(task_id: str) -> Callable[[], bool]:
    return lambda: is_cancelled(task_id)


def snapshot() -> dict:
    with _lock:
        return {
            "queued_task_ids": sorted(_queued_task_ids),
            "running_task_ids": sorted(_running_task_ids),
            "cancelled_task_ids": sorted(_cancelled_task_ids),
        }


def active_ids() -> set[str]:
    with _lock:
        return set(_queued_task_ids) | set(_running_task_ids)

"""Task-runtime marker lifecycle (retry race regression).

The terminal row state is committed before an executor reaches
``mark_finished``. ``wait_finished`` closes that window for an immediate retry;
these tests pin its three outcomes without a server or a worker.
"""

from __future__ import annotations

import threading

from backend.services import task_runtime


def test_wait_finished_returns_when_the_worker_releases_the_marker() -> None:
    task_id = "runtime-wait-released"
    task_runtime.mark_running(task_id)
    timer = threading.Timer(0.1, lambda: task_runtime.mark_finished(task_id))
    try:
        timer.start()
        assert task_runtime.wait_finished(task_id, timeout=2.0) is True
    finally:
        timer.cancel()
        task_runtime.mark_finished(task_id)


def test_wait_finished_times_out_while_the_job_is_still_active() -> None:
    task_id = "runtime-wait-active"
    task_runtime.mark_running(task_id)
    try:
        assert task_runtime.wait_finished(task_id, timeout=0.05) is False
    finally:
        task_runtime.mark_finished(task_id)


def test_wait_finished_returns_immediately_for_an_untracked_job() -> None:
    assert task_runtime.wait_finished("runtime-wait-untracked", timeout=0.05) is True

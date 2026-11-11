"""The one GPU queue (ADR-0009).

Every job that needs the graphics card — transcription today, speech synthesis
later — enters this queue. It is strictly first-in-first-out with a concurrency
of one: at most one job holds the GPU, and the next one starts only after the
current one reaches a terminal state. The queue can be paused: the running job
always finishes, submissions keep accumulating in order, and resuming continues
where the queue left off.

The queue is the ordering authority. Actual execution stays with the task
worker threads in :mod:`backend.services.tasks`: the dispatcher hands exactly
one job at a time to ``tasks.enqueue_for_execution`` and waits for
``task_finished`` before it hands over the next one, so the worker-thread count
can never raise concurrency above one.

Jobs that do not need the GPU (platform subtitles, ticket #42) must not sit in
this queue; :func:`submit_without_gpu` is the seam for them. The caller passes
the executor that should handle such a job, so the queue keeps no knowledge of
the subtitle path and ticket #44 adds the audio/ASR branch without changing
the ordering authority.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable

DEFAULT_KIND = "transcription"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QueueEntry:
    """One waiting job: its id and the public capability kind it belongs to."""

    job_id: str
    kind: str


@dataclass(frozen=True)
class QueueSnapshot:
    """An immutable copy of the queue state for the health/admin endpoints."""

    paused: bool
    length: int
    current_job_id: str | None
    current_job_kind: str | None
    queued: tuple[QueueEntry, ...]


class GpuQueue:
    """FIFO, concurrency-one job queue with pause/resume."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._pending: deque[QueueEntry] = deque()
        self._paused = False
        self._current: QueueEntry | None = None
        self._dispatcher: threading.Thread | None = None

    # -- submission --------------------------------------------------------

    def submit(self, job_id: str, *, kind: str = DEFAULT_KIND) -> None:
        """Append a GPU job. Duplicate ids are ignored, not reordered."""
        with self._condition:
            if self._current is not None and self._current.job_id == job_id:
                return
            if any(entry.job_id == job_id for entry in self._pending):
                return
            self._pending.append(QueueEntry(job_id=job_id, kind=kind))
            self._start_dispatcher_locked()
            self._condition.notify_all()

    def discard(self, job_id: str) -> bool:
        """Drop a job that is still waiting, e.g. after it was cancelled."""
        with self._condition:
            remaining = deque(entry for entry in self._pending if entry.job_id != job_id)
            removed = len(remaining) != len(self._pending)
            self._pending = remaining
            return removed

    def submit_without_gpu(
        self,
        job_id: str,
        *,
        executor: Callable[[str], None],
    ) -> None:
        """Run a job that needs no GPU outside this queue (ticket #44 seam).

        The executor owns the whole job; it never waits for the GPU slot and is
        unaffected by pause. The queue keeps no state for it.
        """
        thread = threading.Thread(
            target=executor,
            args=(job_id,),
            name=f"job-{job_id}",
            daemon=True,
        )
        thread.start()

    # -- admin -------------------------------------------------------------

    def pause(self) -> QueueSnapshot:
        """Stop dispatching new jobs; the running job is left alone."""
        with self._condition:
            self._paused = True
            self._condition.notify_all()
            return self._snapshot_locked()

    def resume(self) -> QueueSnapshot:
        """Continue dispatching waiting jobs in submission order."""
        with self._condition:
            self._paused = False
            self._condition.notify_all()
            return self._snapshot_locked()

    def snapshot(self) -> QueueSnapshot:
        with self._condition:
            return self._snapshot_locked()

    def reset(self) -> None:
        """Drop waiting jobs and unpause.

        Process lifecycle and test harnesses use this; it never touches the job
        that is currently running (its ``task_finished`` callback still clears
        it) so a reset can never make two jobs overlap.
        """
        with self._condition:
            self._pending.clear()
            self._paused = False
            self._condition.notify_all()

    def task_finished(self, job_id: str) -> None:
        """Report that the worker reached a terminal state for ``job_id``."""
        with self._condition:
            if self._current is not None and self._current.job_id == job_id:
                self._current = None
            self._condition.notify_all()

    # -- internals ---------------------------------------------------------

    def _snapshot_locked(self) -> QueueSnapshot:
        return QueueSnapshot(
            paused=self._paused,
            length=len(self._pending),
            current_job_id=self._current.job_id if self._current is not None else None,
            current_job_kind=self._current.kind if self._current is not None else None,
            queued=tuple(self._pending),
        )

    def _start_dispatcher_locked(self) -> None:
        if self._dispatcher is not None and self._dispatcher.is_alive():
            return
        dispatcher = threading.Thread(target=self._dispatch_loop, name="gpu-queue", daemon=True)
        self._dispatcher = dispatcher
        dispatcher.start()

    def _dispatch_loop(self) -> None:
        # One dispatcher for the life of the process: it is the only place that
        # decides which waiting job becomes the current one.
        while True:
            with self._condition:
                while self._paused or self._current is not None or not self._pending:
                    self._condition.wait()
                entry = self._pending.popleft()
                self._current = entry
            try:
                self._run(entry)
            except Exception:
                logger.exception("GPU queue could not dispatch job %s", entry.job_id)
            finally:
                with self._condition:
                    if self._current is not None and self._current.job_id == entry.job_id:
                        self._current = None
                    self._condition.notify_all()

    def _run(self, entry: QueueEntry) -> None:
        """Hand the job to the task worker, then wait for its terminal state."""
        from backend.services import tasks as task_service

        task_service.enqueue_for_execution(entry.job_id)
        with self._condition:
            while self._current is not None and self._current.job_id == entry.job_id:
                self._condition.wait()


_queue = GpuQueue()


def submit(job_id: str, *, kind: str = DEFAULT_KIND) -> None:
    _queue.submit(job_id, kind=kind)


def discard(job_id: str) -> bool:
    return _queue.discard(job_id)


def submit_without_gpu(
    job_id: str,
    *,
    executor: Callable[[str], None],
) -> None:
    _queue.submit_without_gpu(job_id, executor=executor)


def pause() -> QueueSnapshot:
    return _queue.pause()


def resume() -> QueueSnapshot:
    return _queue.resume()


def snapshot() -> QueueSnapshot:
    return _queue.snapshot()


def reset() -> None:
    _queue.reset()


def task_finished(job_id: str) -> None:
    _queue.task_finished(job_id)


__all__ = [
    "DEFAULT_KIND",
    "GpuQueue",
    "QueueEntry",
    "QueueSnapshot",
    "discard",
    "pause",
    "reset",
    "resume",
    "snapshot",
    "submit",
    "submit_without_gpu",
    "task_finished",
]

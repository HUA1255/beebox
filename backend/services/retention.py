"""In-process retention cleanup for jobs and their media (ticket #40).

ADR-0011: BeeBox is not long-term storage. A settled job and its result are
kept for ``BEEBOX_RETENTION_DAYS`` (default 30) and then removed together with
every file it still owns. The service runs once at startup and then on a fixed
interval, and takes an injectable clock so tests can advance time.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from sqlalchemy import func

from backend import config
from backend.database import session as db_session
from backend.database.models import TranscriptionTask
from backend.services import media_retention

logger = logging.getLogger(__name__)

DEFAULT_CLEANUP_INTERVAL_SECONDS = 60 * 60


def utc_now() -> datetime:
    return datetime.now(UTC)


class RetentionService:
    """Delete expired jobs and orphaned uploads on startup and periodically."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        interval_seconds: float = DEFAULT_CLEANUP_INTERVAL_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or utc_now
        self._interval_seconds = max(0.01, float(interval_seconds))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._run_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        """Run one pass now, then keep ticking until :meth:`stop`."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="beebox-retention",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:  # cleanup must never kill the server
                logger.exception("Retention cleanup pass failed")
            if self._stop_event.wait(self._interval_seconds):
                return

    # -- one pass ----------------------------------------------------------
    def run_once(self, *, now: datetime | None = None) -> dict[str, Any]:
        moment = now or self._clock()
        days = config.get_retention_days()
        cutoff = moment - timedelta(days=days)
        summary: dict[str, Any] = {
            "now": moment.isoformat(),
            "retention_days": days,
            "cutoff": cutoff.isoformat(),
            "expired_jobs": [],
            "deleted_jobs": 0,
            "skipped_jobs": [],
            "orphan_files_removed": 0,
            "orphan_blobs_removed": 0,
            "errors": [],
        }

        with self._run_lock:
            db = self._session()
            try:
                expired = (
                    db.query(TranscriptionTask)
                    .filter(
                        ~TranscriptionTask.status.in_(self._active_statuses()),
                        func.coalesce(
                            TranscriptionTask.updated_at,
                            TranscriptionTask.created_at,
                        )
                        <= cutoff,
                    )
                    .all()
                )
                # Capture ids before deleting: delete_task expires the session
                # and each row is unusable after it has been deleted.
                targets = [
                    (row.id, media_retention.task_options(row).get("_v1_file_id"))
                    for row in expired
                ]
                for job_id, file_id in targets:
                    try:
                        deleted = self._delete_job(db, job_id)
                    except Exception as exc:  # keep other jobs in the pass
                        db.rollback()
                        if self._is_state_conflict(exc):
                            # The job became active again between the query and
                            # the delete; its caller owns it now.
                            summary["skipped_jobs"].append(job_id)
                            continue
                        summary["errors"].append(f"{job_id}: {exc}")
                        logger.exception("Retention failed to delete job %s", job_id)
                        continue
                    if not deleted:
                        summary["skipped_jobs"].append(job_id)
                        continue
                    summary["deleted_jobs"] += 1
                    summary["expired_jobs"].append(job_id)
                    if isinstance(file_id, str) and file_id:
                        try:
                            media_retention.delete_v1_file_if_unreferenced(db, file_id)
                        except Exception as exc:
                            db.rollback()
                            summary["errors"].append(f"{job_id} upload {file_id}: {exc}")
                            logger.exception(
                                "Retention failed to delete upload %s for job %s",
                                file_id,
                                job_id,
                            )

                try:
                    orphaned = media_retention.cleanup_orphaned_v1_files(db, cutoff=cutoff)
                except Exception as exc:
                    db.rollback()
                    summary["errors"].append(f"orphaned upload cleanup: {exc}")
                    logger.exception("Retention failed to clean orphaned uploads")
                else:
                    summary["orphan_files_removed"] = orphaned["files_removed"]
                    summary["orphan_blobs_removed"] = orphaned["blobs_removed"]
                    summary["errors"].extend(orphaned["errors"])
            finally:
                db.close()

        if summary["deleted_jobs"] or summary["orphan_files_removed"] or summary["orphan_blobs_removed"]:
            logger.info(
                "Retention removed %s expired jobs, %s orphan files and %s orphan blobs",
                summary["deleted_jobs"],
                summary["orphan_files_removed"],
                summary["orphan_blobs_removed"],
            )
        return summary

    # -- helpers -----------------------------------------------------------
    def _session(self):
        if self._session_factory is not None:
            return self._session_factory()
        db_session.init_db()
        return db_session.SessionLocal()

    @staticmethod
    def _active_statuses() -> set[str]:
        from backend.services.tasks import ACTIVE_TASK_STATUSES

        return ACTIVE_TASK_STATUSES

    @staticmethod
    def _delete_job(db, job_id: str) -> bool:
        from backend.services.tasks import delete_task

        return delete_task(db, job_id)

    @staticmethod
    def _is_state_conflict(exc: Exception) -> bool:
        from backend.services.tasks import TaskStateConflictError

        return isinstance(exc, TaskStateConflictError)

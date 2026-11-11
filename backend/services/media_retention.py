"""Media lifecycle helpers for ticket #40 / ADR-0011.

ADR-0011 replaces long-term server storage: a successful job drops the source
media it consumed, while results and exports stay queryable. These helpers are
kept separate from :mod:`backend.services.tasks` so the completion hook and the
retention service share one ownership check without an import cycle.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from backend import config
from backend.database.models import TranscriptionTask, V1File
from backend.services.task_options import read_task_options

logger = logging.getLogger(__name__)


def task_options(row: TranscriptionTask) -> dict[str, Any]:
    """Return a task's persisted options, tolerating legacy/corrupt rows."""
    return read_task_options(row)


def is_v1_task(row: TranscriptionTask) -> bool:
    """A job created through ``/v1/jobs`` owns its media through BeeBox.

    This includes file uploads and (from #42/#44) video jobs, which carry the
    public source projection in their options.
    """
    options = task_options(row)
    return bool(options.get("_v1_source") or options.get("_v1_file_id"))


def referenced_v1_file_ids(
    db: Session, *, exclude_task_id: str | None = None
) -> set[str]:
    """Ids a job still needs for a future run, optionally ignoring one job.

    A completed job no longer needs its source (ADR-0011 deletes it); every
    other status may still retry or resume and therefore keeps the upload.
    """
    query = db.query(TranscriptionTask.id, TranscriptionTask.options_json).filter(
        TranscriptionTask.status != "completed"
    )
    if exclude_task_id is not None:
        query = query.filter(TranscriptionTask.id != exclude_task_id)
    referenced: set[str] = set()
    for _task_id, raw in query:
        try:
            options = json.loads(raw or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(options, dict):
            continue
        file_id = options.get("_v1_file_id")
        if isinstance(file_id, str) and file_id:
            referenced.add(file_id)
    return referenced


def delete_v1_file_if_unreferenced(
    db: Session, file_id: str, *, exclude_task_id: str | None = None
) -> dict[str, Any]:
    """Delete one stored upload unless another job still needs it.

    A job that has just succeeded must not unlink media another queued job may
    still read; the last owner performs the delete instead.
    """
    row = db.get(V1File, file_id)
    if row is None:
        return {"file_id": file_id, "deleted": False, "reason": "missing"}
    if file_id in referenced_v1_file_ids(db, exclude_task_id=exclude_task_id):
        return {"file_id": file_id, "deleted": False, "reason": "referenced"}

    path = config.resolve_storage_path(row.storage_path)
    db.delete(row)
    db.commit()
    result: dict[str, Any] = {"file_id": file_id, "deleted": True, "removed": False}
    if path is not None:
        try:
            if path.is_file() or path.is_symlink():
                path.unlink(missing_ok=True)
                result["removed"] = True
            elif path.exists():
                logger.warning("Refusing to remove non-file upload path %s", path)
        except OSError as exc:
            logger.warning("Failed to remove upload %s: %s", path, exc)
            result["error"] = str(exc)
    return result


def _resolve_upload_path(row: V1File) -> Path | None:
    return config.resolve_storage_path(row.storage_path)


def _canonical(path: Path) -> Path:
    """Normalise a path for set membership without requiring existence."""
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def cleanup_orphaned_v1_files(db: Session, *, cutoff: datetime) -> dict[str, Any]:
    """Remove unreferenced uploads older than the retention window.

    Only files with no owning job and no live download use are touched, and
    only after ``cutoff``, so a just-uploaded file that is about to become a
    job is never deleted.
    """
    referenced = referenced_v1_file_ids(db)
    files_removed = 0
    blobs_removed = 0
    errors: list[str] = []

    rows = (
        db.query(V1File)
        .filter(V1File.created_at.isnot(None), V1File.created_at <= cutoff)
        .all()
    )
    for row in rows:
        if row.id in referenced:
            continue
        path = _resolve_upload_path(row)
        db.delete(row)
        files_removed += 1
        if path is not None and (path.is_file() or path.is_symlink()):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                errors.append(f"{path}: {exc}")
    if files_removed:
        db.commit()

    # A crashed upload can leave a blob with no row. Compare against the rows
    # that remain so a live download is never touched.
    known_paths = {
        _canonical(resolved)
        for (storage_path,) in db.query(V1File.storage_path).all()
        if (resolved := config.resolve_storage_path(storage_path)) is not None
    }
    upload_root = config.resolve_uploads_dir() / "v1"
    if upload_root.is_dir():
        cutoff_ts = cutoff.timestamp()
        for candidate in upload_root.iterdir():
            if not candidate.is_file() or _canonical(candidate) in known_paths:
                continue
            try:
                if candidate.stat().st_mtime > cutoff_ts:
                    continue
                candidate.unlink(missing_ok=True)
                blobs_removed += 1
            except OSError as exc:
                errors.append(f"{candidate}: {exc}")

    return {
        "files_removed": files_removed,
        "blobs_removed": blobs_removed,
        "errors": errors,
    }

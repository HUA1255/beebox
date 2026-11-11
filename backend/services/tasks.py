from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy.orm import Session

from backend import config
from backend.database import session as db_session
from backend.database.models import TranscriptSegment as DBSegment
from backend.database.models import TaskDiagnostic
from backend.database.models import TaskLog
from backend.database.models import TranscriptionChunk
from backend.database.models import TranscriptionTask
from backend.models import TranscriptSegment, TranscriptionTaskResponse
from backend.services import diagnostics
from backend.services import gpu_queue
from backend.services import media_retention
from backend.services import models as model_service
from backend.services import paragraphs as paragraph_service
from backend.services import postprocess
from backend.services import quality
from backend.services import settings as settings_service
from backend.services import task_runtime
from backend.services import task_logs
from backend.services.chunk_merge import ChunkTranscript, merge_chunk_transcripts
from backend.services.chunking import (
    CHUNK_OVERLAP_MS,
    CHUNK_THRESHOLD_MS,
    CHUNK_WINDOW_MS,
    PlannedChunk,
    needs_chunking,
    plan_audio_chunks,
)
from backend.services.diarization import DIARIZATION_MODEL_NAME
from backend.services.errors import BeeBoxError
from backend.services.media import prepare_media_for_asr, preflight_media, split_audio_chunks, validate_upload_media
from backend.services.process_utils import no_window_kwargs
from backend.services.speech_engine import DiarizationRequest, SpeechRequest, get_speech_engine
from backend.services.task_options import read_task_options
from backend.services.task_transitions import task_transition_lock as _task_transition_lock
from backend.services.uploads import copy_local_path, save_upload
from backend.utils.events import event_bus
from backend.utils.transcript_text import normalize_transcript_text
from backend.utils.transcript_text import transcript_text_from_segments

# Provider transcription keeps its historical start threshold; local audio uses
# the Gate 2 contract threshold (audio longer than 120 s is chunked).
LOCAL_WORKER_POLL_SECONDS = 0.2
DEFAULT_LOCAL_WORKER_STALL_SECONDS = 20 * 60
# A heartbeat counts as activity only after the worker burned this much extra CPU: the
# heartbeat thread itself costs microseconds per beat, so a wedged process never reaches
# the floor while any real inference (CPU or GPU-driven) passes it within seconds.
LOCAL_WORKER_ACTIVITY_CPU_FLOOR = 2.0
# Even a heartbeat that keeps burning CPU (a busy loop) must not pin the local queue forever.
LOCAL_WORKER_HARD_STALL_MULTIPLIER = 3
LOCAL_WORKER_LIVENESS_SECONDS = 30
# A failed-chunk retry is queued like a normal task; the worker consumes this
# internal option instead of re-running the whole pipeline.
_PENDING_CHUNK_RETRY_KEY = "_pending_chunk_retry"


def _local_worker_stall_seconds() -> int:
    try:
        return max(1, int(os.environ.get("BEEBOX_LOCAL_WORKER_STALL_SECONDS", DEFAULT_LOCAL_WORKER_STALL_SECONDS)))
    except ValueError:
        return DEFAULT_LOCAL_WORKER_STALL_SECONDS
LOCAL_QUEUE = "local"
logger = logging.getLogger(__name__)
ACTIVE_TASK_STATUSES = {
    "queued",
    "importing",
    "preprocessing",
    "waiting_model",
    "downloading_model",
    "transcribing",
    "postprocessing",
    "exporting",
}
# Statuses that mean a worker owned the job, i.e. a restart really interrupted
# it. ``queued`` is deliberately excluded: the GPU queue is in-memory, so a
# queued row is waiting for a new process to adopt it, not interrupted work
# (ticket #64).
RUNNING_TASK_STATUSES = ACTIVE_TASK_STATUSES - {"queued"}
_task_queues = {LOCAL_QUEUE: queue.Queue()}
_worker_counts = {LOCAL_QUEUE: 0}
_worker_targets = {LOCAL_QUEUE: 0}
_queue_lock = threading.RLock()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _segments_for_task(db: Session, task_id: str) -> list[TranscriptSegment]:
    rows = (
        db.query(DBSegment)
        .filter(DBSegment.task_id == task_id)
        .order_by(DBSegment.idx.asc())
        .all()
    )
    return [
        TranscriptSegment(
            id=row.idx,
            start=row.start_ms / 1000,
            end=row.end_ms / 1000,
            text=row.text,
            speaker=row.speaker,
            confidence=row.confidence,
            paragraph=row.paragraph,
        )
        for row in rows
    ]


def mark_interrupted_tasks(db: Session) -> int:
    """Fail the jobs a restart actually interrupted.

    Only jobs a worker had picked up are interrupted. ``queued`` rows survive
    untouched: the deploy flow pauses the queue before restarting, callers keep
    submitting, and those jobs must stay queued until the new process adopts
    them (ticket #64). The stored progress is the last value the caller saw and
    is kept; forcing it to 100 would report an interrupted job as finished.
    """
    rows = db.query(TranscriptionTask).filter(TranscriptionTask.status.in_(RUNNING_TASK_STATUSES)).all()
    for row in rows:
        has_completed_chunks = db.query(TranscriptionChunk).filter(TranscriptionChunk.task_id == row.id, TranscriptionChunk.status == "completed").first() is not None
        row.status = "failed_resumable" if has_completed_chunks else "interrupted"
        row.error = "Task was interrupted before the server restarted"
        row.error_code = "TASK_INTERRUPTED"
        row.updated_at = _utc_now()
        task_logs.add_log(db, row.id, "recovery", "warning", row.error, {"status": row.status})
    if rows:
        db.commit()
    return len(rows)


def resume_queued_tasks(db: Session) -> list[str]:
    """Re-submit the jobs that were waiting when the previous process stopped.

    The GPU queue lives in memory, so a restart forgets its waiting entries
    while the job rows remain ``queued``. This adopts them again in their
    original submission order (``created_at``, with id as a stable tiebreak);
    the FIFO queue then runs them in that order. ``start_task_in_background``
    keeps the routing: video-link jobs run outside the GPU queue and ordinary
    transcription jobs re-enter it.

    A single unrestartable row must not abort startup: the service would crash
    in a WinSW restart loop and every other waiting job would be lost too. Each
    job is submitted independently; one that still fails is marked
    ``TASK_RESUME_FAILED`` (retryable) so the caller can resubmit it.
    """
    task_ids = [
        task_id
        for (task_id,) in db.query(TranscriptionTask.id)
        .filter(TranscriptionTask.status == "queued")
        .order_by(TranscriptionTask.created_at.asc(), TranscriptionTask.id.asc())
        .all()
    ]
    # Close the read transaction before dispatching: the first adopted job
    # starts writing immediately and SQLite would otherwise treat this session
    # as a competing reader.
    db.rollback()
    for task_id in task_ids:
        try:
            start_task_in_background(task_id)
        except Exception:
            logger.exception("Could not resume queued task %s after restart", task_id)
            _fail_unresumed_task(task_id)
    return task_ids


def _fail_unresumed_task(task_id: str) -> None:
    """Mark one queued row failed when it could not be adopted at startup."""
    try:
        db = db_session.SessionLocal()
        try:
            row = get_task_row(db, task_id)
            if row is None or row.status != "queued":
                return
            row.status = "failed"
            row.error = "The queued task could not be resumed after the server restart"
            set_error_code(row, "TASK_RESUME_FAILED")
            row.updated_at = _utc_now()
            db.commit()
            task_logs.add_log(
                db,
                task_id,
                "recovery",
                "error",
                row.error,
                {"error_code": row.error_code},
            )
        finally:
            db.close()
    except Exception:
        logger.exception("Could not record the failed resume of queued task %s", task_id)


def reconcile_orphan_tasks(db: Session) -> list[TranscriptionTask]:
    tracked = task_runtime.active_ids()
    rows = db.query(TranscriptionTask).filter(TranscriptionTask.status.in_(RUNNING_TASK_STATUSES)).all()
    orphans: list[TranscriptionTask] = []
    for row in rows:
        if row.id in tracked:
            continue
        has_completed_chunks = db.query(TranscriptionChunk).filter(TranscriptionChunk.task_id == row.id, TranscriptionChunk.status == "completed").first() is not None
        row.status = "failed_resumable" if has_completed_chunks else "interrupted"
        row.error = "Task was orphaned by the worker runtime"
        row.error_code = "TASK_INTERRUPTED"
        row.updated_at = _utc_now()
        task_logs.add_log(db, row.id, "recovery", "warning", row.error, {"status": row.status})
        orphans.append(row)
    if orphans:
        db.commit()
    return orphans


def to_response(db: Session, row: TranscriptionTask) -> TranscriptionTaskResponse:
    try:
        options = json.loads(row.options_json or "{}")
    except json.JSONDecodeError:
        options = {}
    return TranscriptionTaskResponse(
        id=row.id,
        filename=row.filename,
        source=row.source,
        audio_path=row.audio_path,
        normalized_audio_path=row.normalized_audio_path,
        status=row.status,
        progress=row.progress,
        language=row.language,
        model_name=row.model_name,
        duration_ms=row.duration_ms,
        text=row.text,
        error=row.error,
        error_code=row.error_code,
        source_kind=options.get("_source_kind", "managed"),
        options={key: value for key, value in options.items() if not key.startswith("_")},
        segments=_segments_for_task(db, row.id),
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
    )


def _write_options(row: TranscriptionTask, options: dict) -> None:
    row.options_json = json.dumps(options, ensure_ascii=False)


def set_task_state(db: Session, row: TranscriptionTask, status: str, progress: float) -> None:
    # A cancel that lands between two pipeline steps must not be overwritten by
    # the next state transition (that used to resurrect a cancelled task).
    if task_runtime.is_cancelled(row.id):
        raise BeeBoxError("TASK_CANCELLED", "Task was cancelled", stage="cancel")
    row.status = status
    row.progress = progress
    row.updated_at = _utc_now()
    db.commit()
    event_bus.publish(
        "task.updated",
        {"id": row.id, "status": row.status, "progress": row.progress, "error": row.error, "error_code": row.error_code},
    )
    task_logs.add_log(db, row.id, status, "info", f"Task entered {status}", {"progress": progress})


def _cancel_chunks(db: Session, task_id: str) -> None:
    """A cancelled task must not leave any chunk queued, running or completed.

    The cancel request and the worker race: chunks can be created *after* the
    cancel was accepted. Every chunk that exists by the time the worker settles
    is marked cancelled, so callers never observe a cancelled task with queued
    or transcribing chunks.
    """
    for chunk in db.query(TranscriptionChunk).filter(TranscriptionChunk.task_id == task_id).all():
        chunk.status = "cancelled"
        chunk.progress = 100
        chunk.error = "Task was cancelled"
        chunk.error_code = "TASK_CANCELLED"


def set_error_code(row: TranscriptionTask, code: str | None) -> None:
    row.error_code = code
    options = read_task_options(row)
    if code:
        options["error_code"] = code
    else:
        options.pop("error_code", None)
    _write_options(row, options)


def _error_from_exception(exc: Exception) -> tuple[str | None, str, str | None, str | None, str | None, dict | None]:
    if isinstance(exc, BeeBoxError):
        return exc.code, exc.message, exc.stage, exc.command, exc.stderr_excerpt, getattr(exc, "audio_metadata", None)
    return None, str(exc), None, None, None, None


def _raise_if_cancelled(task_id: str) -> None:
    if task_runtime.is_cancelled(task_id):
        raise BeeBoxError("TASK_CANCELLED", "Task was cancelled", stage="cancel")


def _chunk_segments_json(segments: list[TranscriptSegment] | None) -> str | None:
    """Serialise chunk-local aligned segments for a later failed-chunk retry."""
    if not segments:
        return None
    payload = [segment.model_dump() for segment in segments]
    return json.dumps(payload, ensure_ascii=False)


def _segments_from_payload(payload: Any) -> list[TranscriptSegment]:
    segments: list[TranscriptSegment] = []
    for item in payload or []:
        try:
            segments.append(TranscriptSegment.model_validate(item))
        except Exception:
            continue
    return segments


def _load_chunk_segments(chunk: TranscriptionChunk) -> list[TranscriptSegment]:
    """Return the chunk's stored aligned segments, falling back to its text.

    Rows written before ticket #12 (and chunks whose backend produced no
    timestamps) only carry whole-chunk text; they degrade to one unaligned item
    covering the chunk window, exactly like the pre-#12 merge did.
    """
    if chunk.segments_json:
        try:
            raw = json.loads(chunk.segments_json)
        except json.JSONDecodeError:
            raw = None
        if isinstance(raw, list):
            segments = _segments_from_payload(raw)
            if segments:
                return segments
    text = (chunk.text or "").strip()
    if not text:
        return []
    return [TranscriptSegment(id=1, start=0.0, end=0.0, text=text)]


def _settle_unstarted_chunks(db: Session, task_id: str) -> None:
    """A failed run must leave exactly one owner per chunk.

    The failing chunk is already ``failed``; any chunk the abort caught while
    ``transcribing`` returns to ``queued`` so a failed-chunk retry can claim it
    instead of leaving an orphaned owner behind.
    """
    for chunk in (
        db.query(TranscriptionChunk)
        .filter(TranscriptionChunk.task_id == task_id, TranscriptionChunk.status == "transcribing")
        .all()
    ):
        chunk.status = "queued"
        chunk.progress = 0
        chunk.error = None
        chunk.error_code = None
    db.flush()


def _task_status_after_failure(db: Session, row: TranscriptionTask, code: str | None) -> str:
    """Chunk failures are resumable by contract; anything else is terminal."""
    if code == "CHUNK_FAILED":
        _settle_unstarted_chunks(db, row.id)
        return "failed_resumable"
    failed_chunk = (
        db.query(TranscriptionChunk)
        .filter(
            TranscriptionChunk.task_id == row.id,
            TranscriptionChunk.status == "failed",
            TranscriptionChunk.error_code == "CHUNK_FAILED",
        )
        .first()
    )
    if failed_chunk is not None:
        _settle_unstarted_chunks(db, row.id)
        return "failed_resumable"
    return "failed"


def _create_task_row(
    db: Session,
    *,
    filename: str,
    audio_path: Path,
    backend: str,
    model_name: str | None,
    language: str | None,
    output_formats: list[str],
    options: dict[str, Any] | None = None,
    initial_status: str = "queued",
    task_id: str | None = None,
) -> TranscriptionTaskResponse:
    stored_options = {"output_formats": output_formats, **(options or {})}
    stored_options.setdefault("_source_kind", "managed")
    row = TranscriptionTask(
        id=task_id or audio_path.stem,
        filename=filename,
        source=backend,
        audio_path=config.to_storage_path(audio_path),
        status=initial_status,
        progress=0,
        language=language,
        model_name=model_name,
        options_json=json.dumps(stored_options, ensure_ascii=False),
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    response = to_response(db, row)
    start_task_in_background(row.id)
    return response


def start_task_in_background(task_id: str) -> None:
    """Submit a job to the queue that matches its text path (ADR-0009).

    Ordering, the one-at-a-time rule and pause/resume all live in
    :mod:`backend.services.gpu_queue`. Video jobs resolve metadata first and
    run outside the graphics-card slot; a platform-subtitle job finishes
    there (ticket #42) while an audio/ASR job re-enters here as an ordinary
    GPU-queued transcription (ticket #44).
    """
    db_session.init_db()
    db = db_session.SessionLocal()
    try:
        row = get_task_row(db, task_id)
        if row is None:
            return
        from backend.services import video_jobs

        if video_jobs.runs_outside_gpu_queue(row):
            task_runtime.mark_queued(task_id)
            gpu_queue.submit_without_gpu(task_id, executor=video_jobs.execute_video_job)
            return
        # Keep the worker threads warm here as well: the queue service starts
        # them on dispatch, but /tasks/active must already report a worker.
        _ensure_task_workers(settings_service.get_settings(db).max_concurrent_local_tasks)
        gpu_queue.submit(task_id, kind=_job_kind(row))
    finally:
        db.close()


def _job_kind(row: TranscriptionTask) -> str:
    """The public capability kind; #42/#44 can tag a job through its options."""
    value = read_task_options(row).get("_job_kind")
    return value if isinstance(value, str) and value else gpu_queue.DEFAULT_KIND


def enqueue_for_execution(task_id: str) -> None:
    """Hand one job to the task worker; called by the GPU queue dispatcher."""
    db_session.init_db()
    db = db_session.SessionLocal()
    try:
        settings = settings_service.get_settings(db)
        _ensure_task_workers(settings.max_concurrent_local_tasks)
        task_runtime.mark_queued(task_id)
        _task_queues[LOCAL_QUEUE].put(task_id)
    finally:
        db.close()


def _ensure_task_workers(local_count: int) -> None:
    with _queue_lock:
        _worker_targets[LOCAL_QUEUE] = local_count
        _start_missing_workers_locked(LOCAL_QUEUE)


def _start_missing_workers_locked(queue_name: str) -> None:
    while _worker_counts[queue_name] < _worker_targets[queue_name]:
        thread = threading.Thread(target=_task_worker, args=(queue_name,), daemon=True)
        _worker_counts[queue_name] += 1
        try:
            thread.start()
        except BaseException:
            _worker_counts[queue_name] -= 1
            raise


def _task_worker(queue_name: str) -> None:
    work_queue = _task_queues[queue_name]
    try:
        while True:
            task_id = work_queue.get()
            try:
                db = db_session.SessionLocal()
                try:
                    row = get_task_row(db, task_id)
                    if row is not None and row.status != "cancelled" and not task_runtime.is_cancelled(task_id):
                        task_runtime.mark_running(task_id)
                        run_task(db, row)
                    else:
                        task_runtime.mark_finished(task_id)
                finally:
                    db.close()
            except Exception:
                # The worker must never die: contain the escape and fail the task.
                logger.exception("Task worker escaped an unexpected error for task %s", task_id)
                _fail_task_after_escape(task_id)
            except BaseException:
                # Persist the current task best-effort, then let the worker
                # lifecycle replace this thread without surfacing an
                # unhandled thread exception.
                logger.exception("Task worker is exiting unexpectedly for task %s", task_id)
                _fail_task_after_escape(task_id)
                return
            finally:
                # The GPU queue waits for this before dispatching the next job,
                # so it must fire for skipped and failed tasks as well.
                gpu_queue.task_finished(task_id)
                work_queue.task_done()
    finally:
        with _queue_lock:
            _worker_counts[queue_name] = max(0, _worker_counts[queue_name] - 1)
            _start_missing_workers_locked(queue_name)


def _fail_task_after_escape(task_id: str) -> None:
    """Best-effort failure persistence after run_task itself raised."""
    try:
        db = db_session.SessionLocal()
        try:
            db.rollback()
            row = get_task_row(db, task_id)
            if row is None or row.status not in ACTIVE_TASK_STATUSES:
                return
            row.status = "failed"
            row.error = "Task worker encountered an unexpected runtime error"
            set_error_code(row, "WORKER_RUNTIME_ERROR")
            row.progress = 100
            row.updated_at = _utc_now()
            db.commit()
            task_logs.add_log(db, row.id, "failed", "error", row.error, {"error_code": row.error_code})
            event_bus.publish("task.failed", {"id": row.id, "status": row.status, "error": row.error, "error_code": row.error_code})
        finally:
            db.close()
    except Exception:
        logger.exception("Failed to persist escaped worker failure for task %s", task_id)
    finally:
        task_runtime.mark_finished(task_id)


def create_task(
    db: Session,
    *,
    filename: str,
    audio_bytes: bytes,
    backend: str,
    model_name: str | None,
    language: str | None,
    output_formats: list[str],
    options: dict[str, Any] | None = None,
) -> TranscriptionTaskResponse:
    task_id = str(uuid.uuid4())
    suffix = Path(filename).suffix or ".audio"
    audio_path = config.get_uploads_dir() / f"{task_id}{suffix}"
    audio_path.write_bytes(audio_bytes)
    return _create_task_row(
        db,
        filename=filename,
        audio_path=audio_path,
        backend=backend,
        model_name=model_name,
        language=language,
        output_formats=output_formats,
        options=options,
    )


def create_task_from_file(
    db: Session,
    *,
    filename: str,
    file_obj: BinaryIO,
    backend: str,
    model_name: str | None,
    language: str | None,
    output_formats: list[str],
    options: dict[str, Any] | None = None,
    validate_media: bool = False,
) -> TranscriptionTaskResponse:
    task_id = str(uuid.uuid4())
    suffix = Path(filename).suffix or ".audio"
    audio_path = config.get_uploads_dir() / f"{task_id}{suffix}"
    save_upload(file_obj, audio_path)
    if validate_media:
        try:
            validate_upload_media(audio_path)
        except BeeBoxError:
            # A rejected upload must not leave a file or a task row behind.
            audio_path.unlink(missing_ok=True)
            raise
    return _create_task_row(
        db,
        filename=filename,
        audio_path=audio_path,
        backend=backend,
        model_name=model_name,
        language=language,
        output_formats=output_formats,
        options=options,
    )


def create_task_from_path(
    db: Session,
    *,
    path: Path,
    backend: str,
    model_name: str | None,
    language: str | None,
    output_formats: list[str],
    options: dict[str, Any] | None = None,
) -> TranscriptionTaskResponse:
    source_path = Path(path).expanduser()
    if not source_path.is_file():
        raise FileNotFoundError(f"Audio file not found: {path}")
    source_path = source_path.resolve(strict=True)
    task_id = str(uuid.uuid4())
    if config.get_media_ingest_mode() == config.INGEST_MODE_REFERENCE:
        reference_options = {**(options or {}), "_source_kind": "external"}
        return _create_task_row(
            db,
            filename=source_path.name,
            audio_path=source_path,
            backend=backend,
            model_name=model_name,
            language=language,
            output_formats=output_formats,
            options=reference_options,
            task_id=task_id,
        )
    target = config.get_uploads_dir() / f"{task_id}{source_path.suffix or '.audio'}"
    ingest_options = {
        **(options or {}),
        "_ingest_source_path": str(source_path),
        "_ingest_source_size": source_path.stat().st_size,
    }
    return _create_task_row(
        db,
        filename=source_path.name,
        audio_path=target,
        backend=backend,
        model_name=model_name,
        language=language,
        output_formats=output_formats,
        options=ingest_options,
        initial_status="importing",
        task_id=task_id,
    )


def create_task_from_media(
    db: Session,
    *,
    filename: str,
    audio_path: Path,
    backend: str = "local",
    model_name: str | None,
    language: str | None,
    output_formats: list[str],
    options: dict[str, Any] | None = None,
) -> TranscriptionTaskResponse:
    """Queue a task over already-managed media without copying it again.

    ``/v1/files`` owns the upload, so the job references that stored path and
    marks it external: deleting the job must never unlink a file that another
    job (or the caller) may still use.
    """
    stored_path = Path(audio_path)
    if not stored_path.is_file():
        raise FileNotFoundError(f"Media file not found: {stored_path}")
    task_options = {**(options or {}), "_source_kind": "external"}
    return _create_task_row(
        db,
        filename=filename,
        audio_path=stored_path,
        backend=backend,
        model_name=model_name,
        language=language,
        output_formats=output_formats,
        options=task_options,
        # The media stem belongs to the file resource, not the job.
        task_id=str(uuid.uuid4()),
    )


def _import_task_media(db: Session, row: TranscriptionTask) -> None:
    options = read_task_options(row)
    source_value = options.get("_ingest_source_path")
    if not source_value:
        return
    source = Path(str(source_value))
    destination = config.resolve_storage_path(row.audio_path)
    if destination is None:
        raise BeeBoxError("MEDIA_IMPORT_FAILED", "Managed media destination is unavailable", stage="importing")

    set_task_state(db, row, "importing", 0)
    last_progress = -1

    def update_progress(copied: int, total: int) -> None:
        nonlocal last_progress
        _raise_if_cancelled(row.id)
        progress = 9 if total <= 0 else min(9, int(copied / total * 9))
        if progress == last_progress:
            return
        last_progress = progress
        row.progress = progress
        row.updated_at = _utc_now()
        db.commit()
        event_bus.publish(
            "task.updated",
            {"id": row.id, "status": row.status, "progress": row.progress, "error": row.error, "error_code": row.error_code},
        )

    try:
        copy_local_path(source, destination, on_progress=update_progress)
    except BeeBoxError:
        raise
    except Exception as exc:
        raise BeeBoxError(
            "MEDIA_IMPORT_FAILED",
            f"Failed to import {source.name}: {exc}",
            stage="importing",
        ) from exc
    options.pop("_ingest_source_path", None)
    options.pop("_ingest_source_size", None)
    _write_options(row, options)
    db.commit()


def _prepare_task_media(db: Session, row: TranscriptionTask) -> Path:
    audio_path = config.resolve_storage_path(row.audio_path)
    if audio_path is None:
        raise RuntimeError("Task audio file not found")

    normalized_path, metadata = prepare_media_for_asr(
        audio_path,
        output_dir=config.get_derived_audio_dir(),
        output_name=f"{row.id}.wav",
    )
    if normalized_path != audio_path:
        row.normalized_audio_path = config.to_storage_path(normalized_path)
    options = read_task_options(row)
    options["audio_metadata"] = metadata
    # The contract publishes the chunk geometry in options; long audio always
    # uses these values so the inline and worker paths cannot drift apart.
    options.setdefault("chunk_window_ms", CHUNK_WINDOW_MS)
    options.setdefault("chunk_overlap_ms", CHUNK_OVERLAP_MS)
    try:
        options["audio_quality"] = preflight_media(
            audio_path,
            long_audio_threshold_ms=CHUNK_THRESHOLD_MS,
            chunk_window_ms=CHUNK_WINDOW_MS,
            chunk_overlap_ms=CHUNK_OVERLAP_MS,
        )
    except Exception as exc:
        options["audio_quality"] = {"warnings": [str(exc)]}
    _write_options(row, options)
    if metadata.get("duration_ms") is not None and row.duration_ms is None:
        # A video job already carries the platform duration; keep it instead of
        # replacing it with the decoded sample length.
        row.duration_ms = metadata["duration_ms"]
    db.commit()
    return normalized_path


def _transcribe_with_local_model(db: Session, row: TranscriptionTask):
    normalized_path = config.resolve_storage_path(row.normalized_audio_path or row.audio_path)
    if normalized_path is None:
        raise RuntimeError("Task audio file not found")
    if os.environ.get("BEEBOX_INLINE_LOCAL_TRANSCRIPTION") == "1":
        if needs_chunking(row.duration_ms):
            return _transcribe_chunks(db, row, normalized_path)
        return _transcribe_local_path(db, row, normalized_path)
    return _transcribe_local_subprocess(db, row, normalized_path)


def _local_model_name(row: TranscriptionTask) -> str:
    """Rows without a model follow the v1 default instead of a retired whisper fallback."""
    if row.model_name:
        return row.model_name
    raise BeeBoxError(
        "MODEL_NOT_DOWNLOADED",
        "Local transcription requires a model",
        stage="waiting_model",
    )


def _transcribe_local_path(
    db: Session,
    row: TranscriptionTask,
    audio_path: Path,
    *,
    chunk_index: int = 1,
    chunk_total: int = 1,
    chunk_start_ms: int | None = None,
):
    model_name = _local_model_name(row)
    settings = settings_service.get_settings(db)
    options = read_task_options(row)
    engine = get_speech_engine()
    return engine.transcribe(
        SpeechRequest(
            audio_path=str(audio_path),
            model_name=model_name,
            language=row.language,
            vad=bool(options.get("vad", settings.vad)),
            word_timestamps=bool(options.get("word_timestamps", settings.word_timestamps)),
            chunk_index=chunk_index,
            chunk_total=chunk_total,
            chunk_start_ms=chunk_start_ms,
        )
    )


def _local_worker_command(request_path: Path, result_path: Path) -> list[str]:
    command = [sys.executable]
    if not getattr(sys, "frozen", False):
        command.extend(["-m", "backend.server"])
    command.extend(
        [
            "--data-dir",
            str(config.get_data_dir()),
            "--parent-pid",
            str(os.getpid()),
            "--local-worker-request",
            str(request_path),
            "--local-worker-result",
            str(result_path),
        ]
    )
    return command


def _local_worker_environment(concurrency: int) -> dict[str, str]:
    """Give the inference worker an explicit CPU thread budget.

    A worker that inherits a single-threaded pin runs CPU engines such as
    CTranslate2 single-threaded, which measured roughly four times slower than
    the same work with a normal thread count.
    """
    threads = max(1, (os.cpu_count() or 4) // max(1, concurrency))
    environment = os.environ.copy()
    environment["OMP_NUM_THREADS"] = str(threads)
    return environment


def _read_worker_state(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def _read_worker_heartbeat(worker_dir: Path) -> dict[str, Any] | None:
    try:
        value = json.loads((worker_dir / "heartbeat.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def _terminate_worker(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _create_chunk_rows(
    db: Session,
    row: TranscriptionTask,
    planned: list[PlannedChunk],
    *,
    first_status: str = "queued",
    first_progress: float = 0.0,
) -> list[TranscriptionChunk]:
    """Persist the planner output; both ASR paths share this row shape."""
    cancelled = row.status == "cancelled" or task_runtime.is_cancelled(row.id)
    db.query(TranscriptionChunk).filter(TranscriptionChunk.task_id == row.id).delete()
    rows: list[TranscriptionChunk] = []
    for plan in planned:
        first = plan.idx == 1
        chunk = TranscriptionChunk(
            task_id=row.id,
            idx=plan.idx,
            audio_path=config.to_storage_path(plan.path),
            start_ms=plan.start_ms,
            end_ms=plan.end_ms,
            status="cancelled" if cancelled else (first_status if first else "queued"),
            progress=100 if cancelled else (first_progress if first else 0),
            error="Task was cancelled" if cancelled else None,
            error_code="TASK_CANCELLED" if cancelled else None,
        )
        db.add(chunk)
        rows.append(chunk)
    db.commit()
    for chunk in rows:
        db.refresh(chunk)
    return rows


def _local_worker_inputs(db: Session, row: TranscriptionTask, audio_path: Path) -> tuple[list[dict[str, Any]], list[TranscriptionChunk]]:
    if not needs_chunking(row.duration_ms):
        return [{"audio_path": str(audio_path), "start_ms": 0, "end_ms": row.duration_ms or 0}], []

    chunks_dir = config.get_derived_audio_dir() / f"{row.id}_chunks"
    planned = plan_audio_chunks(audio_path, chunks_dir, row.duration_ms)
    rows = _create_chunk_rows(db, row, planned, first_status="transcribing", first_progress=20)
    _raise_if_cancelled(row.id)
    inputs = [
        {"audio_path": str(plan.path), "start_ms": plan.start_ms, "end_ms": plan.end_ms}
        for plan in planned
    ]
    first_chunk = rows[0]
    task_logs.add_log(db, row.id, "chunk", "info", "Chunk 1 started", {"chunk_id": first_chunk.id})
    event_bus.publish(
        "chunk.updated",
        {"task_id": row.id, "chunk_id": first_chunk.id, "status": first_chunk.status, "progress": first_chunk.progress},
    )
    return inputs, rows


def _publish_local_worker_progress(
    db: Session,
    row: TranscriptionTask,
    chunks: list[TranscriptionChunk],
    results: list[dict[str, Any]],
    published: int,
) -> int:
    task_id = getattr(row, "id", None)
    if task_id is not None and task_runtime.is_cancelled(task_id):
        # The worker may report results after the cancel landed; do not mark
        # chunks completed or start the next one for a cancelled task.
        _cancel_chunks(db, task_id)
        db.commit()
        raise BeeBoxError("TASK_CANCELLED", "Task was cancelled", stage="cancel")
    completed = len(results)
    if completed <= published:
        return published
    if not chunks:
        return completed
    if completed > len(chunks):
        raise BeeBoxError("LOCAL_WORKER_PROTOCOL_ERROR", "Local transcription worker returned too many chunk results", stage="transcribing")
    for result_index in range(published, completed):
        chunk = chunks[result_index]
        chunk.status = "completed"
        chunk.progress = 100
        chunk.text = str(results[result_index].get("text") or "")
        chunk.segments_json = _chunk_segments_json(
            _segments_from_payload(results[result_index].get("segments"))
        )
        chunk.error = None
        chunk.error_code = None
        task_logs.add_log(db, row.id, "chunk", "info", f"Chunk {chunk.idx} completed", {"chunk_id": chunk.id})
        event_bus.publish("chunk.updated", {"task_id": row.id, "chunk_id": chunk.id, "status": chunk.status, "progress": chunk.progress})
        if result_index + 1 < len(chunks):
            next_chunk = chunks[result_index + 1]
            next_chunk.status = "transcribing"
            next_chunk.progress = 20
            task_logs.add_log(db, row.id, "chunk", "info", f"Chunk {next_chunk.idx} started", {"chunk_id": next_chunk.id})
        row.progress = 60 + int((result_index + 1) / len(chunks) * 28)
    db.commit()
    return completed


def _chunk_transcript(
    *,
    idx: int,
    start_ms: int | None,
    end_ms: int | None,
    segments: list[TranscriptSegment],
    text: str,
    aligned: bool,
    language: str | None = None,
) -> ChunkTranscript:
    return ChunkTranscript(
        idx=idx,
        start_ms=int(start_ms or 0),
        end_ms=int(end_ms or 0),
        segments=segments,
        text=text,
        aligned=aligned,
        language=language,
    )


def _merge_chunk_result(
    row: TranscriptionTask,
    transcripts: list[ChunkTranscript],
    merged,
    texts: list[str],
    *,
    summary: dict[str, Any],
):
    from backend.models import TranscriptionResult

    return TranscriptionResult(
        text=transcript_text_from_segments(merged.segments) or "\n".join(text for text in texts if text),
        language=next((item.language for item in transcripts if item.language), row.language),
        duration=row.duration_ms / 1000 if row.duration_ms else (merged.segments[-1].end if merged.segments else None),
        segments=merged.segments,
        model_name=row.model_name,
        raw_result_summary={"aligned": merged.aligned, **summary},
    )


def _combine_local_worker_results(
    row: TranscriptionTask,
    inputs: list[dict[str, Any]],
    payloads: list[dict[str, Any]],
):
    from backend.models import TranscriptionResult

    if len(payloads) != len(inputs):
        raise BeeBoxError(
            "LOCAL_WORKER_PROTOCOL_ERROR",
            f"Local transcription worker returned {len(payloads)} of {len(inputs)} expected results",
            stage="transcribing",
        )
    parsed = [TranscriptionResult.model_validate(payload) for payload in payloads]
    chunks = [
        _chunk_transcript(
            idx=index,
            start_ms=item.get("start_ms"),
            end_ms=item.get("end_ms"),
            segments=result.segments,
            text=result.text,
            aligned=bool((result.raw_result_summary or {}).get("aligned")),
            language=result.language,
        )
        for index, (item, result) in enumerate(zip(inputs, parsed, strict=True), 1)
    ]
    merged = merge_chunk_transcripts(chunks)
    return _merge_chunk_result(
        row,
        chunks,
        merged,
        [result.text for result in parsed],
        summary={"chunks": len(chunks)},
    )


def _mark_chunk_failed(
    db: Session,
    row: TranscriptionTask,
    chunk: TranscriptionChunk,
    *,
    total: int | None = None,
    cause_message: str,
    cause_code: str | None = None,
    stage: str | None = None,
    command: str | None = None,
    stderr_excerpt: str | None = None,
    audio_metadata: dict | None = None,
) -> BeeBoxError:
    """Mark one chunk failed and build the task-level CHUNK_FAILED error.

    The failing ``idx`` appears in the chunk row, the task error, the task log
    and the diagnostic so a failed chunk is always localisable.
    """
    label = f"Chunk {chunk.idx}/{total} failed" if total else f"Chunk {chunk.idx} failed"
    message = f"{label}: {cause_message}"
    chunk.status = "failed"
    chunk.progress = 100
    chunk.error = message
    chunk.error_code = "CHUNK_FAILED"
    chunk.segments_json = None
    if db is not None:
        db.commit()
        diagnostics.record_task_diagnostic(
            db,
            task_id=row.id,
            stage=stage or "chunk",
            error_code="CHUNK_FAILED",
            message=message,
            command=command,
            stderr_excerpt=stderr_excerpt,
            model_name=getattr(row, "model_name", None),
            audio_metadata=audio_metadata,
        )
        task_logs.add_log(
            db,
            row.id,
            "chunk",
            "error",
            message,
            {
                "chunk_id": chunk.id,
                "chunk_idx": chunk.idx,
                "error_code": "CHUNK_FAILED",
                "cause_code": cause_code,
            },
        )
    event_bus.publish(
        "chunk.updated",
        {
            "task_id": row.id,
            "chunk_id": chunk.id,
            "status": chunk.status,
            "progress": chunk.progress,
            "error_code": chunk.error_code,
        },
    )
    return BeeBoxError(
        "CHUNK_FAILED",
        message,
        stage=stage or "chunk",
        command=command,
        stderr_excerpt=stderr_excerpt,
        audio_metadata=audio_metadata,
    )


def _transcribe_local_subprocess(db: Session, row: TranscriptionTask, audio_path: Path):
    model_name = _local_model_name(row)
    settings = settings_service.get_settings(db)
    options = read_task_options(row)
    transcribe_options = {
        "language": row.language,
        "vad": options.get("vad", settings.vad),
        "word_timestamps": options.get("word_timestamps", settings.word_timestamps),
    }
    inputs, chunks = _local_worker_inputs(db, row, audio_path)
    worker_dir = config.get_cache_dir() / "task-workers" / row.id
    shutil.rmtree(worker_dir, ignore_errors=True)
    worker_dir.mkdir(parents=True, exist_ok=True)
    request_path = worker_dir / "request.json"
    result_path = worker_dir / "result.json"
    stderr_path = worker_dir / "stderr.log"
    request_path.write_text(
        json.dumps({"model_name": model_name, "options": transcribe_options, "inputs": inputs}, ensure_ascii=False),
        encoding="utf-8",
    )
    published = 0
    process: subprocess.Popen | None = None
    stderr_file = stderr_path.open("w", encoding="utf-8")
    stall_limit = _local_worker_stall_seconds()
    hard_stall_limit = stall_limit * LOCAL_WORKER_HARD_STALL_MULTIPLIER
    last_progress_at = time.monotonic()
    # Heartbeat activity: a slow-but-working worker keeps burning CPU, a wedged one does not.
    activity_reference_cpu: float | None = None
    last_active_at = time.monotonic()
    last_liveness_at = time.monotonic()

    def stderr_excerpt() -> str:
        stderr_file.flush()
        try:
            return stderr_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        except OSError:
            return ""

    try:
        process = subprocess.Popen(
            _local_worker_command(request_path, result_path),
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
            text=True,
            env=_local_worker_environment(settings.max_concurrent_local_tasks),
            **no_window_kwargs(),
        )
        while True:
            if task_runtime.is_cancelled(row.id):
                _terminate_worker(process)
                _cancel_chunks(db, row.id)
                if chunks:
                    db.commit()
                raise BeeBoxError("TASK_CANCELLED", "Task was cancelled", stage="cancel")
            state = _read_worker_state(result_path)
            if state:
                results = state.get("results") if isinstance(state.get("results"), list) else []
                published_before = published
                published = _publish_local_worker_progress(db, row, chunks, results, published)
                if published != published_before or state.get("status") in {"completed", "failed"}:
                    last_progress_at = time.monotonic()
                    last_active_at = last_progress_at
                if state.get("status") == "completed":
                    process.wait(timeout=5)
                    return _combine_local_worker_results(row, inputs, results)
                if state.get("status") == "failed":
                    detail = str(state.get("error") or "Local transcription worker failed")
                    if chunks and len(results) < len(chunks):
                        # The worker processes inputs in order, so the next
                        # unpublished chunk is the one that failed.
                        raise _mark_chunk_failed(
                            db,
                            row,
                            chunks[len(results)],
                            total=len(chunks),
                            cause_message=detail,
                            cause_code="LOCAL_WORKER_FAILED",
                            stage="transcribing",
                            stderr_excerpt=stderr_excerpt(),
                        )
                    raise BeeBoxError(
                        "MODEL_LOAD_FAILED",
                        detail,
                        stage="transcribing",
                        stderr_excerpt=stderr_excerpt(),
                    )
            heartbeat = _read_worker_heartbeat(worker_dir)
            heartbeat_cpu = heartbeat.get("cpu") if heartbeat else None
            if isinstance(heartbeat_cpu, (int, float)):
                if activity_reference_cpu is None or heartbeat_cpu < activity_reference_cpu:
                    activity_reference_cpu = heartbeat_cpu
                elif heartbeat_cpu - activity_reference_cpu >= LOCAL_WORKER_ACTIVITY_CPU_FLOOR:
                    activity_reference_cpu = heartbeat_cpu
                    last_active_at = time.monotonic()
            return_code = process.poll()
            if return_code is not None:
                state = _read_worker_state(result_path)
                detail = str((state or {}).get("error") or f"Local transcription worker exited with code {return_code}")
                if chunks and published < len(chunks):
                    raise _mark_chunk_failed(
                        db,
                        row,
                        chunks[published],
                        total=len(chunks),
                        cause_message=detail,
                        cause_code="LOCAL_WORKER_EXITED",
                        stage="transcribing",
                        stderr_excerpt=stderr_excerpt(),
                    )
                raise BeeBoxError("MODEL_LOAD_FAILED", detail, stage="transcribing", stderr_excerpt=stderr_excerpt())
            now = time.monotonic()
            result_idle = now - last_progress_at
            if result_idle > hard_stall_limit or (result_idle > stall_limit and now - last_active_at > stall_limit):
                # A hung worker (e.g. a device-level inference deadlock) must not
                # pin the task in transcribing and block the whole local queue. A
                # heartbeat that keeps burning CPU buys the hard limit only: slow
                # inference survives, a busy loop does not.
                _terminate_worker(process)
                raise BeeBoxError(
                    "LOCAL_WORKER_STALLED",
                    f"Local transcription worker produced no chunk progress for {int(result_idle)} seconds"
                    f" (worker activity idle {int(now - last_active_at)} seconds) and was terminated",
                    stage="transcribing",
                    stderr_excerpt=stderr_excerpt(),
                )
            if (db is not None and result_idle >= LOCAL_WORKER_LIVENESS_SECONDS
                    and now - last_active_at <= stall_limit and now - last_liveness_at >= LOCAL_WORKER_LIVENESS_SECONDS):
                # Slow-but-active inference keeps the task row visibly alive without
                # inventing progress: updated_at refreshes, progress stays chunk-based.
                row.updated_at = _utc_now()
                db.commit()
                last_liveness_at = now
            time.sleep(LOCAL_WORKER_POLL_SECONDS)
    finally:
        if process is not None:
            _terminate_worker(process)
        stderr_file.close()
        shutil.rmtree(worker_dir, ignore_errors=True)


def _transcribe_path_for_row(
    db: Session,
    row: TranscriptionTask,
    audio_path: Path,
    *,
    chunk_index: int = 1,
    chunk_total: int = 1,
    chunk_start_ms: int | None = None,
):
    if row.source == "local" and row.model_name:
        return _transcribe_local_path(
            db,
            row,
            audio_path,
            chunk_index=chunk_index,
            chunk_total=chunk_total,
            chunk_start_ms=chunk_start_ms,
        )
    raise BeeBoxError("INVALID_TRANSCRIPTION_BACKEND", f"Invalid transcription backend: {row.source}", stage="transcribing")


def _transcribe_chunks(db: Session, row: TranscriptionTask, normalized_path: Path):
    if not needs_chunking(row.duration_ms):
        return _transcribe_path_for_row(db, row, normalized_path)

    chunks_dir = config.get_derived_audio_dir() / f"{row.id}_chunks"
    planned = plan_audio_chunks(normalized_path, chunks_dir, row.duration_ms)
    if not planned:
        return _transcribe_path_for_row(db, row, normalized_path)
    rows = _create_chunk_rows(db, row, planned)
    total = len(planned)

    chunk_transcripts: list[ChunkTranscript] = []
    texts: list[str] = []
    for plan, chunk in zip(planned, rows, strict=True):
        # Re-check before claiming the next chunk: a cancel that landed between
        # chunks must stop the run instead of starting more inference.
        _raise_if_cancelled(row.id)
        chunk.status = "transcribing"
        chunk.progress = 20
        db.commit()
        task_logs.add_log(db, row.id, "chunk", "info", f"Chunk {chunk.idx} started", {"chunk_id": chunk.id})
        event_bus.publish("chunk.updated", {"task_id": row.id, "chunk_id": chunk.id, "status": chunk.status, "progress": chunk.progress})
        try:
            result = _transcribe_path_for_row(
                db,
                row,
                plan.path,
                chunk_index=chunk.idx,
                chunk_total=total,
                chunk_start_ms=plan.start_ms,
            )
            # A cancel during inference wins over the chunk result: never mark a
            # cancelled chunk completed with transcript output.
            _raise_if_cancelled(row.id)
            chunk.text = result.text
            chunk.segments_json = _chunk_segments_json(result.segments)
            chunk.status = "completed"
            chunk.error = None
            chunk.error_code = None
            chunk.progress = 100
            chunk_transcripts.append(
                _chunk_transcript(
                    idx=chunk.idx,
                    start_ms=plan.start_ms,
                    end_ms=plan.end_ms,
                    segments=result.segments,
                    text=result.text,
                    aligned=bool((result.raw_result_summary or {}).get("aligned")),
                    language=result.language,
                )
            )
            texts.append(result.text)
            row.progress = 60 + int(chunk.idx / total * 25)
            db.commit()
            task_logs.add_log(db, row.id, "chunk", "info", f"Chunk {chunk.idx} completed", {"chunk_id": chunk.id})
            event_bus.publish("chunk.updated", {"task_id": row.id, "chunk_id": chunk.id, "status": chunk.status, "progress": chunk.progress})
        except Exception as exc:
            code, message, stage, command, stderr_excerpt, audio_metadata = _error_from_exception(exc)
            if code == "TASK_CANCELLED":
                chunk.status = "cancelled"
                chunk.progress = 100
                chunk.error = message
                chunk.error_code = "TASK_CANCELLED"
                db.commit()
                raise
            raise _mark_chunk_failed(
                db,
                row,
                chunk,
                total=total,
                cause_message=message,
                cause_code=code,
                stage=stage or "chunk",
                command=command,
                stderr_excerpt=stderr_excerpt,
                audio_metadata=audio_metadata,
            ) from exc

    merged = merge_chunk_transcripts(chunk_transcripts)
    return _merge_chunk_result(
        row,
        chunk_transcripts,
        merged,
        texts,
        summary={"chunks": total, "merged": True},
    )


def store_segments(db: Session, row: TranscriptionTask, segments: list[TranscriptSegment]) -> None:
    db.query(DBSegment).filter(DBSegment.task_id == row.id).delete()
    for index, segment in enumerate(segments, 1):
        db.add(
            DBSegment(
                task_id=row.id,
                idx=index,
                start_ms=int(segment.start * 1000),
                end_ms=int(segment.end * 1000),
                text=segment.text,
                speaker=segment.speaker,
                confidence=segment.confidence,
                paragraph=segment.paragraph,
            )
        )


def _option_num_speakers(options: dict[str, Any]) -> int | None:
    value = options.get("num_speakers")
    if value is None or value == "":
        return None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return value


def _apply_speaker_labels_and_postprocess(
    db: Session,
    row: TranscriptionTask,
    segments: list[TranscriptSegment],
    *,
    options: dict[str, Any],
    settings: Any,
    normalized_path: Path | None,
    aligned: bool,
) -> tuple[list[TranscriptSegment], str, bool]:
    """Diarize + postprocess the merged transcript (shared by run and retry)."""
    native_speaker_labels = any(segment.speaker for segment in segments)
    diarization_requested = bool(options.get("diarization", settings.diarization))
    diarized = False
    _raise_if_cancelled(row.id)
    if diarization_requested and not native_speaker_labels:
        if normalized_path is None or not normalized_path.exists():
            # The contract forbids a transcript without speakers when
            # diarization was requested; never silently skip the stage.
            raise BeeBoxError(
                "DIARIZATION_FAILED",
                "Speaker diarization was requested but the audio required for it is unavailable",
                stage="diarize",
            )
        num_speakers = _option_num_speakers(options)
        diarization_model = options.get("diarization_model")
        task_logs.add_log(
            db,
            row.id,
            "diarize",
            "info",
            "Speaker diarization started",
            {"num_speakers": num_speakers, "model": diarization_model or DIARIZATION_MODEL_NAME},
        )
        event_bus.publish(
            "task.updated",
            {"id": row.id, "status": row.status, "progress": row.progress, "error": row.error, "error_code": row.error_code},
        )
        # The aligned transcript is final by now; the engine drops the forced
        # aligner so pyannote does not have to share the 16 GB budget with it.
        diarization_stats: dict[str, Any] = {}
        segments, diarization_stats = get_speech_engine().diarize(
            DiarizationRequest(
                audio_path=str(normalized_path),
                segments=segments,
                num_speakers=num_speakers,
                model_name=diarization_model,
            )
        )
        diarized = True
        if diarization_stats:
            summary = dict(options.get("raw_result_summary") or {})
            summary.update(diarization_stats)
            options["raw_result_summary"] = summary
        task_logs.add_log(
            db,
            row.id,
            "diarize",
            "info",
            "Speaker diarization completed",
            {
                "speakers": sorted({segment.speaker for segment in segments if segment.speaker}),
                "segments": len(segments),
                "cross_speaker_segments": diarization_stats.get("cross_speaker_segments"),
            },
        )
    mode = options.get("postprocess_mode", "safe")
    merge_short_segments = bool(options.get("merge_short_segments", mode == "aggressive"))
    traditional_to_simplified = bool(options.get("traditional_to_simplified", False))
    segments = postprocess.process_segments(
        segments,
        max_chars_per_line=int(options.get("max_chars_per_line", 42)),
        max_lines=int(options.get("max_lines", 2)),
        min_duration_ms=int(options.get("min_duration_ms", 800)),
        merge_short_segments=merge_short_segments,
        traditional_to_simplified=traditional_to_simplified,
        # Linear re-splitting would replace ForcedAligner times with
        # estimates; aligned segments are already split at sentence boundaries.
        split_long_segments=not aligned,
        # min_gap/min_duration forcing would also overwrite aligned times.
        enforce_timing=not aligned,
        # Forced-aligned text is verbatim ASR output; cleaning it would break
        # the sentence round-trip (e.g. collapse legitimate repeated words).
        # Explicit user-requested rewrites (merge/t2s) still take precedence.
        preserve_verbatim=aligned and not merge_short_segments and not traditional_to_simplified,
    )
    return segments, mode, diarized


def run_task(db: Session, row: TranscriptionTask) -> None:
    try:
        if row.status == "cancelled" or task_runtime.is_cancelled(row.id):
            _cancel_chunks(db, row.id)
            db.commit()
            task_runtime.mark_finished(row.id)
            return
        options = read_task_options(row)
        pending_retry = options.pop(_PENDING_CHUNK_RETRY_KEY, None)
        if pending_retry is not None:
            _write_options(row, options)
            _run_pending_chunk_retry(db, row, pending_retry)
            return
        row.error = None
        set_error_code(row, None)
        task_logs.add_log(db, row.id, "queued", "info", "Task picked by worker")
        _import_task_media(db, row)
        set_task_state(db, row, "preprocessing", 10)
        _raise_if_cancelled(row.id)
        normalized_path = _prepare_task_media(db, row)
        set_task_state(db, row, "waiting_model" if row.model_name else "transcribing", 20)
        _raise_if_cancelled(row.id)
        if row.model_name:
            get_speech_engine().ensure_ready(row.model_name)
        set_task_state(db, row, "transcribing", 60)
        _raise_if_cancelled(row.id)
        if row.source == "local" and row.model_name:
            result = _transcribe_with_local_model(db, row)
        else:
            raise BeeBoxError("INVALID_TRANSCRIPTION_BACKEND", f"Invalid transcription backend: {row.source}", stage="transcribing")
        db.refresh(row)
        if row.status == "cancelled" or task_runtime.is_cancelled(row.id):
            _cancel_chunks(db, row.id)
            db.commit()
            return
        set_task_state(db, row, "postprocessing", 90)
        options = read_task_options(row)
        if result.raw_result_summary:
            options["raw_result_summary"] = result.raw_result_summary
        settings = settings_service.get_settings(db)
        segments = result.segments
        aligned = bool((result.raw_result_summary or {}).get("aligned"))
        task_logs.add_log(
            db,
            row.id,
            "align",
            "info" if aligned else "warning",
            "Forced alignment completed" if aligned else "Transcript has no forced-alignment timestamps",
            {"aligned": aligned, "segments": len(segments)},
        )
        if (
            not aligned
            and len(segments) == 1
            and row.duration_ms
            and segments[0].end <= segments[0].start
        ):
            segments = [segments[0].model_copy(update={"end": row.duration_ms / 1000})]
        segments, mode, diarized = _apply_speaker_labels_and_postprocess(
            db,
            row,
            segments,
            options=options,
            settings=settings,
            normalized_path=normalized_path,
            aligned=aligned,
        )
        # Grouping must run after postprocess (which rebuilds segments and drops
        # paragraph) and before persistence.
        segments = paragraph_service.assign_paragraphs(segments)
        _write_options(row, options)
        text_from_segments = transcript_text_from_segments(segments)
        chunk_count = int((result.raw_result_summary or {}).get("chunks") or 0)
        if chunk_count > 1:
            task_logs.add_log(
                db,
                row.id,
                "merge",
                "info",
                "Cross-chunk transcript merged",
                {"chunks": chunk_count, "segments": len(segments)},
            )
        segment_altering_options = (
            mode == "aggressive"
            or bool(options.get("merge_short_segments", False))
            or bool(options.get("traditional_to_simplified", False))
            or diarized
        )
        row.text = text_from_segments if segment_altering_options else (normalize_transcript_text(result.text) or text_from_segments)
        if row.duration_ms is None:
            row.duration_ms = int((result.duration or 0) * 1000)
        store_segments(db, row, segments)
        report = quality.store_quality_report(db, row, segments, options)
        if report["warnings"]:
            task_logs.add_log(db, row.id, "quality", "warning", "Quality warnings detected", report)
        _write_options(row, options)
        _raise_if_cancelled(row.id)
        row.status = "completed"
        row.progress = 100
        row.completed_at = _utc_now()
        _write_options(row, options)
        db.commit()
        _cleanup_completed_task_artifacts(db, row.id)
        event_bus.publish("task.completed", {"id": row.id, "status": row.status, "progress": row.progress})
    except Exception as exc:
        db.rollback()
        fresh = get_task_row(db, row.id)
        if fresh is None:
            logger.warning("Task %s row was deleted during execution; skipping failure persistence", row.id)
            return
        if fresh.status == "cancelled":
            _cancel_chunks(db, row.id)
            db.commit()
            return
        code, message, stage, command, stderr_excerpt, audio_metadata = _error_from_exception(exc)
        if code == "TASK_CANCELLED":
            fresh.status = "cancelled"
            _cancel_chunks(db, row.id)
        else:
            fresh.status = _task_status_after_failure(db, fresh, code)
        fresh.error = message
        set_error_code(fresh, code)
        fresh.progress = 100
        db.commit()
        diagnostics.record_task_diagnostic(
            db,
            task_id=fresh.id,
            stage=stage or fresh.status,
            error_code=code,
            message=message,
            command=command,
            stderr_excerpt=stderr_excerpt,
            model_name=fresh.model_name,
            audio_metadata=audio_metadata,
        )
        task_logs.add_log(db, fresh.id, stage or "failed", "error", message, {"error_code": code})
        event_bus.publish("task.failed", {"id": fresh.id, "status": fresh.status, "error": fresh.error, "error_code": fresh.error_code})
    finally:
        task_runtime.mark_finished(row.id)


def list_tasks(db: Session) -> tuple[list[TranscriptionTaskResponse], int]:
    rows = db.query(TranscriptionTask).order_by(TranscriptionTask.created_at.desc()).all()
    return [to_response(db, row) for row in rows], len(rows)


def active_tasks(db: Session) -> dict[str, Any]:
    settings = settings_service.get_settings(db)
    orphans = reconcile_orphan_tasks(db)
    queued_statuses = {"queued"}
    running_statuses = ACTIVE_TASK_STATUSES - queued_statuses
    queued = db.query(TranscriptionTask).filter(TranscriptionTask.status.in_(queued_statuses)).all()
    running = db.query(TranscriptionTask).filter(TranscriptionTask.status.in_(running_statuses)).all()
    recent_error_row = (
        db.query(TranscriptionTask)
        .filter(TranscriptionTask.error.isnot(None))
        .order_by(TranscriptionTask.updated_at.desc())
        .first()
    )

    def summarize(row: TranscriptionTask) -> dict[str, Any]:
        return {
            "id": row.id,
            "filename": row.filename,
            "status": row.status,
            "progress": row.progress,
            "source": row.source,
            "model_name": row.model_name,
            "error_code": row.error_code,
        }

    return {
        "downloads": model_service.active_downloads(),
        "queued_tasks": [summarize(row) for row in queued],
        "running_tasks": [summarize(row) for row in running],
        "orphan_tasks": [summarize(row) for row in orphans],
        "failed_resumable_tasks": [summarize(row) for row in db.query(TranscriptionTask).filter(TranscriptionTask.status == "failed_resumable").all()],
        "running_chunks": [
            {
                "id": chunk.id,
                "task_id": chunk.task_id,
                "idx": chunk.idx,
                "status": chunk.status,
                "progress": chunk.progress,
                "error_code": chunk.error_code,
            }
            for chunk in db.query(TranscriptionChunk).filter(TranscriptionChunk.status.in_(("queued", "transcribing", "failed"))).all()
        ],
        "local_queue_length": gpu_queue.snapshot().length,
        "local_worker_count": _worker_counts[LOCAL_QUEUE],
        "max_concurrent_local_tasks": settings.max_concurrent_local_tasks,
        "recent_error": recent_error_row.error if recent_error_row else None,
        "worker_state": task_runtime.snapshot(),
        "cancelled_task_ids": task_runtime.snapshot()["cancelled_task_ids"],
    }


def get_task(db: Session, task_id: str) -> TranscriptionTaskResponse | None:
    row = db.query(TranscriptionTask).filter(TranscriptionTask.id == task_id).first()
    return to_response(db, row) if row else None


def get_task_row(db: Session, task_id: str) -> TranscriptionTask | None:
    return db.query(TranscriptionTask).filter(TranscriptionTask.id == task_id).first()


class TaskStateConflictError(Exception):
    """Raised when a task mutation conflicts with its current lifecycle state."""


class TaskActiveError(TaskStateConflictError):
    """Raised when retry/retranscribe/delete targets a task that is still active."""


class ChunkRetryConflictError(TaskStateConflictError):
    """Raised when a chunk is not eligible for a failed-chunk retry."""


def _require_not_active(row: TranscriptionTask, action: str) -> None:
    if row.status in ACTIVE_TASK_STATUSES or row.id in task_runtime.active_ids():
        raise TaskActiveError(f"Cannot {action} task while it is {row.status}; cancel it first")


def retry_task(db: Session, task_id: str) -> TranscriptionTaskResponse | None:
    with _task_transition_lock:
        db.expire_all()
        row = get_task_row(db, task_id)
        if row is None:
            return None
        _require_not_active(row, "retry")
        options = read_task_options(row)
        options.pop(_PENDING_CHUNK_RETRY_KEY, None)
        _write_options(row, options)
        row.status = "queued"
        row.error = None
        set_error_code(row, None)
        row.progress = 0
        row.completed_at = None
        row.text = None
        db.query(DBSegment).filter(DBSegment.task_id == task_id).delete()
        db.query(TranscriptionChunk).filter(TranscriptionChunk.task_id == task_id).delete()
        db.commit()
        db.refresh(row)
        response = to_response(db, row)
        start_task_in_background(row.id)
        return response


def retranscribe_task(
    db: Session,
    task_id: str,
    *,
    model_name: str | None = None,
    language: str | None = None,
    output_formats: list[str] | None = None,
) -> TranscriptionTaskResponse | None:
    with _task_transition_lock:
        db.expire_all()
        row = get_task_row(db, task_id)
        if row is None:
            return None
        _require_not_active(row, "retranscribe")
        if model_name is not None:
            row.model_name = model_name
        if language is not None:
            row.language = language
        options = read_task_options(row)
        options.pop(_PENDING_CHUNK_RETRY_KEY, None)
        if output_formats is not None:
            options["output_formats"] = output_formats
        options.pop("audio_metadata", None)
        options.pop("audio_quality", None)
        _write_options(row, options)
        row.normalized_audio_path = None
        row.status = "queued"
        row.progress = 0
        row.duration_ms = None
        row.text = None
        row.error = None
        set_error_code(row, None)
        row.completed_at = None
        db.query(DBSegment).filter(DBSegment.task_id == task_id).delete()
        db.query(TranscriptionChunk).filter(TranscriptionChunk.task_id == task_id).delete()
        db.commit()
        db.refresh(row)
        response = to_response(db, row)
        start_task_in_background(row.id)
        return response


def cancel_task(db: Session, task_id: str) -> TranscriptionTaskResponse | None:
    with _task_transition_lock:
        db.expire_all()
        row = get_task_row(db, task_id)
        if row is None:
            return None
        if row.status == "cancelled":
            return to_response(db, row)
        if row.status in {"completed", "failed", "failed_resumable", "interrupted"}:
            raise TaskStateConflictError(f"Task is already {row.status}; only queued or running tasks can be cancelled")
        task_runtime.request_cancel(task_id)
        # A cancelled job must leave the GPU queue immediately, even while it
        # is paused; a running job keeps going until its pipeline notices.
        gpu_queue.discard(task_id)
        row.status = "cancelled"
        set_error_code(row, "TASK_CANCELLED")
        row.progress = 100
        row.updated_at = _utc_now()
        _cancel_chunks(db, task_id)
        db.commit()
        db.refresh(row)
        event_bus.publish(
            "task.updated",
            {"id": row.id, "status": row.status, "progress": row.progress, "error": row.error, "error_code": row.error_code},
        )
        task_logs.add_log(db, row.id, "cancelled", "warning", "Task was cancelled", {"error_code": "TASK_CANCELLED"})
        return to_response(db, row)


def delete_task(db: Session, task_id: str) -> bool:
    with _task_transition_lock:
        db.expire_all()
        row = get_task_row(db, task_id)
        if row is None:
            return False
        _require_not_active(row, "delete")
        task_runtime.request_cancel(task_id)
        # Externally referenced media belongs to the user; only BeeBox-owned
        # copies (managed uploads and downloaded video audio) are unlinked.
        if read_task_options(row).get("_source_kind", "managed") in {"managed", "download"}:
            audio_path = config.resolve_storage_path(row.audio_path)
            if audio_path and (audio_path.is_file() or audio_path.is_symlink()):
                audio_path.unlink(missing_ok=True)
        normalized_audio_path = config.resolve_storage_path(row.normalized_audio_path)
        if normalized_audio_path:
            normalized_audio_path.unlink(missing_ok=True)
        chunks = db.query(TranscriptionChunk).filter(TranscriptionChunk.task_id == task_id).all()
        for chunk in chunks:
            chunk_path = config.resolve_storage_path(chunk.audio_path)
            if chunk_path:
                chunk_path.unlink(missing_ok=True)
        # Chunk audio lives in a per-task directory; remove it even when it is
        # already empty so a deleted task leaves no derived-audio skeleton.
        shutil.rmtree(config.get_derived_audio_dir() / f"{task_id}_chunks", ignore_errors=True)
        shutil.rmtree(config.get_cache_dir() / "task-workers" / task_id, ignore_errors=True)
        # Exports are rendered on demand today, but any artefact named after the
        # task must not outlive it (v1 acceptance checks for orphan exports).
        exports_dir = config.get_exports_dir()
        for item in list(exports_dir.rglob(f"*{task_id}*")):
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
        db.query(DBSegment).filter(DBSegment.task_id == task_id).delete()
        db.query(TranscriptionChunk).filter(TranscriptionChunk.task_id == task_id).delete()
        db.query(TaskDiagnostic).filter(TaskDiagnostic.task_id == task_id).delete()
        db.query(TaskLog).filter(TaskLog.task_id == task_id).delete()
        db.delete(row)
        db.flush()
        db.commit()
        return True


def delete_all_tasks(db: Session) -> int:
    task_ids = [row.id for row in db.query(TranscriptionTask.id).all()]
    deleted = 0
    for task_id in task_ids:
        try:
            if delete_task(db, task_id):
                deleted += 1
        except TaskActiveError:
            # Active tasks are cancelled first by their owners; batch delete skips them.
            continue
    return deleted


def _merge_completed_chunks(db: Session, row: TranscriptionTask) -> None:
    """Rebuild the transcript from every completed chunk's stored segments.

    The old implementation re-merged whole-chunk text, which degraded an
    aligned transcript to one coarse segment per chunk. Chunk-local aligned
    segments are persisted on completion (``segments_json``), so a retry can
    reuse a finished chunk byte-for-byte and still keep fine-grained timing.
    """
    chunks = (
        db.query(TranscriptionChunk)
        .filter(TranscriptionChunk.task_id == row.id)
        .order_by(TranscriptionChunk.idx.asc())
        .all()
    )
    if not chunks or any(chunk.status != "completed" for chunk in chunks):
        return
    transcripts: list[ChunkTranscript] = []
    for chunk in chunks:
        segments = _load_chunk_segments(chunk)
        if not segments:
            continue
        # A stored aligned list is reused with its chunk-local times; a legacy
        # text-only chunk degrades to one unaligned item covering its window.
        aligned = bool(chunk.segments_json) and any(segment.end > segment.start for segment in segments)
        transcripts.append(
            _chunk_transcript(
                idx=chunk.idx,
                start_ms=chunk.start_ms,
                end_ms=chunk.end_ms,
                segments=segments,
                text=chunk.text or "",
                aligned=aligned,
            )
        )
    merged = merge_chunk_transcripts(transcripts)
    options = read_task_options(row)
    settings = settings_service.get_settings(db)
    segments, _mode, _diarized = _apply_speaker_labels_and_postprocess(
        db,
        row,
        merged.segments,
        options=options,
        settings=settings,
        # A wav upload is already the normalized audio, so normalized_audio_path
        # stays NULL; fall back to the original upload for diarization.
        normalized_path=config.resolve_storage_path(row.normalized_audio_path or row.audio_path),
        aligned=merged.aligned,
    )
    segments = paragraph_service.assign_paragraphs(segments)
    try:
        task_logs.add_log(
            db,
            row.id,
            "merge",
            "info",
            "Cross-chunk transcript merged after chunk retry",
            {"chunks": len(transcripts), "segments": len(segments), "aligned": merged.aligned},
            commit=False,
        )
        store_segments(db, row, segments)
        row.text = transcript_text_from_segments(segments)
        row.status = "completed"
        row.progress = 100
        row.error = None
        set_error_code(row, None)
        row.completed_at = _utc_now()
        summary = dict(options.get("raw_result_summary") or {})
        summary.update(
            {
                "aligned": merged.aligned,
                "chunks": len(transcripts),
                "merged": True,
                "retry": True,
            }
        )
        options["raw_result_summary"] = summary
        _write_options(row, options)
        db.commit()
        db.refresh(row)
    except Exception:
        db.rollback()
        raise


def _previous_state_payload(row: TranscriptionTask) -> list[Any]:
    return [
        row.status,
        row.progress,
        row.error,
        row.error_code,
        row.completed_at.isoformat() if row.completed_at else None,
    ]


def _previous_state_from_payload(payload: Any) -> tuple[str, float, str | None, str | None, datetime | None]:
    status, progress, error, error_code, completed_at = payload
    return (
        status,
        progress,
        error,
        error_code,
        datetime.fromisoformat(completed_at) if completed_at else None,
    )


def _prepare_failed_chunk_retry(
    db: Session,
    task_id: str,
) -> tuple[TranscriptionTask, list[int], list[Any] | None] | None:
    """Queue a failed-chunk retry instead of running inference inline.

    The endpoint must answer with a ``queued`` task; the normal task worker
    consumes the persisted retry intent and reuses completed chunks.
    """
    with _task_transition_lock:
        db.expire_all()
        row = get_task_row(db, task_id)
        if row is None:
            return None
        _require_not_active(row, "retry chunks for")
        chunk_ids = [
            chunk.id
            for chunk in db.query(TranscriptionChunk)
            .filter(
                TranscriptionChunk.task_id == task_id,
                TranscriptionChunk.status != "completed",
            )
            .order_by(TranscriptionChunk.idx.asc())
            .all()
        ]
        if not chunk_ids:
            return row, [], None
        previous_state = _previous_state_payload(row)
        options = read_task_options(row)
        options[_PENDING_CHUNK_RETRY_KEY] = {
            "chunk_ids": chunk_ids,
            "previous_state": previous_state,
        }
        _write_options(row, options)
        row.status = "queued"
        row.progress = 0
        row.error = None
        set_error_code(row, None)
        row.completed_at = None
        row.updated_at = _utc_now()
        db.commit()
        db.refresh(row)
        return row, chunk_ids, previous_state


def _claim_failed_chunks(
    db: Session,
    task_id: str,
    *,
    chunk_id: int | None,
) -> tuple[TranscriptionTask, list[int], tuple[str, float, str | None, str | None, datetime | None]] | None:
    with _task_transition_lock:
        db.expire_all()
        row = get_task_row(db, task_id)
        if row is None:
            return None
        _require_not_active(row, "retry chunks for")
        query = db.query(TranscriptionChunk).filter(TranscriptionChunk.task_id == task_id)
        if chunk_id is not None:
            chunk = query.filter(TranscriptionChunk.id == chunk_id).first()
            if chunk is None:
                return None
            if chunk.status == "completed":
                raise ChunkRetryConflictError(
                    f"Cannot retry chunk {chunk_id} while it is {chunk.status}; only unfinished chunks can be retried"
                )
            failed_chunk_ids = [chunk.id]
        else:
            # "Failed" includes every chunk that never completed: a failed row
            # and the queued/transcribing rows an aborted run left behind. This
            # is what makes a failed-chunk retry resume the remaining work
            # instead of stopping at the first failed row.
            failed_chunk_ids = [
                chunk.id
                for chunk in query.filter(TranscriptionChunk.status != "completed")
                .order_by(TranscriptionChunk.idx.asc())
                .all()
            ]
            if not failed_chunk_ids:
                return row, [], (
                    row.status,
                    row.progress,
                    row.error,
                    row.error_code,
                    row.completed_at,
                )

        previous_state = (
            row.status,
            row.progress,
            row.error,
            row.error_code,
            row.completed_at,
        )
        row.status = "transcribing"
        row.completed_at = None
        row.updated_at = _utc_now()
        db.commit()
        db.refresh(row)
        task_runtime.mark_running(task_id)
        return row, failed_chunk_ids, previous_state


def _ensure_chunk_audio(db: Session, row: TranscriptionTask) -> int:
    """Re-split derived chunk audio when unfinished chunks lost their wav files.

    A failed chunk can be caused by a missing chunk wav (the fixture-case for
    ticket #5). Retrying it without the audio would fail forever, so rebuild the
    chunk windows from the task's normalized audio. Completed chunks are not
    touched: their text is reused as-is.
    """
    chunks = (
        db.query(TranscriptionChunk)
        .filter(TranscriptionChunk.task_id == row.id)
        .order_by(TranscriptionChunk.idx.asc())
        .all()
    )
    missing = []
    for chunk in chunks:
        if chunk.status == "completed":
            continue
        path = config.resolve_storage_path(chunk.audio_path)
        if path is None or not path.exists():
            missing.append(chunk)
    if not missing:
        return 0
    normalized = config.resolve_storage_path(row.normalized_audio_path)
    if normalized is None or not normalized.exists() or not row.duration_ms:
        # Leave the per-chunk retry to report CHUNK_AUDIO_MISSING with context.
        return 0
    options = read_task_options(row)
    planned = split_audio_chunks(
        normalized,
        config.get_derived_audio_dir() / f"{row.id}_chunks",
        int(row.duration_ms),
        window_ms=int(options.get("chunk_window_ms") or CHUNK_WINDOW_MS),
        overlap_ms=int(options.get("chunk_overlap_ms") or CHUNK_OVERLAP_MS),
    )
    by_idx = {index: path for index, (path, _start, _end) in enumerate(planned, 1)}
    for chunk in chunks:
        path = by_idx.get(chunk.idx)
        if path is not None:
            chunk.audio_path = config.to_storage_path(path)
    db.commit()
    return len(missing)


def _retry_claimed_chunk(
    db: Session,
    row: TranscriptionTask,
    chunk_id: int,
) -> bool:
    chunk = (
        db.query(TranscriptionChunk)
        .filter(
            TranscriptionChunk.task_id == row.id,
            TranscriptionChunk.id == chunk_id,
        )
        .first()
    )
    if chunk is None:
        row.status = "failed"
        row.error = "Chunk row disappeared during retry"
        set_error_code(row, "CHUNK_FAILED")
        db.commit()
        return False
    total = db.query(TranscriptionChunk).filter(TranscriptionChunk.task_id == row.id).count()
    path = config.resolve_storage_path(chunk.audio_path)
    if path is None or not path.exists():
        error = _mark_chunk_failed(
            db,
            row,
            chunk,
            total=total,
            cause_message="Chunk audio file not found",
            cause_code="CHUNK_AUDIO_MISSING",
            stage="chunk",
        )
        row.status = "failed_resumable"
        row.error = error.message
        set_error_code(row, "CHUNK_FAILED")
        row.progress = 100
        db.commit()
        return False
    chunk.status = "transcribing"
    chunk.progress = 20
    chunk.error = None
    chunk.error_code = None
    db.commit()
    task_logs.add_log(
        db,
        row.id,
        "chunk",
        "info",
        f"Chunk {chunk.idx} retry started",
        {"chunk_id": chunk.id},
    )
    try:
        _raise_if_cancelled(row.id)
        result = _transcribe_path_for_row(
            db,
            row,
            path,
            chunk_index=chunk.idx,
            chunk_total=total,
            chunk_start_ms=chunk.start_ms,
        )
        _raise_if_cancelled(row.id)
        chunk.text = result.text or transcript_text_from_segments(result.segments)
        chunk.segments_json = _chunk_segments_json(result.segments)
        chunk.status = "completed"
        chunk.progress = 100
        db.commit()
        task_logs.add_log(
            db,
            row.id,
            "chunk",
            "info",
            f"Chunk {chunk.idx} retry completed",
            {"chunk_id": chunk.id, "segments": len(result.segments or [])},
        )
        return True
    except Exception as exc:
        code, message, stage, command, stderr_excerpt, audio_metadata = _error_from_exception(exc)
        if code == "TASK_CANCELLED":
            _cancel_chunks(db, row.id)
            row.status = "cancelled"
            row.error = message
            set_error_code(row, "TASK_CANCELLED")
            row.progress = 100
            row.completed_at = _utc_now()
            db.commit()
            return False
        error = _mark_chunk_failed(
            db,
            row,
            chunk,
            total=total,
            cause_message=message,
            cause_code=code,
            stage=stage or "chunk",
            command=command,
            stderr_excerpt=stderr_excerpt,
            audio_metadata=audio_metadata,
        )
        row.status = "failed_resumable"
        row.error = error.message
        set_error_code(row, "CHUNK_FAILED")
        row.progress = 100
        db.commit()
        return False


def _retry_failed_chunk_ids(
    db: Session,
    row: TranscriptionTask,
    chunk_ids: list[int],
    previous_state: tuple[str, float, str | None, str | None, datetime | None],
) -> TranscriptionTaskResponse:
    task_id = row.id
    try:
        try:
            _ensure_chunk_audio(db, row)
        except Exception as exc:
            code, message, stage, _command, _stderr, _metadata = _error_from_exception(exc)
            with _task_transition_lock:
                db.expire_all()
                failed = get_task_row(db, task_id)
                if failed is not None:
                    failed.status = "failed_resumable"
                    failed.error = message
                    set_error_code(failed, "CHUNK_FAILED")
                    failed.progress = 100
                    failed.completed_at = _utc_now()
                    db.commit()
                    task_logs.add_log(db, task_id, stage or "chunk", "error", message, {"error_code": "CHUNK_FAILED"})
                    return to_response(db, failed)
            raise
        all_retried = True
        for chunk_id in chunk_ids:
            if not _retry_claimed_chunk(db, row, chunk_id):
                all_retried = False
        with _task_transition_lock:
            db.expire_all()
            row = get_task_row(db, task_id)
            if row is None:
                raise RuntimeError("Task disappeared while retrying failed chunks")
            chunks = db.query(TranscriptionChunk).filter(TranscriptionChunk.task_id == task_id).all()
            if row.status == "cancelled" or task_runtime.is_cancelled(task_id):
                _cancel_chunks(db, task_id)
                row.status = "cancelled"
                row.progress = 100
                set_error_code(row, "TASK_CANCELLED")
                row.completed_at = _utc_now()
                db.commit()
                db.refresh(row)
            elif chunks and all(chunk.status == "completed" for chunk in chunks):
                _merge_completed_chunks(db, row)
                _cleanup_completed_task_artifacts(db, task_id)
                db.refresh(row)
            elif all_retried:
                status, progress, error, error_code, completed_at = previous_state
                row.status = status
                row.progress = progress
                row.error = error
                set_error_code(row, error_code)
                row.completed_at = completed_at
                row.updated_at = _utc_now()
                db.commit()
                db.refresh(row)
            return to_response(db, row)
    except BaseException as exc:
        db.rollback()
        try:
            failed = get_task_row(db, task_id)
            if failed is not None and isinstance(exc, BeeBoxError) and exc.code == "TASK_CANCELLED":
                failed.status = "cancelled"
                failed.progress = 100
                set_error_code(failed, "TASK_CANCELLED")
                failed.completed_at = _utc_now()
                db.commit()
            elif failed is not None and failed.status == "transcribing":
                code, message, _stage, _command, _stderr, _metadata = _error_from_exception(exc)
                failed.error = message or "Chunk retry was interrupted"
                failed.progress = 100
                failed.completed_at = _utc_now()
                unfinished = (
                    db.query(TranscriptionChunk)
                    .filter(
                        TranscriptionChunk.task_id == task_id,
                        TranscriptionChunk.status == "failed",
                    )
                    .count()
                )
                if unfinished or code == "CHUNK_FAILED":
                    # A chunk-level failure is resumable: the completed chunks
                    # stay reusable and only the failed ones run again.
                    failed.status = "failed_resumable"
                    set_error_code(failed, "CHUNK_FAILED")
                else:
                    # Merge/diarization failed after every chunk completed;
                    # retrying chunks would be a no-op, so fail honestly.
                    failed.status = "failed"
                    set_error_code(failed, code)
                db.commit()
        except Exception:
            db.rollback()
            logger.exception("Failed to persist interrupted chunk retry for task %s", task_id)
        raise
    finally:
        task_runtime.mark_finished(task_id)


def retry_chunk(db: Session, task_id: str, chunk_id: int) -> TranscriptionTaskResponse | None:
    claim = _claim_failed_chunks(db, task_id, chunk_id=chunk_id)
    if claim is None:
        return None
    row, chunk_ids, previous_state = claim
    return _retry_failed_chunk_ids(db, row, chunk_ids, previous_state)


def retry_failed_chunks(db: Session, task_id: str) -> TranscriptionTaskResponse | None:
    """Queue a failed-chunk retry and return the queued task.

    The worker entry point (``run_task``) consumes the persisted intent, so the
    HTTP response never depends on GPU work finishing inline.
    """
    preparation = _prepare_failed_chunk_retry(db, task_id)
    if preparation is None:
        return None
    row, chunk_ids, _previous_state = preparation
    response = to_response(db, row)
    if chunk_ids:
        start_task_in_background(row.id)
    return response


def _run_pending_chunk_retry(db: Session, row: TranscriptionTask, pending: Any) -> None:
    """Worker-side failed-chunk retry branch, sharing the task queue."""
    previous_state = _previous_state_from_payload(pending.get("previous_state"))
    row.status = "transcribing"
    row.progress = 60
    row.updated_at = _utc_now()
    db.commit()
    try:
        _retry_failed_chunk_ids(db, row, list(pending.get("chunk_ids") or []), previous_state)
    except BaseException:
        # _retry_failed_chunk_ids persists the failure itself; the worker must
        # not turn a resumable chunk failure into a generic runtime error.
        logger.exception("Failed-chunk retry failed for task %s", row.id)
    finally:
        task_runtime.mark_finished(row.id)


def _cleanup_completed_task_artifacts(db: Session, task_id: str) -> None:
    """Apply ADR-0011 to a completed task: drop source and derived media.

    ``/v1`` jobs always drop both the source upload and derived audio; the
    legacy ``/tasks`` surface keeps its historical
    ``delete_derived_on_complete`` switch until ticket #56 retires it.

    Cleanup must never fail a completed task, so every error becomes a warning
    log entry instead of an exception.
    """
    try:
        with _task_transition_lock:
            db.expire_all()
            completed = get_task_row(db, task_id)
            if completed is None or completed.status != "completed":
                return
            v1_job = media_retention.is_v1_task(completed)
            if not v1_job and not config.delete_derived_audio_on_complete():
                return
            _cleanup_task_artifacts_for_row(db, completed)
            if v1_job:
                options = read_task_options(completed)
                file_id = options.get("_v1_file_id")
                if isinstance(file_id, str) and file_id:
                    media_retention.delete_v1_file_if_unreferenced(
                        db, file_id, exclude_task_id=completed.id
                    )
            db.commit()
    except Exception as exc:  # cleanup must not fail a completed task
        task_logs.add_log(db, task_id, "cleanup", "warning", f"Media cleanup failed: {exc}", None)


def _cleanup_task_artifacts_for_row(db: Session, row: TranscriptionTask) -> dict[str, Any]:
    removed: list[str] = []
    errors: list[str] = []
    normalized = config.resolve_storage_path(row.normalized_audio_path)
    source = config.resolve_storage_path(row.audio_path)
    for path in [normalized]:
        if path and path != source and path.exists():
            try:
                path.unlink()
                removed.append(str(path))
            except OSError as exc:
                errors.append(f"{path}: {exc}")
    chunks = db.query(TranscriptionChunk).filter(TranscriptionChunk.task_id == row.id).all()
    for chunk in chunks:
        path = config.resolve_storage_path(chunk.audio_path)
        if path and path.exists():
            try:
                path.unlink()
                removed.append(str(path))
            except OSError as exc:
                errors.append(f"{path}: {exc}")
    # The per-task chunk directory must disappear with its files; unlinking
    # files alone leaves an empty skeleton behind after every long task.
    chunks_dir = config.get_derived_audio_dir() / f"{row.id}_chunks"
    if chunks_dir.exists():
        try:
            shutil.rmtree(chunks_dir)
            removed.append(str(chunks_dir))
        except OSError as exc:
            errors.append(f"{chunks_dir}: {exc}")
    # Chunk rows are kept: they carry the per-chunk text/segments that made a
    # retry cheap and remain useful for diagnostics. Only the audio is derived.
    row.normalized_audio_path = None
    # Audio downloaded for a URL job is BeeBox-owned temporary media; ADR-0011
    # drops it together with the source it was derived from.
    if read_task_options(row).get("_source_kind") == "download" and source is not None:
        if source.exists():
            try:
                source.unlink()
                removed.append(str(source))
            except OSError as exc:
                errors.append(f"{source}: {exc}")
        if not source.exists():
            row.audio_path = ""
    return {"removed": removed, "errors": errors}


def cleanup_task_artifacts(db: Session, task_id: str) -> dict[str, Any] | None:
    with _task_transition_lock:
        db.expire_all()
        row = get_task_row(db, task_id)
        if row is None:
            return None
        _require_not_active(row, "clean up artifacts for")
        result = _cleanup_task_artifacts_for_row(db, row)
        db.commit()
        return result


def relink_task_media(db: Session, task_id: str, *, path: Path) -> TranscriptionTaskResponse | None:
    with _task_transition_lock:
        db.expire_all()
        row = get_task_row(db, task_id)
        if row is None:
            return None
        _require_not_active(row, "relink media for")
        source_path = Path(path).expanduser()
        if not source_path.is_file():
            raise FileNotFoundError(f"Audio file not found: {path}")
        source_path = source_path.resolve(strict=True)
        # Stale derived audio was generated from the previous source; invalidate it.
        _cleanup_task_artifacts_for_row(db, row)
        row.audio_path = config.to_storage_path(source_path)
        options = read_task_options(row)
        options["_source_kind"] = "external"
        _write_options(row, options)
        row.updated_at = _utc_now()
        task_logs.add_log(
            db,
            row.id,
            "relink",
            "info",
            f"Task media relinked to {source_path}",
            {},
            commit=False,
        )
        db.commit()
        db.refresh(row)
    return to_response(db, row)


def create_batch_tasks(
    db: Session,
    *,
    files: list[tuple[str, BinaryIO]],
    backend: str,
    model_name: str | None,
    language: str | None,
    output_formats: list[str],
    options: dict[str, Any] | None = None,
    validate_media: bool = False,
) -> tuple[list[TranscriptionTaskResponse], list[dict[str, str]]]:
    """Create one task per uploaded file; a bad file does not fail its siblings."""
    items: list[TranscriptionTaskResponse] = []
    failures: list[dict[str, str]] = []
    for filename, file_obj in files:
        try:
            items.append(
                create_task_from_file(
                    db,
                    filename=filename,
                    file_obj=file_obj,
                    backend=backend,
                    model_name=model_name,
                    language=language,
                    output_formats=output_formats,
                    options=options,
                    validate_media=validate_media,
                )
            )
        except Exception as exc:
            # A failing file must not poison the shared session for the rest of
            # the batch; roll back its partial writes and keep going.
            db.rollback()
            failures.append({"filename": filename, "error": str(exc)})
    return items, failures


def task_as_dict(task: TranscriptionTaskResponse) -> dict[str, Any]:
    return task.model_dump()

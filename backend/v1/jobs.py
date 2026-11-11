"""``/v1/jobs``: create, list and inspect transcription jobs.

A job is a projection of an existing ``transcription_tasks`` row. Ticket #36
covers creation, listing and status; ticket #37 adds cancel/retry/delete/SSE
on the same router and reuses :mod:`backend.v1.status`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from backend import config
from backend.database import get_db
from backend.database import session as db_session
from backend.database.models import TranscriptionChunk, TranscriptionTask, V1File
from backend.services import exports as export_service
from backend.services import media_retention
from backend.services import models as model_service
from backend.services import settings as settings_service
from backend.services import speakers as speaker_service
from backend.services import tasks as task_service
from backend.services.task_options import (
    DEFAULT_OUTPUT_FORMATS,
    build_task_options,
    read_task_options,
)
from backend.v1.errors import ApiError, ErrorResponse
from backend.v1.schemas import (
    JobCreateRequest,
    JobListResponse,
    JobOptionsInfo,
    JobResponse,
    JobResultResponse,
    JobSourceInfo,
    PublicJobStatus,
    ResultParagraph,
    ResultSegment,
    ResultSpeaker,
)
from backend.v1.status import public_job_state

router = APIRouter(
    prefix="/v1/jobs",
    tags=["v1"],
    responses={401: {"model": ErrorResponse}},
)

# Public status -> internal task statuses (the inverse of the projection).
_STATUS_FILTERS: dict[str, set[str]] = {
    "queued": {"created", "queued"},
    "running": {
        "importing",
        "preprocessing",
        "waiting_model",
        "downloading_model",
        "transcribing",
        "postprocessing",
        "exporting",
    },
    "succeeded": {"completed"},
    "failed": {"failed", "failed_resumable", "interrupted"},
    "cancelled": {"cancelled"},
}


def _source_info(row: TranscriptionTask, options: dict) -> JobSourceInfo:
    stored = options.get("_v1_source")
    source = dict(stored) if isinstance(stored, dict) else {}
    return JobSourceInfo(
        type=str(source.get("type") or "file"),
        file_id=source.get("file_id") or options.get("_v1_file_id"),
        filename=source.get("filename") or row.filename,
        text_source=str(source.get("text_source") or "asr"),
        language=row.language,
        duration_ms=row.duration_ms,
        url=source.get("url"),
        title=source.get("title"),
        author=source.get("author"),
        part=source.get("part"),
        part_title=source.get("part_title"),
        subtitle_language=source.get("subtitle_language"),
    )


def _num_speakers(options: dict) -> int | None:
    value = options.get("num_speakers")
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return value if isinstance(value, int) else None


def _options_info(row: TranscriptionTask, options: dict) -> JobOptionsInfo:
    subtitle_language = options.get("subtitle_language")
    prefer = options.get("prefer")
    return JobOptionsInfo(
        language=row.language,
        diarization=bool(options.get("diarization", True)),
        num_speakers=_num_speakers(options),
        model=row.model_name or config.DEFAULT_LOCAL_MODEL_NAME,
        subtitle_language=subtitle_language if isinstance(subtitle_language, str) else None,
        prefer=prefer if prefer == "asr" else None,
    )


def job_response(row: TranscriptionTask) -> JobResponse:
    """Project one task row onto the public job contract."""
    options = read_task_options(row)
    state = public_job_state(row)
    return JobResponse(
        id=row.id,
        status=state.status,
        stage=state.stage,
        progress=state.progress,
        error=row.error,
        error_code=state.error_code,
        retryable=state.retryable,
        source=_source_info(row, options),
        options=_options_info(row, options),
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
    )


def _resolve_model(db: Session, requested: str | None) -> str:
    settings = settings_service.get_settings(db)
    model_name = requested or settings.default_model_name or config.DEFAULT_LOCAL_MODEL_NAME
    if model_service.get_model_config(model_name) is None:
        raise ApiError(400, f"Unknown model: {model_name}", "MODEL_NOT_FOUND", "job")
    if model_service.is_model_downloaded(model_name):
        compatibility = model_service.check_model_compatibility(model_name)
        if not compatibility["compatible"]:
            raise ApiError(409, compatibility["message"], "MODEL_COMPATIBILITY_FAILED", "job")
    return model_name


def _job_row(db: Session, job_id: str) -> TranscriptionTask:
    row = task_service.get_task_row(db, job_id)
    if row is None:
        raise ApiError(404, f"Job {job_id} not found", "JOB_NOT_FOUND", "job")
    return row


_ACTIVE_STATUSES = frozenset({"queued", "running"})


@router.post(
    "/{job_id}/cancel",
    response_model=JobResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def cancel_job(job_id: str, db: Session = Depends(get_db)) -> JobResponse:
    """Cancel a queued job immediately or stop a running one cooperatively."""
    row = _job_row(db, job_id)
    state = public_job_state(row)
    if state.status not in _ACTIVE_STATUSES:
        raise ApiError(
            409,
            f"Job {job_id} is {state.status}; only queued or running jobs can be cancelled",
            "JOB_NOT_CANCELLABLE",
            "cancel",
        )
    try:
        updated = task_service.cancel_task(db, job_id)
    except task_service.TaskStateConflictError as exc:
        raise ApiError(409, str(exc), "JOB_NOT_CANCELLABLE", "cancel") from exc
    if updated is None:
        raise ApiError(404, f"Job {job_id} not found", "JOB_NOT_FOUND", "cancel")
    db.expire_all()
    return job_response(_job_row(db, job_id))


@router.post(
    "/{job_id}/retry",
    response_model=JobResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def retry_job(job_id: str, db: Session = Depends(get_db)) -> JobResponse:
    """Retry a retryable failure, resuming from the first unfinished chunk."""
    row = _job_row(db, job_id)
    state = public_job_state(row)
    if state.status != "failed":
        raise ApiError(
            409,
            f"Job {job_id} is {state.status}; only failed jobs can be retried",
            "JOB_NOT_RETRYABLE",
            "retry",
        )
    if not state.retryable:
        raise ApiError(
            409,
            f"Job {job_id} failed with {state.error_code}; this failure is not retryable",
            "JOB_NOT_RETRYABLE",
            "retry",
        )
    chunk_query = db.query(TranscriptionChunk.id).filter(TranscriptionChunk.task_id == job_id)
    has_chunks = chunk_query.first() is not None
    unfinished = chunk_query.filter(TranscriptionChunk.status != "completed").first() is not None
    try:
        if has_chunks and unfinished:
            # Reuse the chunk-level retry: completed chunks keep their text.
            updated = task_service.retry_failed_chunks(db, job_id)
        else:
            # A failure before the first chunk (or after every chunk completed)
            # has no resume point; rerun the job from the start.
            updated = task_service.retry_task(db, job_id)
    except task_service.TaskStateConflictError as exc:
        raise ApiError(409, str(exc), "JOB_NOT_RETRYABLE", "retry") from exc
    if updated is None:
        raise ApiError(404, f"Job {job_id} not found", "JOB_NOT_FOUND", "retry")
    db.expire_all()
    return job_response(_job_row(db, job_id))


def _job_file_id(row: TranscriptionTask) -> str | None:
    """The upload a file-sourced job owns, if any."""
    options = read_task_options(row)
    file_id = options.get("_v1_file_id")
    if isinstance(file_id, str) and file_id:
        return file_id
    source = options.get("_v1_source")
    if isinstance(source, dict):
        source_file_id = source.get("file_id")
        if isinstance(source_file_id, str) and source_file_id:
            return source_file_id
    return None


@router.delete(
    "/{job_id}",
    status_code=204,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def delete_job(job_id: str, db: Session = Depends(get_db)) -> Response:
    """Delete a terminal job, its result and its now-unreferenced source."""
    row = _job_row(db, job_id)
    state = public_job_state(row)
    if state.status in _ACTIVE_STATUSES:
        raise ApiError(
            409,
            f"Job {job_id} is {state.status}; cancel it before deleting",
            "JOB_ACTIVE",
            "delete",
        )
    file_id = _job_file_id(row)
    try:
        deleted = task_service.delete_task(db, job_id)
    except task_service.TaskStateConflictError as exc:
        raise ApiError(409, str(exc), "JOB_ACTIVE", "delete") from exc
    if not deleted:
        raise ApiError(404, f"Job {job_id} not found", "JOB_NOT_FOUND", "delete")
    if file_id is not None:
        # Explicit deletion ends the media lifecycle immediately instead of
        # waiting for the retention window (#40); a file another job still
        # needs is kept by the ownership check.
        media_retention.delete_v1_file_if_unreferenced(db, file_id)
    return Response(status_code=204)


@router.post(
    "",
    response_model=JobResponse,
    status_code=201,
    responses={
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
)
def create_job(request: JobCreateRequest, db: Session = Depends(get_db)) -> JobResponse:
    if request.kind != "transcription":
        raise ApiError(
            400,
            f"Unsupported job kind: {request.kind}",
            "UNSUPPORTED_KIND",
            "job",
        )
    if request.source.type == "url":
        if not request.source.url or not request.source.url.strip():
            raise ApiError(400, "source.url is required for url sources", "INVALID_SOURCE", "job")
        from backend.services import video_jobs
        from backend.services.video_platform import VideoPlatformError

        try:
            row = video_jobs.create_video_job(
                db,
                url=request.source.url,
                subtitle_language=request.options.subtitle_language,
                prefer=request.options.prefer,
                options=request.options.model_dump(exclude_none=True),
            )
        except VideoPlatformError as exc:
            # A malformed or unsupported link is a caller mistake: no job row
            # is written, so the task list cannot grow from a rejected link.
            raise ApiError(400, exc.message, exc.code, "job") from exc
        return job_response(row)
    if request.source.type != "file":
        raise ApiError(
            400,
            f"Unsupported source type: {request.source.type}",
            "UNSUPPORTED_SOURCE_TYPE",
            "job",
        )
    if not request.source.file_id:
        raise ApiError(400, "source.file_id is required for file sources", "INVALID_SOURCE", "job")

    file_row = db.get(V1File, request.source.file_id)
    if file_row is None:
        raise ApiError(404, f"File {request.source.file_id} not found", "FILE_NOT_FOUND", "job")
    media_path = config.resolve_storage_path(file_row.storage_path)
    if media_path is None or not media_path.is_file():
        raise ApiError(404, f"File {file_row.id} content is no longer stored", "FILE_NOT_FOUND", "job")

    options = request.options
    model_name = _resolve_model(db, options.model)
    task_options = build_task_options(
        num_speakers=options.num_speakers,
        # The contract default is diarization on when the caller omits it.
        diarization=True if options.diarization is None else options.diarization,
        vad=None,
        word_timestamps=None,
        postprocess_mode=None,
        traditional_to_simplified=None,
    )
    task_options["_source_kind"] = "external"
    task_options["_v1_file_id"] = file_row.id
    task_options["_v1_source"] = {
        "type": "file",
        "file_id": file_row.id,
        "filename": file_row.filename,
        "text_source": "asr",
    }
    created = task_service.create_task_from_media(
        db,
        filename=file_row.filename,
        audio_path=media_path,
        model_name=model_name,
        language=options.language,
        output_formats=list(DEFAULT_OUTPUT_FORMATS),
        options=task_options,
    )
    row = task_service.get_task_row(db, created.id)
    if row is None:  # pragma: no cover - the row was just committed
        raise ApiError(500, "Job row disappeared after creation", "JOB_CREATION_FAILED", "job")
    return job_response(row)


@router.get("", response_model=JobListResponse, responses={422: {"model": ErrorResponse}})
def list_jobs(
    db: Session = Depends(get_db),
    status: PublicJobStatus | None = Query(default=None),
    kind: str | None = Query(default=None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> JobListResponse:
    if kind is not None and kind != "transcription":
        raise ApiError(400, f"Unsupported job kind: {kind}", "UNSUPPORTED_KIND", "job")
    query = db.query(TranscriptionTask)
    if status is not None:
        query = query.filter(TranscriptionTask.status.in_(_STATUS_FILTERS[status]))
    total = query.count()
    rows = (
        query.order_by(TranscriptionTask.created_at.desc(), TranscriptionTask.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return JobListResponse(items=[job_response(row) for row in rows], total=total)


@router.get(
    "/{job_id}",
    response_model=JobResponse,
    responses={404: {"model": ErrorResponse}},
)
def get_job(job_id: str, db: Session = Depends(get_db)) -> JobResponse:
    return job_response(_job_row(db, job_id))


def _require_succeeded(row: TranscriptionTask, stage: str) -> None:
    if row.status != "completed":
        raise ApiError(
            409,
            f"Job {row.id} is {public_job_state(row).status}; the result is not available yet",
            "JOB_NOT_SUCCEEDED",
            stage,
        )


@router.get(
    "/{job_id}/result",
    response_model=JobResultResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def get_job_result(job_id: str, db: Session = Depends(get_db)) -> JobResultResponse:
    row = _job_row(db, job_id)
    _require_succeeded(row, "result")
    task = task_service.get_task(db, job_id)
    if task is None:  # pragma: no cover - the row was fetched above
        raise ApiError(404, f"Job {job_id} not found", "JOB_NOT_FOUND", "result")

    segments = [
        ResultSegment(
            id=segment.id,
            start=segment.start,
            end=segment.end,
            text=segment.text,
            speaker=segment.speaker,
            confidence=segment.confidence,
            paragraph=segment.paragraph,
        )
        for segment in task.segments
    ]
    paragraphs = [
        ResultParagraph(
            index=index,
            start=group[0].start,
            end=group[-1].end,
            speaker=group[0].speaker,
            text=export_service.paragraph_body(group),
            segment_ids=[segment.id for segment in group],
        )
        for index, group in enumerate(export_service.paragraph_groups(task.segments), 1)
    ]
    speakers = [
        ResultSpeaker(**speaker) for speaker in speaker_service.list_speakers(db, job_id)
    ]
    options = read_task_options(row)
    return JobResultResponse(
        job_id=job_id,
        status="succeeded",
        text=row.text or "",
        language=row.language,
        duration_ms=row.duration_ms,
        segments=segments,
        paragraphs=paragraphs,
        speakers=speakers,
        source=_source_info(row, options),
    )


_EXPORT_MEDIA_TYPES = {
    "txt": "text/plain; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "srt": "application/x-subrip; charset=utf-8",
}


@router.get(
    "/{job_id}/exports/{format}",
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def export_job(job_id: str, format: str, db: Session = Depends(get_db)) -> Response:
    row = _job_row(db, job_id)
    _require_succeeded(row, "export")
    normalized = export_service.normalize_export_format(format)
    if normalized is None:
        raise ApiError(400, f"Unsupported export format: {format}", "UNSUPPORTED_FORMAT", "export")
    task = task_service.get_task(db, job_id)
    if task is None:  # pragma: no cover - the row was fetched above
        raise ApiError(404, f"Job {job_id} not found", "JOB_NOT_FOUND", "export")

    title = export_service.export_stem(task.filename)
    if normalized == "txt":
        content = export_service.render_txt(title, task.segments)
    elif normalized == "md":
        content = export_service.render_markdown(title, task.segments)
    else:
        content = export_service.render_srt(task.segments)
    filename = f"{title}.{normalized}"
    return Response(
        content=content,
        media_type=_EXPORT_MEDIA_TYPES[normalized],
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
_EVENT_POLL_SECONDS = 0.2


def _job_event_snapshot(job_id: str) -> dict | None:
    """Read the public projection in its own short-lived session."""
    db_session.init_db()
    db = db_session.SessionLocal()
    try:
        row = task_service.get_task_row(db, job_id)
        if row is None:
            return None
        state = public_job_state(row)
        return {
            "id": row.id,
            "status": state.status,
            "stage": state.stage,
            "progress": state.progress,
            "error": row.error,
            "error_code": state.error_code,
            "retryable": state.retryable,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
    finally:
        db.close()


async def _job_event_stream(job_id: str) -> AsyncIterator[dict[str, str]]:
    """Emit one event per projection change, then close on a terminal state.

    Polling the row keeps the stream correct even when progress changes are
    written by another process; the standard polling path stays available and
    SSE is purely additive (ADR-0009).
    """
    previous: dict | None = None
    while True:
        snapshot = await asyncio.to_thread(_job_event_snapshot, job_id)
        if snapshot is None:
            yield {"event": "job.deleted", "data": json.dumps({"id": job_id})}
            return
        if snapshot != previous:
            previous = snapshot
            yield {"event": "job", "data": json.dumps(snapshot, ensure_ascii=False)}
            if snapshot["status"] in _TERMINAL_STATUSES:
                return
        await asyncio.sleep(_EVENT_POLL_SECONDS)


@router.get(
    "/{job_id}/events",
    responses={404: {"model": ErrorResponse}},
)
async def job_events(job_id: str) -> EventSourceResponse:
    """Stream status/stage/progress changes for one job (optional transport)."""
    exists = await asyncio.to_thread(_job_exists, job_id)
    if not exists:
        raise ApiError(404, f"Job {job_id} not found", "JOB_NOT_FOUND", "events")
    return EventSourceResponse(_job_event_stream(job_id), ping=15)


def _job_exists(job_id: str) -> bool:
    db_session.init_db()
    db = db_session.SessionLocal()
    try:
        return task_service.get_task_row(db, job_id) is not None
    finally:
        db.close()


__all__ = ["router", "job_response"]

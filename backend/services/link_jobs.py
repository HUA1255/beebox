"""Shared plumbing for every job that obtains media from a link (spec #18).

Two sources turn an outside address into a transcription:
:mod:`backend.services.video_jobs` owns media links (``source.type = "url"``)
and :mod:`backend.services.direct_media` owns media direct links
(``source.type = "direct"``). Both write the same option keys, fetch their
media outside the GPU slot, persist the downloaded audio the same way, re-enter
the GPU queue through the same seam and fail with the same record.

That lifecycle lives here so a source module only describes what is specific to
it: how the media is obtained and which log stage and message report it. The
behaviour, the log contents and the public error codes are unchanged by this
split.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from backend import config
from backend.database import session as db_session
from backend.database.models import TranscriptionTask
from backend.services import diagnostics
from backend.services import models as model_service
from backend.services import settings as settings_service
from backend.services import task_logs
from backend.services import task_runtime
from backend.services import tasks as task_service
from backend.services.errors import BeeBoxError
from backend.services.task_options import DEFAULT_OUTPUT_FORMATS
from backend.services.video_platform import (
    TEXT_PATH_ASR,
    TEXT_SOURCE_ASR,
)
from backend.utils.events import event_bus

logger = logging.getLogger(__name__)

# Downloaded audio is temporary BeeBox-owned media, not a caller upload and not
# a path the caller owns; ADR-0011 cleanup deletes it with the finished job.
SOURCE_KIND_DOWNLOAD = "download"
DEFAULT_LINK_FAILURE_CODE = "VIDEO_PLATFORM_FAILED"

#: Options key that carries the caller's original ``source`` object.
SOURCE_OPTION = "_v1_source"
#: Options key that carries the transcription request fields.
REQUEST_OPTION = "_video_request"


def write_options(row: TranscriptionTask, options: dict[str, Any]) -> None:
    """Persist the job's options; the caller commits."""
    row.options_json = json.dumps(options, ensure_ascii=False)


def source_from_options(options: dict[str, Any]) -> dict[str, Any]:
    """The stored ``source`` object, copied so a caller may mutate it."""
    value = options.get(SOURCE_OPTION)
    return dict(value) if isinstance(value, dict) else {}


def request_from_options(options: dict[str, Any]) -> dict[str, Any]:
    """The stored transcription request fields, copied."""
    value = options.get(REQUEST_OPTION)
    return dict(value) if isinstance(value, dict) else {}


def base_options(
    source: dict[str, Any], request: dict[str, Any], *, text_path: str
) -> dict[str, Any]:
    """The options a link job starts with.

    ``text_path`` is the source's own not-yet-transcribed state: a media link
    starts on the subtitle path, a direct link on the download path. Either way
    the job is outside the GPU queue until it has ASR media.
    """
    return {
        "output_formats": list(DEFAULT_OUTPUT_FORMATS),
        # No audio to diarize until a download stores media; replaced then.
        "diarization": False,
        # Until the download stores a file, delete/retention must not unlink a
        # path on the job's behalf.
        "_source_kind": "external",
        "_video_text_path": text_path,
        REQUEST_OPTION: request,
        SOURCE_OPTION: source,
    }


def requested_asr_options(options: dict[str, Any] | None) -> dict[str, Any]:
    """Keep the contract fields that the ASR branch needs, nothing else."""
    requested = options if isinstance(options, dict) else {}
    kept: dict[str, Any] = {}
    for key in ("prefer", "language", "model", "num_speakers", "diarization"):
        value = requested.get(key)
        if value is not None:
            kept[key] = value
    if isinstance(kept.get("prefer"), str):
        kept["prefer"] = kept["prefer"].strip().lower()
    return kept


def resolve_asr_model_name(db: Session, request: dict[str, Any]) -> str:
    """The model an ASR job will run; an unknown model fails before download."""
    requested = request.get("model")
    settings = settings_service.get_settings(db)
    name = str(requested or settings.default_model_name or config.DEFAULT_LOCAL_MODEL_NAME)
    if model_service.get_model_config(name) is None:
        raise BeeBoxError("MODEL_NOT_FOUND", f"Unknown model: {name}", stage="waiting_model")
    return name


def is_cancelled(row: TranscriptionTask) -> bool:
    """True when the row or its runtime ticket says the job was cancelled."""
    return row.status == "cancelled" or task_runtime.is_cancelled(row.id)


def check_cancelled(row: TranscriptionTask) -> None:
    """Stop a fetch between steps instead of finishing cancelled work."""
    if is_cancelled(row):
        raise BeeBoxError("TASK_CANCELLED", "Task was cancelled", stage="cancel")


def store_downloaded_audio(
    db: Session,
    row: TranscriptionTask,
    options: dict[str, Any],
    source: dict[str, Any],
    *,
    audio_path: Path,
    model_name: str,
    request: dict[str, Any],
    log_stage: str,
    log_message: str,
) -> None:
    """Persist a validated download and put the job back in the GPU queue.

    The file becomes BeeBox-owned temporary media, the text path becomes ASR,
    and the row re-enters through the normal queue seam so ordering, pausing
    and the one-at-a-time rule stay with the GPU queue (ADR-0009). A retried
    ASR failure therefore stays on the GPU path and never re-downloads.

    ``log_stage`` and ``log_message`` are the source's own wording; nothing
    else differs between a media link and a direct link.
    """
    options.update(
        {
            "_source_kind": SOURCE_KIND_DOWNLOAD,
            "_video_text_path": TEXT_PATH_ASR,
            "diarization": bool(request.get("diarization", True)),
        }
    )
    if request.get("num_speakers") is not None:
        options["num_speakers"] = request["num_speakers"]
    source.update({"text_source": TEXT_SOURCE_ASR, "subtitle_language": None})
    options[SOURCE_OPTION] = source
    write_options(row, options)
    row.audio_path = config.to_storage_path(audio_path)
    row.source = "local"
    row.model_name = model_name
    if request.get("language") is not None:
        row.language = request["language"]
    db.commit()
    task_logs.add_log(
        db,
        row.id,
        log_stage,
        "info",
        log_message,
        {"model": row.model_name, "bytes": audio_path.stat().st_size},
    )
    task_service.set_task_state(db, row, "queued", 0)


def record_job_failure(db: Session, job_id: str, exc: BaseException) -> None:
    """Persist a failed link job: status, error code, diagnostic, log, event."""
    try:
        db.rollback()
        fresh = task_service.get_task_row(db, job_id)
        if fresh is None or fresh.status == "cancelled":
            return
        code = exc.code if isinstance(exc, BeeBoxError) else DEFAULT_LINK_FAILURE_CODE
        message = exc.message if isinstance(exc, BeeBoxError) else str(exc)
        stage = (exc.stage if isinstance(exc, BeeBoxError) else None) or "video"
        cancelled = code == "TASK_CANCELLED"
        fresh.status = "cancelled" if cancelled else "failed"
        fresh.error = message
        task_service.set_error_code(fresh, code)
        fresh.progress = 100
        fresh.updated_at = datetime.now(UTC)
        db.commit()
        diagnostics.record_task_diagnostic(
            db,
            task_id=fresh.id,
            stage=stage,
            error_code=code,
            message=message,
        )
        task_logs.add_log(db, fresh.id, stage, "error", message, {"error_code": code})
        event_bus.publish(
            "task.updated" if cancelled else "task.failed",
            {
                "id": fresh.id,
                "status": fresh.status,
                "progress": fresh.progress,
                "error": fresh.error,
                "error_code": fresh.error_code,
            },
        )
    except Exception:
        logger.exception("Failed to persist link job failure for %s", job_id)


def hand_off_to_gpu_queue(job_id: str, *, label: str) -> None:
    """Submit an ASR-ready job as an ordinary transcription (ADR-0009).

    ``label`` only shapes the failure log ("video" or "direct").
    """
    try:
        task_service.start_task_in_background(job_id)
    except Exception as exc:
        logger.exception("Failed to hand %s job %s to the GPU queue", label, job_id)
        db = db_session.SessionLocal()
        try:
            record_job_failure(db, job_id, exc)
        finally:
            db.close()


__all__ = [
    "DEFAULT_LINK_FAILURE_CODE",
    "REQUEST_OPTION",
    "SOURCE_KIND_DOWNLOAD",
    "SOURCE_OPTION",
    "base_options",
    "check_cancelled",
    "hand_off_to_gpu_queue",
    "is_cancelled",
    "record_job_failure",
    "request_from_options",
    "requested_asr_options",
    "resolve_asr_model_name",
    "source_from_options",
    "store_downloaded_audio",
    "write_options",
]

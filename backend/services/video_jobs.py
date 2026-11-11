"""Video-link jobs and how they obtain their transcript text (tickets #42-#44).

Creation decides the text path before any queueing: an unusable link is
rejected before any job row exists (the caller gets ``INVALID_URL`` or
``UNSUPPORTED_URL``), and a supported video job is handed to
:func:`backend.services.gpu_queue.submit_without_gpu` so its
metadata resolution never occupies the single GPU slot.

The executor asks :func:`decide_subtitle` (ticket #43) which path the video
takes, then runs it:

subtitle (#42/#43)
    A usable platform subtitle track is converted into the same
    segments/paragraphs rows ASR jobs write and the job completes without
    touching the speech engine. Automatic captions are de-rolled before
    conversion, and the text source distinguishes manual from automatic.
audio/ASR (#44)
    When no usable subtitle exists, or the caller sent ``options.prefer=asr``,
    only the audio stream is downloaded. The job is then re-routed through
    :func:`backend.services.tasks.start_task_in_background`, which submits it
    to the normal GPU queue as an ordinary transcription; the downloaded audio
    is BeeBox-owned temporary media and ticket #40's completion cleanup drops
    it. A caller-specified ``subtitle_language`` never falls back to another
    language: the job fails with ``VIDEO_SUBTITLE_UNAVAILABLE`` instead.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from backend import config
from backend.database import session as db_session
from backend.database.models import TranscriptionTask
from backend.models import TranscriptSegment
from backend.services import diagnostics
from backend.services import models as model_service
from backend.services import settings as settings_service
from backend.services import task_logs
from backend.services import task_runtime
from backend.services import tasks as task_service
from backend.services import bilibili_login
from backend.services.errors import BeeBoxError
from backend.services.paragraphs import assign_paragraphs
from backend.services.task_options import DEFAULT_OUTPUT_FORMATS, read_task_options
from backend.services.video_platform import (
    PLATFORM_BILIBILI,
    REASON_PREFER_ASR,
    REASON_SUBTITLE_LANGUAGE_UNAVAILABLE,
    TEXT_PATH_ASR,
    TEXT_PATH_SUBTITLE,
    TEXT_SOURCE_ASR,
    TEXT_SOURCE_AUTO,
    TEXT_SOURCE_MANUAL,
    TEXT_SOURCE_PENDING,
    TRACK_KIND_AUTO,
    TRACK_KIND_MANUAL,
    AudioDownloadError,
    DownloadOptions,
    LoginRequiredError,
    SubtitleCue,
    SubtitleUnavailableError,
    clean_auto_captions,
    decide_subtitle,
    download_options,
    get_video_platform,
    parse_video_url,
)
from backend.utils.events import event_bus
from backend.utils.transcript_text import transcript_text_from_segments

logger = logging.getLogger(__name__)

PREFER_ASR = "asr"
VIDEO_SOURCE_TYPE = "url"
# Downloaded audio is temporary BeeBox-owned media, not a caller upload and not
# a path the caller owns; ADR-0011 cleanup deletes it with the finished job.
SOURCE_KIND_DOWNLOAD = "download"
DEFAULT_VIDEO_FAILURE_CODE = "VIDEO_PLATFORM_FAILED"


def _write_options(row: TranscriptionTask, options: dict[str, Any]) -> None:
    row.options_json = json.dumps(options, ensure_ascii=False)


def _source(options: dict[str, Any]) -> dict[str, Any]:
    value = options.get("_v1_source")
    return dict(value) if isinstance(value, dict) else {}


def _request(options: dict[str, Any]) -> dict[str, Any]:
    value = options.get("_video_request")
    return dict(value) if isinstance(value, dict) else {}


def is_video_job(row: TranscriptionTask) -> bool:
    """True when the job was created from a video link."""
    return _source(read_task_options(row)).get("type") == VIDEO_SOURCE_TYPE


def is_audio_video_job(row: TranscriptionTask) -> bool:
    """True once a video job has chosen the speech-recognition path."""
    options = read_task_options(row)
    return (
        _source(options).get("type") == VIDEO_SOURCE_TYPE
        and options.get("_video_text_path") == TEXT_PATH_ASR
    )


def runs_outside_gpu_queue(row: TranscriptionTask) -> bool:
    """Video jobs resolve metadata/subtitles/download audio outside the GPU slot.

    Only the ASR path hands the job back to the GPU queue; the subtitle path
    never touches it at all (ADR-0009).
    """
    return is_video_job(row) and not is_audio_video_job(row)


def _base_options(source: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    return {
        "output_formats": list(DEFAULT_OUTPUT_FORMATS),
        # A subtitle job never has audio to diarize; the ASR branch replaces
        # this with the caller's preference (default on, like file jobs).
        "diarization": False,
        # A URL job owns no media file yet, so delete/retention must never
        # unlink a path on its behalf until the audio path stores one.
        "_source_kind": "external",
        "_video_text_path": TEXT_PATH_SUBTITLE,
        "_video_request": request,
        "_v1_source": source,
    }


def _with_selection_options(
    options: dict[str, Any],
    subtitle_language: str | None,
    prefer: str | None,
) -> dict[str, Any]:
    """Persist the caller's text-path requests so the executor and the API agree."""
    if subtitle_language:
        options["subtitle_language"] = subtitle_language
    if prefer:
        options["prefer"] = prefer
    return options


def _create_row(
    db: Session,
    *,
    filename: str,
    options: dict[str, Any],
) -> TranscriptionTask:
    row = TranscriptionTask(
        id=str(uuid.uuid4()),
        filename=filename,
        source="url",
        # URL jobs own no media file; the column stays empty instead of
        # pointing at a path that delete/retention must never unlink.
        audio_path="",
        status="queued",
        progress=0,
        options_json=json.dumps(options, ensure_ascii=False),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _requested_options(options: dict[str, Any] | None) -> dict[str, Any]:
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


def create_video_job(
    db: Session,
    *,
    url: str,
    subtitle_language: str | None = None,
    prefer: str | None = None,
    options: dict[str, Any] | None = None,
) -> TranscriptionTask:
    """Create a queued job row for ``url`` and start it outside the GPU queue.

    A malformed link raises ``INVALID_URL`` and an unsupported one (other host,
    playlist, collection, live stream) raises ``UNSUPPORTED_URL`` before any
    row is written: both are caller mistakes with no work to do, so the route
    answers 400 and no job resource exists. ``subtitle_language`` and
    ``prefer=asr`` restrict or override the text path; ticket #43 decides it
    against the platform's track list, and ticket #44 runs the ASR path.
    ``options`` carries the transcription request fields (language, model,
    speakers, diarization) the ASR branch needs.
    """
    value = (url or "").strip()
    request = _requested_options(options)
    ref = parse_video_url(value)

    source = {
        "type": VIDEO_SOURCE_TYPE,
        "url": ref.original_url,
        "platform": ref.platform,
        "video_id": ref.video_id,
        "part": ref.part,
        "filename": ref.original_url,
        "text_source": TEXT_SOURCE_PENDING,
    }
    row = _create_row(
        db,
        filename=ref.original_url,
        options=_with_selection_options(
            _base_options(source, request), subtitle_language, prefer
        ),
    )
    task_service.start_task_in_background(row.id)
    return row


def _check_cancelled(row: TranscriptionTask) -> None:
    if row.status == "cancelled" or task_runtime.is_cancelled(row.id):
        raise BeeBoxError("TASK_CANCELLED", "Task was cancelled", stage="cancel")


def _cues_to_segments(cues: tuple[SubtitleCue, ...]) -> list[TranscriptSegment]:
    segments: list[TranscriptSegment] = []
    for cue in cues:
        text = (cue.text or "").strip()
        if not text:
            continue
        start = max(0, cue.start_ms) / 1000
        end = max(cue.start_ms, cue.end_ms) / 1000
        segments.append(
            TranscriptSegment(id=len(segments) + 1, start=start, end=end, text=text)
        )
    return segments


def _text_source(track) -> str:
    return TEXT_SOURCE_MANUAL if track.kind == TRACK_KIND_MANUAL else TEXT_SOURCE_AUTO


def _store_metadata(
    db: Session,
    row: TranscriptionTask,
    options: dict[str, Any],
    source: dict[str, Any],
    *,
    ref,
    info,
) -> None:
    row.duration_ms = info.duration_ms
    source.update(
        {
            "title": info.title,
            "author": info.author,
            "duration_ms": info.duration_ms,
            "platform": ref.platform,
            "video_id": ref.video_id,
            "part": ref.part,
            "part_title": info.part_title,
            "filename": info.title,
        }
    )
    row.filename = info.title or row.filename
    options["_v1_source"] = source
    _write_options(row, options)
    db.commit()
    task_logs.add_log(
        db,
        row.id,
        "video",
        "info",
        "Video metadata fetched",
        {"title": info.title, "tracks": len(info.tracks), "duration_ms": info.duration_ms},
    )


def _asr_model_name(db: Session, request: dict[str, Any]) -> str:
    requested = request.get("model")
    settings = settings_service.get_settings(db)
    name = str(requested or settings.default_model_name or config.DEFAULT_LOCAL_MODEL_NAME)
    if model_service.get_model_config(name) is None:
        raise BeeBoxError("MODEL_NOT_FOUND", f"Unknown model: {name}", stage="waiting_model")
    return name


def _download_audio(
    db: Session,
    row: TranscriptionTask,
    options: dict[str, Any],
    source: dict[str, Any],
    *,
    platform,
    ref,
    download: DownloadOptions,
) -> None:
    """Download only the audio stream and hand the job to the GPU queue."""
    destination = config.get_uploads_dir() / f"{row.id}.wav"
    request = _request(options)
    # Resolve the model before spending a download on a job that cannot run.
    model_name = _asr_model_name(db, request)
    task_logs.add_log(
        db,
        row.id,
        "video",
        "info",
        "Downloading audio only for speech recognition",
        {"platform": ref.platform, "video_id": ref.video_id, "part": ref.part},
    )
    task_service.set_task_state(db, row, "preprocessing", 15)
    _check_cancelled(row)
    try:
        platform.download_audio(ref, destination, options=download)
        _check_cancelled(row)
        if not destination.is_file():
            raise AudioDownloadError("The audio download produced no file")
    except BaseException:
        # Nothing persisted this path yet, so a failed or cancelled download
        # must not leave temporary audio behind.
        destination.unlink(missing_ok=True)
        raise

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
    options["_v1_source"] = source
    _write_options(row, options)
    row.audio_path = config.to_storage_path(destination)
    row.source = "local"
    row.model_name = model_name
    if request.get("language") is not None:
        row.language = request["language"]
    db.commit()
    task_logs.add_log(
        db,
        row.id,
        "video",
        "info",
        "Audio download completed; queued for transcription",
        {"model": row.model_name, "bytes": destination.stat().st_size},
    )
    # The job re-enters through the normal queue seam: the GPU queue now owns
    # ordering, pausing and the one-at-a-time rule.
    task_service.set_task_state(db, row, "queued", 0)


def _complete_from_subtitles(
    db: Session,
    row: TranscriptionTask,
    options: dict[str, Any],
    source: dict[str, Any],
    *,
    platform,
    track,
    download: DownloadOptions,
) -> None:
    task_logs.add_log(
        db,
        row.id,
        "video",
        "info",
        "Platform subtitle selected",
        {"language": track.language, "kind": track.kind, "format": track.format},
    )
    task_service.set_task_state(db, row, "transcribing", 40)
    _check_cancelled(row)
    document = platform.download_subtitle(track, options=download)
    _check_cancelled(row)
    cues = document.cues
    if track.kind == TRACK_KIND_AUTO:
        # Automatic captions roll and repeat; manual subtitles do not.
        cues = clean_auto_captions(cues)
    if not cues:
        raise SubtitleUnavailableError(
            f"The {track.language!r} platform subtitle track contains no cues"
        )

    segments = assign_paragraphs(_cues_to_segments(cues))
    if not segments:
        raise SubtitleUnavailableError(
            f"The {track.language!r} platform subtitle track contains no text"
        )

    source.update(
        {
            "text_source": _text_source(track),
            "subtitle_language": track.language,
        }
    )
    options["_v1_source"] = source
    _write_options(row, options)
    row.language = track.language
    row.text = transcript_text_from_segments(segments)
    if row.duration_ms is None:
        row.duration_ms = int(round(segments[-1].end * 1000)) if segments else None
    task_service.store_segments(db, row, segments)
    task_logs.add_log(
        db,
        row.id,
        "video",
        "info",
        "Platform subtitle converted to transcript",
        {
            "language": track.language,
            "segments": len(segments),
            "text_source": source["text_source"],
        },
    )
    _check_cancelled(row)
    row.status = "completed"
    row.progress = 100
    row.completed_at = datetime.now(UTC)
    db.commit()
    event_bus.publish("task.completed", {"id": row.id, "status": row.status, "progress": row.progress})


def _require_bilibili_login(
    db: Session,
    row: TranscriptionTask,
    *,
    ref,
    prefer_asr: bool,
) -> None:
    """Refuse a Bilibili job unless a usable login is stored (ADR-0012).

    A Bilibili request without a login may silently lose the platform
    subtitles and the higher-quality audio, which is exactly the downgrade
    ADR-0012 forbids: the job fails with ``VIDEO_LOGIN_REQUIRED`` (retryable)
    instead. ``prefer=asr`` is the caller's explicit choice to accept whatever
    the anonymous audio contains, so it bypasses the gate.
    """
    if ref.platform != PLATFORM_BILIBILI or prefer_asr:
        return
    status = bilibili_login.login_status()
    if not bilibili_login.login_unavailable(status):
        return
    task_logs.add_log(
        db,
        row.id,
        "video",
        "warning",
        "Bilibili login unavailable; refusing anonymous access",
        {"login_state": status.state},
    )
    raise LoginRequiredError(
        "The Bilibili login is unavailable "
        f"({status.state}); sign in from the console and retry the job"
    )


def _run_video_job(db: Session, row: TranscriptionTask) -> bool:
    """Resolve the text path; return True when the GPU queue now owns the job."""
    options = read_task_options(row)
    source = _source(options)
    ref = parse_video_url(str(source.get("url") or ""))
    platform = get_video_platform()
    request = _request(options)
    # One settings object for the whole job: the configured download proxy and
    # timeout reach metadata, subtitle and audio calls alike.
    download = download_options()
    prefer_asr = str(request.get("prefer") or "").strip().lower() == PREFER_ASR
    # Fail before any network call: an unavailable login is a persistent,
    # caller-fixable condition, not something to discover halfway through.
    _require_bilibili_login(db, row, ref=ref, prefer_asr=prefer_asr)

    task_service.set_task_state(db, row, "preprocessing", 5)
    task_logs.add_log(
        db,
        row.id,
        "video",
        "info",
        "Fetching video metadata",
        {"platform": ref.platform, "video_id": ref.video_id, "part": ref.part},
    )
    _check_cancelled(row)
    info = platform.fetch_video_info(ref, options=download)

    _store_metadata(db, row, options, source, ref=ref, info=info)

    subtitle_language = options.get("subtitle_language")
    requested_language = subtitle_language if isinstance(subtitle_language, str) else None
    decision = decide_subtitle(
        info,
        subtitle_language=requested_language,
        prefer_asr=prefer_asr,
    )
    task_logs.add_log(
        db,
        row.id,
        "video",
        "info",
        "Video text path selected",
        {
            "path": decision.path,
            "reason": decision.reason,
            "language": decision.track.language if decision.track else None,
            "kind": decision.track.kind if decision.track else None,
        },
    )
    if decision.path == TEXT_PATH_ASR:
        if decision.reason == REASON_SUBTITLE_LANGUAGE_UNAVAILABLE:
            # A caller-specified language never falls back to another one.
            raise SubtitleUnavailableError(
                f"No platform subtitle track matches subtitle_language={requested_language!r}"
            )
        task_logs.add_log(
            db,
            row.id,
            "video",
            "info",
            "Speech recognition requested by the caller"
            if decision.reason == REASON_PREFER_ASR
            else "No usable platform subtitle; using speech recognition",
            {"prefer": request.get("prefer")},
        )
        _download_audio(
            db, row, options, source, platform=platform, ref=ref, download=download
        )
        return True

    _complete_from_subtitles(
        db,
        row,
        options,
        source,
        platform=platform,
        track=decision.track,
        download=download,
    )
    return False


def _record_failure(db: Session, job_id: str, exc: BaseException) -> None:
    try:
        db.rollback()
        fresh = task_service.get_task_row(db, job_id)
        if fresh is None or fresh.status == "cancelled":
            return
        code = exc.code if isinstance(exc, BeeBoxError) else DEFAULT_VIDEO_FAILURE_CODE
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
        logger.exception("Failed to persist video job failure for %s", job_id)


def _hand_off_to_gpu_queue(job_id: str) -> None:
    """Submit the audio/ASR job as an ordinary transcription (ADR-0009)."""
    try:
        task_service.start_task_in_background(job_id)
    except Exception as exc:
        logger.exception("Failed to hand video job %s to the GPU queue", job_id)
        db = db_session.SessionLocal()
        try:
            _record_failure(db, job_id, exc)
        finally:
            db.close()


def execute_video_job(job_id: str) -> None:
    """Resolve one video job outside the GPU queue.

    A subtitle job finishes here; an ASR job is handed to the GPU queue after
    downloading its audio, so this executor never holds the GPU slot while
    fetching metadata or media.
    """
    db = db_session.SessionLocal()
    handoff = False
    try:
        row = task_service.get_task_row(db, job_id)
        if row is None or row.status == "cancelled" or task_runtime.is_cancelled(job_id):
            return
        task_runtime.mark_running(job_id)
        handoff = _run_video_job(db, row)
    except Exception as exc:
        _record_failure(db, job_id, exc)
    finally:
        try:
            db.close()
        finally:
            task_runtime.mark_finished(job_id)
            if handoff:
                # Marking this executor finished first prevents its exit from
                # clearing the queue bookkeeping the hand-off just created.
                _hand_off_to_gpu_queue(job_id)


__all__ = [
    "DEFAULT_VIDEO_FAILURE_CODE",
    "SOURCE_KIND_DOWNLOAD",
    "TEXT_PATH_ASR",
    "TEXT_PATH_SUBTITLE",
    "create_video_job",
    "execute_video_job",
    "is_audio_video_job",
    "is_video_job",
    "runs_outside_gpu_queue",
]

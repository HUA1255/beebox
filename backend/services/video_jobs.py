"""Video-link jobs and how they obtain their transcript text (tickets #42-#44).

Creation decides the text path before any queueing: an unusable link is
rejected before any job row exists (the caller gets ``INVALID_URL`` or
``UNSUPPORTED_URL``), and a supported video job is handed to
:func:`backend.services.gpu_queue.submit_without_gpu` so its
metadata resolution never occupies the single GPU slot. A short link (ticket
#29) is also recognized offline; execution expands it before the login gate
and any platform call, and only an X single post continues.

The executor asks :func:`decide_subtitle` (ticket #43) which path the video
takes, then runs it:

subtitle (#42/#43)
    A usable platform subtitle track is converted into the same
    segments/paragraphs rows ASR jobs write and the job completes without
    touching the speech engine. Automatic captions are de-rolled before
    conversion, and the text source distinguishes manual from automatic.
audio/ASR (#44)
    When no usable subtitle exists, or the caller sent ``options.prefer=asr``,
    only the audio stream is downloaded. A platform whose rule disables
    platform subtitles (X and Zhihu, tickets #28/#30) always takes this path,
    even when the downloader reports tracks. The job is then re-routed through
    :func:`backend.services.tasks.start_task_in_background`, which submits it
    to the normal GPU queue as an ordinary transcription; the downloaded audio
    is BeeBox-owned temporary media and ticket #40's completion cleanup drops
    it. A caller-specified ``subtitle_language`` never falls back to another
    language: the job fails with ``VIDEO_SUBTITLE_UNAVAILABLE`` instead.

The option plumbing, the post-download hand-off and the failure record are
shared with the direct media source through
:mod:`backend.services.link_jobs`; only what a media link does differently
(metadata, subtitle selection, the audio fetch) lives here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from backend import config
from backend.database import session as db_session
from backend.database.models import TranscriptionTask
from backend.models import TranscriptSegment
from backend.services import bilibili_login
from backend.services import link_jobs
from backend.services import task_logs
from backend.services import task_runtime
from backend.services import tasks as task_service
from backend.services.paragraphs import assign_paragraphs
from backend.services.task_options import read_task_options
from backend.services.video_platform import (
    REASON_PREFER_ASR,
    REASON_SUBTITLE_LANGUAGE_UNAVAILABLE,
    TEXT_PATH_ASR,
    TEXT_PATH_SUBTITLE,
    TEXT_SOURCE_AUTO,
    TEXT_SOURCE_MANUAL,
    TEXT_SOURCE_PENDING,
    TRACK_KIND_AUTO,
    TRACK_KIND_MANUAL,
    AudioDownloadError,
    DownloadOptions,
    InvalidUrlError,
    LoginRequiredError,
    SubtitleCue,
    SubtitleUnavailableError,
    UnsupportedUrlError,
    VideoRef,
    clean_auto_captions,
    decide_subtitle,
    download_options,
    get_video_platform,
    parse_video_url,
    platform_rule,
)
from backend.services.video_platform import short_links
from backend.utils.events import event_bus
from backend.utils.transcript_text import transcript_text_from_segments

PREFER_ASR = "asr"
VIDEO_SOURCE_TYPE = "url"

# Re-exported for compatibility with callers that imported the link helpers
# from this module; the implementation lives in
# :mod:`backend.services.link_jobs` so both link sources share one copy.
DEFAULT_VIDEO_FAILURE_CODE = link_jobs.DEFAULT_LINK_FAILURE_CODE
SOURCE_KIND_DOWNLOAD = link_jobs.SOURCE_KIND_DOWNLOAD
record_job_failure = link_jobs.record_job_failure
requested_asr_options = link_jobs.requested_asr_options
resolve_asr_model_name = link_jobs.resolve_asr_model_name


def is_video_job(row: TranscriptionTask) -> bool:
    """True when the job was created from a video link."""
    return (
        link_jobs.source_from_options(read_task_options(row)).get("type")
        == VIDEO_SOURCE_TYPE
    )


def is_audio_video_job(row: TranscriptionTask) -> bool:
    """True once a video job has chosen the speech-recognition path."""
    options = read_task_options(row)
    return (
        link_jobs.source_from_options(options).get("type") == VIDEO_SOURCE_TYPE
        and options.get("_video_text_path") == TEXT_PATH_ASR
    )


def runs_outside_gpu_queue(row: TranscriptionTask) -> bool:
    """Video jobs resolve metadata/subtitles/download audio outside the GPU slot.

    Only the ASR path hands the job back to the GPU queue; the subtitle path
    never touches it at all (ADR-0009).
    """
    return is_video_job(row) and not is_audio_video_job(row)


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
    )
    link_jobs.write_options(row, options)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


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
    request = link_jobs.requested_asr_options(options)
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
            link_jobs.base_options(source, request, text_path=TEXT_PATH_SUBTITLE),
            subtitle_language,
            prefer,
        ),
    )
    task_service.start_task_in_background(row.id)
    return row


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
    link_jobs.write_options(row, options)
    db.commit()
    task_logs.add_log(
        db,
        row.id,
        "video",
        "info",
        "Video metadata fetched",
        {"title": info.title, "tracks": len(info.tracks), "duration_ms": info.duration_ms},
    )


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
    request = link_jobs.request_from_options(options)
    # Resolve the model before spending a download on a job that cannot run.
    model_name = link_jobs.resolve_asr_model_name(db, request)
    task_logs.add_log(
        db,
        row.id,
        "video",
        "info",
        "Downloading audio only for speech recognition",
        {"platform": ref.platform, "video_id": ref.video_id, "part": ref.part},
    )
    task_service.set_task_state(db, row, "preprocessing", 15)
    link_jobs.check_cancelled(row)
    try:
        platform.download_audio(ref, destination, options=download)
        link_jobs.check_cancelled(row)
        if not destination.is_file():
            raise AudioDownloadError("The audio download produced no file")
    except BaseException:
        # Nothing persisted this path yet, so a failed or cancelled download
        # must not leave temporary audio behind.
        destination.unlink(missing_ok=True)
        raise

    # The job re-enters through the normal queue seam: the GPU queue now owns
    # ordering, pausing and the one-at-a-time rule.
    link_jobs.store_downloaded_audio(
        db,
        row,
        options,
        source,
        audio_path=destination,
        model_name=model_name,
        request=request,
        log_stage="video",
        log_message="Audio download completed; queued for transcription",
    )


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
    link_jobs.check_cancelled(row)
    document = platform.download_subtitle(track, options=download)
    link_jobs.check_cancelled(row)
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
    link_jobs.write_options(row, options)
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
    link_jobs.check_cancelled(row)
    row.status = "completed"
    row.progress = 100
    row.completed_at = datetime.now(UTC)
    db.commit()
    event_bus.publish("task.completed", {"id": row.id, "status": row.status, "progress": row.progress})


def _platform_subtitles_available(platform: str) -> bool:
    """Whether the platform's rule table offers usable subtitles (ticket #28).

    X and Zhihu report caption tracks that are not usable platform subtitles;
    their rule carries ``has_platform_subtitles=False`` and every such job is
    transcribed from audio. Only accepted links reach this point, so the rule
    always exists; an unknown platform is treated as having no usable
    subtitles rather than silently trusting the downloader's track list.
    """
    rule = platform_rule(platform)
    return bool(rule and rule.capabilities.has_platform_subtitles)


def _require_bilibili_login(
    db: Session,
    row: TranscriptionTask,
    *,
    ref,
    prefer_asr: bool,
) -> None:
    """Refuse a job on a login-gated platform unless a usable login exists.

    The rule table decides which platforms are gated (ADR-0012); only Bilibili
    sets ``requires_login``. A Bilibili request without a login may silently
    lose the platform subtitles and the higher-quality audio, which is exactly
    the downgrade ADR-0012 forbids: the job fails with
    ``VIDEO_LOGIN_REQUIRED`` (retryable) instead. ``prefer=asr`` is the
    caller's explicit choice to accept whatever the anonymous audio contains,
    so it bypasses the gate.
    """
    rule = platform_rule(ref.platform)
    if rule is None or not rule.capabilities.requires_login or prefer_asr:
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


def _expand_short_link(
    db: Session,
    row: TranscriptionTask,
    ref: VideoRef,
    *,
    download: DownloadOptions,
) -> VideoRef:
    """Resolve a submitted short link to the single X post it points at (#29).

    Submit stays offline, so expansion is the job's first network step and
    runs before the login gate, metadata fetch and every download. Only an X
    single post continues; every other target, login page and transport
    failure is reported with the original short link so the caller can tell
    which submission failed. Nothing has been downloaded at this point.
    """
    task_logs.add_log(
        db,
        row.id,
        "video",
        "info",
        "Expanding short link",
        {"short_link": ref.original_url},
    )
    final_url = short_links.expand_short_link(ref.original_url, options=download)
    if short_links.is_login_page(final_url, platform=ref.platform):
        raise LoginRequiredError(
            f"The short link {ref.original_url} led to a login or verification "
            "page; sign in and retry the job"
        )
    try:
        expanded = parse_video_url(final_url)
    except (InvalidUrlError, UnsupportedUrlError) as exc:
        raise UnsupportedUrlError(
            f"The short link {ref.original_url} does not point to a single X post"
        ) from exc
    if expanded.platform != ref.platform or expanded.short_link:
        raise UnsupportedUrlError(
            f"The short link {ref.original_url} does not point to a single X post"
        )
    task_logs.add_log(
        db,
        row.id,
        "video",
        "info",
        "Short link expanded",
        {"video_id": expanded.video_id},
    )
    return expanded


def _run_video_job(db: Session, row: TranscriptionTask) -> bool:
    """Resolve the text path; return True when the GPU queue now owns the job."""
    options = read_task_options(row)
    source = link_jobs.source_from_options(options)
    ref = parse_video_url(str(source.get("url") or ""))
    platform = get_video_platform()
    request = link_jobs.request_from_options(options)
    # One settings object for the whole job: the configured download proxy and
    # timeout reach metadata, subtitle and audio calls alike.
    download = download_options()
    if ref.short_link:
        # A short link hides its target: resolve it before anything else, with
        # the same settings the platform calls will receive (ticket #29).
        ref = _expand_short_link(db, row, ref, download=download)
    prefer_asr = str(request.get("prefer") or "").strip().lower() == PREFER_ASR
    # Fail before any network call: an unavailable login is a persistent,
    # caller-fixable condition, not something to discover halfway through.
    _require_bilibili_login(db, row, ref=ref, prefer_asr=prefer_asr)

    subtitle_language = options.get("subtitle_language")
    requested_language = subtitle_language if isinstance(subtitle_language, str) else None
    platform_subtitles = _platform_subtitles_available(ref.platform)
    if not platform_subtitles and requested_language:
        # The capability is known offline, so refuse before fetching anything.
        # A caller-specified language never falls back to another text source.
        task_logs.add_log(
            db,
            row.id,
            "video",
            "warning",
            "Platform has no subtitles; refusing the requested language",
            {"platform": ref.platform, "subtitle_language": requested_language},
        )
        raise SubtitleUnavailableError(
            f"{ref.platform} links have no platform subtitles; "
            f"subtitle_language={requested_language!r} cannot be used"
        )

    task_service.set_task_state(db, row, "preprocessing", 5)
    task_logs.add_log(
        db,
        row.id,
        "video",
        "info",
        "Fetching video metadata",
        {"platform": ref.platform, "video_id": ref.video_id, "part": ref.part},
    )
    link_jobs.check_cancelled(row)
    info = platform.fetch_video_info(ref, options=download)

    _store_metadata(db, row, options, source, ref=ref, info=info)

    if not platform_subtitles:
        # Never consult the reported tracks: a platform without usable
        # subtitles always transcribes its audio (tickets #28, #30).
        task_logs.add_log(
            db,
            row.id,
            "video",
            "info",
            "Platform subtitles are unavailable; using speech recognition",
            {"platform": ref.platform, "reported_tracks": len(info.tracks)},
        )
        _download_audio(
            db, row, options, source, platform=platform, ref=ref, download=download
        )
        return True

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
        link_jobs.record_job_failure(db, job_id, exc)
    finally:
        try:
            db.close()
        finally:
            task_runtime.mark_finished(job_id)
            if handoff:
                # Marking this executor finished first prevents its exit from
                # clearing the queue bookkeeping the hand-off just created.
                link_jobs.hand_off_to_gpu_queue(job_id, label="video")


__all__ = [
    "DEFAULT_VIDEO_FAILURE_CODE",
    "SOURCE_KIND_DOWNLOAD",
    "TEXT_PATH_ASR",
    "TEXT_PATH_SUBTITLE",
    "create_video_job",
    "execute_video_job",
    "is_audio_video_job",
    "is_video_job",
    "record_job_failure",
    "requested_asr_options",
    "resolve_asr_model_name",
    "runs_outside_gpu_queue",
]

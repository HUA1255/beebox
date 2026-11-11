"""Public status projection for jobs (ADR-0009).

The pipeline has many internal task states (``preprocessing``, ``transcribing``,
``failed_resumable``, ...). Callers only ever see five statuses plus a coarse
``stage`` and a 0–1 ``progress``. Everything in this module is pure so ticket
#37 (cancel/retry/SSE) and #39 (queue pause) can reuse the same vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.database.models import TranscriptionTask

PUBLIC_STATUS_QUEUED = "queued"
PUBLIC_STATUS_RUNNING = "running"
PUBLIC_STATUS_SUCCEEDED = "succeeded"
PUBLIC_STATUS_FAILED = "failed"
PUBLIC_STATUS_CANCELLED = "cancelled"
PUBLIC_STATUSES = (
    PUBLIC_STATUS_QUEUED,
    PUBLIC_STATUS_RUNNING,
    PUBLIC_STATUS_SUCCEEDED,
    PUBLIC_STATUS_FAILED,
    PUBLIC_STATUS_CANCELLED,
)

STAGE_QUEUED = "queued"
STAGE_INTAKE = "intake"
STAGE_MODEL = "model"
STAGE_TRANSCRIBE = "transcribe"
STAGE_POSTPROCESS = "postprocess"
STAGE_EXPORT = "export"
STAGE_DONE = "done"
STAGE_FAILED = "failed"
STAGE_CANCELLED = "cancelled"
STAGES = (
    STAGE_QUEUED,
    STAGE_INTAKE,
    STAGE_MODEL,
    STAGE_TRANSCRIBE,
    STAGE_POSTPROCESS,
    STAGE_EXPORT,
    STAGE_DONE,
    STAGE_FAILED,
    STAGE_CANCELLED,
)

# internal status -> (public status, public stage)
_STATUS_MAP: dict[str, tuple[str, str]] = {
    "created": (PUBLIC_STATUS_QUEUED, STAGE_QUEUED),
    "queued": (PUBLIC_STATUS_QUEUED, STAGE_QUEUED),
    "importing": (PUBLIC_STATUS_RUNNING, STAGE_INTAKE),
    "preprocessing": (PUBLIC_STATUS_RUNNING, STAGE_INTAKE),
    "waiting_model": (PUBLIC_STATUS_RUNNING, STAGE_MODEL),
    "downloading_model": (PUBLIC_STATUS_RUNNING, STAGE_MODEL),
    "transcribing": (PUBLIC_STATUS_RUNNING, STAGE_TRANSCRIBE),
    "postprocessing": (PUBLIC_STATUS_RUNNING, STAGE_POSTPROCESS),
    "exporting": (PUBLIC_STATUS_RUNNING, STAGE_EXPORT),
    "completed": (PUBLIC_STATUS_SUCCEEDED, STAGE_DONE),
    "failed": (PUBLIC_STATUS_FAILED, STAGE_FAILED),
    "failed_resumable": (PUBLIC_STATUS_FAILED, STAGE_FAILED),
    "interrupted": (PUBLIC_STATUS_FAILED, STAGE_FAILED),
    "cancelled": (PUBLIC_STATUS_CANCELLED, STAGE_CANCELLED),
}

# A failure is retryable unless the input itself is permanently unusable.
NON_RETRYABLE_ERROR_CODES = frozenset(
    {
        "UNSUPPORTED_FORMAT",
        "UNSUPPORTED_MEDIA_FORMAT",
        "NO_AUDIO_STREAM",
        "MODEL_NOT_FOUND",
        "MODEL_NOT_DOWNLOADED",
        "MODEL_COMPATIBILITY_FAILED",
        "INVALID_TRANSCRIPTION_BACKEND",
        "SPEECH_ENGINE_INVALID",
        "SPEECH_ENGINE_CONFIG_INVALID",
        # A malformed or unsupported link is not fixed by retrying it.
        "INVALID_URL",
        "UNSUPPORTED_URL",
        # Ticket #44 turns a subtitle-less video into the audio/ASR path; a
        # caller-specified language with no matching track cannot succeed on
        # retry either.
        "VIDEO_SUBTITLE_UNAVAILABLE",
        # A missing local tool is a deployment problem: retrying cannot make
        # ffmpeg/ffprobe appear (ticket #85).
        "LOCAL_TOOL_MISSING",
    }
)

DEFAULT_FAILURE_CODE = "TASK_FAILED"


@dataclass(frozen=True)
class PublicJobState:
    status: str
    stage: str
    progress: float
    error_code: str | None
    retryable: bool


def public_error_code(code: str | None, *, status: str) -> str | None:
    """Uppercase the stored code and give every failure a stable default."""
    normalized = (code or "").strip().upper()
    if normalized:
        return normalized
    if status == PUBLIC_STATUS_FAILED:
        return DEFAULT_FAILURE_CODE
    if status == PUBLIC_STATUS_CANCELLED:
        return "TASK_CANCELLED"
    return None


def _public_progress(value: float | None) -> float:
    try:
        progress = float(value or 0.0) / 100.0
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, round(progress, 4)))


def public_job_state(row: TranscriptionTask) -> PublicJobState:
    """Project one task row onto the public status vocabulary."""
    status, stage = _STATUS_MAP.get(row.status, (PUBLIC_STATUS_FAILED, STAGE_FAILED))
    error_code = public_error_code(row.error_code, status=status)
    retryable = status == PUBLIC_STATUS_FAILED and error_code not in NON_RETRYABLE_ERROR_CODES
    progress = _public_progress(row.progress)
    if status == PUBLIC_STATUS_SUCCEEDED:
        progress = 1.0
    return PublicJobState(
        status=status,
        stage=stage,
        progress=progress,
        error_code=error_code,
        retryable=retryable,
    )

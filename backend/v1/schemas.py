"""Pydantic contract models for the ``/v1`` surface.

Field names and shapes are the public contract: the committed
``openapi.json`` snapshot is generated from these models (see
``scripts/export_openapi.py``), and ticket #37-#42 extend them additively.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

PublicJobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
BilibiliLoginState = Literal["valid", "expired", "absent"]
BilibiliLoginSessionState = Literal[
    "idle", "pending", "scanned", "confirmed", "failed", "cancelled"
]


class QueueState(BaseModel):
    """The GPU queue as reported by ``/v1/health`` (ticket #39)."""

    paused: bool
    length: int = Field(description="Jobs waiting for the GPU; the running job is excluded")
    current_job_id: str | None = Field(
        default=None,
        description=(
            "Id of the running job; null on ``/v1/health`` without a valid "
            "Bearer token"
        ),
    )
    current_job_kind: str | None = Field(
        default=None,
        description=(
            "Kind of the running job; null on ``/v1/health`` without a valid "
            "Bearer token"
        ),
    )


class QueuedJob(BaseModel):
    job_id: str
    kind: str


class QueueStatusResponse(BaseModel):
    paused: bool
    length: int = Field(description="Jobs waiting for the GPU; the running job is excluded")
    current_job_id: str | None = None
    current_job_kind: str | None = None
    queued: list[QueuedJob] = Field(default_factory=list)


class GpuState(BaseModel):
    """Non-sensitive GPU facts for the health page (spec story 60)."""

    available: bool = Field(description="A CUDA device is usable by the runtime")
    name: str | None = Field(default=None, description="Device name reported by the runtime")
    memory_total_mb: int | None = Field(
        default=None, ge=0, description="Total device memory in MiB"
    )
    memory_used_mb: int | None = Field(
        default=None, ge=0, description="Device memory in use in MiB when the runtime was probed"
    )


class HealthResponse(BaseModel):
    online: bool
    ready: bool
    version: str
    commit: str | None = Field(default=None, description="Git commit SHA of the running build")
    queue: QueueState
    gpu: GpuState = Field(
        default_factory=lambda: GpuState(available=False),
        description="GPU availability and memory; never serial numbers, driver paths or PIDs",
    )
    yt_dlp_version: str | None = Field(
        default=None,
        description="Version of the downloader used for video-link jobs",
    )
    bilibili_login: BilibiliLoginState = Field(
        description=(
            "Bilibili credential state derived from the write-only cookie file: "
            "valid, expired or absent. Never contains the credential itself."
        )
    )


class BilibiliLoginStartResponse(BaseModel):
    """The freshly generated QR code handed to the console (ticket #46)."""

    status: Literal["pending"] = Field(description="The QR code is waiting to be scanned")
    qr_url: str | None = Field(
        default=None,
        description="Login URL when the platform exposes one; the image is authoritative",
    )
    qr_image: str | None = Field(
        default=None,
        description="PNG QR code as a data:image/png;base64 URL for the console",
    )
    expires_in: int = Field(ge=0, description="Seconds until the QR code expires")


class BilibiliLoginStatusResponse(BaseModel):
    """The state of the one in-flight QR login (ticket #46)."""

    status: BilibiliLoginSessionState
    detail: str | None = Field(
        default=None,
        description="Safe failure context; never contains the credential",
    )


class ModelDownloadState(BaseModel):
    """The download worker as seen by a caller (ticket #41)."""

    state: Literal["idle", "downloading", "cancelling", "succeeded", "failed", "cancelled"]
    progress: float = Field(ge=0.0, le=1.0)
    bytes_downloaded: int = Field(ge=0)
    total_bytes: int | None = None
    filename: str | None = None
    error: str | None = None
    error_code: str | None = None


class ModelInfo(BaseModel):
    """One registered model and its storage/load state."""

    id: str = Field(description="Stable registry id, e.g. whisper-base")
    name: str = Field(description="Human-readable catalog name")
    engine: str
    source: str
    repo_id: str | None = None
    size_bytes: int = Field(ge=0, description="Bytes currently used on disk")
    downloaded: bool = Field(description="A completed install is recorded")
    complete: bool = Field(description="Every file the engine needs is present")
    missing_files: list[str] = Field(default_factory=list)
    loaded: bool = Field(description="Inference state is resident")
    download: ModelDownloadState
    storage_status: Literal["available", "read_only", "unavailable"]


class ModelListResponse(BaseModel):
    items: list[ModelInfo]
    total: int


class DownloadTicketRequest(BaseModel):
    path: str = Field(
        ...,
        description="Exact v1 path the ticket may be used on, e.g. /v1/files/{id}/content",
    )
    method: Literal["GET", "HEAD"] = Field(default="GET", description="HTTP method the ticket covers")


class DownloadTicketResponse(BaseModel):
    ticket: str
    path: str
    method: str
    expires_at: datetime


class FileMetadata(BaseModel):
    id: str
    filename: str
    content_type: str | None = None
    size_bytes: int
    sha256: str
    created_at: datetime
    download_url: str


class FileListResponse(BaseModel):
    items: list[FileMetadata]
    total: int


class YouTubeCookieStatus(BaseModel):
    """The uploaded YouTube cookie jar, described without exposing its contents."""

    configured: bool
    size_bytes: int = Field(ge=0)
    cookie_count: int = Field(ge=0)
    updated_at: datetime | None = None


class JobSourceRequest(BaseModel):
    type: str = Field(default="file", description="Source type: file or url")
    file_id: str | None = Field(default=None, description="Uploaded file id for type=file")
    url: str | None = Field(
        default=None,
        description="Bilibili or YouTube video URL for type=url (ticket #42)",
    )


class JobOptionsRequest(BaseModel):
    language: str | None = None
    diarization: bool | None = None
    num_speakers: int | None = Field(default=None, ge=1, le=10)
    model: str | None = None
    subtitle_language: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{1,8})*$",
        description=(
            "Restrict a video-link job to platform subtitles in this language "
            "(BCP-47 tag such as zh-Hans or en); no other language is used"
        ),
    )
    prefer: Literal["asr"] | None = Field(
        default=None,
        description="Force the audio/speech-recognition path for a video-link job",
    )


class JobCreateRequest(BaseModel):
    kind: str = Field(default="transcription", description="Capability kind; only transcription in v1")
    source: JobSourceRequest
    options: JobOptionsRequest = Field(default_factory=JobOptionsRequest)


class JobSourceInfo(BaseModel):
    type: str
    file_id: str | None = None
    filename: str | None = None
    text_source: str = Field(
        default="asr",
        description=(
            "Where the transcript text came from: asr, platform_manual or "
            "platform_auto; platform_pending before a link job selects its path"
        ),
    )
    language: str | None = None
    duration_ms: int | None = None
    url: str | None = Field(default=None, description="Original video link for type=url")
    title: str | None = Field(default=None, description="Video title for type=url")
    author: str | None = Field(default=None, description="Video author/uploader for type=url")
    part: int | None = Field(
        default=None,
        description=(
            "Bilibili multi-part index selected by ?p=N (1 when the link "
            "names no part); null for platforms without parts"
        ),
    )
    part_title: str | None = Field(
        default=None,
        description="Title of the selected Bilibili part, from platform metadata",
    )
    subtitle_language: str | None = Field(
        default=None,
        description="Language of the platform subtitle track that produced the text",
    )


class JobOptionsInfo(BaseModel):
    language: str | None = None
    diarization: bool
    num_speakers: int | None = None
    model: str
    subtitle_language: str | None = Field(
        default=None,
        description="Requested subtitle language for a video-link job, when one was given",
    )
    prefer: Literal["asr"] | None = Field(
        default=None,
        description="Requested text path override for a video-link job",
    )


class JobResponse(BaseModel):
    id: str
    kind: Literal["transcription"] = "transcription"
    status: PublicJobStatus
    stage: str
    progress: float = Field(ge=0.0, le=1.0)
    error: str | None = None
    error_code: str | None = None
    retryable: bool = False
    source: JobSourceInfo
    options: JobOptionsInfo
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class JobListResponse(BaseModel):
    items: list[JobResponse]
    total: int


class ResultSegment(BaseModel):
    id: int
    start: float
    end: float
    text: str
    speaker: str | None = None
    confidence: float | None = None
    paragraph: int | None = None


class ResultParagraph(BaseModel):
    index: int
    start: float
    end: float
    speaker: str | None = None
    text: str
    segment_ids: list[int] = Field(default_factory=list)


class ResultSpeaker(BaseModel):
    speaker: str | None = None
    display_name: str | None = None
    segment_count: int
    duration_ms: int


class JobResultResponse(BaseModel):
    job_id: str
    status: Literal["succeeded"]
    text: str
    language: str | None = None
    duration_ms: int | None = None
    segments: list[ResultSegment]
    paragraphs: list[ResultParagraph]
    speakers: list[ResultSpeaker]
    source: JobSourceInfo

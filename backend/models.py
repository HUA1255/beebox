"""Internal response models for the task pipeline and transcription engine.

The legacy HTTP routers were deleted by ticket #56; the classes below are the
ones the services and the ``/v1`` projection still use. Legacy route-shaped
models (health filesystem, batch, runtime status, model storage inventory,
media-storage settings, readiness, preflight, ticket requests) were removed
with those routes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

TaskStatus = Literal[
    "created",
    "queued",
    "importing",
    "preprocessing",
    "waiting_model",
    "downloading_model",
    "transcribing",
    "postprocessing",
    "exporting",
    "completed",
    "failed",
    "failed_resumable",
    "cancelled",
    "interrupted",
]

TaskSourceKind = Literal["managed", "external", "download"]


class ASRModelStatus(BaseModel):
    model_name: str
    display_name: str
    engine: str
    source: str
    repo_id: str | None = None
    model_size: str
    size_mb: int
    languages: list[str]
    runtime: str
    supported_devices: list[Literal["cpu", "cuda", "mps", "mlx"]]
    supports_timestamps: bool
    supports_word_timestamps: bool
    supports_diarization: bool
    supports_streaming: bool
    downloaded: bool | None
    downloading: bool
    loaded: bool
    error: str | None = None
    size_on_disk_mb: float | None = None
    download_error: str | None = None
    download_error_code: str | None = None
    license: str | None = None
    attribution: str | None = None
    compatible: bool | None = None
    compatibility_error: str | None = None
    compatibility_error_code: str | None = None
    cache_detected: bool = False
    cache_size_mb: float | None = None
    cache_path: str | None = None
    preferred_source: str | None = None
    source_candidates: list[dict[str, Any]] = Field(default_factory=list)
    installed_source: str | None = None
    installed_repo_id: str | None = None
    last_verified_at: str | None = None
    storage_status: Literal["available", "read_only", "unavailable"] = "available"
    storage_error: str | None = None


class TranscriptSegment(BaseModel):
    id: int
    start: float
    end: float
    text: str
    speaker: str | None = None
    confidence: float | None = None
    # 1-based paragraph number within one task; None until grouping runs.
    paragraph: int | None = Field(default=None, ge=1)


class TranscriptionResult(BaseModel):
    text: str
    language: str | None = None
    duration: float | None = None
    segments: list[TranscriptSegment] = Field(default_factory=list)
    words: list[dict[str, Any]] = Field(default_factory=list)
    model_name: str | None = None
    raw_result_summary: dict[str, Any] | None = None


class ASRSettingsResponse(BaseModel):
    id: int = 1
    default_model_name: str | None = None
    default_language: str
    timestamps: bool
    word_timestamps: bool
    diarization: bool
    vad: bool
    output_formats: list[str]
    max_concurrent_local_tasks: int
    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None
    updated_at: datetime | None = None


class TranscriptionTaskResponse(BaseModel):
    id: str
    filename: str
    source: str
    audio_path: str
    normalized_audio_path: str | None = None
    status: TaskStatus
    progress: float
    language: str | None = None
    model_name: str | None = None
    duration_ms: int | None = None
    text: str | None = None
    error: str | None = None
    error_code: str | None = None
    source_kind: TaskSourceKind = "managed"
    options: dict[str, Any] = Field(default_factory=dict)
    segments: list[TranscriptSegment] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class TaskDiagnosticResponse(BaseModel):
    id: int
    task_id: str
    stage: str
    error_code: str | None = None
    message: str
    command: str | None = None
    stderr_excerpt: str | None = None
    model_name: str | None = None
    runtime_snapshot: dict[str, Any] | None = None
    audio_metadata: dict[str, Any] | None = None
    created_at: datetime


class TaskLogResponse(BaseModel):
    id: int
    task_id: str
    stage: str
    level: str
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class TaskQualityResponse(BaseModel):
    task_id: str
    warnings: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)

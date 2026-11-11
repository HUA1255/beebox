from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import declarative_base

Base = declarative_base()


def utc_now() -> datetime:
    return datetime.now(UTC)


class TranscriptionTask(Base):
    __tablename__ = "transcription_tasks"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    filename = Column(String, nullable=False)
    source = Column(String, nullable=False, default="upload")
    audio_path = Column(String, nullable=False)
    normalized_audio_path = Column(String, nullable=True)
    status = Column(String, nullable=False, default="created")
    progress = Column(Float, nullable=False, default=0.0)
    language = Column(String, nullable=True)
    model_name = Column(String, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    text = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    error_code = Column(String, nullable=True)
    options_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)
    completed_at = Column(DateTime, nullable=True)


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey("transcription_tasks.id"), nullable=False)
    idx = Column(Integer, nullable=False)
    start_ms = Column(Integer, nullable=False)
    end_ms = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)
    speaker = Column(String, nullable=True)
    confidence = Column(Float, nullable=True)
    # Task-level paragraph number (1-based). None for transcripts written before
    # the paragraph contract and for segments that have not been grouped yet.
    paragraph = Column(Integer, nullable=True)


class TranscriptionChunk(Base):
    __tablename__ = "transcription_chunks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey("transcription_tasks.id"), nullable=False)
    idx = Column(Integer, nullable=False)
    audio_path = Column(String, nullable=False)
    start_ms = Column(Integer, nullable=False)
    end_ms = Column(Integer, nullable=False)
    status = Column(String, nullable=False, default="queued")
    progress = Column(Float, nullable=False, default=0.0)
    text = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    error_code = Column(String, nullable=True)
    # Chunk-local aligned segments, JSON encoded. Null for chunks that never
    # produced a transcript.
    segments_json = Column(Text, nullable=True)


class TaskDiagnostic(Base):
    __tablename__ = "task_diagnostics"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey("transcription_tasks.id"), nullable=False)
    stage = Column(String, nullable=False)
    error_code = Column(String, nullable=True)
    message = Column(Text, nullable=False)
    command = Column(Text, nullable=True)
    stderr_excerpt = Column(Text, nullable=True)
    model_name = Column(String, nullable=True)
    runtime_snapshot_json = Column(Text, nullable=True)
    audio_metadata_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utc_now)


class TaskLog(Base):
    __tablename__ = "task_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey("transcription_tasks.id"), nullable=False)
    stage = Column(String, nullable=False)
    level = Column(String, nullable=False, default="info")
    message = Column(Text, nullable=False)
    data_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, default=utc_now)


class V1File(Base):
    """A file uploaded through ``POST /v1/files``.

    Jobs reference the stored file; keeping metadata in a table (instead of
    deriving it from the uploads directory) makes query/delete stable and lets
    later retention tickets reason about ownership.
    """

    __tablename__ = "v1_files"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    filename = Column(String, nullable=False)
    content_type = Column(String, nullable=True)
    size_bytes = Column(Integer, nullable=False)
    sha256 = Column(String(64), nullable=False)
    storage_path = Column(String, nullable=False)
    created_at = Column(DateTime, default=utc_now)


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"

    version = Column(String, primary_key=True)
    applied_at = Column(DateTime, default=utc_now)


class ASRSettings(Base):
    __tablename__ = "asr_settings"

    id = Column(Integer, primary_key=True, default=1)
    default_model_name = Column(String, nullable=True, default="qwen3-asr-1.7b")
    default_language = Column(String, nullable=False, default="auto")
    timestamps = Column(Boolean, nullable=False, default=True)
    word_timestamps = Column(Boolean, nullable=False, default=False)
    diarization = Column(Boolean, nullable=False, default=True)
    vad = Column(Boolean, nullable=False, default=True)
    output_formats_json = Column(Text, nullable=False, default='["txt","md","srt"]')
    max_concurrent_local_tasks = Column(Integer, nullable=False, default=1)
    ffmpeg_path = Column(Text, nullable=True)
    ffprobe_path = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)

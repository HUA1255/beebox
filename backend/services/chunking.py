from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backend.services.media import (
    CHUNK_OVERLAP_MS,
    CHUNK_THRESHOLD_MS,
    CHUNK_WINDOW_MS,
    exceeds_chunk_threshold,
    plan_chunk_windows,
    split_audio_chunks,
)

__all__ = [
    "CHUNK_OVERLAP_MS",
    "CHUNK_THRESHOLD_MS",
    "CHUNK_WINDOW_MS",
    "PlannedChunk",
    "needs_chunking",
    "plan_audio_chunks",
    "plan_chunk_windows",
]


@dataclass(frozen=True)
class PlannedChunk:
    idx: int
    path: Path
    start_ms: int
    end_ms: int


def needs_chunking(duration_ms: int | None) -> bool:
    """The single chunk decision shared by the inline and worker ASR paths."""
    return exceeds_chunk_threshold(duration_ms, CHUNK_THRESHOLD_MS)


def plan_audio_chunks(
    audio_path: Path,
    output_dir: Path,
    duration_ms: int | None,
    *,
    window_ms: int = CHUNK_WINDOW_MS,
    overlap_ms: int = CHUNK_OVERLAP_MS,
) -> list[PlannedChunk]:
    """Split ``audio_path`` into the contract windows and describe the parts."""
    if not needs_chunking(duration_ms):
        return []
    files = split_audio_chunks(
        audio_path,
        output_dir,
        int(duration_ms or 0),
        window_ms=window_ms,
        overlap_ms=overlap_ms,
    )
    return [
        PlannedChunk(idx=index, path=path, start_ms=start_ms, end_ms=end_ms)
        for index, (path, start_ms, end_ms) in enumerate(files, 1)
    ]

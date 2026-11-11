"""Derived speaker summaries.

Speakers are not a table: they are labels on ``transcript_segments.speaker``.
``display_speaker`` is the single display rule shared by the API and the
exporters: pure digit labels render as ``说话人 N``, custom names render
verbatim, ``None`` means unassigned.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.database.models import TranscriptSegment as DBSegment

_DIGIT_LABEL = re.compile(r"^[0-9]+$")
MAX_SPEAKER_NAME_LENGTH = 40


def display_speaker(speaker: str | None) -> str | None:
    """Render a stored speaker label for display; ``None`` stays unassigned."""
    if speaker is None:
        return None
    return f"说话人 {speaker}" if _DIGIT_LABEL.fullmatch(speaker) else speaker


def list_speakers(db: Session, task_id: str) -> list[dict[str, Any]]:
    rows = (
        db.query(
            DBSegment.speaker.label("speaker"),
            func.count(DBSegment.id).label("segment_count"),
            func.sum(DBSegment.end_ms - DBSegment.start_ms).label("duration_ms"),
            func.min(DBSegment.idx).label("first_idx"),
        )
        .filter(DBSegment.task_id == task_id)
        .group_by(DBSegment.speaker)
        .order_by(func.min(DBSegment.idx).asc())
        .all()
    )
    return [
        {
            "speaker": row.speaker,
            "display_name": display_speaker(row.speaker),
            "segment_count": int(row.segment_count),
            "duration_ms": int(row.duration_ms or 0),
        }
        for row in rows
    ]

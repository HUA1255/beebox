"""Clean platform automatic captions before they become transcript segments.

Platform automatic captions (in practice YouTube's) are not written as
sentences. Each cue repeats the last line of the cue before it, tiny cues
repeat the current line while the next one is being recognised, and short
fragments are spread over many cues. Ticket #43 asks for the repetitions to be
removed and the fragments merged into readable cues, without restoring
punctuation and without calling any model.

The cleaner is pure and operates on parsed :class:`SubtitleCue` values. It is
only applied to ``auto`` tracks; manual subtitles are already authored text and
are used exactly as uploaded.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import replace

from backend.services.video_platform.models import SubtitleCue
from backend.utils.transcript_text import ends_sentence, needs_space_between

# Merge consecutive fragments while the reading length stays at or below this
# many display units (a CJK/fullwidth character counts as two). 44 units is
# one comfortable subtitle line: about 22 Chinese characters or 44 Latin ones.
CLEAN_TARGET_UNITS = 44
# Never merge across a silence longer than this.
CLEAN_MAX_GAP_MS = 1500
# Never let a merged cue run longer than this, even with no silence.
CLEAN_MAX_CUE_MS = 8000

_CJK_RANGES = "\u2e80-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef"
_CJK_SPACE = re.compile(rf"(?<=[{_CJK_RANGES}])[ \t]+(?=[{_CJK_RANGES}])")


def display_units(text: str) -> int:
    """Approximate reading length: East-Asian wide characters count double."""
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in text)


def _normalize_spaces(text: str) -> str:
    """Drop the spaces YouTube puts between CJK words inside one caption line."""
    return _CJK_SPACE.sub("", text)


def _join_text(left: str, right: str) -> str:
    if not left:
        return right
    if not right:
        return left
    return f"{left} {right}" if needs_space_between(left, right) else f"{left}{right}"


def _cue_lines(cue: SubtitleCue) -> list[str]:
    lines = cue.lines or ((cue.text,) if cue.text else ())
    return [line for line in (_normalize_spaces(str(item).strip()) for item in lines) if line]


def _dedupe_adjacent(lines: list[str]) -> list[str]:
    deduped: list[str] = []
    for line in lines:
        if not deduped or deduped[-1] != line:
            deduped.append(line)
    return deduped


def _rolling_overlap(previous: list[str], current: list[str]) -> int:
    """How many leading ``current`` lines repeat the tail of ``previous``."""
    for size in range(min(len(previous), len(current)), 0, -1):
        if previous[-size:] == current[:size]:
            return size
    return 0


def _remove_rolling_duplicates(cues: list[SubtitleCue]) -> list[SubtitleCue]:
    kept: list[SubtitleCue] = []
    for cue in cues:
        lines = _dedupe_adjacent(_cue_lines(cue))
        if kept:
            overlap = _rolling_overlap(_cue_lines(kept[-1]), lines)
            lines = lines[overlap:]
        if not lines:
            # The whole cue repeated the previous one: keep its end time so the
            # surviving cue covers the time the text was on screen.
            if kept and cue.end_ms > kept[-1].end_ms:
                kept[-1] = replace(kept[-1], end_ms=cue.end_ms)
            continue
        kept.append(
            SubtitleCue(
                start_ms=cue.start_ms,
                end_ms=max(cue.start_ms, cue.end_ms),
                text=_join_lines(lines),
                lines=tuple(lines),
            )
        )
    return kept


def _join_lines(lines: list[str]) -> str:
    text = lines[0]
    for line in lines[1:]:
        text = _join_text(text, line)
    return text


def _merge_fragments(cues: list[SubtitleCue]) -> list[SubtitleCue]:
    merged: list[SubtitleCue] = []
    for cue in cues:
        if merged:
            previous = merged[-1]
            gap = cue.start_ms - previous.end_ms
            fits_window = gap <= CLEAN_MAX_GAP_MS and cue.end_ms - previous.start_ms <= CLEAN_MAX_CUE_MS
            fits_line = display_units(previous.text) + display_units(cue.text) <= CLEAN_TARGET_UNITS
            unfinished = not ends_sentence(previous.text)
            if fits_window and fits_line and unfinished:
                lines = tuple(_cue_lines(previous) + _cue_lines(cue))
                merged[-1] = SubtitleCue(
                    start_ms=previous.start_ms,
                    end_ms=max(previous.end_ms, cue.end_ms),
                    text=_join_text(previous.text, cue.text),
                    lines=lines,
                )
                continue
        merged.append(cue)
    return merged


def clean_auto_captions(cues) -> tuple[SubtitleCue, ...]:
    """Return the readable cues for one platform automatic caption track."""
    normalized: list[SubtitleCue] = []
    for cue in cues:
        lines = _cue_lines(cue)
        if not lines:
            continue
        normalized.append(
            SubtitleCue(
                start_ms=cue.start_ms,
                end_ms=max(cue.start_ms, cue.end_ms),
                text=_join_lines(lines),
                lines=tuple(lines),
            )
        )
    return tuple(_merge_fragments(_remove_rolling_duplicates(normalized)))


__all__ = [
    "CLEAN_MAX_CUE_MS",
    "CLEAN_MAX_GAP_MS",
    "CLEAN_TARGET_UNITS",
    "clean_auto_captions",
    "display_units",
]

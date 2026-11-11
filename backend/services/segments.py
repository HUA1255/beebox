from __future__ import annotations

import unicodedata
from typing import Iterable

from backend.models import TranscriptSegment
from backend.utils.transcript_text import (
    SENTENCE_END_CHARS,
    ends_sentence,
    needs_space_between,
)

# Characters the forced aligner never emits a timestamp for. Anything that is
# punctuation, a separator or a control/format character is re-attached to the
# neighbouring timed token so the original transcript text survives untouched.
_UNTIMED_CATEGORIES = frozenset({"P", "Z", "C"})

# Natural pause between two aligned tokens that starts a new segment.
_PAUSE_THRESHOLD_SECONDS = 0.6

# Safety cap for continuous speech without sentence punctuation or pauses.
_FALLBACK_MAX_CHARS = 60


def _is_timed_character(char: str) -> bool:
    if char.isspace():
        return False
    return unicodedata.category(char)[0] not in _UNTIMED_CATEGORIES


def build_char_timeline(text: str, items: Iterable[dict]) -> list[dict]:
    """Map every character of ``text`` to the aligner token that covers it.

    The aligner emits one item per character (CJK) or word (space-delimited
    languages) with punctuation removed. Tokens are consumed in order and
    matched to the non-punctuation characters of the original transcript, so
    punctuation can be re-attached to the preceding token (or to the first
    token when it leads the text).

    A word token may still contain in-word punctuation such as the apostrophe
    in ``Honey's``. Only the timed characters of a token consume transcript
    positions; the apostrophe stays an untimed character of the text and is
    re-attached to the preceding timed one.

    A token/text length mismatch raises ``ValueError`` instead of shifting the
    transcript: the caller must treat that as an alignment failure.
    """
    entries = list(items)
    timed_positions = [index for index, char in enumerate(text) if _is_timed_character(char)]
    token_lengths = [
        sum(1 for char in str(item.get("text") or "") if _is_timed_character(char))
        for item in entries
    ]
    if sum(token_lengths) != len(timed_positions):
        raise ValueError(
            f"aligner tokens ({sum(token_lengths)}) do not match non-punctuation text ({len(timed_positions)})"
        )

    timeline: list[dict | None] = [None] * len(text)
    cursor = 0
    for item, token_length in zip(entries, token_lengths):
        start = float(item.get("start_time") or 0.0)
        end = float(item.get("end_time") or 0.0)
        for _ in range(token_length):
            position = timed_positions[cursor]
            cursor += 1
            timeline[position] = {
                "char": text[position],
                "start": start,
                "end": end,
                "timed": True,
            }

    last_timed: dict | None = None
    for index, char in enumerate(text):
        if timeline[index] is not None:
            last_timed = timeline[index]
            continue
        if last_timed is not None:
            timeline[index] = {
                "char": char,
                "start": last_timed["end"],
                "end": last_timed["end"],
                "timed": False,
            }
            continue
        # Leading punctuation/whitespace before the first speech: pin it to the
        # start of the next timed token so it lands in the first segment.
        next_timed = next((entry for entry in timeline[index + 1 :] if entry is not None), None)
        pinned = float(next_timed["start"]) if next_timed is not None else 0.0
        timeline[index] = {"char": char, "start": pinned, "end": pinned, "timed": False}

    return [entry for entry in timeline if entry is not None]


def _is_untimed_punctuation(entry: dict) -> bool:
    return not bool(entry["timed"]) and unicodedata.category(str(entry["char"]))[0] == "P"


def _boundary_preserves_text(text: str, cut: int) -> bool:
    """Return whether cutting at ``cut`` keeps the round-tripped text verbatim.

    Segment text is stripped at both edges, so a cut is only lossless when the
    join between the two trimmed halves reproduces the original whitespace:
    an existing space must be re-inserted by the join rule and a missing space
    must not be invented.
    """
    if cut <= 0 or cut >= len(text):
        return True
    left_index = cut - 1
    while left_index >= 0 and text[left_index].isspace():
        left_index -= 1
    right_index = cut
    while right_index < len(text) and text[right_index].isspace():
        right_index += 1
    if left_index < 0 or right_index >= len(text):
        return True
    had_space = left_index != cut - 1 or right_index != cut
    inserted_space = needs_space_between(text[left_index], text[right_index])
    return had_space == inserted_space


def _sentence_boundaries(timeline: list[dict]) -> set[int]:
    boundaries: set[int] = set()
    for index, entry in enumerate(timeline):
        if entry["char"] not in SENTENCE_END_CHARS:
            continue
        cut = index + 1
        while cut < len(timeline) and _is_untimed_punctuation(timeline[cut]):
            cut += 1
        boundaries.add(cut)
    return boundaries


def _pause_boundaries(timeline: list[dict], text: str, pause_threshold: float) -> set[int]:
    """Cut at a long pause only when the speech before it finished a sentence.

    A pause in the middle of a word (``麻烦举个｜手。``) says nothing about the
    sentence structure; splitting there used to break a word across two
    segments. The text before the cut is checked as a whole so a sentence-final
    punctuation followed by a closing quote or bracket still counts.
    """
    boundaries: set[int] = set()
    if pause_threshold <= 0:
        return boundaries
    previous_end: float | None = None
    for index, entry in enumerate(timeline):
        if not entry["timed"]:
            continue
        start = float(entry["start"])
        if (
            previous_end is not None
            and start - previous_end >= pause_threshold
            and ends_sentence(text[:index])
        ):
            boundaries.add(index)
        previous_end = float(entry["end"])
    return boundaries


def _timed_prefix(timeline: list[dict]) -> list[int]:
    prefix = [0]
    for entry in timeline:
        prefix.append(prefix[-1] + (1 if entry["timed"] else 0))
    return prefix


def _fallback_cut(
    timeline: list[dict],
    text: str,
    *,
    start: int,
    index: int,
    hard_limit: bool,
    timed_prefix: list[int],
) -> int | None:
    """Choose where to force a cut once a segment reaches the fallback limit.

    Punctuation immediately at the cut is pulled into the current segment so
    the next segment never starts with it. Unless the caller supplied an
    explicit ``max_chars`` budget, the cut also has to survive the verbatim
    round-trip; the search prefers the latest safe boundary and only extends
    past the limit when no earlier one exists.
    """
    length = len(timeline)
    cut = index
    while cut < length and _is_untimed_punctuation(timeline[cut]):
        cut += 1
    if hard_limit:
        return cut if cut > start else None
    if cut >= length:
        return length
    for candidate in range(cut, start, -1):
        if timed_prefix[candidate] <= timed_prefix[start]:
            break
        if candidate < length and _is_untimed_punctuation(timeline[candidate]):
            continue
        if _boundary_preserves_text(text, candidate):
            return candidate
    for candidate in range(cut + 1, length + 1):
        if candidate < length and _is_untimed_punctuation(timeline[candidate]):
            continue
        if _boundary_preserves_text(text, candidate):
            return candidate
    return None


def _group_timeline(
    timeline: list[dict],
    text: str,
    *,
    pause_threshold: float,
    max_chars: int,
) -> list[tuple[list[str], float, float]]:
    if not timeline:
        return []
    length = len(timeline)
    hard_limit = int(max_chars) > 0
    limit = int(max_chars) if hard_limit else _FALLBACK_MAX_CHARS
    sentence_boundaries = _sentence_boundaries(timeline)
    pause_boundaries = _pause_boundaries(timeline, text, float(pause_threshold))
    timed_prefix = _timed_prefix(timeline)

    starts = [0]
    segment_start = 0
    index = 1
    while index < length:
        has_timed = timed_prefix[index] > timed_prefix[segment_start]
        if has_timed and index in sentence_boundaries and _boundary_preserves_text(text, index):
            starts.append(index)
            segment_start = index
            index = segment_start + 1
            continue
        if has_timed and index in pause_boundaries and _boundary_preserves_text(text, index):
            starts.append(index)
            segment_start = index
            index = segment_start + 1
            continue
        if has_timed and limit > 0 and index - segment_start >= limit:
            cut = _fallback_cut(
                timeline,
                text,
                start=segment_start,
                index=index,
                hard_limit=hard_limit,
                timed_prefix=timed_prefix,
            )
            if cut is not None and cut > segment_start:
                if cut >= length:
                    break
                starts.append(cut)
                segment_start = cut
                index = segment_start + 1
                continue
        index += 1

    groups: list[tuple[list[str], float, float]] = []
    for start_index, end_index in zip(starts, starts[1:] + [length]):
        entries = timeline[start_index:end_index]
        timed = [entry for entry in entries if entry["timed"]]
        if timed:
            start = float(timed[0]["start"])
            end = float(timed[-1]["end"])
        else:
            start = float(entries[0]["start"])
            end = float(entries[0]["end"])
        groups.append(([entry["char"] for entry in entries], start, end))
    return groups


def segments_from_alignment(
    text: str,
    items: Iterable[dict],
    *,
    max_chars: int = 0,
    pause_threshold: float = _PAUSE_THRESHOLD_SECONDS,
    min_duration: float = 0.05,
) -> list[TranscriptSegment]:
    """Build monotonic, non-overlapping sentence-level segments from aligner items.

    Cuts follow natural speech boundaries: sentence-final punctuation first,
    then pauses of at least ``pause_threshold`` seconds that fall after a
    finished sentence, then a ``_FALLBACK_MAX_CHARS`` safety cap for continuous
    speech. ``max_chars`` is an explicit legacy budget for other callers; ``0``
    (the default) leaves segmentation to the sentence rules. Text is preserved
    verbatim apart from leading/trailing whitespace on each segment, and
    segment ids are renumbered from 1.
    """
    if not text or not text.strip():
        return []
    minimum = max(float(min_duration), 0.001)
    timeline = build_char_timeline(text, items)
    segments: list[TranscriptSegment] = []
    previous_end = 0.0
    for chars, start, end in _group_timeline(
        timeline,
        text,
        pause_threshold=float(pause_threshold),
        max_chars=int(max_chars),
    ):
        segment_text = "".join(chars).strip()
        if not segment_text:
            continue
        start = max(float(start), previous_end)
        end = max(float(end), start + minimum)
        segments.append(
            TranscriptSegment(
                id=len(segments) + 1,
                start=start,
                end=end,
                text=segment_text,
            )
        )
        previous_end = end
    return segments

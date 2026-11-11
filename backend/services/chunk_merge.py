from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from backend.models import TranscriptSegment

DEFAULT_MIN_DURATION = 0.001
_MAX_DEDUPE_PASSES = 64

_OWNER_SELF = "self"
_OWNER_PREVIOUS = "previous"
_OWNER_NEXT = "next"


@dataclass(frozen=True)
class ChunkTranscript:
    """One chunk's recognizer output, still in chunk-local time."""

    idx: int
    start_ms: int
    end_ms: int
    segments: list[TranscriptSegment]
    text: str = ""
    aligned: bool = False
    language: str | None = None


@dataclass(frozen=True)
class MergedTranscript:
    segments: list[TranscriptSegment]
    aligned: bool


@dataclass
class _Item:
    chunk_pos: int
    chunk_positions: set[int]
    chunk_idx: int
    start: float
    end: float
    text: str
    normalised: str
    speaker: str | None
    confidence: float | None
    owner: str


def _normalise(text: str) -> str:
    """Comparison form: no whitespace or punctuation, case-folded."""
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE).casefold()


def _text_relation(left: str, right: str) -> str:
    """Classify two normalised texts that cover the same boundary speech.

    ``"same"`` means an exact re-emission, ``"left-contained"`` means ``left``
    is a truncated copy contained in ``right`` (the chunk end cut it), and
    ``"right-contained"`` is the mirror case (the chunk start cut it).
    """
    if not left or not right:
        return "none"
    if left == right:
        return "same"
    if len(left) < 2 or len(right) < 2:
        # A one-character interjection may legitimately repeat at a boundary.
        return "none"
    if right.startswith(left) or right.endswith(left):
        return "left-contained"
    if left.startswith(right) or left.endswith(right):
        return "right-contained"
    return "none"


def _strictly_overlaps(left: _Item, right: _Item) -> bool:
    return left.start < right.end and right.start < left.end


def _mostly_covers(owner: _Item, item: _Item) -> bool:
    """True when ``owner`` spans more than half of ``item`` in time."""
    overlap = min(item.end, owner.end) - max(item.start, owner.start)
    duration = item.end - item.start
    return overlap > 0 and overlap > duration / 2


def _candidate_items(
    chunks: Sequence[ChunkTranscript],
    *,
    min_duration: float,
) -> list[_Item]:
    ordered = sorted(chunks, key=lambda chunk: (chunk.start_ms, chunk.idx))
    items: list[_Item] = []
    for position, chunk in enumerate(ordered):
        chunk_start = chunk.start_ms / 1000
        chunk_end = chunk.end_ms / 1000
        previous_overlap_s = 0.0
        if position > 0:
            previous_overlap_s = max(0.0, ordered[position - 1].end_ms - chunk.start_ms) / 1000
        next_overlap_s = 0.0
        if position + 1 < len(ordered):
            next_overlap_s = max(0.0, chunk.end_ms - ordered[position + 1].start_ms) / 1000
        left_cut = chunk_start + previous_overlap_s / 2
        right_cut = chunk_end - next_overlap_s / 2
        for segment in chunk.segments:
            text = (segment.text or "").strip()
            if not text:
                continue
            start = float(segment.start) + chunk_start
            end = float(segment.end) + chunk_start
            if not chunk.aligned and segment.end <= segment.start:
                # Backends without timestamps emit one zero-length segment per
                # chunk; treat it as covering the whole chunk window.
                start = chunk_start
                end = chunk_end
            start = min(max(start, chunk_start), chunk_end)
            end = min(max(end, chunk_start), chunk_end)
            if end <= start:
                end = min(chunk_end, start + min_duration)
            if end <= start:
                start = max(chunk_start, end - min_duration)
            if end <= start:
                end = start + min_duration
            midpoint = (start + end) / 2
            owner = _OWNER_SELF
            if previous_overlap_s > 0 and midpoint < left_cut:
                owner = _OWNER_PREVIOUS
            elif next_overlap_s > 0 and midpoint >= right_cut:
                owner = _OWNER_NEXT
            items.append(
                _Item(
                    chunk_pos=position,
                    chunk_positions={position},
                    chunk_idx=chunk.idx,
                    start=start,
                    end=end,
                    text=text,
                    normalised=_normalise(text),
                    speaker=segment.speaker,
                    confidence=segment.confidence,
                    owner=owner,
                )
            )
    return items


def _deduplicate(items: list[_Item]) -> list[_Item]:
    """Drop re-emissions of the same speech that both chunks heard.

    Only items from adjacent chunks that overlap in time are compared. Exact
    copies keep the most complete span; truncated copies (prefix/suffix) are
    dropped in favour of the complete sentence. Unrelated boundary speech is
    never touched.
    """
    for _ in range(_MAX_DEDUPE_PASSES):
        changed = False
        for index, left in enumerate(items):
            for right in items[index + 1 :]:
                if left.chunk_pos == right.chunk_pos:
                    continue
                if abs(left.chunk_pos - right.chunk_pos) > 1:
                    continue
                if not _strictly_overlaps(left, right):
                    continue
                relation = _text_relation(left.normalised, right.normalised)
                if relation == "none":
                    continue
                if relation == "same":
                    winner, loser = (
                        (left, right) if (left.end, left.start) >= (right.end, right.start) else (right, left)
                    )
                elif relation == "left-contained":
                    winner, loser = right, left
                else:
                    winner, loser = left, right
                winner.start = min(winner.start, loser.start)
                winner.end = max(winner.end, loser.end)
                winner.chunk_positions |= loser.chunk_positions
                if winner.speaker is None:
                    winner.speaker = loser.speaker
                if winner.confidence is None:
                    winner.confidence = loser.confidence
                items[:] = [candidate for candidate in items if candidate is not loser]
                changed = True
                break
            if changed:
                break
        if not changed:
            break
    return items


def _suffix_prefix_split(earlier_text: str, later_normalised: str) -> int | None:
    """Find the longest suffix of ``earlier_text`` re-emitted at ``later``'s start.

    A boundary sentence can be split unevenly: the earlier chunk emits previous
    speech plus the first half of the sentence, while the later chunk re-emits
    that half and completes it. The duplicate portion is a suffix of the earlier
    segment and a prefix of the later one. Returning the split index lets the
    caller keep the earlier-only text exactly once.
    """
    if len(later_normalised) < 2:
        return None
    for position in range(len(earlier_text)):
        suffix = _normalise(earlier_text[position:])
        if len(suffix) < 2:
            break
        if len(suffix) > len(later_normalised):
            continue
        if later_normalised.startswith(suffix):
            return position
    return None


def _trim_overlapping_tails(items: list[_Item], *, min_duration: float) -> list[_Item]:
    """Remove text re-emitted at a boundary from the earlier segment's tail."""
    for _ in range(_MAX_DEDUPE_PASSES):
        changed = False
        for earlier in list(items):
            for later in list(items):
                if later.chunk_pos != earlier.chunk_pos + 1:
                    continue
                if not _strictly_overlaps(earlier, later):
                    continue
                split = _suffix_prefix_split(earlier.text, later.normalised)
                if split is None:
                    continue
                if split == 0:
                    items[:] = [candidate for candidate in items if candidate is not earlier]
                else:
                    earlier.text = earlier.text[:split].rstrip()
                    if not earlier.text:
                        items[:] = [candidate for candidate in items if candidate is not earlier]
                    else:
                        earlier.normalised = _normalise(earlier.text)
                        if later.start > earlier.start + min_duration:
                            # The later chunk owns the speech from its own
                            # audio start; clipping here keeps its mapped time.
                            earlier.end = min(earlier.end, later.start)
                changed = True
                break
            if changed:
                break
        if not changed:
            break
    return items


def _drop_uncovered_boundary_copies(items: list[_Item]) -> list[_Item]:
    """Apply the core-boundary ownership rule after text de-duplication.

    A copy owned by the neighbouring chunk is dropped only when that neighbour
    actually produced overlapping speech. If the neighbour missed the utterance
    entirely, the copy is kept so boundary sentences are never lost.
    """
    kept: list[_Item] = []
    for item in items:
        if item.owner == _OWNER_SELF:
            kept.append(item)
            continue
        neighbour_position = item.chunk_pos - 1 if item.owner == _OWNER_PREVIOUS else item.chunk_pos + 1
        covered = any(
            neighbour_position in other.chunk_positions and _mostly_covers(other, item)
            for other in items
            if other is not item
        )
        if not covered:
            kept.append(item)
    return kept


def merge_chunk_transcripts(
    chunks: Sequence[ChunkTranscript],
    *,
    min_duration: float = DEFAULT_MIN_DURATION,
) -> MergedTranscript:
    """Merge chunk-local aligned items into one global, monotonic transcript.

    The contract is:
    - every item is offset by its chunk ``start_ms`` into global time;
    - the 2 s overlap is owned deterministically: the midpoint boundary rule
      gives each chunk the non-overlapping core plus half of each overlap;
    - text re-emitted in the overlap is de-duplicated (including a truncated
      copy of a sentence cut by the chunk end);
    - the result is sorted and every segment satisfies ``start < end`` without
      overlapping its predecessor. Distinct boundary speech survives.
    """
    if not chunks:
        return MergedTranscript(segments=[], aligned=False)
    items = _candidate_items(chunks, min_duration=min_duration)
    items = _deduplicate(items)
    items = _trim_overlapping_tails(items, min_duration=min_duration)
    items = _drop_uncovered_boundary_copies(items)
    items.sort(key=lambda item: (item.start, item.chunk_pos, item.end, _normalise(item.text)))

    segments: list[TranscriptSegment] = []
    previous_end = 0.0
    for item in items:
        start = max(item.start, previous_end)
        end = max(item.end, start + min_duration)
        segments.append(
            TranscriptSegment(
                id=len(segments) + 1,
                start=start,
                end=end,
                text=item.text,
                speaker=item.speaker,
                confidence=item.confidence,
                # Paragraphs are task-level and assigned after the merge, so
                # merged segments always start ungrouped.
                paragraph=None,
            )
        )
        previous_end = end
    return MergedTranscript(segments=segments, aligned=_aligned_for_content_chunks(chunks))


def _aligned_for_content_chunks(chunks: list[ChunkTranscript]) -> bool:
    """Aggregate alignment only across chunks that produced text.

    A silent tail chunk is expected to have no text and an ``aligned`` flag of
    False (the ASR adapter never invokes the aligner for empty output). That
    must not mark a fully aligned multi-chunk transcript as unaligned or emit
    the false "no forced-alignment timestamps" warning.
    """
    content_chunks = [
        chunk
        for chunk in chunks
        if (chunk.text or "").strip() or any((segment.text or "").strip() for segment in chunk.segments)
    ]
    return all(chunk.aligned for chunk in content_chunks)

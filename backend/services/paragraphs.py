"""Group final transcript segments into paragraphs.

The pass is deliberately pure: it reads an ordered segment list, never mutates
it (nor the database) and returns copies carrying a 1-based ``paragraph``
number. Running it twice on its own output yields the same result.

Boundary rules, in the order they are applied:

1. a new speaker starts a new paragraph;
2. a pause of at least 1.0s starts a new paragraph only when the previous
   segment finished a sentence (sentence-final punctuation, optionally behind
   closing quotes/brackets: ``。”``, ``。」``);
3. a paragraph that already covers 120s is closed before the next segment.

Paragraphs longer than 120s (rule 3 can overshoot by one segment) are cut
back at segment boundaries, preferring the latest sentence-final punctuation
inside the window, then the latest pause of at least 0.8s that also follows a
finished sentence, then a hard cut at the last boundary that fits. Because the
pause tier also requires a finished sentence, it never reaches a boundary the
punctuation tier rejected; it only makes the preference explicit and falls
through to the hard cut, which still keeps every paragraph within 120s.

Paragraphs shorter than 10s are absorbed into a neighbouring paragraph of the
same speaker. The hard limit is not a precondition for that merge: when the
union would exceed 120s it is re-cut with the same boundary ladder, which
prefers a cut that keeps the piece before the cut at 10s or more as well. A
short paragraph therefore survives only when its neighbours speak differently,
or when no segment boundary can satisfy both limits at once (a single segment
spanning almost the whole window, for instance). The soft floor never merges
across speakers, and the 120s hard limit is never broken.
"""

from __future__ import annotations

from backend.models import TranscriptSegment
from backend.utils.transcript_text import ends_sentence

# 1.0s is the pause that starts a new paragraph; 0.8s is only a fallback cut
# when a paragraph has to be trimmed to the 120s hard limit.
PARAGRAPH_PAUSE_SECONDS = 1.0
PARAGRAPH_FALLBACK_PAUSE_SECONDS = 0.8
PARAGRAPH_MAX_SECONDS = 120.0
PARAGRAPH_MIN_SECONDS = 10.0


def _duration(segments: list[TranscriptSegment]) -> float:
    return segments[-1].end - segments[0].start


def _starts_new_paragraph(paragraph: list[TranscriptSegment], segment: TranscriptSegment) -> bool:
    previous = paragraph[-1]
    paused = segment.start - previous.end >= PARAGRAPH_PAUSE_SECONDS
    return (
        segment.speaker != previous.speaker
        or (paused and ends_sentence(previous.text))
        or previous.end - paragraph[0].start >= PARAGRAPH_MAX_SECONDS
    )


def _clears_soft_floors(group: list[TranscriptSegment], cut: int) -> bool:
    """True when splitting ``group`` at ``cut`` leaves both pieces at 10s or more.

    The piece after the cut is the one the soft-floor merge used to protect;
    checking the piece before it too is what stops a re-cut from simply moving
    the short paragraph to the other side of the boundary.
    """
    return (
        group[cut - 1].end - group[0].start >= PARAGRAPH_MIN_SECONDS
        and group[-1].end - group[cut].start >= PARAGRAPH_MIN_SECONDS
    )


def _prefer_complete_tail(
    group: list[TranscriptSegment], boundaries: list[int]
) -> int:
    """Pick the latest boundary that leaves a full-size tail when possible.

    Cutting at the very last boundary of a paragraph that just crossed 120s
    can leave a few seconds behind; that tail could not be merged back (the
    merged duration would exceed the hard limit). Preferring an earlier
    boundary of the same kind keeps both paragraphs within the soft floor.

    This is the hard-limit fallback, reached only when no boundary keeps both
    pieces above the soft floor: a full tail still wins over the latest cut.
    """
    group_end = group[-1].end
    roomy = [
        boundary
        for boundary in boundaries
        if group_end - group[boundary].start >= PARAGRAPH_MIN_SECONDS
    ]
    return (roomy or boundaries)[-1]


def _choose_cut(group: list[TranscriptSegment]) -> int:
    """Return the segment index where ``group`` is split, or 0 if impossible.

    The cut has to stay inside the 120s window. Among those boundaries one
    that keeps both pieces at or above the 10s floor is preferred, following
    the punctuation / pause / hard-cut ladder; only when no such boundary
    exists does the hard limit win on its own, keeping a full tail if it can.
    """
    window_end = group[0].start + PARAGRAPH_MAX_SECONDS
    boundaries = [
        index
        for index in range(1, len(group))
        if group[index - 1].end <= window_end
    ]
    if not boundaries:
        return 0
    tiers = (
        [index for index in boundaries if ends_sentence(group[index - 1].text)],
        [
            index
            for index in boundaries
            if ends_sentence(group[index - 1].text)
            and group[index].start - group[index - 1].end >= PARAGRAPH_FALLBACK_PAUSE_SECONDS
        ],
        boundaries,
    )
    for tier in tiers:
        balanced = [index for index in tier if _clears_soft_floors(group, index)]
        if balanced:
            return balanced[-1]
    for tier in tiers:
        if tier:
            return _prefer_complete_tail(group, tier)
    return boundaries[-1]


def _split_over_limit(group: list[TranscriptSegment]) -> list[list[TranscriptSegment]]:
    pieces: list[list[TranscriptSegment]] = []
    remaining = group
    while len(remaining) > 1 and _duration(remaining) > PARAGRAPH_MAX_SECONDS:
        cut = _choose_cut(remaining)
        if cut <= 0:
            break
        pieces.append(remaining[:cut])
        remaining = remaining[cut:]
    pieces.append(remaining)
    return pieces


def _can_merge(
    left: list[TranscriptSegment], right: list[TranscriptSegment]
) -> bool:
    return (
        left[0].speaker == right[0].speaker
        and _duration(left + right) <= PARAGRAPH_MAX_SECONDS
    )


def _can_be_re_cut(neighbour: list[TranscriptSegment]) -> bool:
    """True when a union with ``neighbour`` can be cut back to the hard limit.

    Everything reaching the merge pass is already within 120s except a single
    segment longer than that, which no cut can fix. Merging a short paragraph
    onto such a segment would leave one uncuttable paragraph over the limit,
    so those neighbours never take part in an absorption.
    """
    return _duration(neighbour) <= PARAGRAPH_MAX_SECONDS


def _absorb_short_paragraphs(
    groups: list[list[TranscriptSegment]],
) -> list[list[TranscriptSegment]]:
    """Merge every short paragraph into a neighbour of the same speaker.

    A neighbour whose union already fits the hard limit wins, the right one
    first to keep the historical bias. When neither union fits, the short
    paragraph still joins the right neighbour (then the left one) as long as
    the union can be cut back: it is re-split by ``_split_over_limit``
    afterwards instead of standing next to its own speaker. Speakers never
    mix, and an unsplittable over-limit neighbour is never merged into.
    """
    merged = list(groups)
    index = 0
    while index < len(merged):
        group = merged[index]
        if _duration(group) >= PARAGRAPH_MIN_SECONDS:
            index += 1
            continue
        left = merged[index - 1] if index > 0 else None
        right = merged[index + 1] if index + 1 < len(merged) else None
        right_joins = (
            right is not None
            and right[0].speaker == group[0].speaker
            and _can_be_re_cut(right)
        )
        left_joins = (
            left is not None
            and left[0].speaker == group[0].speaker
            and _can_be_re_cut(left)
        )
        if right_joins and _can_merge(group, right):
            merged[index : index + 2] = [group + right]
            continue
        if left_joins and _can_merge(left, group):
            merged[index - 1 : index + 1] = [left + group]
            index -= 1
            continue
        if right_joins:
            merged[index : index + 2] = [group + right]
            continue
        if left_joins:
            merged[index - 1 : index + 1] = [left + group]
            index -= 1
            continue
        index += 1
    return merged


def _short_paragraph_violations(groups: list[list[TranscriptSegment]]) -> int:
    """Count short paragraphs that still border their own speaker."""
    violations = 0
    for index, group in enumerate(groups):
        if _duration(group) >= PARAGRAPH_MIN_SECONDS:
            continue
        speaker = group[0].speaker
        neighbours = groups[max(index - 1, 0) : index] + groups[index + 1 : index + 2]
        if any(neighbour[0].speaker == speaker for neighbour in neighbours):
            violations += 1
    return violations


def _layout(groups: list[list[TranscriptSegment]]) -> tuple[tuple[int, ...], ...]:
    """Identity key of a grouping, used to detect a repeating layout."""
    return tuple(tuple(id(segment) for segment in group) for group in groups)


def _merge_short_paragraphs(
    groups: list[list[TranscriptSegment]],
) -> list[list[TranscriptSegment]]:
    """Absorb short paragraphs and re-cut whatever crosses the hard limit.

    Absorbing a short paragraph can push its paragraph past 120s; the union is
    then split again, and that cut can in turn leave a short piece next to
    yet another same-speaker paragraph. The pass repeats until the layout
    stops changing. A layout that keeps a short paragraph is only returned
    when no segment boundary satisfies the soft floor and the hard limit at
    once (one segment spanning almost the whole window, for example), so the
    loop keeps the layout with the fewest surviving violations.
    """
    best = list(groups)
    best_violations = _short_paragraph_violations(best)
    current = best
    seen = {_layout(current)}
    for _ in range(2 * len(groups) + 2):
        absorbed = _absorb_short_paragraphs(current)
        bounded = [piece for group in absorbed for piece in _split_over_limit(group)]
        violations = _short_paragraph_violations(bounded)
        if violations < best_violations:
            best, best_violations = bounded, violations
            if not violations:
                return best
        layout = _layout(bounded)
        if layout in seen:
            return best
        seen.add(layout)
        current = bounded
    return best


def assign_paragraphs(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
    """Return copies of ``segments`` with contiguous 1-based paragraph numbers."""
    if not segments:
        return []

    groups: list[list[TranscriptSegment]] = [[segments[0]]]
    for segment in segments[1:]:
        if _starts_new_paragraph(groups[-1], segment):
            groups.append([segment])
        else:
            groups[-1].append(segment)

    bounded = [piece for group in groups for piece in _split_over_limit(group)]
    merged = _merge_short_paragraphs(bounded)

    return [
        segment.model_copy(update={"paragraph": number})
        for number, group in enumerate(merged, 1)
        for segment in group
    ]

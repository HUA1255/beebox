from __future__ import annotations

import re

import pytest

from backend.models import TranscriptSegment
from backend.services.chunk_merge import ChunkTranscript, merge_chunk_transcripts
from backend.services.chunking import (
    CHUNK_OVERLAP_MS,
    CHUNK_WINDOW_MS,
    needs_chunking,
    plan_chunk_windows,
)


def _seg(start: float, end: float, text: str) -> TranscriptSegment:
    return TranscriptSegment(id=1, start=start, end=end, text=text)


def _chunk(
    idx: int,
    start_ms: int,
    end_ms: int,
    *segments: TranscriptSegment,
    aligned: bool = True,
    text: str | None = None,
) -> ChunkTranscript:
    return ChunkTranscript(
        idx=idx,
        start_ms=start_ms,
        end_ms=end_ms,
        segments=list(segments),
        text=text if text is not None else " ".join(segment.text for segment in segments),
        aligned=aligned,
    )


def _compact(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)


# --- shared planner -------------------------------------------------------


def test_planner_uses_the_contract_window_and_overlap() -> None:
    assert (CHUNK_WINDOW_MS, CHUNK_OVERLAP_MS) == (120_000, 2_000)
    assert plan_chunk_windows(180_000) == [(0, 120_000), (118_000, 180_000)]
    assert plan_chunk_windows(240_000) == [
        (0, 120_000),
        (118_000, 238_000),
        (236_000, 240_000),
    ]


def test_planner_chunks_only_audio_longer_than_the_window() -> None:
    assert needs_chunking(None) is False
    assert needs_chunking(119_999) is False
    assert needs_chunking(120_000) is False
    assert needs_chunking(120_001) is True
    assert plan_chunk_windows(120_000) == [(0, 120_000)]


# --- cross-chunk merge ----------------------------------------------------


def test_merge_offsets_chunk_items_to_global_time_and_sorts_by_start() -> None:
    # Deliberately out of order: the merge must not depend on caller ordering.
    second = _chunk(2, 118_000, 180_000, _seg(5.0, 6.0, "gamma"))
    first = _chunk(1, 0, 120_000, _seg(110.0, 111.0, "beta"), _seg(0.0, 1.0, "alpha"))

    merged = merge_chunk_transcripts([second, first])

    assert [segment.text for segment in merged.segments] == ["alpha", "beta", "gamma"]
    assert merged.segments[2].start == pytest.approx(123.0)
    assert merged.segments[2].end == pytest.approx(124.0)
    assert merged.aligned is True


def test_merge_keeps_a_boundary_sentence_once_and_prefers_the_complete_copy() -> None:
    first = _chunk(
        1,
        0,
        120_000,
        _seg(116.0, 117.0, "前一句"),
        # Truncated by the chunk end: the next chunk hears the whole sentence.
        _seg(118.6, 120.0, "我们今天讲三个"),
    )
    second = _chunk(
        2,
        118_000,
        180_000,
        _seg(0.6, 3.0, "我们今天讲三个问题。"),
        _seg(4.0, 5.0, "后一句"),
    )

    merged = merge_chunk_transcripts([first, second])

    assert [segment.text for segment in merged.segments] == ["前一句", "我们今天讲三个问题。", "后一句"]
    assert _compact(merged_text := "".join(segment.text for segment in merged.segments)).count(
        _compact("我们今天讲三个问题")
    ) == 1
    assert merged.segments[1].start == pytest.approx(118.6)
    assert merged.segments[1].end == pytest.approx(121.0)
    assert "前一句" in merged_text


def test_merge_keeps_distinct_boundary_speech_from_both_sides() -> None:
    first = _chunk(
        1,
        0,
        120_000,
        _seg(117.0, 118.0, "前句"),
        _seg(118.2, 118.8, "左侧短句"),
    )
    second = _chunk(
        2,
        118_000,
        180_000,
        _seg(1.4, 1.9, "右侧短句"),
        _seg(2.5, 3.5, "后句"),
    )

    merged = merge_chunk_transcripts([first, second])

    assert [segment.text for segment in merged.segments] == ["前句", "左侧短句", "右侧短句", "后句"]
    assert merged.segments[2].start == pytest.approx(119.4)


def test_merge_dedupes_a_reemitted_run_across_the_boundary_exactly_once() -> None:
    first = _chunk(
        1,
        0,
        120_000,
        _seg(116.0, 117.0, "A"),
        _seg(117.0, 118.0, "B"),
        _seg(118.0, 119.6, "C"),
    )
    second = _chunk(
        2,
        118_000,
        180_000,
        _seg(0.0, 1.0, "B"),
        _seg(1.0, 2.0, "C"),
        _seg(2.0, 4.0, "D"),
    )

    merged = merge_chunk_transcripts([second, first])

    assert [segment.text for segment in merged.segments] == ["A", "B", "C", "D"]
    # The more complete copy of C wins and covers the full 118.0-120.0 span.
    assert merged.segments[2].start == pytest.approx(118.0)
    assert merged.segments[2].end == pytest.approx(120.0)


def test_merge_trims_a_partial_overlap_sentence_reemitted_at_the_next_chunk_start() -> None:
    header = "前面讲了很多内容然后最后一个是他"
    boundary = "会写大量的这种"
    first = _chunk(1, 0, 120_000, _seg(105.0, 120.0, f"{header}{boundary}。"))
    second = _chunk(2, 118_000, 180_000, _seg(0.0, 20.0, f"{boundary}没有人用的代码，就是虽然你给了约束。"))

    merged = merge_chunk_transcripts([first, second])

    assert [segment.text for segment in merged.segments] == [header, f"{boundary}没有人用的代码，就是虽然你给了约束。"]
    assert _compact("".join(segment.text for segment in merged.segments)).count(_compact(boundary)) == 1
    # The earlier segment keeps its own text but yields the overlap start to the
    # later chunk, whose mapped time for the boundary sentence is preserved.
    assert merged.segments[0].end == pytest.approx(118.0)
    assert merged.segments[1].start == pytest.approx(118.0)


def test_merge_enforces_sorted_monotonic_non_overlapping_segments() -> None:
    first = _chunk(1, 0, 120_000, _seg(110.0, 119.5, "甲段"))
    second = _chunk(2, 118_000, 180_000, _seg(0.0, 2.0, "乙段"))

    merged = merge_chunk_transcripts([first, second])

    segments = merged.segments
    assert [segment.text for segment in segments] == ["甲段", "乙段"]
    for previous, current in zip(segments, segments[1:]):
        assert previous.end <= current.start
    for segment in segments:
        assert segment.start < segment.end
    assert segments[0].start == pytest.approx(110.0)
    assert segments[0].end == pytest.approx(119.5)
    assert segments[1].start == pytest.approx(119.5)
    assert segments[1].end == pytest.approx(120.0)


def test_merge_expands_timestampless_chunk_segments_to_the_chunk_window() -> None:
    first = _chunk(1, 0, 120_000, _seg(0.0, 0.0, "alpha"), aligned=False)
    second = _chunk(2, 118_000, 238_000, _seg(0.0, 0.0, "bravo"), aligned=False)

    merged = merge_chunk_transcripts([first, second])

    assert [segment.text for segment in merged.segments] == ["alpha", "bravo"]
    assert (merged.segments[0].start, merged.segments[0].end) == (0.0, 120.0)
    assert (merged.segments[1].start, merged.segments[1].end) == (120.0, 238.0)
    assert merged.aligned is False


def test_merge_ignores_silent_chunks_when_aggregating_alignment() -> None:
    spoken = _chunk(1, 0, 120_000, _seg(0.0, 1.0, "alpha"), aligned=True)
    silent = _chunk(2, 118_000, 238_000, aligned=False, text="")

    merged = merge_chunk_transcripts([spoken, silent])

    assert [segment.text for segment in merged.segments] == ["alpha"]
    assert merged.aligned is True


def test_merge_ignores_silent_chunk_with_blank_segments_when_aggregating() -> None:
    spoken = _chunk(1, 0, 120_000, _seg(0.0, 1.0, "alpha"), aligned=True)
    silent = _chunk(2, 118_000, 238_000, _seg(0.0, 0.0, "   "), aligned=False, text="")

    merged = merge_chunk_transcripts([spoken, silent])

    assert merged.aligned is True


def test_merge_stays_unaligned_when_a_text_bearing_chunk_is_unaligned() -> None:
    aligned = _chunk(1, 0, 120_000, _seg(0.0, 1.0, "alpha"), aligned=True)
    unaligned = _chunk(2, 118_000, 238_000, _seg(0.0, 0.0, "bravo"), aligned=False)

    merged = merge_chunk_transcripts([aligned, unaligned])

    assert merged.aligned is False


def test_merge_widens_zero_length_aligned_segments() -> None:
    merged = merge_chunk_transcripts([_chunk(1, 0, 180_000, _seg(5.0, 5.0, "零长度"))])

    assert len(merged.segments) == 1
    assert merged.segments[0].start == pytest.approx(5.0)
    assert merged.segments[0].end > merged.segments[0].start


def test_merge_is_deterministic_for_any_chunk_order() -> None:
    chunks = [
        _chunk(2, 118_000, 180_000, _seg(0.0, 1.0, "B"), _seg(1.0, 2.0, "C"), _seg(2.0, 4.0, "D")),
        _chunk(1, 0, 120_000, _seg(116.0, 117.0, "A"), _seg(117.0, 118.0, "B"), _seg(118.0, 119.6, "C")),
    ]

    forward = merge_chunk_transcripts(chunks)
    backward = merge_chunk_transcripts(list(reversed(chunks)))

    assert [(segment.start, segment.end, segment.text) for segment in forward.segments] == [
        (segment.start, segment.end, segment.text) for segment in backward.segments
    ]

from __future__ import annotations

import re
import unicodedata

import pytest

from backend.models import TranscriptSegment
from backend.services.segments import (
    _pause_boundaries,
    build_char_timeline,
    segments_from_alignment,
)
from backend.utils.transcript_text import transcript_text_from_segments


def _items(*entries: tuple[str, float, float]) -> list[dict]:
    return [
        {"text": text, "start_time": start, "end_time": end}
        for text, start, end in entries
    ]


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _timed_characters(text: str) -> list[str]:
    return [
        char
        for char in text
        if not char.isspace() and unicodedata.category(char)[0] != "P"
    ]


def _char_items(text: str, *, step: float = 0.1) -> list[dict]:
    """One aligner token per timed character, with no pauses."""
    return _items(
        *[
            (char, index * step, (index + 1) * step)
            for index, char in enumerate(_timed_characters(text))
        ]
    )


def _roundtrip(segments: list[TranscriptSegment]) -> str:
    return transcript_text_from_segments(segments)


def test_build_char_timeline_reattaches_punctuation_to_the_preceding_token() -> None:
    text = "你好，世界。"
    items = _items(("你", 0.0, 0.2), ("好", 0.2, 0.4), ("世", 0.6, 0.8), ("界", 0.8, 1.0))

    timeline = build_char_timeline(text, items)

    assert [entry["char"] for entry in timeline] == list(text)
    comma = timeline[2]
    assert comma["timed"] is False
    assert (comma["start"], comma["end"]) == (0.4, 0.4)
    period = timeline[5]
    assert period["timed"] is False
    assert (period["start"], period["end"]) == (1.0, 1.0)


def test_build_char_timeline_attaches_leading_punctuation_to_the_first_token() -> None:
    text = "……好"
    items = _items(("好", 2.0, 2.4))

    timeline = build_char_timeline(text, items)

    assert timeline[0]["timed"] is False
    assert timeline[1]["timed"] is False
    assert (timeline[0]["start"], timeline[0]["end"]) == (2.0, 2.0)
    assert (timeline[1]["start"], timeline[1]["end"]) == (2.0, 2.0)


def test_build_char_timeline_rejects_token_text_mismatch() -> None:
    with pytest.raises(ValueError):
        build_char_timeline("你好世界", _items(("你", 0.0, 0.2), ("好", 0.2, 0.4)))


def test_segments_from_alignment_keeps_all_text_and_punctuation() -> None:
    text = "张三：今天，我们讨论 ASR。"
    tokens = [char for char in text if char not in "：，。 "]
    items = _items(*[(token, index * 0.25, index * 0.25 + 0.2) for index, token in enumerate(tokens)])

    segments = segments_from_alignment(text, items, pause_threshold=10.0, max_chars=240)

    assert _compact("".join(segment.text for segment in segments)) == _compact(text)
    assert "：" in "".join(segment.text for segment in segments)
    assert all(segment.start < segment.end for segment in segments)


def test_segments_from_alignment_corrects_zero_length_and_negative_duration_tokens() -> None:
    text = "甲。乙。丙。"
    items = _items(("甲", 0.24, 0.24), ("乙", 1.0, 0.9), ("丙", 2.0, 2.0))

    segments = segments_from_alignment(text, items, pause_threshold=0.1, min_duration=0.05)

    assert len(segments) == 3
    for segment in segments:
        assert segment.start < segment.end
    assert [segment.text for segment in segments] == ["甲。", "乙。", "丙。"]
    assert segments[0].start == pytest.approx(0.24)
    assert segments[0].end == pytest.approx(0.29)
    assert segments[1].start == pytest.approx(1.0)
    assert segments[1].end == pytest.approx(1.05)


def test_segments_from_alignment_keeps_segments_sorted_and_non_overlapping() -> None:
    text = "ab"
    items = _items(("a", 0.0, 2.0), ("b", 1.0, 3.0))

    segments = segments_from_alignment(text, items, max_chars=1)

    assert [segment.text for segment in segments] == ["a", "b"]
    assert segments[0].start < segments[0].end
    assert segments[1].start < segments[1].end
    assert segments[1].start >= segments[0].end
    assert [segment.id for segment in segments] == [1, 2]


def test_segments_from_alignment_splits_at_sentence_ends_and_max_chars() -> None:
    text = "甲乙。丙丁戊己"
    items = _items(
        ("甲", 0.0, 0.2),
        ("乙", 0.2, 0.4),
        ("丙", 3.0, 3.2),
        ("丁", 3.2, 3.4),
        ("戊", 3.4, 3.6),
        ("己", 3.6, 3.8),
    )

    by_gap = segments_from_alignment(text, items, pause_threshold=1.5)
    assert [segment.text for segment in by_gap] == ["甲乙。", "丙丁戊己"]

    by_chars = segments_from_alignment(text, items, pause_threshold=10.0, max_chars=2)
    assert [segment.text for segment in by_chars] == ["甲乙。", "丙丁", "戊己"]


def test_segments_from_alignment_returns_nothing_for_blank_text() -> None:
    assert segments_from_alignment("   ", []) == []
    assert all(isinstance(segment, TranscriptSegment) for segment in segments_from_alignment("a", _items(("a", 0.0, 0.1))))


def test_segments_from_alignment_splits_chinese_at_sentence_punctuation() -> None:
    text = "今天天气很好。我们去公园吧！"
    segments = segments_from_alignment(text, _char_items(text))

    assert [segment.text for segment in segments] == ["今天天气很好。", "我们去公园吧！"]
    assert _roundtrip(segments) == text
    assert all(segment.start < segment.end for segment in segments)


def test_segments_from_alignment_splits_english_at_sentence_punctuation() -> None:
    text = "Hello world. How are you? Fine!"
    segments = segments_from_alignment(text, _char_items(text))

    assert [segment.text for segment in segments] == ["Hello world.", "How are you?", "Fine!"]
    assert _roundtrip(segments) == text


def test_segments_from_alignment_splits_mixed_chinese_and_english() -> None:
    text = "我们用 Qwen3 做 ASR。结果很好！"
    segments = segments_from_alignment(text, _char_items(text))

    assert [segment.text for segment in segments] == ["我们用 Qwen3 做 ASR。", "结果很好！"]
    assert _roundtrip(segments) == text


def test_segments_from_alignment_falls_back_when_speech_has_no_punctuation() -> None:
    text = "测" * 130
    segments = segments_from_alignment(text, _char_items(text))

    assert [len(segment.text) for segment in segments] == [60, 60, 10]
    assert _roundtrip(segments) == text


def test_segments_from_alignment_keeps_closing_quote_with_the_sentence() -> None:
    text = "他说：“你好。”然后走了。"
    segments = segments_from_alignment(text, _char_items(text))

    assert [segment.text for segment in segments] == ["他说：“你好。”", "然后走了。"]
    assert _roundtrip(segments) == text


def test_segments_from_alignment_does_not_split_on_a_pause_mid_sentence() -> None:
    text = "你好世界"
    items = _items(
        ("你", 0.0, 0.2),
        ("好", 0.2, 0.4),
        ("世", 1.2, 1.4),
        ("界", 1.4, 1.6),
    )

    # The 0.8s pause falls inside an unfinished sentence ("你好"), so it is
    # not a boundary; only sentence-final punctuation may cut there.
    segments = segments_from_alignment(text, items)

    assert [segment.text for segment in segments] == ["你好世界"]
    assert _roundtrip(segments) == text


def test_segments_from_alignment_keeps_the_acceptance_pause_inside_a_word() -> None:
    # ticket #23b: a 1.28s pause between "举个" and "手。" must not split the
    # word; the finished sentence after 手 is what ends the segment.
    text = "麻烦举个手。"
    items = _items(
        ("麻", 0.0, 0.2),
        ("烦", 0.2, 0.4),
        ("举", 0.4, 0.6),
        ("个", 0.6, 0.8),
        ("手", 2.08, 2.3),
    )

    segments = segments_from_alignment(text, items)

    assert [segment.text for segment in segments] == ["麻烦举个手。"]
    assert _roundtrip(segments) == text


def test_pause_boundaries_require_six_hundred_ms_after_a_finished_sentence() -> None:
    unfinished = "麻烦举个手。"

    def _unfinished_timeline(hand_start: float) -> list[dict]:
        return build_char_timeline(
            unfinished,
            _items(
                ("麻", 0.0, 0.2),
                ("烦", 0.2, 0.4),
                ("举", 0.4, 0.6),
                ("个", 0.6, 0.8),
                ("手", hand_start, hand_start + 0.2),
            ),
        )

    # 0.599s is below the threshold.
    assert _pause_boundaries(_unfinished_timeline(1.399), unfinished, 0.6) == set()
    # 0.6s is long enough, but "个" has not finished a sentence.
    assert _pause_boundaries(_unfinished_timeline(1.4), unfinished, 0.6) == set()

    finished = "你好。世界"

    def _finished_timeline(world_start: float) -> list[dict]:
        return build_char_timeline(
            finished,
            _items(
                ("你", 0.0, 0.2),
                ("好", 0.2, 0.4),
                ("世", world_start, world_start + 0.2),
                ("界", world_start + 0.2, world_start + 0.4),
            ),
        )

    # "你好。" finishes the sentence, so the same 0.6s pause is a boundary.
    assert _pause_boundaries(_finished_timeline(0.999), finished, 0.6) == set()
    assert _pause_boundaries(_finished_timeline(1.0), finished, 0.6) == {3}


def test_segments_from_alignment_splits_after_a_closing_quote_that_follows_a_full_stop() -> None:
    text = "他说：“你好。”世界"
    items = _items(
        ("他", 0.0, 0.2),
        ("说", 0.2, 0.4),
        ("你", 0.4, 0.6),
        ("好", 0.6, 0.8),
        ("世", 1.5, 1.7),
        ("界", 1.7, 1.9),
    )

    segments = segments_from_alignment(text, items)

    assert [segment.text for segment in segments] == ["他说：“你好。”", "世界"]
    assert _roundtrip(segments) == text


def test_segments_from_alignment_keeps_punctuation_at_segment_edges() -> None:
    leading = "……你好。"
    leading_segments = segments_from_alignment(leading, _char_items(leading))
    assert [segment.text for segment in leading_segments] == ["……你好。"]
    assert _roundtrip(leading_segments) == leading

    trailing = "你好。"
    trailing_segments = segments_from_alignment(trailing, _char_items(trailing))
    assert [segment.text for segment in trailing_segments] == ["你好。"]
    assert _roundtrip(trailing_segments) == trailing


def test_segments_from_alignment_fallback_never_starts_the_next_segment_with_punctuation() -> None:
    text = "测" * 60 + "。" + "试" * 40
    segments = segments_from_alignment(text, _char_items(text))

    assert segments[0].text == "测" * 60 + "。"
    assert segments[1].text == "试" * 40
    assert _roundtrip(segments) == text


def test_segments_from_alignment_default_does_not_use_the_old_84_char_budget() -> None:
    text = "测" * 100
    items = _char_items(text)

    default = segments_from_alignment(text, items)
    assert [len(segment.text) for segment in default] == [60, 40]

    budgeted = segments_from_alignment(text, items, max_chars=84)
    assert [len(segment.text) for segment in budgeted] == [84, 16]
    assert _roundtrip(default) == text


def test_postprocess_keeps_aligned_times_when_long_split_is_disabled() -> None:
    from backend.services import postprocess

    text = "测试文本。" * 40
    segment = TranscriptSegment(id=1, start=1.0, end=9.0, text=text)

    kept = postprocess.process_segments([segment], split_long_segments=False)
    assert len(kept) == 1
    assert (kept[0].start, kept[0].end) == (1.0, 9.0)

    resplit = postprocess.process_segments([segment], split_long_segments=True)
    assert len(resplit) > 1


def test_postprocess_can_preserve_forced_aligner_times_exactly() -> None:
    from backend.services import postprocess

    segments = [
        TranscriptSegment(id=1, start=0.0, end=0.3, text="第一"),
        TranscriptSegment(id=2, start=0.3, end=0.6, text="第二"),
    ]

    kept = postprocess.process_segments(
        segments,
        merge_short_segments=False,
        split_long_segments=False,
        enforce_timing=False,
    )
    assert [(segment.start, segment.end) for segment in kept] == [(0.0, 0.3), (0.3, 0.6)]

    forced = postprocess.process_segments(
        segments,
        merge_short_segments=False,
        split_long_segments=False,
        enforce_timing=True,
    )
    assert forced[1].start > 0.6


def test_postprocess_still_cleans_and_merges_when_timing_is_not_forced() -> None:
    from backend.services import postprocess

    segments = [
        TranscriptSegment(id=1, start=0.0, end=1.2, text="你好 ， ", speaker="1"),
        TranscriptSegment(id=2, start=1.2, end=1.5, text="世界", speaker="1"),
    ]

    processed = postprocess.process_segments(
        segments,
        merge_short_segments=True,
        min_duration_ms=800,
        split_long_segments=False,
        enforce_timing=False,
    )

    assert len(processed) == 1
    assert processed[0].text == "你好，世界"
    assert (processed[0].start, processed[0].end) == (0.0, 1.5)


def test_build_char_timeline_ignores_in_word_punctuation_in_token_length() -> None:
    # Real failure: three ``Honey's`` tokens made the token character count
    # exceed the transcript's timed characters by three apostrophes.
    text = "Honey's Engineer"
    items = _items(("Honey's", 0.0, 1.0), ("Engineer", 1.0, 2.0))

    timeline = build_char_timeline(text, items)

    assert [entry["char"] for entry in timeline] == list(text)
    apostrophe = timeline[text.index("'")]
    assert apostrophe["timed"] is False
    assert apostrophe["start"] == apostrophe["end"] == 1.0
    timed = [(entry["char"], entry["start"], entry["end"]) for entry in timeline if entry["timed"]]
    assert [char for char, _start, _end in timed] == list("HoneysEngineer")
    assert all((start, end) == (0.0, 1.0) for _char, start, end in timed[:6])
    assert all((start, end) == (1.0, 2.0) for _char, start, end in timed[6:])


def test_build_char_timeline_ignores_whitespace_inside_a_token() -> None:
    text = "hello world"
    timeline = build_char_timeline(text, _items(("hello world", 0.0, 1.0)))

    assert [entry["char"] for entry in timeline] == list(text)
    assert all(entry["timed"] for entry in timeline if not entry["char"].isspace())


def test_segments_from_alignment_keeps_in_word_punctuation_verbatim() -> None:
    text = "Honey's Engineer 分享"
    items = _items(
        ("Honey's", 0.0, 1.0),
        ("Engineer", 1.0, 2.0),
        ("分", 2.0, 2.2),
        ("享", 2.2, 2.4),
    )

    segments = segments_from_alignment(text, items)

    assert transcript_text_from_segments(segments) == text
    assert "'" in segments[0].text


def test_build_char_timeline_still_rejects_a_real_token_length_mismatch() -> None:
    with pytest.raises(ValueError, match="aligner tokens"):
        build_char_timeline("ab", _items(("abc", 0.0, 1.0)))

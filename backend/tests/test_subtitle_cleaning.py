"""Cleaning platform automatic captions (ticket #43).

YouTube's automatic captions use a rolling display: every cue starts with the
last line of the previous cue, and tiny half-second cues repeat the current
line before the next one arrives. The cleaner removes those repetitions,
extends the surviving cue's timeline and merges short fragments into readable
cues. It never adds punctuation and never calls a model.

The golden files are hand-written expectations for realistic VTT/json3
samples; the value is that they are independent of the implementation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.services.video_platform import SubtitleCue, clean_auto_captions, parse_subtitle

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "subtitle_cleaning"


def read_fixture(name: str) -> str:
    return (FIXTURE_ROOT / name).read_text(encoding="utf-8-sig")


def golden(name: str) -> list[dict]:
    return json.loads(read_fixture(name))["cues"]


def as_dicts(cues) -> list[dict]:
    return [
        {"start_ms": cue.start_ms, "end_ms": cue.end_ms, "text": cue.text} for cue in cues
    ]


@pytest.mark.parametrize(
    ("source", "fmt", "expected"),
    [
        ("rolling-zh.vtt", "vtt", "rolling-zh.golden.json"),
        ("rolling-en.vtt", "vtt", "rolling-en.golden.json"),
        ("rolling-json3.json3", "json3", "rolling-json3.golden.json"),
        ("append-json3.json3", "json3", "append-json3.golden.json"),
    ],
)
def test_auto_captions_are_cleaned_to_the_golden_cues(
    source: str, fmt: str, expected: str
) -> None:
    parsed = parse_subtitle(read_fixture(source), fmt)

    cleaned = clean_auto_captions(parsed)

    assert as_dicts(cleaned) == golden(expected)


def test_rolling_duplicate_line_is_removed_and_the_previous_cue_is_extended() -> None:
    first = "第一行内容很长很长很长很长很长很长"
    second = "第二行内容也很长很长很长很长"
    cues = [
        SubtitleCue(start_ms=0, end_ms=1000, text=first, lines=(first,)),
        SubtitleCue(start_ms=1000, end_ms=1100, text=first, lines=(first,)),
        SubtitleCue(
            start_ms=1100,
            end_ms=2500,
            text=f"{first} {second}",
            lines=(first, second),
        ),
    ]

    cleaned = clean_auto_captions(cues)

    assert as_dicts(cleaned) == [
        {"start_ms": 0, "end_ms": 1100, "text": first},
        {"start_ms": 1100, "end_ms": 2500, "text": second},
    ]


def test_cleaning_is_idempotent() -> None:
    parsed = parse_subtitle(read_fixture("rolling-zh.vtt"), "vtt")

    once = clean_auto_captions(parsed)
    twice = clean_auto_captions(once)

    assert as_dicts(twice) == as_dicts(once)


def test_cues_without_line_metadata_are_still_deduplicated() -> None:
    first = "hello world this is a fairly long automatic caption line"
    second = "another piece of text that should stay separate"
    cues = [
        SubtitleCue(start_ms=0, end_ms=1000, text=first),
        SubtitleCue(start_ms=1000, end_ms=2000, text=first),
        SubtitleCue(start_ms=2000, end_ms=3000, text=second),
    ]

    cleaned = clean_auto_captions(cues)

    assert as_dicts(cleaned) == [
        {"start_ms": 0, "end_ms": 2000, "text": first},
        {"start_ms": 2000, "end_ms": 3000, "text": second},
    ]


def test_merging_does_not_cross_a_pause_or_a_finished_sentence() -> None:
    cues = [
        SubtitleCue(start_ms=0, end_ms=1000, text="结束。", lines=("结束。",)),
        SubtitleCue(start_ms=1000, end_ms=2000, text="下一句", lines=("下一句",)),
        SubtitleCue(start_ms=5000, end_ms=6000, text="很远", lines=("很远",)),
        SubtitleCue(start_ms=10000, end_ms=11000, text="也很远", lines=("也很远",)),
    ]

    cleaned = clean_auto_captions(cues)

    # The finished sentence stays its own cue; the 3.0s gap and the 4.0s gap
    # are both wider than the merge window.
    assert [cue.text for cue in cleaned] == ["结束。", "下一句", "很远", "也很远"]

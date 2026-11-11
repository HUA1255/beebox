"""Ticket #20: group final segments into speaker-pure paragraphs.

The grouping is a pure, idempotent post-pass over the postprocessed segment
list. These tests pin the boundary rules, the 120s fallback ladder and the 10s
soft floor, plus the pipeline landing points that persist the numbers.
"""

from __future__ import annotations

import collections
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.models import TranscriptSegment
from backend.services.exports import render_txt
from backend.services.paragraphs import (
    PARAGRAPH_MAX_SECONDS,
    PARAGRAPH_MIN_SECONDS,
    assign_paragraphs,
)
from support import fake_speech_env, make_wav_bytes


def seg(
    index: int,
    start: float,
    end: float,
    speaker: str | None = None,
    text: str = "文本",
) -> TranscriptSegment:
    return TranscriptSegment(id=index, start=start, end=end, text=text, speaker=speaker)


def _paragraphs(segments: list[TranscriptSegment]) -> list[int | None]:
    return [segment.paragraph for segment in segments]


def _assert_paragraph_invariants(segments: list[TranscriptSegment]) -> None:
    """Contiguous 1-based numbering, speaker purity per paragraph."""
    numbers = [number for number in _paragraphs(segments) if number is not None]
    assert len(numbers) == len(segments)
    assert numbers[0] == 1
    assert all(current - previous in (0, 1) for previous, current in zip(numbers, numbers[1:])), numbers
    by_paragraph: dict[int, list[TranscriptSegment]] = collections.defaultdict(list)
    for segment, number in zip(segments, numbers):
        by_paragraph[number].append(segment)
    assert sorted(by_paragraph) == list(range(1, len(by_paragraph) + 1))
    for members in by_paragraph.values():
        assert len({member.speaker for member in members}) == 1


# ---------------------------------------------------------------------------
# Pure boundary rules
# ---------------------------------------------------------------------------


def test_empty_input_stays_empty() -> None:
    assert assign_paragraphs([]) == []


def test_speaker_change_starts_a_new_paragraph() -> None:
    segments = [seg(1, 0, 10, "S01"), seg(2, 10, 20, "S02"), seg(3, 20, 30, "S01")]

    result = assign_paragraphs(segments)

    assert _paragraphs(result) == [1, 2, 3]


def test_pause_of_one_second_after_a_finished_sentence_starts_a_new_paragraph() -> None:
    segments = [seg(1, 0, 10, "S01", "上一句。"), seg(2, 11, 21, "S01", "下一句。")]

    result = assign_paragraphs(segments)

    assert _paragraphs(result) == [1, 2]


def test_pause_of_one_second_inside_an_unfinished_sentence_keeps_the_paragraph() -> None:
    # ticket #23b: the real recording had "…麻烦举个" | "手。" split at a 1.28s
    # pause. A pause says nothing about the sentence until the sentence ends.
    segments = [seg(1, 0, 10, "S01", "麻烦举个"), seg(2, 11, 21, "S01", "手。")]

    result = assign_paragraphs(segments)

    assert _paragraphs(result) == [1, 1]


def test_pause_after_a_closing_quote_starts_a_new_paragraph() -> None:
    segments = [
        seg(1, 0, 10, "S01", "他说：“好。”"),
        seg(2, 11, 21, "S01", "下一句。"),
    ]

    result = assign_paragraphs(segments)

    assert _paragraphs(result) == [1, 2]


def test_pause_after_a_closing_bracket_starts_a_new_paragraph() -> None:
    segments = [
        seg(1, 0, 10, "S01", "（完。）"),
        seg(2, 11, 21, "S01", "下一句。"),
    ]

    result = assign_paragraphs(segments)

    assert _paragraphs(result) == [1, 2]


def test_pause_just_under_one_second_keeps_the_paragraph() -> None:
    segments = [seg(1, 0, 10, "S01", "上一句。"), seg(2, 10.99, 20.99, "S01", "下一句。")]

    result = assign_paragraphs(segments)

    assert _paragraphs(result) == [1, 1]


def test_none_is_its_own_speaker_value() -> None:
    segments = [
        seg(1, 0, 10, None),
        seg(2, 10, 20, None),
        seg(3, 20, 30, "S01"),
        seg(4, 30, 40, None),
    ]

    result = assign_paragraphs(segments)

    assert _paragraphs(result) == [1, 1, 2, 3]


def test_paragraph_starts_once_the_current_one_reaches_the_hard_limit() -> None:
    segments = [seg(1, 0, 60, "S01"), seg(2, 60, 120, "S01"), seg(3, 120, 130, "S01")]

    result = assign_paragraphs(segments)

    assert _paragraphs(result) == [1, 1, 2]


# ---------------------------------------------------------------------------
# 10s soft floor
# ---------------------------------------------------------------------------


def test_short_paragraph_merges_with_a_same_speaker_neighbour() -> None:
    segments = [seg(1, 0, 3, "S01", "短句。"), seg(2, 4, 14, "S01", "长句。")]

    result = assign_paragraphs(segments)

    assert _paragraphs(result) == [1, 1]


def test_short_paragraph_does_not_merge_across_speakers() -> None:
    segments = [seg(1, 0, 3, "S01"), seg(2, 4, 14, "S02")]

    result = assign_paragraphs(segments)

    assert _paragraphs(result) == [1, 2]


def test_exactly_ten_seconds_is_not_short() -> None:
    segments = [seg(1, 0, 10, "S01", "上一句。"), seg(2, 11, 21, "S01", "下一句。")]

    result = assign_paragraphs(segments)

    assert _paragraphs(result) == [1, 2]


def test_soft_merge_never_creates_a_paragraph_over_the_hard_limit() -> None:
    # 115s + 8s is still longer than 120s, so the short paragraph stays
    # separate even though the speaker matches: the hard limit wins.
    segments = [seg(1, 0, 115, "S01"), seg(2, 116, 124, "S01")]

    result = assign_paragraphs(segments)

    assert _paragraphs(result) == [1, 2]


def _paragraph_durations(segments: list[TranscriptSegment]) -> dict[int, float]:
    by_number: dict[int, list[TranscriptSegment]] = collections.defaultdict(list)
    for segment in segments:
        by_number[segment.paragraph].append(segment)
    return {
        number: members[-1].end - members[0].start
        for number, members in by_number.items()
    }


def _assert_no_short_paragraph_beside_its_own_speaker(
    segments: list[TranscriptSegment],
) -> None:
    """No paragraph over 120s and no <10s paragraph next to the same speaker."""
    by_number: dict[int, list[TranscriptSegment]] = collections.defaultdict(list)
    for segment in segments:
        by_number[segment.paragraph].append(segment)
    numbers = sorted(by_number)
    assert numbers == list(range(1, len(numbers) + 1))
    durations = _paragraph_durations(segments)
    for number in numbers:
        assert durations[number] <= PARAGRAPH_MAX_SECONDS + 1e-9, (number, durations[number])
        if durations[number] >= PARAGRAPH_MIN_SECONDS:
            continue
        speaker = by_number[number][0].speaker
        neighbours = [
            by_number[other][0].speaker
            for other in (number - 1, number + 1)
            if other in by_number
        ]
        assert speaker not in neighbours, (number, durations[number], speaker, neighbours)


# The acceptance recording behind ticket #20: paragraphs 3, 4 and 5 all
# belong to speaker 1 and last 111.04s, 9.44s and 115.12s. Neither short
# neighbour can be absorbed within 120s, which used to leave the 9.44s aside
# standing next to two paragraphs of its own speaker.
_ACCEPTANCE_GEOMETRY = [
    # paragraph 3 (speaker 1, 55.44 - 166.48)
    (55.44, 60.24),
    (60.96, 66.08),
    (66.56, 75.92),
    (75.92, 84.56),
    (84.56, 86.24),
    (87.04, 87.28),
    (88.0, 95.28),
    (95.36, 106.0),
    (106.08, 109.2),
    (109.84, 118.24),
    (118.24, 122.96),
    (123.92, 124.16),
    (124.8, 134.16),
    (134.16, 143.12),
    (143.2, 148.32),
    (148.88, 156.16),
    (156.56, 166.48),
    # paragraph 4 (speaker 1, 168.08 - 177.52, 9.44s)
    (168.08, 173.76),
    (174.72, 176.48),
    (177.12, 177.52),
    # paragraph 5 (speaker 1, 178.96 - 294.08, 115.12s)
    (178.96, 185.12),
    (185.52, 192.4),
    (192.88, 203.52),
    (203.52, 212.32),
    (212.96, 223.36),
    (223.76, 233.68),
    (233.68, 238.16),
    (238.48, 240.0),
    (240.4, 250.64),
    (250.64, 254.88),
    (255.68, 260.16),
    (260.48, 265.2),
    (265.44, 267.6),
    (268.08, 271.44),
    (272.08, 278.88),
    (279.52, 282.96),
    (283.28, 290.72),
    (291.6, 292.0),
    (292.0, 294.08),
]


def _acceptance_segments(speaker: str = "1") -> list[TranscriptSegment]:
    return [
        seg(index, start, end, speaker, "句子。")
        for index, (start, end) in enumerate(_ACCEPTANCE_GEOMETRY, 1)
    ]


def test_acceptance_short_aside_is_absorbed_into_a_same_speaker_neighbour() -> None:
    """Regression for the 82.7-minute recording's only failed paragraph."""
    result = assign_paragraphs(_acceptance_segments())

    _assert_paragraph_invariants(result)
    _assert_no_short_paragraph_beside_its_own_speaker(result)

    durations = _paragraph_durations(result)
    assert sorted(durations) == [1, 2, 3]
    # Paragraph 3 is untouched, the 9.44s aside joins what follows it, and the
    # re-cut keeps the new pair inside 120s on both sides.
    assert durations[1] == pytest.approx(111.04)
    assert durations[2] == pytest.approx(114.88)
    assert durations[3] == pytest.approx(10.8)
    assert [segment.start for segment in result if segment.paragraph == 2][0] == pytest.approx(168.08)


def test_absorbing_the_acceptance_aside_is_idempotent() -> None:
    once = assign_paragraphs(_acceptance_segments())
    twice = assign_paragraphs(once)

    assert _paragraphs(twice) == _paragraphs(once)
    assert twice == once


def test_short_opening_paragraph_is_absorbed_and_the_union_is_re_cut() -> None:
    # 7s + 116s of the same speaker: absorbing overshoots 120s, so the union
    # is cut again instead of leaving the 7s opening paragraph in place.
    segments = [
        seg(1, 0, 7, "S01", "开场。"),
        seg(2, 8.2, 38.2, "S01", "甲。"),
        seg(3, 38.2, 68.2, "S01", "乙。"),
        seg(4, 68.2, 98.2, "S01", "丙。"),
        seg(5, 98.2, 124.2, "S01", "丁。"),
    ]

    result = assign_paragraphs(segments)

    _assert_paragraph_invariants(result)
    _assert_no_short_paragraph_beside_its_own_speaker(result)
    durations = _paragraph_durations(result)
    assert sorted(durations) == [1, 2]
    assert all(duration >= PARAGRAPH_MIN_SECONDS for duration in durations.values())


def test_short_trailing_paragraph_is_absorbed_and_the_union_is_re_cut() -> None:
    # 110s + 9s of the same speaker at the end of the transcript.
    segments = [
        seg(1, 0, 55, "S01", "甲。"),
        seg(2, 55, 110, "S01", "乙。"),
        seg(3, 111.2, 120.2, "S01", "丙。"),
    ]

    result = assign_paragraphs(segments)

    _assert_paragraph_invariants(result)
    _assert_no_short_paragraph_beside_its_own_speaker(result)
    durations = _paragraph_durations(result)
    assert sorted(durations) == [1, 2]
    assert all(duration >= PARAGRAPH_MIN_SECONDS for duration in durations.values())


def test_short_paragraph_between_two_same_speaker_paragraphs_is_absorbed() -> None:
    # Both neighbours speak the same language: the 5s aside joins the one
    # whose union already fits the hard limit (no re-cut needed).
    segments = [
        seg(1, 0, 70, "S01", "甲。"),
        seg(2, 71.2, 76.2, "S01", "短。"),
        seg(3, 77.4, 117.4, "S01", "乙。"),
        seg(4, 117.4, 157.4, "S01", "丙。"),
        seg(5, 157.4, 195, "S01", "丁。"),
        seg(6, 196.2, 230, "S02", "戊。"),
    ]

    result = assign_paragraphs(segments)

    _assert_paragraph_invariants(result)
    _assert_no_short_paragraph_beside_its_own_speaker(result)
    assert _paragraphs(result) == [1, 1, 2, 2, 2, 3]


def test_short_paragraph_merges_left_and_is_re_cut_when_the_union_overshoots() -> None:
    # Only the left neighbour shares the speaker and 118s + 3s exceeds 120s,
    # so the union is cut again instead of keeping the 3s tail.
    segments = [
        seg(1, 0, 55, "S01", "甲。"),
        seg(2, 55, 118, "S01", "乙。"),
        seg(3, 119.2, 122.2, "S01", "丙。"),
        seg(4, 123.4, 150, "S02", "丁。"),
    ]

    result = assign_paragraphs(segments)

    _assert_paragraph_invariants(result)
    _assert_no_short_paragraph_beside_its_own_speaker(result)
    assert _paragraphs(result) == [1, 2, 2, 3]
    durations = _paragraph_durations(result)
    assert durations[2] >= PARAGRAPH_MIN_SECONDS
    assert durations[2] <= PARAGRAPH_MAX_SECONDS


def test_short_paragraph_survives_only_when_no_cut_clears_both_floors() -> None:
    # One 118s segment cannot be cut, so the 3s tail would either exceed 120s
    # (merged) or stay short (split). The hard limit wins; this is the only
    # documented way for a same-speaker short paragraph to survive.
    segments = [seg(1, 0, 118, "S01"), seg(2, 119.2, 122.2, "S01")]

    result = assign_paragraphs(segments)

    assert _paragraphs(result) == [1, 2]
    assert max(_paragraph_durations(result).values()) <= PARAGRAPH_MAX_SECONDS
    assert _paragraph_durations(result)[2] == pytest.approx(3)


def test_short_paragraph_is_not_absorbed_into_an_uncuttable_over_limit_neighbour() -> None:
    # A 129s segment is already past the hard limit and cannot be cut; adding
    # the 2.6s tail would leave one paragraph of 132s, so the tail stands.
    segments = [seg(1, 0, 129.13, "S01", "很长。"), seg(2, 129.9, 132.47, "S01", "尾巴。")]

    result = assign_paragraphs(segments)

    assert _paragraphs(result) == [1, 2]
    durations = _paragraph_durations(result)
    assert durations[1] == pytest.approx(129.13)
    assert durations[2] == pytest.approx(2.57)


# ---------------------------------------------------------------------------


def test_hard_limit_cut_prefers_the_last_sentence_final_punctuation() -> None:
    segments = [
        seg(1, 0, 40, "S01", "第一句。"),
        seg(2, 40, 80, "S01", "第二句。"),
        seg(3, 80, 110, "S01", "第三句。"),
        seg(4, 110, 130, "S01", "没有标点"),
    ]

    result = assign_paragraphs(segments)

    assert _paragraphs(result) == [1, 1, 1, 2]
    assert max(segment.end for segment in result if segment.paragraph == 1) <= 120


def test_hard_limit_cut_does_not_prefer_a_pause_after_an_unfinished_sentence() -> None:
    # No boundary ends a sentence, so the 0.85s pause must not drive the cut:
    # the hard ladder takes the last boundary that fits the window instead.
    segments = [
        seg(1, 0, 40, "S01", "甲"),
        seg(2, 40, 80, "S01", "乙"),
        seg(3, 80.85, 110, "S01", "丙"),
        seg(4, 110, 130, "S01", "丁"),
    ]

    result = assign_paragraphs(segments)

    assert _paragraphs(result) == [1, 1, 1, 2]
    assert result[3].start == 110
    assert max(_paragraph_durations(result).values()) <= PARAGRAPH_MAX_SECONDS


def test_hard_limit_cut_falls_back_to_a_hard_segment_boundary() -> None:
    segments = [
        seg(1, 0, 50, "S01", "甲"),
        seg(2, 50, 100, "S01", "乙"),
        seg(3, 100, 130, "S01", "丙"),
    ]

    result = assign_paragraphs(segments)

    assert _paragraphs(result) == [1, 1, 2]
    assert result[2].start == 100


def test_hard_limit_fallback_keeps_a_ten_second_tail_when_it_can() -> None:
    # The last punctuation inside the window would leave a 6s tail; the
    # previous punctuation is chosen so both paragraphs clear the soft floor.
    segments = [
        seg(1, 0, 100, "S01", "第一句。"),
        seg(2, 100, 119, "S01", "第二句。"),
        seg(3, 119, 125, "S01", "尾巴"),
    ]

    result = assign_paragraphs(segments)

    assert _paragraphs(result) == [1, 2, 2]
    assert result[1].start == 100


# ---------------------------------------------------------------------------
# Purity, idempotency and global invariants
# ---------------------------------------------------------------------------


def test_grouping_is_idempotent() -> None:
    segments = [
        seg(1, 0, 3, "S01", "第一句。"),
        seg(2, 4, 14, "S01", "第二句。"),
        seg(3, 14, 30, "S02", "第三句。"),
        seg(4, 40, 70, "S02", "第四句。"),
    ]

    once = assign_paragraphs(segments)
    twice = assign_paragraphs(once)

    assert _paragraphs(once) == [1, 1, 2, 3]
    assert twice == once


def test_grouping_does_not_mutate_the_input_segments() -> None:
    segments = [seg(1, 0, 10, "S01"), seg(2, 10, 20, "S02")]

    result = assign_paragraphs(segments)

    assert all(segment.paragraph is None for segment in segments)
    assert result[0] is not segments[0]


def test_numbering_is_contiguous_and_paragraphs_are_speaker_pure() -> None:
    segments = [
        seg(1, 0, 3, "S01", "第一句。"),
        seg(2, 4, 14, "S01", "第二句。"),
        seg(3, 14, 30, "S02", "第三句。"),
        seg(4, 31, 80, "S02", "第四句。"),
        seg(5, 81, 130, "S02", "第五句。"),
        seg(6, 130, 140, "S03", "第六句。"),
    ]

    result = assign_paragraphs(segments)

    _assert_paragraph_invariants(result)
    assert _paragraphs(result) == [1, 1, 2, 3, 4, 5]


def test_short_paragraphs_only_survive_next_to_a_different_speaker() -> None:
    segments = [
        seg(1, 0, 3, "S01", "第一句。"),
        seg(2, 4, 14, "S02", "第二句。"),
        seg(3, 24, 34, "S02", "第三句。"),
        seg(4, 34, 35, "S03", "第四句。"),
    ]

    result = assign_paragraphs(segments)

    durations = {}
    for number in sorted({segment.paragraph for segment in result}):
        members = [segment for segment in result if segment.paragraph == number]
        durations[number] = members[-1].end - members[0].start
    assert durations[1] == 3  # S01 only borders a different speaker
    assert durations[2] == 10
    assert durations[3] == 10
    assert durations[4] == 1  # S03 only borders a different speaker


# ---------------------------------------------------------------------------
# Pipeline landing points (HTTP only: upload -> task -> poll -> result)
# ---------------------------------------------------------------------------


def _make_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BEEBOX_INLINE_LOCAL_TRANSCRIPTION", "1")
    monkeypatch.delenv("BEEBOX_API_TOKEN", raising=False)
    from backend.app import create_app

    return TestClient(create_app())


def _wait_for_task(client: TestClient, task_id: str, predicate, description: str) -> dict:
    deadline = time.time() + 30
    last_task: dict = {}
    while time.time() < deadline:
        response = client.get(f"/v1/jobs/{task_id}")
        assert response.status_code == 200
        last_task = response.json()
        if predicate(last_task):
            return last_task
        time.sleep(0.05)
    raise AssertionError(f"{task_id} never reached {description}; last={last_task}")


def _create_job(client: TestClient, filename: str, content: bytes, **options) -> str:
    uploaded = client.post("/v1/files", files={"file": (filename, content, "audio/wav")})
    assert uploaded.status_code == 201, uploaded.text
    created = client.post(
        "/v1/jobs",
        json={
            "source": {"type": "file", "file_id": uploaded.json()["id"]},
            "options": {"model": "whisper-base", "diarization": False, **options},
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def test_completed_task_stores_paragraph_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_speech_env(
        monkeypatch,
        segments=[
            {"start": 0.0, "end": 3.0, "text": "第一句。"},
            {"start": 4.0, "end": 14.0, "text": "第二句。"},
            {"start": 14.0, "end": 24.0, "text": "第三句。", "speaker": "S02"},
        ],
        text="第一句。第二句。第三句。",
        duration=24.0,
    )
    client = _make_client(tmp_path, monkeypatch)

    task_id = _create_job(client, "paragraphs.wav", make_wav_bytes(24.0))
    _wait_for_task(
        client, task_id, lambda row: row["status"] == "succeeded", "succeeded"
    )
    task = client.get(f"/v1/jobs/{task_id}/result").json()

    assert _paragraphs_of(task) == [1, 1, 2]
    assert [segment["speaker"] for segment in task["segments"]] == [None, None, "S02"]


def _paragraphs_of(task: dict) -> list[int | None]:
    return [segment["paragraph"] for segment in task["segments"]]


def test_chunk_retry_merge_groups_stored_segments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_speech_env(
        monkeypatch,
        segments=[{"start": 0.0, "end": 40.0, "text": "音频。"}],
        text="音频。",
        duration=40.0,
        diarization={"speakers": ["S01", "S02"]},
    )
    client = _make_client(tmp_path, monkeypatch)

    task_id = _create_job(
        client, "retry.wav", make_wav_bytes(181.0), diarization=True
    )
    _wait_for_task(
        client, task_id, lambda row: row["status"] == "succeeded", "succeeded"
    )
    task = client.get(f"/v1/jobs/{task_id}/result").json()

    assert _paragraphs_of(task) == [1, 2]
    assert [segment["speaker"] for segment in task["segments"]] == ["S01", "S02"]

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from backend.models import TranscriptSegment
from backend.services.errors import BeeBoxError

from backend.services.diarization import (
    DiarizationTurn,
    apply_diarization,
    assign_speakers,
    count_cross_speaker_segments,
    is_diarization_ready,
    renumber_speakers,
    smooth_speaker_segments,
    smooth_turns,
)


def _segment(segment_id: int, start: float, end: float, text: str = "文本") -> TranscriptSegment:
    return TranscriptSegment(id=segment_id, start=start, end=end, text=text)


def _turn(start: float, end: float, speaker: str) -> DiarizationTurn:
    return DiarizationTurn(start=start, end=end, speaker=speaker)


def test_assign_speakers_uses_maximum_time_overlap() -> None:
    segments = [_segment(1, 0.0, 4.0)]
    turns = [_turn(0.0, 1.0, "A"), _turn(1.0, 4.0, "B")]

    assigned = assign_speakers(segments, turns)

    assert assigned[0].speaker == "B"


def test_assign_speakers_accumulates_all_turns_of_one_speaker() -> None:
    segments = [_segment(1, 0.0, 3.0)]
    turns = [_turn(0.0, 1.0, "A"), _turn(1.0, 2.0, "A"), _turn(2.0, 3.0, "B")]

    assigned = assign_speakers(segments, turns)

    assert assigned[0].speaker == "A"


def test_assign_speakers_breaks_equal_overlap_ties_by_earliest_turn() -> None:
    segments = [_segment(1, 0.0, 2.0)]
    turns = [_turn(0.0, 1.0, "B"), _turn(1.0, 2.0, "A")]

    assigned = assign_speakers(segments, turns)
    reversed_assigned = assign_speakers(segments, list(reversed(turns)))

    assert assigned[0].speaker == "B"
    assert reversed_assigned[0].speaker == "B"


def test_assign_speakers_leaves_a_segment_far_from_every_turn_unassigned() -> None:
    segments = [_segment(1, 10.0, 11.0)]
    turns = [_turn(0.0, 1.0, "A")]

    assigned = assign_speakers(segments, turns, gap_fill_seconds=0.5)

    assert assigned[0].speaker is None


def test_assign_speakers_fills_a_small_gap_from_the_nearest_turn() -> None:
    segments = [_segment(1, 1.2, 1.8)]
    turns = [_turn(0.0, 1.0, "A"), _turn(2.0, 3.0, "A")]

    assigned = assign_speakers(segments, turns, gap_fill_seconds=0.5)

    assert assigned[0].speaker == "A"


def test_assign_speakers_does_not_invent_a_speaker_for_an_ambiguous_gap() -> None:
    segments = [_segment(1, 1.4, 1.6)]
    turns = [_turn(0.0, 1.0, "A"), _turn(2.0, 3.0, "B")]

    assigned = assign_speakers(segments, turns, gap_fill_seconds=0.5)

    assert assigned[0].speaker is None


def test_assign_speakers_keeps_text_and_timestamps_untouched() -> None:
    segment = _segment(7, 0.5, 2.5, "原始文本。")
    turns = [_turn(0.0, 3.0, "A")]

    assigned = assign_speakers([segment], turns)

    assert assigned[0].id == 7
    assert assigned[0].text == "原始文本。"
    assert (assigned[0].start, assigned[0].end) == (0.5, 2.5)


def test_renumber_speakers_orders_labels_by_first_appearance() -> None:
    segments = [
        _segment(1, 0.0, 1.0),
        _segment(2, 1.0, 2.0),
        _segment(3, 2.0, 3.0),
        _segment(4, 3.0, 4.0),
    ]
    segments[0].speaker = "SPEAKER_01"
    segments[1].speaker = "SPEAKER_00"
    segments[2].speaker = None
    segments[3].speaker = "SPEAKER_01"

    renumbered = renumber_speakers(segments)

    assert [segment.speaker for segment in renumbered] == ["1", "2", None, "1"]


def test_smooth_turns_absorbs_a_short_speaker_island() -> None:
    turns = [_turn(0.0, 2.0, "A"), _turn(2.0, 2.2, "X"), _turn(2.2, 5.0, "A")]

    smoothed = smooth_turns(turns, min_turn_seconds=0.3)

    assert [(turn.start, turn.end, turn.speaker) for turn in smoothed] == [(0.0, 5.0, "A")]


def test_smooth_turns_keeps_a_short_turn_between_different_speakers() -> None:
    turns = [_turn(0.0, 2.0, "A"), _turn(2.0, 2.2, "X"), _turn(2.2, 5.0, "B")]

    smoothed = smooth_turns(turns, min_turn_seconds=0.3)

    assert [(turn.start, turn.end, turn.speaker) for turn in smoothed] == [
        (0.0, 2.0, "A"),
        (2.0, 2.2, "X"),
        (2.2, 5.0, "B"),
    ]


def test_smooth_turns_merges_same_speaker_turns_across_a_small_gap() -> None:
    turns = [_turn(0.0, 1.0, "A"), _turn(1.3, 2.0, "A")]

    smoothed = smooth_turns(turns, merge_gap_seconds=0.5)

    assert [(turn.start, turn.end, turn.speaker) for turn in smoothed] == [(0.0, 2.0, "A")]


def test_smooth_turns_keeps_same_speaker_turns_across_a_large_gap() -> None:
    turns = [_turn(0.0, 1.0, "A"), _turn(2.0, 3.0, "A")]

    smoothed = smooth_turns(turns, merge_gap_seconds=0.5)

    assert len(smoothed) == 2


def test_smooth_speaker_segments_merges_a_short_same_speaker_tail() -> None:
    segments = [
        _segment(1, 0.0, 1.0, "你好"),
        _segment(2, 1.0, 1.4, "世界"),
    ]
    for segment in segments:
        segment.speaker = "A"

    smoothed = smooth_speaker_segments(segments, min_segment_ms=800)

    assert len(smoothed) == 1
    assert smoothed[0].text == "你好世界"
    assert smoothed[0].speaker == "A"
    assert (smoothed[0].start, smoothed[0].end) == (0.0, 1.4)


def test_smooth_speaker_segments_keeps_a_short_other_speaker_between_same_speaker() -> None:
    segments = [
        _segment(1, 0.0, 1.0, "甲"),
        _segment(2, 1.0, 1.4, "嗯"),
        _segment(3, 1.4, 2.5, "乙"),
    ]
    for segment, speaker in zip(segments, ["A", "B", "A"], strict=True):
        segment.speaker = speaker

    smoothed = smooth_speaker_segments(segments, min_segment_ms=800)

    assert [(segment.text, segment.speaker) for segment in smoothed] == [
        ("甲", "A"),
        ("嗯", "B"),
        ("乙", "A"),
    ]
    assert [(segment.start, segment.end) for segment in smoothed] == [(0.0, 1.0), (1.0, 1.4), (1.4, 2.5)]


def test_smooth_speaker_segments_merges_same_speaker_across_a_default_half_second_gap() -> None:
    segments = [
        _segment(1, 0.0, 2.0, "第一句"),
        _segment(2, 2.5, 2.9, "尾音"),
    ]
    for segment in segments:
        segment.speaker = "A"

    smoothed = smooth_speaker_segments(segments)

    assert len(smoothed) == 1
    assert smoothed[0].text == "第一句尾音"
    assert (smoothed[0].start, smoothed[0].end) == (0.0, 2.9)


def test_smooth_speaker_segments_does_not_merge_same_speaker_across_a_one_second_pause() -> None:
    segments = [
        _segment(1, 0.0, 2.0, "第一句"),
        _segment(2, 3.0, 3.4, "尾音"),
    ]
    for segment in segments:
        segment.speaker = "A"

    smoothed = smooth_speaker_segments(segments)

    assert [segment.text for segment in smoothed] == ["第一句", "尾音"]
    assert [(segment.start, segment.end) for segment in smoothed] == [(0.0, 2.0), (3.0, 3.4)]


def test_smooth_speaker_segments_keeps_a_short_turn_between_different_speakers() -> None:
    segments = [
        _segment(1, 0.0, 1.0, "甲"),
        _segment(2, 1.0, 1.4, "嗯"),
        _segment(3, 1.4, 2.5, "乙"),
    ]
    for segment, speaker in zip(segments, ["A", "B", "C"], strict=True):
        segment.speaker = speaker

    smoothed = smooth_speaker_segments(segments, min_segment_ms=800)

    assert [segment.text for segment in smoothed] == ["甲", "嗯", "乙"]


def test_smooth_speaker_segments_joins_latin_words_with_a_space() -> None:
    segments = [
        _segment(1, 0.0, 1.0, "hello"),
        _segment(2, 1.0, 1.4, "world"),
    ]
    for segment in segments:
        segment.speaker = "A"

    smoothed = smooth_speaker_segments(segments, min_segment_ms=800)

    assert smoothed[0].text == "hello world"


def test_smooth_speaker_segments_leaves_long_segments_untouched() -> None:
    segments = [
        _segment(1, 0.0, 2.0, "第一段"),
        _segment(2, 2.0, 4.0, "第二段"),
    ]
    for segment, speaker in zip(segments, ["A", "B"], strict=True):
        segment.speaker = speaker

    smoothed = smooth_speaker_segments(segments, min_segment_ms=800)

    assert [(segment.start, segment.end, segment.text, segment.speaker) for segment in smoothed] == [
        (0.0, 2.0, "第一段", "A"),
        (2.0, 4.0, "第二段", "B"),
    ]


def test_count_cross_speaker_segments_flags_a_secondary_turn_with_enough_overlap() -> None:
    segments = [_segment(1, 0.0, 10.0)]
    segments[0].speaker = "A"
    turns = [_turn(0.0, 5.0, "A"), _turn(5.0, 9.0, "B")]

    stats = count_cross_speaker_segments(segments, turns)

    assert stats["cross_speaker_segments"] == 1
    assert "cross_speaker_definition" in stats
    detail = stats["cross_speaker_details"][0]
    assert (detail["start"], detail["end"], detail["speaker"]) == (0.0, 10.0, "A")
    assert detail["secondary"][0]["speaker"] == "B"
    assert detail["secondary"][0]["turn_overlap"] == pytest.approx(4.0)
    assert detail["secondary"][0]["turn_ratio"] == pytest.approx(0.4)
    assert detail["secondary"][0]["share"] == pytest.approx(4.0 / 9.0, abs=1e-3)


def test_count_cross_speaker_segments_ignores_a_turn_touching_too_little_of_the_segment() -> None:
    segments = [_segment(1, 0.0, 10.0)]
    segments[0].speaker = "A"
    turns = [_turn(0.0, 9.5, "A"), _turn(9.5, 10.0, "B")]

    stats = count_cross_speaker_segments(segments, turns)

    assert stats["cross_speaker_segments"] == 0
    assert stats["cross_speaker_details"] == []


def test_count_cross_speaker_segments_ignores_a_secondary_share_below_the_threshold() -> None:
    # Pyannote can emit overlapping turns; the share is measured against the
    # total turn speech, so B owns 4s of 14s here even though its turn covers
    # 40% of the segment on its own.
    segments = [_segment(1, 0.0, 10.0)]
    segments[0].speaker = "A"
    turns = [_turn(0.0, 10.0, "A"), _turn(0.0, 4.0, "B")]

    stats = count_cross_speaker_segments(segments, turns)

    assert stats["cross_speaker_segments"] == 0


def test_count_cross_speaker_segments_returns_zero_when_no_turn_overlaps() -> None:
    segments = [_segment(1, 10.0, 12.0)]
    turns = [_turn(0.0, 1.0, "A")]

    stats = count_cross_speaker_segments(segments, turns)

    assert stats["cross_speaker_segments"] == 0
    assert stats["cross_speaker_details"] == []


def test_count_cross_speaker_segments_handles_a_zero_length_segment() -> None:
    segments = [_segment(1, 1.0, 1.0)]
    segments[0].speaker = "A"
    turns = [_turn(0.0, 2.0, "A"), _turn(0.0, 2.0, "B")]

    stats = count_cross_speaker_segments(segments, turns)

    assert stats["cross_speaker_segments"] == 0


def test_count_cross_speaker_segments_caps_stored_details() -> None:
    segments = [_segment(index, float(index * 10), float(index * 10 + 10)) for index in range(30)]
    for segment in segments:
        segment.speaker = "A"
    turns = [
        turn
        for index in range(30)
        for turn in (
            _turn(index * 10.0, index * 10.0 + 5.0, "A"),
            _turn(index * 10.0 + 5.0, index * 10.0 + 9.0, "B"),
        )
    ]

    stats = count_cross_speaker_segments(segments, turns, max_details=5)

    assert stats["cross_speaker_segments"] == 30
    assert len(stats["cross_speaker_details"]) == 5


def test_apply_diarization_reports_cross_speaker_segments_under_final_labels(tmp_path, monkeypatch) -> None:
    audio_path = tmp_path / "meeting.wav"
    _write_wav(audio_path)
    pipeline = _FakePipeline(
        [
            (0.0, 5.0, "SPEAKER_00"),
            (5.0, 9.0, "SPEAKER_01"),
        ]
    )
    _fake_diarization(monkeypatch, tmp_path, pipeline)
    stats: dict = {}

    assigned = apply_diarization(
        str(audio_path),
        [_segment(1, 0.0, 10.0, "甲说了一半乙说了另一半")],
        stats=stats,
    )

    assert [segment.speaker for segment in assigned] == ["1"]
    assert stats["cross_speaker_segments"] == 1
    detail = stats["cross_speaker_details"][0]
    assert detail["speaker"] == "1"
    assert detail["secondary"][0]["speaker"] == "2"
    assert stats["cross_speaker_definition"]


def test_apply_diarization_does_not_require_a_stats_dict(tmp_path, monkeypatch) -> None:
    audio_path = tmp_path / "meeting.wav"
    _write_wav(audio_path)
    _fake_diarization(monkeypatch, tmp_path, _FakePipeline([(0.0, 1.0, "SPEAKER_00")]))

    assigned = apply_diarization(str(audio_path), [_segment(1, 0.0, 1.0)])

    assert [segment.speaker for segment in assigned] == ["1"]


class _FakeSegment:
    def __init__(self, start: float, end: float) -> None:
        self.start = start
        self.end = end


class _FakeAnnotation:
    def __init__(self, turns: list[tuple[float, float, str]]) -> None:
        self._turns = turns

    def itertracks(self, yield_label: bool = False):
        for start, end, speaker in self._turns:
            yield _FakeSegment(start, end), None, speaker


class _FakePipeline:
    def __init__(
        self,
        turns: list[tuple[float, float, str]],
        *,
        error: Exception | None = None,
    ) -> None:
        self.turns = turns
        self.error = error
        self.calls: list[tuple[dict, dict]] = []
        self.device = None

    def to(self, device):
        self.device = device
        return self

    def __call__(self, audio, **kwargs):
        self.calls.append((audio, kwargs))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(speaker_diarization=_FakeAnnotation(self.turns))


def _write_pyannote_files(model_dir) -> None:
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "config.yaml").write_text("pipeline: {}\n", encoding="utf-8")


def _write_wav(path, samples: int = 1600, sample_rate: int = 16000) -> None:
    import soundfile as sf

    sf.write(str(path), np.zeros(samples, dtype=np.float32), sample_rate)


def _fake_diarization(monkeypatch, tmp_path, pipeline: _FakePipeline):
    from backend.services import diarization

    model_dir = tmp_path / "pyannote-community-1"
    _write_pyannote_files(model_dir)
    monkeypatch.setattr(diarization, "diarization_model_dir", lambda model_name=None: model_dir)
    monkeypatch.setattr(diarization, "_load_pipeline", lambda directory: pipeline)
    diarization.unload_pipeline()
    return diarization


def test_is_diarization_ready_needs_no_token_and_checks_the_model_dir(tmp_path) -> None:
    missing = tmp_path / "missing"
    assert is_diarization_ready(missing) is False

    _write_pyannote_files(missing)
    assert is_diarization_ready(missing) is True


def test_apply_diarization_reads_audio_in_memory_and_honours_num_speakers(tmp_path, monkeypatch) -> None:
    audio_path = tmp_path / "meeting.wav"
    _write_wav(audio_path)
    pipeline = _FakePipeline(
        [
            (0.0, 0.6, "SPEAKER_00"),
            (0.6, 1.0, "SPEAKER_01"),
        ]
    )
    diarization = _fake_diarization(monkeypatch, tmp_path, pipeline)
    segments = [_segment(1, 0.0, 0.6, "你好"), _segment(2, 0.6, 1.0, "世界")]

    assigned = apply_diarization(str(audio_path), segments, num_speakers=2)

    audio, kwargs = pipeline.calls[0]
    assert set(audio) == {"waveform", "sample_rate"}
    assert tuple(audio["waveform"].shape) == (1, 1600)
    assert audio["sample_rate"] == 16000
    assert kwargs == {"num_speakers": 2}
    assert [segment.speaker for segment in assigned] == ["1", "2"]
    assert [segment.text for segment in assigned] == ["你好", "世界"]
    assert [(segment.start, segment.end) for segment in assigned] == [(0.0, 0.6), (0.6, 1.0)]
    assert diarization.is_diarization_ready()


def test_apply_diarization_omits_num_speakers_for_auto_detection(tmp_path, monkeypatch) -> None:
    audio_path = tmp_path / "meeting.wav"
    _write_wav(audio_path)
    pipeline = _FakePipeline([(0.0, 1.0, "SPEAKER_00")])
    _fake_diarization(monkeypatch, tmp_path, pipeline)

    apply_diarization(str(audio_path), [_segment(1, 0.0, 1.0)])

    assert pipeline.calls[0][1] == {}


def test_apply_diarization_fails_when_the_model_directory_is_missing(tmp_path, monkeypatch) -> None:
    from backend.services import diarization

    monkeypatch.setattr(diarization, "diarization_model_dir", lambda model_name=None: tmp_path / "missing")
    diarization.unload_pipeline()

    with pytest.raises(BeeBoxError) as excinfo:
        apply_diarization(str(tmp_path / "missing.wav"), [_segment(1, 0.0, 1.0)])

    assert excinfo.value.code == "DIARIZATION_FAILED"
    assert excinfo.value.stage == "diarize"
    assert "not available" in excinfo.value.message


def test_apply_diarization_fails_when_no_turns_are_returned(tmp_path, monkeypatch) -> None:
    audio_path = tmp_path / "meeting.wav"
    _write_wav(audio_path)
    _fake_diarization(monkeypatch, tmp_path, _FakePipeline([]))

    with pytest.raises(BeeBoxError) as excinfo:
        apply_diarization(str(audio_path), [_segment(1, 0.0, 1.0)])

    assert excinfo.value.code == "DIARIZATION_FAILED"
    assert excinfo.value.stage == "diarize"
    assert "no speaker turns" in excinfo.value.message


def test_apply_diarization_wraps_inference_errors(tmp_path, monkeypatch) -> None:
    audio_path = tmp_path / "meeting.wav"
    _write_wav(audio_path)
    _fake_diarization(monkeypatch, tmp_path, _FakePipeline([], error=RuntimeError("cuda exploded")))

    with pytest.raises(BeeBoxError) as excinfo:
        apply_diarization(str(audio_path), [_segment(1, 0.0, 1.0)])

    assert excinfo.value.code == "DIARIZATION_FAILED"
    assert excinfo.value.stage == "diarize"
    assert "cuda exploded" in excinfo.value.message


def test_apply_diarization_rejects_an_out_of_range_num_speakers(tmp_path, monkeypatch) -> None:
    audio_path = tmp_path / "meeting.wav"
    _write_wav(audio_path)
    pipeline = _FakePipeline([(0.0, 1.0, "SPEAKER_00")])
    _fake_diarization(monkeypatch, tmp_path, pipeline)

    with pytest.raises(BeeBoxError) as excinfo:
        apply_diarization(str(audio_path), [_segment(1, 0.0, 1.0)], num_speakers=11)

    assert excinfo.value.code == "DIARIZATION_FAILED"
    assert excinfo.value.stage == "diarize"
    assert pipeline.calls == []


def test_assign_speakers_fills_from_the_only_near_turn_when_the_other_is_far() -> None:
    segments = [_segment(1, 1.4, 1.6)]
    turns = [_turn(0.0, 0.5, "A"), _turn(2.0, 3.0, "B")]

    assigned = assign_speakers(segments, turns, gap_fill_seconds=0.5)

    assert assigned[0].speaker == "B"


def test_smooth_speaker_segments_does_not_bridge_a_long_silence() -> None:
    segments = [
        _segment(1, 0.0, 2.0, "第一段"),
        _segment(2, 10.0, 10.4, "嗯"),
    ]
    for segment in segments:
        segment.speaker = "A"

    smoothed = smooth_speaker_segments(segments, min_segment_ms=800, max_gap_seconds=1.5)

    assert [segment.text for segment in smoothed] == ["第一段", "嗯"]
    assert (smoothed[1].start, smoothed[1].end) == (10.0, 10.4)


def test_assign_speakers_handles_a_zero_length_segment_inside_a_turn() -> None:
    segments = [_segment(1, 1.0, 1.0)]
    turns = [_turn(0.0, 2.0, "A")]

    assigned = assign_speakers(segments, turns)

    assert assigned[0].speaker == "A"

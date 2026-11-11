from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from backend.models import TranscriptSegment
from backend.services.errors import BeeBoxError
from backend.services.model_paths import resolve_model_dir
from backend.services.postprocess import join_text

DIARIZATION_MODEL_NAME = "pyannote-community-1"

# A segment that sits in a diarization gap is only filled from the nearest turn
# when that turn is close enough for the assignment to be defensible.
DEFAULT_GAP_FILL_SECONDS = 0.5

# A segment is reported as cross-speaker when a secondary voice owns a real
# share of the speech inside it, not just a brief overlap fragment.
CROSS_SPEAKER_MIN_SECONDARY_SHARE = 0.30
CROSS_SPEAKER_MIN_TURN_RATIO = 0.40
CROSS_SPEAKER_DETAIL_LIMIT = 20

CROSS_SPEAKER_DEFINITION = (
    "A segment counts as cross-speaker when, over the speech covered by its "
    "time span, a secondary speaker owns >= 30% of the total turn overlap and "
    "at least one of that speaker's turns overlaps >= 40% of the segment "
    "duration."
)

_lock = threading.Lock()
_cached_dir: Path | None = None
_cached_pipeline: object | None = None


@dataclass(frozen=True)
class DiarizationTurn:
    """One speaker turn emitted by pyannote, in seconds."""

    start: float
    end: float
    speaker: str


def _clamp_turns(turns: Iterable[DiarizationTurn]) -> list[DiarizationTurn]:
    clamped: list[DiarizationTurn] = []
    for turn in turns:
        start = max(0.0, float(turn.start))
        end = max(start, float(turn.end))
        if end <= start:
            continue
        clamped.append(DiarizationTurn(start=start, end=end, speaker=str(turn.speaker)))
    return clamped


def _overlap_totals(
    start: float,
    end: float,
    turns: Sequence[DiarizationTurn],
) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    """Return per-speaker overlap seconds, largest single-turn overlap and earliest start."""
    totals: dict[str, float] = {}
    longest: dict[str, float] = {}
    first_starts: dict[str, float] = {}
    for turn in turns:
        overlap = min(end, turn.end) - max(start, turn.start)
        if overlap > 0:
            totals[turn.speaker] = totals.get(turn.speaker, 0.0) + overlap
            longest[turn.speaker] = max(longest.get(turn.speaker, 0.0), overlap)
            first_starts.setdefault(turn.speaker, turn.start)
        elif end <= start and turn.start <= start <= turn.end:
            # Backends without timestamps can emit zero-length segments; treat
            # the point as a minimal overlap instead of leaving it unassigned.
            totals[turn.speaker] = totals.get(turn.speaker, 0.0) + 1e-9
            first_starts.setdefault(turn.speaker, turn.start)
    return totals, longest, first_starts


def _nearest_gap_fill(
    start: float,
    end: float,
    turns: Sequence[DiarizationTurn],
    gap_fill_seconds: float,
) -> str | None:
    """Fill a gap-only segment only when the nearest turn is unambiguous.

    Both neighbouring turns agreeing on the speaker is the clear case. When only
    one side has a turn inside the threshold, that turn is still close enough to
    own the gap. Two different speakers inside the threshold mean the gap is
    genuinely ambiguous, so the segment stays unassigned.
    """
    previous = max((turn for turn in turns if turn.end <= start), key=lambda turn: turn.end, default=None)
    following = min((turn for turn in turns if turn.start >= end), key=lambda turn: turn.start, default=None)
    near_previous = previous if previous is not None and start - previous.end <= gap_fill_seconds else None
    near_following = following if following is not None and following.start - end <= gap_fill_seconds else None
    if near_previous is None and near_following is None:
        return None
    if near_previous is not None and near_following is not None:
        return near_previous.speaker if near_previous.speaker == near_following.speaker else None
    return (near_previous or near_following).speaker


def assign_speakers(
    segments: Sequence[TranscriptSegment],
    turns: Sequence[DiarizationTurn],
    *,
    gap_fill_seconds: float = DEFAULT_GAP_FILL_SECONDS,
) -> list[TranscriptSegment]:
    """Assign a speaker to every segment by maximum time overlap.

    Overlapping speech belongs to the speaker whose turns cover more of the
    segment; a turn covering more than half of it decides, and a contested
    boundary still goes to the longer overlap. Equal overlap is resolved
    deterministically by the earliest overlapping turn (then the label), so
    re-running produces the same labels regardless of input order. Segments that
    fall inside a silence gap keep ``speaker=None`` unless the nearest turn
    makes a fill clearly valid.
    """
    ordered = sorted(_clamp_turns(turns), key=lambda turn: (turn.start, turn.end, turn.speaker))
    updated: list[TranscriptSegment] = []
    for segment in segments:
        start = max(0.0, float(segment.start))
        end = max(start, float(segment.end))
        totals, _longest, first_starts = _overlap_totals(start, end, ordered)
        if totals:
            speaker = max(
                totals,
                key=lambda candidate: (totals[candidate], -first_starts[candidate], candidate),
            )
        else:
            speaker = _nearest_gap_fill(start, end, ordered, max(0.0, gap_fill_seconds))
        updated.append(segment.model_copy(update={"speaker": speaker}))
    return updated


def count_cross_speaker_segments(
    segments: Sequence[TranscriptSegment],
    turns: Sequence[DiarizationTurn],
    *,
    min_secondary_share: float = CROSS_SPEAKER_MIN_SECONDARY_SHARE,
    min_turn_ratio: float = CROSS_SPEAKER_MIN_TURN_RATIO,
    max_details: int = CROSS_SPEAKER_DETAIL_LIMIT,
) -> dict[str, Any]:
    """Count segments whose text span also covers a substantial second voice.

    Text cannot be split at a speaker change after alignment (the characters no
    longer carry per-word times), so the transcript keeps one label per
    segment. This measurement makes the contamination visible instead of
    pretending it does not exist.
    """
    ordered = sorted(_clamp_turns(turns), key=lambda turn: (turn.start, turn.end, turn.speaker))
    limit = max(0, int(max_details))
    share_floor = max(0.0, float(min_secondary_share))
    ratio_floor = max(0.0, float(min_turn_ratio))
    count = 0
    details: list[dict[str, Any]] = []
    for segment in segments:
        start = max(0.0, float(segment.start))
        end = max(start, float(segment.end))
        duration = end - start
        if duration <= 0:
            continue
        totals, longest, first_starts = _overlap_totals(start, end, ordered)
        speech_total = sum(totals.values())
        if speech_total <= 0:
            continue
        primary = max(
            totals,
            key=lambda candidate: (totals[candidate], -first_starts[candidate], candidate),
        )
        secondaries = [
            {
                "speaker": speaker,
                "share": round(totals[speaker] / speech_total, 3),
                "turn_overlap": round(longest[speaker], 3),
                "turn_ratio": round(longest[speaker] / duration, 3),
            }
            for speaker in totals
            if speaker != primary
            and totals[speaker] / speech_total >= share_floor
            and longest[speaker] / duration >= ratio_floor
        ]
        if not secondaries:
            continue
        count += 1
        if len(details) < limit:
            details.append(
                {
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "speaker": segment.speaker,
                    "secondary": sorted(secondaries, key=lambda item: (-item["share"], item["speaker"])),
                }
            )
    return {
        "cross_speaker_segments": count,
        "cross_speaker_definition": CROSS_SPEAKER_DEFINITION,
        "cross_speaker_details": details,
    }


def renumber_speakers(segments: Sequence[TranscriptSegment]) -> list[TranscriptSegment]:
    """Renumber non-null labels to ``"1"``, ``"2"``, ... by first appearance.

    Unassigned segments keep ``None``. Text and timestamps are never touched.
    """
    mapping: dict[str, str] = {}
    updated: list[TranscriptSegment] = []
    for segment in segments:
        speaker = segment.speaker
        if speaker is not None:
            if speaker not in mapping:
                mapping[speaker] = str(len(mapping) + 1)
            speaker = mapping[speaker]
        updated.append(segment.model_copy(update={"speaker": speaker}))
    return updated


def smooth_turns(
    turns: Sequence[DiarizationTurn],
    *,
    min_turn_seconds: float = 0.3,
    merge_gap_seconds: float = 0.5,
) -> list[DiarizationTurn]:
    """Remove short spurious speaker islands and join same-speaker turns.

    A turn shorter than ``min_turn_seconds`` that is sandwiched between two
    turns of the *same* other speaker is almost certainly a diarization
    artefact (a click, breath or overlap fragment), so it is relabelled to that
    speaker. A short turn between two different speakers is a real switch and
    is preserved. Same-speaker turns separated by at most
    ``merge_gap_seconds`` are joined.
    """

    def merge_adjacent(entries: list[DiarizationTurn]) -> list[DiarizationTurn]:
        merged: list[DiarizationTurn] = []
        for turn in entries:
            if (
                merged
                and merged[-1].speaker == turn.speaker
                and turn.start - merged[-1].end <= merge_gap_seconds
            ):
                previous = merged[-1]
                merged[-1] = DiarizationTurn(
                    start=previous.start,
                    end=max(previous.end, turn.end),
                    speaker=previous.speaker,
                )
            else:
                merged.append(turn)
        return merged

    ordered = sorted(_clamp_turns(turns), key=lambda turn: (turn.start, turn.end, turn.speaker))
    if merge_gap_seconds > 0:
        ordered = merge_adjacent(ordered)
    for _ in range(len(ordered) + 1):
        if min_turn_seconds <= 0:
            break
        changed = False
        relabelled: list[DiarizationTurn] = []
        for index, turn in enumerate(ordered):
            duration = turn.end - turn.start
            previous = ordered[index - 1] if index > 0 else None
            following = ordered[index + 1] if index + 1 < len(ordered) else None
            if (
                duration < min_turn_seconds
                and previous is not None
                and following is not None
                and previous.speaker == following.speaker
                and turn.speaker != previous.speaker
            ):
                relabelled.append(DiarizationTurn(start=turn.start, end=turn.end, speaker=previous.speaker))
                changed = True
            else:
                relabelled.append(turn)
        ordered = merge_adjacent(relabelled) if merge_gap_seconds > 0 else relabelled
        if not changed:
            break
    return ordered


def _absorb(previous: TranscriptSegment, *following: TranscriptSegment) -> TranscriptSegment:
    text = previous.text
    for segment in following:
        text = join_text(text, segment.text)
    return previous.model_copy(
        update={
            "text": text,
            "end": max(previous.end, *(segment.end for segment in following)),
        }
    )


def smooth_speaker_segments(
    segments: Sequence[TranscriptSegment],
    *,
    min_segment_ms: int = 800,
    max_gap_seconds: float = 0.5,
) -> list[TranscriptSegment]:
    """Merge short fragments only when they share the same speaker.

    Splitting at sentence boundaries can land a short utterance between two
    turns of another speaker. Folding it into the surrounding run would relabel
    that text as the wrong speaker, so only same-speaker neighbours are ever
    joined. Rules, applied left to right:
    - a short segment is appended to the previous segment when it has the same
      speaker and they are at most ``max_gap_seconds`` apart;
    - a short segment between different speakers is a real turn and is kept,
      even when both neighbours share one speaker;
    - a leading short segment is folded into the following same-speaker segment.
    Text is concatenated in order and the timestamps span both segments. A
    short fragment after a longer pause is kept separate: the default gap is
    half a second so the >= 1s silences later used as paragraph boundaries are
    not erased by smoothing.
    """
    if not segments:
        return []
    minimum = max(0, int(min_segment_ms)) / 1000.0
    maximum_gap = max(0.0, float(max_gap_seconds))
    ordered = list(segments)
    result: list[TranscriptSegment] = []
    index = 0
    while index < len(ordered):
        segment = ordered[index]
        duration = max(0.0, segment.end - segment.start)
        if duration < minimum:
            previous = result[-1] if result else None
            following = ordered[index + 1] if index + 1 < len(ordered) else None
            if (
                previous is not None
                and previous.speaker == segment.speaker
                and segment.start - previous.end <= maximum_gap
            ):
                result[-1] = _absorb(previous, segment)
                index += 1
                continue
            if (
                previous is None
                and following is not None
                and following.speaker == segment.speaker
                and following.start - segment.end <= maximum_gap
            ):
                ordered[index + 1] = _absorb(segment, following)
                index += 1
                continue
        result.append(segment)
        index += 1
    return result


def is_diarization_ready(model_dir: Path | None = None) -> bool:
    try:
        import pyannote.audio  # noqa: F401
    except Exception:
        return False
    directory = model_dir or diarization_model_dir()
    return _model_files_present(directory)


def diarization_model_dir(model_name: str | None = None) -> Path:
    """Resolve the preseeded pyannote directory (no HF token, no network)."""
    override = os.environ.get("BEEBOX_DIARIZATION_MODEL_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return resolve_model_dir(model_name or DIARIZATION_MODEL_NAME)


def _model_files_present(model_dir: Path) -> bool:
    return (model_dir / "config.yaml").exists()


def unload_pipeline() -> bool:
    """Drop the cached pipeline so its VRAM can be reclaimed."""
    global _cached_dir, _cached_pipeline
    with _lock:
        removed = _cached_pipeline is not None
        _cached_dir = None
        _cached_pipeline = None
    if removed:
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
    return removed


def _load_pipeline(model_dir: Path):
    import torch
    from pyannote.audio import Pipeline

    pipeline = Pipeline.from_pretrained(str(model_dir))
    pipeline.to(torch.device("cuda" if torch.cuda.is_available() else "cpu"))
    return pipeline


def _get_pipeline(model_dir: Path):
    global _cached_dir, _cached_pipeline
    with _lock:
        if _cached_pipeline is not None and _cached_dir == model_dir:
            return _cached_pipeline
        if not _model_files_present(model_dir):
            raise BeeBoxError(
                "DIARIZATION_FAILED",
                f"Diarization model is not available at {model_dir}",
                stage="diarize",
            )
        try:
            pipeline = _load_pipeline(model_dir)
        except Exception as exc:
            raise BeeBoxError(
                "DIARIZATION_FAILED",
                f"Failed to load diarization model from {model_dir}: {type(exc).__name__}: {exc}",
                stage="diarize",
            ) from exc
        _cached_dir = model_dir
        _cached_pipeline = pipeline
        return pipeline


def _read_waveform(audio_path: str):
    """Decode the normalized wav with soundfile, never torchcodec.

    pyannote 4's file-path input goes through torchcodec, whose DLLs are broken
    on Windows. Passing ``{"waveform": (channels, samples), "sample_rate": sr}``
    keeps the audio in memory and bypasses that path entirely.
    """
    import soundfile as sf
    import torch

    wf, sample_rate = sf.read(str(audio_path), dtype="float32", always_2d=True)
    if wf.size == 0:
        raise ValueError(f"audio file is empty: {audio_path}")
    return torch.from_numpy(wf.T), int(sample_rate)


def _extract_turns(output) -> list[DiarizationTurn]:
    annotation = getattr(output, "speaker_diarization", None)
    if annotation is None:
        annotation = output
    itertracks = getattr(annotation, "itertracks", None)
    if itertracks is None:
        raise TypeError(f"unexpected diarization output: {type(output).__name__}")
    return [
        DiarizationTurn(start=float(turn.start), end=float(turn.end), speaker=str(speaker))
        for turn, _, speaker in itertracks(yield_label=True)
    ]


def apply_diarization(
    audio_path: str,
    segments: list[TranscriptSegment],
    *,
    num_speakers: int | None = None,
    model_name: str | None = None,
    stats: dict[str, Any] | None = None,
) -> list[TranscriptSegment]:
    """Diarize ``audio_path`` and assign speakers to already-aligned segments.

    The pipeline is loaded from the local preseeded directory (no token or
    network) and invoked with an in-memory waveform. Any load, decode or
    inference failure raises ``BeeBoxError("DIARIZATION_FAILED")``: a
    transcript without speakers must never be stored silently.

    When ``stats`` is given it is filled in place with the cross-speaker segment
    measurement. Both the segments and the turns are only available here, so
    this is the single place where contamination can be observed.
    """
    if num_speakers is not None:
        if (
            isinstance(num_speakers, bool)
            or not isinstance(num_speakers, int)
            or not 1 <= num_speakers <= 10
        ):
            raise BeeBoxError(
                "DIARIZATION_FAILED",
                f"num_speakers must be an integer between 1 and 10, got {num_speakers!r}",
                stage="diarize",
            )
    model_dir = diarization_model_dir(model_name)
    if not _model_files_present(model_dir):
        raise BeeBoxError(
            "DIARIZATION_FAILED",
            f"Diarization model is not available at {model_dir}",
            stage="diarize",
        )
    try:
        pipeline = _get_pipeline(model_dir)
        waveform, sample_rate = _read_waveform(audio_path)
        call_kwargs = {"num_speakers": num_speakers} if num_speakers is not None else {}
        output = pipeline({"waveform": waveform, "sample_rate": sample_rate}, **call_kwargs)
        turns = _extract_turns(output)
    except BeeBoxError:
        raise
    except Exception as exc:
        raise BeeBoxError(
            "DIARIZATION_FAILED",
            f"Speaker diarization failed: {type(exc).__name__}: {exc}",
            stage="diarize",
        ) from exc
    if not turns:
        raise BeeBoxError(
            "DIARIZATION_FAILED",
            "Speaker diarization produced no speaker turns",
            stage="diarize",
        )
    smoothed_turns = smooth_turns(turns)
    assigned = assign_speakers(segments, smoothed_turns)
    smoothed = smooth_speaker_segments(assigned)
    renumbered = renumber_speakers(smoothed)
    if stats is not None:
        label_map = {
            original.speaker: final.speaker
            for original, final in zip(smoothed, renumbered)
            if original.speaker is not None
        }
        # A speaker that only ever appears as a secondary voice never owns a
        # segment, so it has no number yet; continue the same sequence.
        next_label = len(set(label_map.values())) + 1
        for turn in smoothed_turns:
            if turn.speaker not in label_map:
                label_map[turn.speaker] = str(next_label)
                next_label += 1
        final_turns = [
            DiarizationTurn(
                start=turn.start,
                end=turn.end,
                speaker=label_map[turn.speaker],
            )
            for turn in smoothed_turns
        ]
        stats.update(count_cross_speaker_segments(renumbered, final_turns))
    return renumbered

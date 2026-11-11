"""Subtitle track priority for video-link jobs (ticket #43).

The ticket fixes the order in which a platform's tracks are considered:

1. a manual Chinese track (Simplified script preferred),
2. an automatic Chinese track,
3. a manual track in the video's original language,
4. an automatic track in the video's original language.

Platform machine-translated tracks are never candidates, and neither are
formats the subtitle parser cannot read. ``subtitle_language`` restricts the
whole decision to one language; ``prefer=asr`` skips the platform tracks
entirely and routes the job to speech recognition (ticket #44 runs it).

Every tie is broken deterministically: Chinese script rank (Simplified, then
an unspecified script, then Traditional) followed by the order the platform
reported the tracks in. When the platform does not report the video's original
language, every non-Chinese track is treated as a candidate for tiers 3 and 4;
the platform order then decides.

Bilibili publishes its AI-generated captions as ``ai-<lang>`` tracks (ticket
#65). The marker is not a language: :func:`canonical_track` strips it so
``ai-zh`` is a Chinese automatic caption, and an ``ai-<lang>`` track whose
language differs from the video's original language is a platform machine
translation, which is never selected.

The module is pure: no network, no database and no downloader import, so the
fake platform can exercise every combination offline.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace

from backend.services.video_platform.models import (
    TRACK_KIND_AUTO,
    TRACK_KIND_MANUAL,
    SubtitleTrack,
    VideoInfo,
)
from backend.services.video_platform.subtitles import SUPPORTED_SUBTITLE_FORMATS

TEXT_PATH_SUBTITLE = "subtitle"
TEXT_PATH_ASR = "asr"

REASON_PREFER_ASR = "prefer_asr"
REASON_NO_USABLE_SUBTITLE = "no_usable_subtitle"
REASON_SUBTITLE_LANGUAGE_UNAVAILABLE = "subtitle_language_unavailable"
REASON_MANUAL_CHINESE = "manual_chinese"
REASON_AUTO_CHINESE = "auto_chinese"
REASON_MANUAL_ORIGINAL = "manual_original"
REASON_AUTO_ORIGINAL = "auto_original"
REASON_MANUAL_REQUESTED = "manual_requested"
REASON_AUTO_REQUESTED = "auto_requested"

_TAG_SEPARATOR = re.compile(r"[-_]")
# Bilibili names its AI-generated captions ``ai-<lang>`` (ticket #65).
_AI_CAPTION_MARKER = "ai"
_CHINESE_FAMILIES = frozenset({"zh", "cmn"})
_SIMPLIFIED_REGIONS = frozenset({"cn", "sg", "my"})
_TRADITIONAL_REGIONS = frozenset({"tw", "hk", "mo"})

_SCRIPT_SIMPLIFIED = "hans"
_SCRIPT_TRADITIONAL = "hant"
_SCRIPT_UNKNOWN = ""
# Simplified first, then a tag that does not say which script it uses, then
# Traditional. Ticket #43 asks for 简体优先 without dropping Traditional tracks.
_CHINESE_SCRIPT_RANK = {_SCRIPT_SIMPLIFIED: 0, _SCRIPT_UNKNOWN: 1, _SCRIPT_TRADITIONAL: 2}


@dataclass(frozen=True)
class SubtitleDecision:
    """Which text path a video job must take, and why.

    ``path`` is :data:`TEXT_PATH_SUBTITLE` (with ``track`` set) or
    :data:`TEXT_PATH_ASR` (with ``track`` empty). ``reason`` is a stable label
    written to the job log so an operator can see which tier won.
    """

    path: str
    track: SubtitleTrack | None
    reason: str


def normalize_language_tag(tag: str | None) -> str:
    """Lowercase a language tag and normalise ``_`` separators to ``-``."""
    return "-".join(part for part in _TAG_SEPARATOR.split((tag or "").strip().lower()) if part)


def canonical_language(tag: str | None) -> tuple[str, bool]:
    """Split Bilibili's AI-caption marker off a language tag (ticket #65).

    Returns ``(language, is_ai_caption)``. ``ai-zh`` and ``ai-zh-Hans`` are
    Bilibili's AI captions in Chinese; the marker is not a language, so the
    canonical language is what follows it. The platform's spelling of the
    remainder is preserved (``zh-Hans`` stays ``zh-Hans``).
    """
    raw = (tag or "").strip()
    parts = [part for part in _TAG_SEPARATOR.split(raw) if part]
    if len(parts) >= 2 and parts[0].lower() == _AI_CAPTION_MARKER:
        return "-".join(parts[1:]), True
    return raw, False


def language_family(tag: str | None) -> str:
    """The primary language subtag (``zh-Hans`` -> ``zh``)."""
    normalized = normalize_language_tag(tag)
    return normalized.split("-", 1)[0]


def _same_language_family(left: str | None, right: str | None) -> bool:
    """Whether two tags name one language, treating ``zh`` and ``cmn`` as equal.

    The module already folds Mandarin into the Chinese family for selection
    (:data:`_CHINESE_FAMILIES`); the machine-translation test must use the same
    equivalence so ``ai-zh`` is not called a translation of a ``cmn`` video.
    """
    left_family = language_family(left)
    right_family = language_family(right)
    if left_family in _CHINESE_FAMILIES and right_family in _CHINESE_FAMILIES:
        return True
    return left_family == right_family


def canonical_track(
    track: SubtitleTrack, *, original_language: str | None = None
) -> SubtitleTrack:
    """Return the track as selection should see it.

    Bilibili's ``ai-<lang>`` tracks are platform automatic captions in
    ``<lang>``. When the video's original language is known and differs from
    ``<lang>``, the track is a platform machine translation, which the
    glossary excludes from 平台字幕 (ticket #65). With an unknown original
    language the canonical language is kept and platform order decides, as
    for every other track.
    """
    language, is_ai_caption = canonical_language(track.language)
    if not is_ai_caption:
        return track
    translated = track.translated
    if original_language:
        translated = translated or not _same_language_family(
            language, original_language
        )
    return replace(
        track,
        language=language,
        kind=TRACK_KIND_AUTO,
        translated=translated,
    )


def chinese_script(tag: str | None) -> str:
    """``hans``, ``hant`` or ``""`` (unknown/not Chinese) for one language tag."""
    if language_family(tag) not in _CHINESE_FAMILIES:
        return _SCRIPT_UNKNOWN
    parts = normalize_language_tag(tag).split("-")[1:]
    for part in parts:
        if part == _SCRIPT_SIMPLIFIED:
            return _SCRIPT_SIMPLIFIED
        if part == _SCRIPT_TRADITIONAL:
            return _SCRIPT_TRADITIONAL
    for part in parts:
        if part in _SIMPLIFIED_REGIONS:
            return _SCRIPT_SIMPLIFIED
        if part in _TRADITIONAL_REGIONS:
            return _SCRIPT_TRADITIONAL
    return _SCRIPT_UNKNOWN


def _chinese_script_rank(tag: str | None) -> int:
    return _CHINESE_SCRIPT_RANK[chinese_script(tag)]


def _matches_requested_language(track_language: str, requested: str) -> bool:
    """Whether a track satisfies a ``subtitle_language`` request.

    A bare family (``zh``, ``en``) accepts any tag in that family. A request
    that names a Chinese script (``zh-Hans``, ``zh-Hant``, ``zh-CN``,
    ``zh-TW``) only accepts tracks in that script.
    """
    if language_family(track_language) != language_family(requested):
        return False
    requested_script = chinese_script(requested)
    if requested_script:
        return chinese_script(track_language) == requested_script
    return True


def _is_usable(track: SubtitleTrack) -> bool:
    return (
        not track.translated
        and track.kind in {TRACK_KIND_MANUAL, TRACK_KIND_AUTO}
        and (track.format or "").strip().lower() in SUPPORTED_SUBTITLE_FORMATS
    )


def _is_chinese(track: SubtitleTrack) -> bool:
    return language_family(track.language) in _CHINESE_FAMILIES


def _ordered(
    tracks: list[tuple[int, SubtitleTrack]],
    *,
    chinese: bool,
    requested: str | None = None,
) -> list[SubtitleTrack]:
    """Deterministic order: script/exactness rank, then platform order."""
    requested_tag = normalize_language_tag(requested) if requested else ""

    def key(item: tuple[int, SubtitleTrack]) -> tuple[int, int, int]:
        index, track = item
        if chinese:
            rank = _chinese_script_rank(track.language)
        else:
            rank = 0
        exact = 0 if requested_tag and normalize_language_tag(track.language) == requested_tag else 1
        return (rank, exact, index)

    return [track for _, track in sorted(tracks, key=key)]


def _first_of_kind(
    usable: list[tuple[int, SubtitleTrack]],
    kind: str,
    *,
    predicate: Callable[[SubtitleTrack], bool],
    chinese: bool,
    requested: str | None = None,
) -> SubtitleTrack | None:
    candidates = [
        (index, track) for index, track in usable if track.kind == kind and predicate(track)
    ]
    if not candidates:
        return None
    return _ordered(candidates, chinese=chinese, requested=requested)[0]


def _decision(track: SubtitleTrack | None, reason: str) -> SubtitleDecision:
    if track is None:
        return SubtitleDecision(path=TEXT_PATH_ASR, track=None, reason=reason)
    return SubtitleDecision(path=TEXT_PATH_SUBTITLE, track=track, reason=reason)


def decide_subtitle(
    info: VideoInfo,
    *,
    subtitle_language: str | None = None,
    prefer_asr: bool = False,
    original_language: str | None = None,
) -> SubtitleDecision:
    """Choose the track that supplies the transcript, or the ASR path.

    ``subtitle_language`` restricts selection to the requested language;
    ``prefer_asr`` always returns :data:`TEXT_PATH_ASR`. Both are the caller's
    request, so neither falls back to a different language. ``original_language``
    overrides :attr:`VideoInfo.original_language` (a caller may know the spoken
    language even when the platform does not report it).
    """
    if prefer_asr:
        return SubtitleDecision(path=TEXT_PATH_ASR, track=None, reason=REASON_PREFER_ASR)

    # Normalise every track before anything looks at it, so a Bilibili
    # ``ai-zh`` track is a Chinese automatic caption and an ``ai-en``
    # translation of a Chinese video is excluded (ticket #65).
    original = normalize_language_tag(original_language or info.original_language)
    tracks = [
        canonical_track(track, original_language=original) for track in info.tracks
    ]
    usable = [
        (index, track) for index, track in enumerate(tracks) if _is_usable(track)
    ]
    if not usable:
        return _decision(None, REASON_NO_USABLE_SUBTITLE)

    if subtitle_language:
        requested = normalize_language_tag(subtitle_language)
        chinese_request = language_family(requested) in _CHINESE_FAMILIES

        def matches_request(track: SubtitleTrack) -> bool:
            return _matches_requested_language(track.language, requested)

        manual = _first_of_kind(
            usable,
            TRACK_KIND_MANUAL,
            predicate=matches_request,
            chinese=chinese_request,
            requested=requested,
        )
        if manual is not None:
            return _decision(manual, REASON_MANUAL_REQUESTED)
        automatic = _first_of_kind(
            usable,
            TRACK_KIND_AUTO,
            predicate=matches_request,
            chinese=chinese_request,
            requested=requested,
        )
        if automatic is not None:
            return _decision(automatic, REASON_AUTO_REQUESTED)
        return _decision(None, REASON_SUBTITLE_LANGUAGE_UNAVAILABLE)

    manual = _first_of_kind(
        usable, TRACK_KIND_MANUAL, predicate=_is_chinese, chinese=True
    )
    if manual is not None:
        return _decision(manual, REASON_MANUAL_CHINESE)
    automatic = _first_of_kind(
        usable, TRACK_KIND_AUTO, predicate=_is_chinese, chinese=True
    )
    if automatic is not None:
        return _decision(automatic, REASON_AUTO_CHINESE)

    if original:
        def in_original(track: SubtitleTrack) -> bool:
            return language_family(track.language) == language_family(original)

        manual = _first_of_kind(
            usable, TRACK_KIND_MANUAL, predicate=in_original, chinese=False
        )
        if manual is not None:
            return _decision(manual, REASON_MANUAL_ORIGINAL)
        automatic = _first_of_kind(
            usable, TRACK_KIND_AUTO, predicate=in_original, chinese=False
        )
        if automatic is not None:
            return _decision(automatic, REASON_AUTO_ORIGINAL)
        return _decision(None, REASON_NO_USABLE_SUBTITLE)

    # The platform did not say what the video's original language is. Every
    # non-Chinese track is then an "original language" candidate, in platform
    # order; Chinese translated tracks were filtered out above.
    def not_chinese(track: SubtitleTrack) -> bool:
        return not _is_chinese(track)

    manual = _first_of_kind(
        usable, TRACK_KIND_MANUAL, predicate=not_chinese, chinese=False
    )
    if manual is not None:
        return _decision(manual, REASON_MANUAL_ORIGINAL)
    automatic = _first_of_kind(
        usable, TRACK_KIND_AUTO, predicate=not_chinese, chinese=False
    )
    if automatic is not None:
        return _decision(automatic, REASON_AUTO_ORIGINAL)
    return _decision(None, REASON_NO_USABLE_SUBTITLE)


__all__ = [
    "REASON_AUTO_CHINESE",
    "REASON_AUTO_ORIGINAL",
    "REASON_AUTO_REQUESTED",
    "REASON_MANUAL_CHINESE",
    "REASON_MANUAL_ORIGINAL",
    "REASON_MANUAL_REQUESTED",
    "REASON_NO_USABLE_SUBTITLE",
    "REASON_PREFER_ASR",
    "REASON_SUBTITLE_LANGUAGE_UNAVAILABLE",
    "TEXT_PATH_ASR",
    "TEXT_PATH_SUBTITLE",
    "SubtitleDecision",
    "canonical_language",
    "canonical_track",
    "chinese_script",
    "decide_subtitle",
    "language_family",
    "normalize_language_tag",
]

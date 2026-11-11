"""Subtitle selection priority (ticket #43).

These tests drive the public decision seam the video job uses: given the
tracks a platform offers, which track supplies the transcript (or must the job
fall through to speech recognition)? The order under test is the ticket's:

中文人工字幕（简体优先）→ 中文平台自动字幕 → 原语言人工字幕 → 原语言自动字幕

and platform machine-translated tracks are never selected.
"""

from __future__ import annotations

import pytest

from backend.services.video_platform import (
    TEXT_PATH_ASR,
    TEXT_PATH_SUBTITLE,
    SubtitleTrack,
    VideoInfo,
    VideoRef,
    decide_subtitle,
)

REF = VideoRef(
    platform="youtube",
    video_id="dQw4w9WgXcQ",
    canonical_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    original_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
)


def track(
    language: str,
    kind: str = "manual",
    *,
    translated: bool = False,
    fmt: str = "srt",
) -> SubtitleTrack:
    return SubtitleTrack(
        language=language,
        kind=kind,
        format=fmt,
        url=f"https://example.invalid/{language}.{fmt}",
        translated=translated,
    )


def info(*tracks: SubtitleTrack, original_language: str | None = None) -> VideoInfo:
    return VideoInfo(
        ref=REF,
        title="Sample",
        author=None,
        duration_ms=1000,
        tracks=tuple(tracks),
        original_language=original_language,
    )


def selected(decision) -> tuple[str, str] | None:
    assert decision.path == TEXT_PATH_SUBTITLE
    assert decision.track is not None
    return decision.track.language, decision.track.kind


# ---------------------------------------------------------------------------
# Priority order
# ---------------------------------------------------------------------------


def test_manual_chinese_beats_auto_chinese_and_every_other_language() -> None:
    decision = decide_subtitle(
        info(
            track("en", "auto"),
            track("en", "manual"),
            track("zh-Hant", "auto"),
            track("zh-Hant", "manual"),
        )
    )

    assert selected(decision) == ("zh-Hant", "manual")
    assert decision.reason == "manual_chinese"


def test_simplified_chinese_is_preferred_between_manual_chinese_tracks() -> None:
    decision = decide_subtitle(
        info(
            track("zh-Hant", "manual"),
            track("zh-Hans", "manual"),
        )
    )

    assert selected(decision) == ("zh-Hans", "manual")


def test_auto_chinese_beats_manual_subtitles_in_the_original_language() -> None:
    # Ticket order puts 中文平台自动字幕 above 原语言人工字幕.
    decision = decide_subtitle(
        info(track("en", "manual"), track("zh-Hans", "auto")),
        original_language="en",
    )

    assert selected(decision) == ("zh-Hans", "auto")
    assert decision.reason == "auto_chinese"


@pytest.mark.parametrize(
    ("original_language", "expected_language"),
    [("ja", "ja"), ("en", "en")],
)
def test_original_language_manual_beats_original_language_auto(
    original_language: str, expected_language: str
) -> None:
    decision = decide_subtitle(
        info(
            track("de", "manual"),
            track("en", "auto"),
            track("ja", "manual"),
            track("en", "manual"),
            track("ja", "auto"),
        ),
        original_language=original_language,
    )

    assert selected(decision) == (expected_language, "manual")
    assert decision.reason == "manual_original"


def test_original_language_auto_is_the_last_resort() -> None:
    decision = decide_subtitle(
        info(
            track("en", "manual"),
            track("ja", "auto"),
        ),
        original_language="ja",
    )

    assert selected(decision) == ("ja", "auto")
    assert decision.reason == "auto_original"


def test_original_language_is_taken_from_video_metadata_when_not_overridden() -> None:
    decision = decide_subtitle(
        info(track("en", "manual"), track("ja", "manual"), original_language="ja")
    )

    assert selected(decision) == ("ja", "manual")


def test_without_original_language_any_manual_non_chinese_track_is_used_in_platform_order() -> None:
    decision = decide_subtitle(info(track("en", "auto"), track("ja", "manual"), track("de", "manual")))

    assert selected(decision) == ("ja", "manual")


# ---------------------------------------------------------------------------
# Machine-translated tracks are never selected
# ---------------------------------------------------------------------------


def test_machine_translated_chinese_never_outranks_a_real_original_track() -> None:
    decision = decide_subtitle(
        info(
            track("zh-Hans", "manual", translated=True),
            track("en", "manual"),
            original_language="en",
        )
    )

    assert selected(decision) == ("en", "manual")


def test_only_machine_translated_tracks_route_to_speech_recognition() -> None:
    decision = decide_subtitle(
        info(
            track("zh-Hans", "manual", translated=True),
            track("en", "auto", translated=True),
            track("ja", "auto", translated=True),
            original_language="ja",
        )
    )

    assert decision.path == TEXT_PATH_ASR
    assert decision.track is None
    assert decision.reason == "no_usable_subtitle"


def test_unsupported_subtitle_formats_are_ignored() -> None:
    decision = decide_subtitle(
        info(
            track("zh-Hans", "manual", fmt="ass"),
            track("en", "auto", fmt="vtt"),
        )
    )

    assert selected(decision) == ("en", "auto")


# ---------------------------------------------------------------------------
# Bilibili AI captions (ticket #65)
# ---------------------------------------------------------------------------


def test_bilibili_ai_chinese_caption_is_a_platform_auto_chinese_track() -> None:
    decision = decide_subtitle(info(track("ai-zh", "auto")))

    assert selected(decision) == ("zh", "auto")
    assert decision.reason == "auto_chinese"


def test_bilibili_ai_track_is_automatic_even_when_reported_as_manual() -> None:
    decision = decide_subtitle(info(track("ai-zh", "manual")))

    assert selected(decision) == ("zh", "auto")


def test_manual_chinese_subtitles_beat_the_bilibili_ai_chinese_caption() -> None:
    decision = decide_subtitle(info(track("ai-zh", "auto"), track("zh-Hans", "manual")))

    assert selected(decision) == ("zh-Hans", "manual")
    assert decision.reason == "manual_chinese"


def test_subtitle_language_zh_matches_the_bilibili_ai_chinese_caption() -> None:
    decision = decide_subtitle(info(track("ai-zh", "auto")), subtitle_language="zh")

    assert selected(decision) == ("zh", "auto")
    assert decision.reason == "auto_requested"


def test_bilibili_ai_prefix_is_stripped_from_the_returned_track() -> None:
    decision = decide_subtitle(info(track("ai-zh-Hans", "auto")))

    assert selected(decision) == ("zh-Hans", "auto")


def test_bilibili_ai_track_in_the_original_language_is_a_real_caption() -> None:
    decision = decide_subtitle(info(track("ai-zh", "auto"), original_language="zh-Hans"))

    assert selected(decision) == ("zh", "auto")
    assert decision.reason == "auto_chinese"


def test_a_mandarin_video_is_not_a_translation_of_its_zh_caption() -> None:
    """``cmn`` and ``zh`` are one family here, as everywhere else in selection."""
    for original in ("cmn", "cmn-Hans", "zh", "zh-Hans"):
        decision = decide_subtitle(
            info(track("ai-zh", "auto"), original_language=original)
        )
        assert selected(decision) == ("zh", "auto"), original
        assert decision.reason == "auto_chinese", original


def test_an_english_video_still_marks_its_ai_zh_track_as_a_translation() -> None:
    decision = decide_subtitle(
        info(track("ai-zh", "auto"), original_language="en")
    )

    assert decision.path == TEXT_PATH_ASR
    assert decision.reason == "no_usable_subtitle"


def test_bilibili_ai_translation_is_never_selected_when_the_original_language_differs() -> None:
    decision = decide_subtitle(info(track("ai-en", "auto"), original_language="zh"))

    assert decision.path == TEXT_PATH_ASR
    assert decision.track is None
    assert decision.reason == "no_usable_subtitle"


def test_bilibili_ai_chinese_caption_beats_its_own_ai_translations() -> None:
    decision = decide_subtitle(
        info(track("ai-en", "auto"), track("ai-zh", "auto"), original_language="zh")
    )

    assert selected(decision) == ("zh", "auto")
    assert decision.reason == "auto_chinese"


def test_requested_language_does_not_match_a_bilibili_ai_translation() -> None:
    decision = decide_subtitle(
        info(track("ai-en", "auto"), original_language="zh"),
        subtitle_language="en",
    )

    assert decision.path == TEXT_PATH_ASR
    assert decision.track is None
    assert decision.reason == "no_usable_subtitle"


def test_without_an_original_language_ai_tracks_keep_the_canonical_language() -> None:
    # The platform did not say what the video's language is, so nothing marks
    # the track as a translation; platform order decides (ticket #65).
    decision = decide_subtitle(info(track("ai-en", "auto")))

    assert selected(decision) == ("en", "auto")
    assert decision.reason == "auto_original"


# ---------------------------------------------------------------------------
# subtitle_language restricts the language
# ---------------------------------------------------------------------------


def test_subtitle_language_selects_the_requested_language_over_the_default_priority() -> None:
    decision = decide_subtitle(
        info(track("zh-Hans", "manual"), track("en", "auto")),
        subtitle_language="en",
    )

    assert selected(decision) == ("en", "auto")
    assert decision.reason == "auto_requested"


def test_subtitle_language_keeps_manual_over_auto_inside_the_language() -> None:
    decision = decide_subtitle(
        info(track("en", "auto"), track("en", "manual")),
        subtitle_language="en-US",
    )

    assert selected(decision) == ("en", "manual")


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("zh", "zh-Hans"),
        ("zh-Hans", "zh-Hans"),
        ("zh-Hant", "zh-Hant"),
        ("zh-TW", "zh-Hant"),
        ("zh-CN", "zh-Hans"),
    ],
)
def test_chinese_script_tags_are_normalized_for_matching(requested: str, expected: str) -> None:
    decision = decide_subtitle(
        info(track("zh-Hant", "manual"), track("zh-Hans", "manual")),
        subtitle_language=requested,
    )

    assert selected(decision) == (expected, "manual")


def test_subtitle_language_without_a_matching_track_routes_to_speech_recognition() -> None:
    decision = decide_subtitle(
        info(track("zh-Hans", "manual"), track("en", "manual")),
        subtitle_language="fr",
    )

    assert decision.path == TEXT_PATH_ASR
    assert decision.track is None
    assert decision.reason == "subtitle_language_unavailable"


def test_subtitle_language_does_not_fall_back_to_machine_translations() -> None:
    decision = decide_subtitle(
        info(track("en", "auto", translated=True)),
        subtitle_language="en",
    )

    assert decision.path == TEXT_PATH_ASR


# ---------------------------------------------------------------------------
# prefer=asr
# ---------------------------------------------------------------------------


def test_prefer_asr_ignores_usable_subtitles_and_routes_to_speech_recognition() -> None:
    decision = decide_subtitle(
        info(track("zh-Hans", "manual")),
        prefer_asr=True,
    )

    assert decision.path == TEXT_PATH_ASR
    assert decision.track is None
    assert decision.reason == "prefer_asr"

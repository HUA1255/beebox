"""Value objects exchanged across the video platform boundary (ticket #42)."""

from __future__ import annotations

from dataclasses import dataclass, field

PLATFORM_BILIBILI = "bilibili"
PLATFORM_YOUTUBE = "youtube"
SUPPORTED_PLATFORMS = (PLATFORM_BILIBILI, PLATFORM_YOUTUBE)

TRACK_KIND_MANUAL = "manual"
TRACK_KIND_AUTO = "auto"
TRACK_KINDS = (TRACK_KIND_MANUAL, TRACK_KIND_AUTO)

TEXT_SOURCE_MANUAL = "platform_manual"
TEXT_SOURCE_AUTO = "platform_auto"
TEXT_SOURCE_ASR = "asr"
# Transitional value while a URL job has not selected its text path yet.
TEXT_SOURCE_PENDING = "platform_pending"

LOGIN_VALID = "valid"
LOGIN_EXPIRED = "expired"
LOGIN_ABSENT = "absent"
LOGIN_NOT_REQUIRED = "not_required"
LOGIN_UNKNOWN = "unknown"


@dataclass(frozen=True)
class VideoRef:
    """One recognized single-video link.

    ``part`` is the Bilibili multi-part index (``?p=N``), defaulting to 1; it
    stays ``None`` for platforms without the concept. ``canonical_url`` points
    at exactly that part so a download never covers the whole list.
    """

    platform: str
    video_id: str
    canonical_url: str
    original_url: str
    part: int | None = None


@dataclass(frozen=True)
class SubtitleTrack:
    """One subtitle track offered by the platform.

    ``url`` is the direct download location used by the yt-dlp implementation;
    ``path`` is the local sample used by the fake platform; ``content`` is the
    inline payload yt-dlp returns for platforms such as Bilibili. ``kind`` is
    ``manual`` or ``auto``; ``translated`` marks machine-translated captions,
    which are never selected. ``platform`` names the platform that offered the
    track, so a download failure is classified without inspecting the URL.
    """

    language: str
    kind: str
    format: str
    url: str | None = None
    path: str | None = None
    content: str | None = None
    translated: bool = False
    name: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    video_id: str | None = None
    platform: str | None = None


@dataclass(frozen=True)
class VideoInfo:
    """Everything a job needs to know about a video before downloading."""

    ref: VideoRef
    title: str
    author: str | None
    duration_ms: int | None
    tracks: tuple[SubtitleTrack, ...]
    login_required: bool = False
    part_title: str | None = None
    # The video's spoken language when the platform reports one; ticket #43
    # uses it to rank the non-Chinese ("original language") tiers.
    original_language: str | None = None


@dataclass(frozen=True)
class SubtitleCue:
    """One subtitle cue; times are integer milliseconds to keep SRT exact.

    ``lines`` keeps the caption's own line breaks (an automatic caption repeats
    the previous cue's last line as its own first line); ``text`` is the same
    content joined for display. Both are populated by the parsers; parsers that
    only produce text leave ``lines`` empty and cleaning falls back to it.
    """

    start_ms: int
    end_ms: int
    text: str
    lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class SubtitleDocument:
    """A downloaded subtitle track in platform-neutral form."""

    language: str
    kind: str
    cues: tuple[SubtitleCue, ...]


@dataclass(frozen=True)
class LoginStatus:
    """Login state surfaced by the platform for the health/management surface."""

    state: str
    detail: str | None = None


__all__ = [
    "LOGIN_ABSENT",
    "LOGIN_EXPIRED",
    "LOGIN_NOT_REQUIRED",
    "LOGIN_UNKNOWN",
    "LOGIN_VALID",
    "PLATFORM_BILIBILI",
    "PLATFORM_YOUTUBE",
    "SUPPORTED_PLATFORMS",
    "TEXT_SOURCE_ASR",
    "TEXT_SOURCE_AUTO",
    "TEXT_SOURCE_MANUAL",
    "TEXT_SOURCE_PENDING",
    "TRACK_KINDS",
    "TRACK_KIND_AUTO",
    "TRACK_KIND_MANUAL",
    "LoginStatus",
    "SubtitleCue",
    "SubtitleDocument",
    "SubtitleTrack",
    "VideoInfo",
    "VideoRef",
]

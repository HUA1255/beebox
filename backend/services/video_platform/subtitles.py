"""Parse platform subtitle files into timestamped cues (ticket #42).

The downloader may hand back SRT, WebVTT, Bilibili subtitle JSON or YouTube
json3 captions. Parsing is pure and offline; ticket #43 adds the cleaning
rules for rolling auto-captions on top of the cues produced here.
"""

from __future__ import annotations

import html
import json
import re

from backend.services.video_platform.models import SubtitleCue

SUPPORTED_SUBTITLE_FORMATS = frozenset({"srt", "vtt", "json", "json3"})

_TIMESTAMP = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})$")
_TAG = re.compile(r"<[^>]*>")
_ASS_TAG = re.compile(r"\{[^}]*\}")
_WHITESPACE = re.compile(r"\s+")


def _parse_timestamp(value: str) -> int | None:
    match = _TIMESTAMP.match(value.strip())
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    milliseconds = int(match.group(4).ljust(3, "0"))
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + milliseconds


def _clean_line(value: str) -> str:
    text = _TAG.sub("", value)
    text = _ASS_TAG.sub("", text)
    text = html.unescape(text)
    return _WHITESPACE.sub(" ", text).strip()


def _cue_from_block(lines: list[str]) -> SubtitleCue | None:
    for index, line in enumerate(lines):
        if "-->" not in line:
            continue
        left, _, right = line.partition("-->")
        start = _parse_timestamp(left)
        end = _parse_timestamp(right.strip().split()[0] if right.strip() else "")
        if start is None or end is None:
            return None
        cleaned = [_clean_line(item) for item in lines[index + 1 :]]
        cleaned = [item for item in cleaned if item]
        if not cleaned:
            return None
        return SubtitleCue(
            start_ms=start,
            end_ms=max(start, end),
            text=" ".join(cleaned),
            lines=tuple(cleaned),
        )
    return None


def parse_srt(content: str) -> list[SubtitleCue]:
    normalized = content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    cues: list[SubtitleCue] = []
    # Only truly empty lines separate cues: YouTube's automatic captions use a
    # whitespace-only first line as the "position" line inside a cue.
    for block in re.split(r"\n{2,}", normalized):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        cue = _cue_from_block(lines)
        if cue is not None:
            cues.append(cue)
    return cues


def parse_vtt(content: str) -> list[SubtitleCue]:
    normalized = content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    cues: list[SubtitleCue] = []
    for block in re.split(r"\n{2,}", normalized):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        first = lines[0].upper()
        if first.startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        cue = _cue_from_block(lines)
        if cue is not None:
            cues.append(cue)
    return cues


def _milliseconds_from_seconds(value) -> int | None:
    try:
        return int(round(float(value) * 1000))
    except (TypeError, ValueError):
        return None


def _parse_bilibili_json(payload: dict) -> list[SubtitleCue]:
    cues: list[SubtitleCue] = []
    for item in payload.get("body") or []:
        if not isinstance(item, dict):
            continue
        start = _milliseconds_from_seconds(item.get("from"))
        end = _milliseconds_from_seconds(item.get("to"))
        text = _clean_line(str(item.get("content") or ""))
        if start is None or end is None or not text:
            continue
        cues.append(
            SubtitleCue(
                start_ms=start,
                end_ms=max(start, end),
                text=text,
                lines=(text,),
            )
        )
    return cues


def _parse_json3(payload: dict) -> list[SubtitleCue]:
    cues: list[SubtitleCue] = []
    for event in payload.get("events") or []:
        if not isinstance(event, dict):
            continue
        try:
            start = int(event.get("tStartMs") or 0)
            duration = int(event.get("dDurationMs") or 0)
        except (TypeError, ValueError):
            continue
        if not duration:
            continue
        # json3 marks line breaks with a "\n" segment; keeping them is what
        # lets the cleaner recognise YouTube's rolling duplicate lines.
        raw = "".join(
            str(seg.get("utf8") or "") for seg in event.get("segs") or [] if isinstance(seg, dict)
        )
        lines = [line for line in (_clean_line(part) for part in raw.split("\n")) if line]
        if not lines:
            continue
        cues.append(
            SubtitleCue(
                start_ms=start,
                end_ms=start + duration,
                text=" ".join(lines),
                lines=tuple(lines),
            )
        )
    return cues


def _parse_json(content: str) -> list[SubtitleCue]:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("body"), list):
        return _parse_bilibili_json(payload)
    if isinstance(payload.get("events"), list):
        return _parse_json3(payload)
    return []


def parse_subtitle(content: str, fmt: str | None = None) -> list[SubtitleCue]:
    """Parse subtitle bytes decoded as text, using ``fmt`` or content sniffing."""
    normalized_format = (fmt or "").strip().lower().lstrip(".")
    stripped = (content or "").lstrip("\ufeff \t\r\n")
    if normalized_format in {"json", "json3"} or stripped.startswith("{"):
        return _parse_json(stripped)
    if normalized_format == "vtt" or stripped.startswith("WEBVTT"):
        return parse_vtt(content)
    return parse_srt(content)


__all__ = [
    "SUPPORTED_SUBTITLE_FORMATS",
    "parse_srt",
    "parse_subtitle",
    "parse_vtt",
]

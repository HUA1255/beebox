"""Transcript export renderers (tickets #10 and #21).

The v1 templates are:

- TXT: one paragraph block per ``paragraph``, first line ``[HH:MM:SS] 说话人 1``
  (just ``[HH:MM:SS]`` when the speaker is unassigned), body on the next line,
  paragraphs separated by one blank line.
- Markdown: ``# <stem>`` followed by one ``## `HH:MM:SS` 说话人 1`` block per
  paragraph with the paragraph body below a blank line.
- SRT: unchanged, one cue per segment, ``说话人 1: text``.

Paragraph bodies join segment texts with the shared
:func:`backend.utils.transcript_text.needs_space_between` rule (Chinese runs
stay glued, a Chinese/Latin boundary keeps one space), so concatenating the
bodies in order reproduces the task ``text``.

``paragraph is None`` marks a legacy or edited-away segment that was never
grouped: every such segment forms its own paragraph instead of being merged
into one large block.

Every v1 export is UTF-8, LF and ends with exactly one trailing newline.
Speaker labels use the shared display rule from :mod:`backend.services.speakers`
(pure digits -> ``说话人 N``, custom names verbatim, ``None`` -> no prefix).
"""

from __future__ import annotations

import json

from backend.models import TranscriptSegment
from backend.services.speakers import display_speaker
from backend.utils.transcript_text import needs_space_between

SUPPORTED_EXPORT_FORMATS = frozenset({"txt", "md", "srt"})
EXPORT_FORMAT_ALIASES = {"markdown": "md"}


def normalize_export_format(fmt: str | None) -> str | None:
    """Return the canonical v1 format (``txt``/``md``/``srt``) or ``None``."""
    value = (fmt or "").strip().lower()
    value = EXPORT_FORMAT_ALIASES.get(value, value)
    return value if value in SUPPORTED_EXPORT_FORMATS else None


def export_stem(filename: str | None) -> str:
    """The ``<stem>`` used by the Markdown title and the download filename."""
    leaf = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    stem = leaf.rsplit(".", 1)[0] if "." in leaf else leaf
    return stem or "transcript"


def _milliseconds(seconds: float) -> int:
    return int(round(seconds * 1000))


def _srt_time(seconds: float) -> str:
    milliseconds_total = _milliseconds(seconds)
    total_seconds, milliseconds = divmod(milliseconds_total, 1000)
    minutes_total, seconds_part = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes_total, 60)
    return f"{hours:02}:{minutes:02}:{seconds_part:02},{milliseconds:03}"


def _clock_time(seconds: float) -> str:
    """``HH:MM:SS`` truncated to whole seconds, consistent with SRT."""
    total_seconds = _milliseconds(seconds) // 1000
    minutes_total, seconds_part = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes_total, 60)
    return f"{hours:02}:{minutes:02}:{seconds_part:02}"


def _vtt_time(seconds: float) -> str:
    return _srt_time(seconds).replace(",", ".")


def _ass_time(seconds: float) -> str:
    milliseconds_total = _milliseconds(seconds)
    total_seconds, milliseconds = divmod(milliseconds_total, 1000)
    minutes_total, seconds_part = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes_total, 60)
    centiseconds = milliseconds // 10
    return f"{hours}:{minutes:02}:{seconds_part:02}.{centiseconds:02}"


def _clean_text(text: str) -> str:
    """Keep the export line endings LF even when edited text carries CRLF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _finish(content: str) -> str:
    """Guarantee exactly one trailing newline."""
    return content.rstrip("\n") + "\n"


def _speaker_prefix(speaker: str | None) -> str:
    name = display_speaker(speaker)
    return f"{name}: " if name else ""


def _segment_text(segment: TranscriptSegment) -> str:
    return f"{_speaker_prefix(segment.speaker)}{_clean_text(segment.text)}"


def paragraph_groups(segments: list[TranscriptSegment]) -> list[list[TranscriptSegment]]:
    """Group ordered segments by ``paragraph``; every ``None`` stands alone."""
    groups: list[list[TranscriptSegment]] = []
    for segment in segments:
        if segment.paragraph is None or not groups or groups[-1][0].paragraph != segment.paragraph:
            groups.append([segment])
        else:
            groups[-1].append(segment)
    return groups


def paragraph_body(segments: list[TranscriptSegment]) -> str:
    """Join one paragraph's segment texts with the shared spacing rule."""
    body = ""
    for segment in segments:
        part = _clean_text(segment.text or "").strip()
        if not part:
            continue
        if body and needs_space_between(body, part):
            body += " "
        body += part
    return body


def _paragraph_header(segment: TranscriptSegment) -> str:
    header = f"[{_clock_time(segment.start)}]"
    name = display_speaker(segment.speaker)
    return f"{header} {name}" if name else header


def render_txt(_title: str, segments: list[TranscriptSegment]) -> str:
    """Render TXT with one block per paragraph."""
    blocks = [
        f"{_paragraph_header(group[0])}\n{paragraph_body(group)}"
        for group in paragraph_groups(segments)
    ]
    return _finish("\n\n".join(blocks))


def render_srt(segments: list[TranscriptSegment]) -> str:
    blocks = []
    for index, segment in enumerate(segments, 1):
        blocks.append(
            f"{index}\n{_srt_time(segment.start)} --> {_srt_time(segment.end)}\n{_segment_text(segment)}"
        )
    return _finish("\n\n".join(blocks))


def render_vtt(segments: list[TranscriptSegment]) -> str:
    blocks = ["WEBVTT"]
    for segment in segments:
        blocks.append(f"{_vtt_time(segment.start)} --> {_vtt_time(segment.end)}\n{_segment_text(segment)}")
    return _finish("\n\n".join(blocks))


def render_ass(segments: list[TranscriptSegment]) -> str:
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1280\nPlayResY: 720\n\n"
        "[V4+ Styles]\n"
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,"
        "Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
        "Alignment,MarginL,MarginR,MarginV,Encoding\n"
        "Style: Default,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,"
        "0,0,1,2,0,2,10,10,30,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = [
        f"Dialogue: 0,{_ass_time(segment.start)},{_ass_time(segment.end)},Default,,0,0,0,,{_segment_text(segment)}"
        for segment in segments
    ]
    return header + "\n".join(lines)


def _markdown_paragraph(group: list[TranscriptSegment]) -> str:
    heading = f"## `{_clock_time(group[0].start)}`"
    name = display_speaker(group[0].speaker)
    if name:
        heading += f" {name}"
    return f"{heading}\n\n{paragraph_body(group)}"


def render_markdown(title: str, segments: list[TranscriptSegment]) -> str:
    """Render Markdown with one section per paragraph."""
    body = "\n\n".join(_markdown_paragraph(group) for group in paragraph_groups(segments))
    return _finish(f"# {title}\n\n{body}" if body else f"# {title}")


def render_json(task: dict, segments: list[TranscriptSegment]) -> str:
    payload = {**task, "segments": [segment.model_dump() for segment in segments]}
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)

from __future__ import annotations

import re
from typing import Iterable

from backend.models import TranscriptSegment


_NO_LEADING_SPACE = set(".,!?;:，。！？；：、)]}）】》」』")
_NO_TRAILING_SPACE = set("([{（【《「『")

# Sentence-final punctuation shared by the sentence segmenter and the
# paragraph grouper (a paragraph cut prefers a boundary that ends a sentence).
SENTENCE_END_CHARS = frozenset("。！？…；.!?;")

# Closing quotes and brackets that may trail sentence-final punctuation
# without changing the fact that the sentence is finished: 。”, 。」, 。" all
# count. Whitespace between the punctuation and a closer is skipped too.
_SENTENCE_TRAILERS = frozenset("”’\"'」』）】》〕〉〗｝］)]}")


def ends_sentence(text: str | None) -> bool:
    """Return whether ``text`` ends with sentence-final punctuation.

    Trailing whitespace and closing quotes/brackets are ignored, so ``。”``
    and ``。」`` still read as a finished sentence. Empty text, or text ending
    on any other character (a word, a comma, a colon, an opening bracket) is
    unfinished.
    """
    value = (text or "").rstrip()
    index = len(value) - 1
    while index >= 0 and (value[index] in _SENTENCE_TRAILERS or value[index].isspace()):
        index -= 1
    return index >= 0 and value[index] in SENTENCE_END_CHARS


def needs_space_between(left: str, right: str) -> bool:
    if not left or not right:
        return False
    previous = left[-1]
    current = right[0]
    if previous.isspace() or current.isspace():
        return False
    if current in _NO_LEADING_SPACE or previous in _NO_TRAILING_SPACE:
        return False
    return (previous.isascii() and previous.isalnum()) or (current.isascii() and current.isalnum())


def transcript_text_from_segments(segments: Iterable[TranscriptSegment]) -> str:
    text = ""
    for segment in segments:
        part = (segment.text or "").strip()
        if not part:
            continue
        if text and needs_space_between(text, part):
            text += " "
        text += part
    return normalize_transcript_text(text)


def normalize_transcript_text(text: str | None) -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    value = re.sub(r"\s+([.,!?;:，。！？；：、])", r"\1", value)
    value = re.sub(r"([.!?;:,])(?=[A-Za-z])", r"\1 ", value)
    return value

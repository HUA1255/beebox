from backend.models import TranscriptSegment
from backend.utils.transcript_text import ends_sentence
from backend.utils.transcript_text import normalize_transcript_text
from backend.utils.transcript_text import transcript_text_from_segments


def test_transcript_text_from_segments_uses_readable_plain_text_spacing() -> None:
    assert (
        transcript_text_from_segments(
            [
                TranscriptSegment(id=1, start=0, end=1, text="本地模型真实"),
                TranscriptSegment(id=2, start=1, end=2, text="转写结果"),
            ]
        )
        == "本地模型真实转写结果"
    )
    assert (
        transcript_text_from_segments(
            [
                TranscriptSegment(id=1, start=0, end=1, text="真实 provider"),
                TranscriptSegment(id=2, start=1, end=2, text="转写结果"),
            ]
        )
        == "真实 provider 转写结果"
    )
    assert (
        transcript_text_from_segments(
            [
                TranscriptSegment(id=1, start=0, end=1, text="hello"),
                TranscriptSegment(id=2, start=1, end=2, text="world"),
                TranscriptSegment(id=3, start=2, end=3, text="."),
            ]
        )
        == "hello world."
    )


def test_normalize_transcript_text_adds_english_sentence_spacing() -> None:
    assert normalize_transcript_text("part of Dota?So Artifacts") == "part of Dota? So Artifacts"


def test_ends_sentence_accepts_every_sentence_final_mark() -> None:
    for text in ("结束。", "结束！", "结束？", "结束…", "结束；", "Done.", "Done!", "Done?", "Done;", "Done…"):
        assert ends_sentence(text), text


def test_ends_sentence_looks_past_trailing_closing_quotes_and_brackets() -> None:
    for text in ("他说：“好。”", "他说：「好。」", "（结束。）", "「结束。」", 'He said "done."', "结束。”", "结束。 」"):
        assert ends_sentence(text), text


def test_ends_sentence_rejects_unfinished_text() -> None:
    for text in ("麻烦举个", "你好，", "你好：", "“你好”", "", "   ", None):
        assert not ends_sentence(text), text

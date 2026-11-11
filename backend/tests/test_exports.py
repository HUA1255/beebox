"""Ticket #10 / #21: export closed loop (TXT / Markdown / SRT).

Seams under test:

- the pure renderers in ``backend.services.exports``: TXT and Markdown group
  segments by the 1-based ``paragraph`` number, SRT keeps one cue per segment;
- the public HTTP export routes, including the ``markdown`` alias and the
  rejection of v1-external formats.

Paragraph bodies are joined with the shared ``needs_space_between`` rule, so
concatenating the rendered bodies in order reproduces the task ``text``
(Chinese runs stay glued, Latin boundaries keep one space).

Rows are seeded directly for the route tests so this suite exercises the
export contract without running a model pipeline; the real pipeline is covered
by the ticket #20/#21 evidence.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.database import init_db
from backend.database import session as db_session
from backend.database.models import (
    TranscriptSegment as DBSegment,
    TranscriptionTask,
)
from backend.models import TranscriptSegment
from backend.services import tasks as task_service
from backend.services.exports import render_markdown, render_srt, render_txt
from backend.utils.transcript_text import (
    needs_space_between,
    normalize_transcript_text,
    transcript_text_from_segments,
)

# Two paragraphs of two Chinese segments each, a single custom-named speaker
# paragraph, an unassigned speaker paragraph, and a mixed Chinese/Latin
# paragraph: enough to pin grouping, display names and the join rule.
SEGMENTS = [
    TranscriptSegment(id=1, start=0.0, end=2.0, text="大家好。", speaker="1", paragraph=1),
    TranscriptSegment(id=2, start=2.0, end=4.5, text="今天我们讲三个问题。", speaker="1", paragraph=1),
    TranscriptSegment(id=3, start=4.5, end=6.8, text="我是张三。", speaker="张三", paragraph=2),
    TranscriptSegment(id=4, start=6.8, end=9.0, text="未分配发言。", speaker=None, paragraph=3),
    TranscriptSegment(id=5, start=9.0, end=11.0, text="Next we switch to English.", speaker="4", paragraph=4),
    TranscriptSegment(id=6, start=11.0, end=13.0, text="And back to 中文。", speaker="4", paragraph=4),
]


def _txt_paragraphs(content: str) -> list[tuple[str, str]]:
    """Return ``(header_line, body)`` per TXT paragraph block."""
    blocks = content.rstrip("\n").split("\n\n")
    paragraphs = []
    for block in blocks:
        header, _, body = block.partition("\n")
        paragraphs.append((header, body))
    return paragraphs


def _markdown_paragraphs(content: str) -> list[tuple[str, str]]:
    """Return ``(heading_text, body)`` per Markdown ``##`` block."""
    parts = re.split(r"(?m)^## ", content)[1:]
    paragraphs = []
    for part in parts:
        heading, _, body = part.partition("\n\n")
        paragraphs.append((heading, body.rstrip("\n")))
    return paragraphs


def _reassemble(bodies: list[str]) -> str:
    """Join paragraph bodies with the shared rule, then normalize like task text."""
    text = ""
    for body in bodies:
        if not body:
            continue
        if text and needs_space_between(text, body):
            text += " "
        text += body
    return normalize_transcript_text(text)


# ---------------------------------------------------------------------------
# Renderer seams
# ---------------------------------------------------------------------------


def test_txt_groups_segments_by_paragraph_and_puts_the_header_on_its_own_line() -> None:
    assert render_txt("ignored", SEGMENTS) == (
        "[00:00:00] 说话人 1\n"
        "大家好。今天我们讲三个问题。\n"
        "\n"
        "[00:00:04] 张三\n"
        "我是张三。\n"
        "\n"
        "[00:00:06]\n"
        "未分配发言。\n"
        "\n"
        "[00:00:09] 说话人 4\n"
        "Next we switch to English. And back to 中文。\n"
    )


def test_markdown_uses_one_block_per_paragraph_with_a_time_anchor_and_speaker() -> None:
    assert render_markdown("0304", SEGMENTS) == (
        "# 0304\n"
        "\n"
        "## `00:00:00` 说话人 1\n"
        "\n"
        "大家好。今天我们讲三个问题。\n"
        "\n"
        "## `00:00:04` 张三\n"
        "\n"
        "我是张三。\n"
        "\n"
        "## `00:00:06`\n"
        "\n"
        "未分配发言。\n"
        "\n"
        "## `00:00:09` 说话人 4\n"
        "\n"
        "Next we switch to English. And back to 中文。\n"
    )


def test_srt_keeps_one_cue_per_segment_with_segment_timestamps() -> None:
    srt = render_srt(SEGMENTS)

    assert srt.count(" --> ") == len(SEGMENTS)
    assert srt == (
        "1\n"
        "00:00:00,000 --> 00:00:02,000\n"
        "说话人 1: 大家好。\n"
        "\n"
        "2\n"
        "00:00:02,000 --> 00:00:04,500\n"
        "说话人 1: 今天我们讲三个问题。\n"
        "\n"
        "3\n"
        "00:00:04,500 --> 00:00:06,800\n"
        "张三: 我是张三。\n"
        "\n"
        "4\n"
        "00:00:06,800 --> 00:00:09,000\n"
        "未分配发言。\n"
        "\n"
        "5\n"
        "00:00:09,000 --> 00:00:11,000\n"
        "说话人 4: Next we switch to English.\n"
        "\n"
        "6\n"
        "00:00:11,000 --> 00:00:13,000\n"
        "说话人 4: And back to 中文。\n"
    )


def test_paragraph_count_equals_the_highest_paragraph_number() -> None:
    highest = max(segment.paragraph for segment in SEGMENTS if segment.paragraph is not None)

    assert len(_txt_paragraphs(render_txt("ignored", SEGMENTS))) == highest
    assert len(_markdown_paragraphs(render_markdown("0304", SEGMENTS))) == highest


def test_paragraph_bodies_reassemble_to_the_task_text() -> None:
    task_text = transcript_text_from_segments(SEGMENTS)
    # Independent literal: Chinese clauses are glued, Chinese/Latin boundaries
    # keep one space (including after the Chinese full stop before "Next").
    assert task_text == (
        "大家好。今天我们讲三个问题。我是张三。未分配发言。 "
        "Next we switch to English. And back to 中文。"
    )

    txt_bodies = [body for _header, body in _txt_paragraphs(render_txt("ignored", SEGMENTS))]
    md_bodies = [body for _heading, body in _markdown_paragraphs(render_markdown("0304", SEGMENTS))]

    assert _reassemble(txt_bodies) == task_text
    assert _reassemble(md_bodies) == task_text


def test_none_paragraph_segments_each_form_their_own_paragraph() -> None:
    legacy = [
        TranscriptSegment(id=1, start=0.0, end=1.0, text="第一句。", speaker="1", paragraph=None),
        TranscriptSegment(id=2, start=1.0, end=2.0, text="第二句。", speaker="1", paragraph=None),
        TranscriptSegment(id=3, start=2.0, end=3.0, text="Third.", speaker="2", paragraph=None),
    ]

    assert render_txt("ignored", legacy) == (
        "[00:00:00] 说话人 1\n"
        "第一句。\n"
        "\n"
        "[00:00:01] 说话人 1\n"
        "第二句。\n"
        "\n"
        "[00:00:02] 说话人 2\n"
        "Third.\n"
    )
    assert [heading for heading, _body in _markdown_paragraphs(render_markdown("legacy", legacy))] == [
        "`00:00:00` 说话人 1",
        "`00:00:01` 说话人 1",
        "`00:00:02` 说话人 2",
    ]


def test_timestamps_cross_hour_boundaries_and_keep_millisecond_precision() -> None:
    boundary = [TranscriptSegment(id=1, start=3599.999, end=3600.5, text="边界", speaker="2", paragraph=1)]

    txt = render_txt("ignored", boundary)
    srt = render_srt(boundary)

    assert txt == "[00:59:59] 说话人 2\n边界\n"
    assert "00:59:59,999 --> 01:00:00,500" in srt


@pytest.mark.parametrize(
    "content",
    [
        render_txt("ignored", SEGMENTS),
        render_markdown("0304", SEGMENTS),
        render_srt(SEGMENTS),
    ],
)
def test_every_export_is_utf8_lf_with_exactly_one_trailing_newline(content: str) -> None:
    # Round-trip through UTF-8 to prove the payload survives the wire encoding.
    assert content.encode("utf-8").decode("utf-8") == content
    assert "\r" not in content
    assert content.endswith("\n")
    assert not content.endswith("\n\n")


def test_renderers_normalize_crlf_inside_edited_text_to_lf() -> None:
    segment = TranscriptSegment(id=1, start=0.0, end=1.0, text="第一行\r\n第二行", speaker="1", paragraph=1)

    assert "\r" not in render_txt("ignored", [segment])
    assert "第一行\n第二行" in render_txt("ignored", [segment])


# ---------------------------------------------------------------------------
# HTTP export seam
# ---------------------------------------------------------------------------


def make_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BEEBOX_MODEL_STORAGE_ROOT", str(tmp_path / "models-root"))
    monkeypatch.delenv("BEEBOX_API_TOKEN", raising=False)
    monkeypatch.setattr(task_service, "start_task_in_background", lambda _task_id: None)
    from backend.app import create_app

    client = TestClient(create_app())
    init_db()
    return client


def _db():
    return db_session.SessionLocal()


def _seed_task(
    db,
    segments: list[tuple[int, int, str, str | None, int | None]],
    *,
    filename: str = "diar-300s.m4a",
    status: str = "completed",
) -> str:
    task = TranscriptionTask(
        id=str(uuid.uuid4()),
        filename=filename,
        source="local",
        audio_path=f"uploads/{filename}",
        status=status,
        progress=100 if status == "completed" else 60,
        options_json="{}",
        duration_ms=300_000,
    )
    db.add(task)
    db.flush()
    models = [
        TranscriptSegment(
            id=idx,
            start=start_ms / 1000,
            end=end_ms / 1000,
            text=text,
            speaker=speaker,
            paragraph=paragraph,
        )
        for idx, (start_ms, end_ms, text, speaker, paragraph) in enumerate(segments, start=1)
    ]
    task.text = transcript_text_from_segments(models)
    for idx, (start_ms, end_ms, text, speaker, paragraph) in enumerate(segments, start=1):
        db.add(
            DBSegment(
                task_id=task.id,
                idx=idx,
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
                speaker=speaker,
                paragraph=paragraph,
            )
        )
    db.commit()
    return task.id


def test_export_routes_serve_paragraph_txt_md_srt_and_the_markdown_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(tmp_path, monkeypatch)
    db = _db()
    try:
        task_id = _seed_task(
            db,
            [
                (0, 3_200, "大家好。", "1", 1),
                (3_200, 6_800, "继续。", "2", 2),
            ],
        )
    finally:
        db.close()

    bodies = {}
    for fmt in ("txt", "md", "markdown", "srt"):
        response = client.get(f"/v1/jobs/{task_id}/exports/{fmt}")
        assert response.status_code == 200, response.text
        if fmt == "srt":
            assert response.headers["content-type"].startswith("application/x-subrip")
        elif fmt == "md" or fmt == "markdown":
            assert response.headers["content-type"].startswith("text/markdown")
        else:
            assert response.headers["content-type"].startswith("text/plain")
        response.content.decode("utf-8")
        bodies[fmt] = response.content.decode("utf-8")

    assert bodies["md"] == bodies["markdown"]
    assert bodies["txt"] == (
        "[00:00:00] 说话人 1\n"
        "大家好。\n"
        "\n"
        "[00:00:03] 说话人 2\n"
        "继续。\n"
    )
    assert bodies["md"] == (
        "# diar-300s\n"
        "\n"
        "## `00:00:00` 说话人 1\n"
        "\n"
        "大家好。\n"
        "\n"
        "## `00:00:03` 说话人 2\n"
        "\n"
        "继续。\n"
    )
    assert bodies["srt"] == (
        "1\n"
        "00:00:00,000 --> 00:00:03,200\n"
        "说话人 1: 大家好。\n"
        "\n"
        "2\n"
        "00:00:03,200 --> 00:00:06,800\n"
        "说话人 2: 继续。\n"
    )


@pytest.mark.parametrize("fmt", ["pdf", "docx", "vtt", "ass", "json"])
def test_unknown_export_formats_return_400_with_export_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fmt: str
) -> None:
    client = make_client(tmp_path, monkeypatch)
    db = _db()
    try:
        task_id = _seed_task(db, [(0, 1_000, "文本", "1", 1)])
    finally:
        db.close()

    response = client.get(f"/v1/jobs/{task_id}/exports/{fmt}")

    assert response.status_code == 400, response.text
    body = response.json()
    assert body["error_code"] == "UNSUPPORTED_FORMAT"
    assert body["stage"] == "export"



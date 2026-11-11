"""Ticket #17: ``paragraph`` is part of the transcript segment contract.

The field is a 1-based paragraph number within one task. ``None`` means the
segment has not been grouped yet, which is also the state of every transcript
written before the paragraph pipeline exists.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from backend.database.migrations import run_migrations
from backend.database.models import Base, SchemaMigration
from backend.database.models import TranscriptSegment as DBSegment
from backend.models import TranscriptSegment
from backend.services.chunk_merge import ChunkTranscript, merge_chunk_transcripts
from support import fake_speech_env, make_wav_bytes

PARAGRAPH_MIGRATION = "20261101_002_segment_paragraph"

# The pre-#17 shape of ``transcript_segments``: no ``paragraph`` column.
_LEGACY_SEGMENTS_DDL = """
CREATE TABLE transcript_segments (
    id INTEGER NOT NULL,
    task_id VARCHAR NOT NULL,
    idx INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    text TEXT NOT NULL,
    speaker VARCHAR,
    confidence FLOAT,
    PRIMARY KEY (id)
)
"""


def _make_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BEEBOX_INLINE_LOCAL_TRANSCRIPTION", "1")
    monkeypatch.delenv("BEEBOX_API_TOKEN", raising=False)
    fake_speech_env(
        monkeypatch,
        segments=[
            {"start": 0.0, "end": 3.0, "text": "第一句。"},
            {"start": 4.0, "end": 14.0, "text": "第二句。"},
            {"start": 14.0, "end": 24.0, "text": "第三句。", "speaker": "S02"},
        ],
        text="第一句。第二句。第三句。",
        duration=24.0,
    )
    from backend.app import create_app

    return TestClient(create_app())


def _wait_for_completed(client: TestClient, task_id: str) -> dict:
    deadline = time.time() + 30
    last_task: dict = {}
    while time.time() < deadline:
        response = client.get(f"/v1/jobs/{task_id}")
        assert response.status_code == 200
        last_task = response.json()
        if last_task["status"] == "succeeded":
            return last_task
        assert last_task["status"] not in {"failed", "cancelled"}, last_task
        time.sleep(0.05)
    raise AssertionError(f"{task_id} never completed; last={last_task}")


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


def test_legacy_database_gains_paragraph_column_without_losing_data(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    # Create every current table except transcript_segments, then install the
    # legacy shape by hand so the migration has real pre-#17 data to upgrade.
    legacy_tables = [
        table for name, table in Base.metadata.tables.items() if name != "transcript_segments"
    ]
    Base.metadata.create_all(engine, tables=legacy_tables)
    with engine.begin() as conn:
        conn.execute(text(_LEGACY_SEGMENTS_DDL))
        conn.execute(
            text(
                "INSERT INTO transcription_tasks "
                "(id, filename, source, audio_path, status, progress, options_json) "
                "VALUES ('legacy-task', 'legacy.wav', 'local', 'uploads/legacy.wav', "
                "'completed', 100, '{}')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO transcript_segments "
                "(id, task_id, idx, start_ms, end_ms, text, speaker, confidence) "
                "VALUES (1, 'legacy-task', 1, 0, 1200, 'legacy text', '1', NULL)"
            )
        )

    factory = sessionmaker(bind=engine)
    run_migrations(engine, factory)
    run_migrations(engine, factory)

    columns = {column["name"] for column in inspect(engine).get_columns("transcript_segments")}
    assert "paragraph" in columns
    with factory() as db:
        row = db.get(DBSegment, 1)
        assert row is not None
        assert row.text == "legacy text"
        assert row.paragraph is None
        assert db.query(SchemaMigration).filter_by(version=PARAGRAPH_MIGRATION).count() == 1
        db.add(
            DBSegment(
                task_id="legacy-task",
                idx=2,
                start_ms=1200,
                end_ms=2400,
                text="grouped text",
                paragraph=1,
            )
        )
        db.commit()
        grouped = db.query(DBSegment).filter_by(task_id="legacy-task", idx=2).one()
        assert grouped.paragraph == 1
    engine.dispose()


def test_fresh_database_has_paragraph_and_repeated_migration_is_a_no_op(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    run_migrations(engine, factory)
    run_migrations(engine, factory)

    columns = {column["name"] for column in inspect(engine).get_columns("transcript_segments")}
    assert "paragraph" in columns
    with factory() as db:
        assert db.query(SchemaMigration).filter_by(version=PARAGRAPH_MIGRATION).count() == 1
    engine.dispose()


# ---------------------------------------------------------------------------
# Pydantic contract
# ---------------------------------------------------------------------------


def test_segment_paragraph_defaults_to_ungrouped_and_starts_at_one() -> None:
    assert TranscriptSegment(id=1, start=0.0, end=1.0, text="text").paragraph is None
    assert TranscriptSegment(id=1, start=0.0, end=1.0, text="text", paragraph=3).paragraph == 3
    for invalid in (0, -1):
        with pytest.raises(ValidationError):
            TranscriptSegment(id=1, start=0.0, end=1.0, text="text", paragraph=invalid)


# ---------------------------------------------------------------------------
# HTTP round-trip
# ---------------------------------------------------------------------------


def test_store_and_read_segments_preserve_paragraph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _make_client(tmp_path, monkeypatch)

    uploaded = client.post(
        "/v1/files",
        files={"file": ("store.wav", make_wav_bytes(24.0), "audio/wav")},
    )
    assert uploaded.status_code == 201, uploaded.text
    created = client.post(
        "/v1/jobs",
        json={
            "source": {"type": "file", "file_id": uploaded.json()["id"]},
            "options": {"model": "whisper-base", "diarization": False},
        },
    )
    assert created.status_code == 201, created.text
    task = _wait_for_completed(client, created.json()["id"])

    result = client.get(f"/v1/jobs/{task['id']}/result").json()
    assert [segment["paragraph"] for segment in result["segments"]] == [1, 1, 2]
    assert all(segment["paragraph"] is not None for segment in result["segments"])
    # The same numbers come back from a fresh result read.
    reread = client.get(f"/v1/jobs/{task['id']}/result").json()
    assert [segment["paragraph"] for segment in reread["segments"]] == [1, 1, 2]


# ---------------------------------------------------------------------------
# Cross-chunk merge
# ---------------------------------------------------------------------------


def test_chunk_merge_leaves_paragraph_assignment_to_the_task_level_grouping() -> None:
    merged = merge_chunk_transcripts(
        [
            ChunkTranscript(
                idx=1,
                start_ms=0,
                end_ms=120_000,
                # A chunk-local segment cannot own a task-level paragraph.
                segments=[TranscriptSegment(id=1, start=0.0, end=1.0, text="hello", paragraph=5)],
                aligned=True,
            )
        ]
    )

    assert [segment.text for segment in merged.segments] == ["hello"]
    assert merged.segments[0].paragraph is None

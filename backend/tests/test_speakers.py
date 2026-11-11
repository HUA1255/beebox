"""Ticket #9: derived speaker list, exposed on ``/v1/jobs/{id}/result``.

Rows are seeded directly so the tests exercise the summary logic without
running a model pipeline; ticket #56 removed the standalone legacy
``GET /tasks/{id}/speakers`` route.
"""

from __future__ import annotations

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
from backend.services import tasks as task_service


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
    segments: list[tuple[int, int, str, str | None]],
    *,
    status: str = "completed",
) -> str:
    task = TranscriptionTask(
        id=str(uuid.uuid4()),
        filename="diar-300s.m4a",
        source="local",
        audio_path="uploads/diar-300s.m4a",
        status=status,
        progress=100 if status == "completed" else 60,
        options_json="{}",
        text="\n".join(text for _start, _end, text, _speaker in segments),
        duration_ms=300_000,
    )
    db.add(task)
    db.flush()
    for idx, (start_ms, end_ms, text, speaker) in enumerate(segments, start=1):
        db.add(
            DBSegment(
                task_id=task.id,
                idx=idx,
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
                speaker=speaker,
            )
        )
    db.commit()
    return task.id


def test_speakers_derives_counts_durations_and_first_appearance_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(tmp_path, monkeypatch)
    db = _db()
    try:
        task_id = _seed_task(
            db,
            [
                (0, 2_000, "你好", "2"),
                (2_000, 5_000, "大家好", "1"),
                (5_000, 6_500, "我是张三", "张三"),
                (6_500, 7_000, "嗯", None),
                (7_000, 11_000, "继续", "2"),
            ],
        )
    finally:
        db.close()

    response = client.get(f"/v1/jobs/{task_id}/result")

    assert response.status_code == 200, response.text
    items = response.json()["speakers"]
    assert [item["speaker"] for item in items] == ["2", "1", "张三", None]
    assert [item["segment_count"] for item in items] == [2, 1, 1, 1]
    assert [item["duration_ms"] for item in items] == [6_000, 3_000, 1_500, 500]
    assert [item["display_name"] for item in items] == ["说话人 2", "说话人 1", "张三", None]


def test_speakers_missing_task_returns_404_and_empty_task_returns_no_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(tmp_path, monkeypatch)
    db = _db()
    try:
        task_id = _seed_task(db, [])
    finally:
        db.close()

    assert client.get("/v1/jobs/does-not-exist/result").status_code == 404
    empty = client.get(f"/v1/jobs/{task_id}/result")
    assert empty.status_code == 200
    assert empty.json()["speakers"] == []

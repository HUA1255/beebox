from __future__ import annotations

import json
import queue
import subprocess
import threading
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from backend.database import session as db_session
from backend.database.models import TranscriptionChunk, TranscriptionTask
from backend.models import TranscriptionResult
from backend.services import media
from backend.services import task_runtime
from backend.services import tasks as task_service
from backend.services.errors import BeeBoxError
from backend.services.ffmpeg_tools import ToolStatus
from support import fake_speech_env


def _init_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    fake_speech_env(monkeypatch)
    db_session.init_db()


def test_concurrent_retry_only_enqueues_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_db(tmp_path, monkeypatch)
    setup_db = db_session.SessionLocal()
    try:
        setup_db.add(
            TranscriptionTask(
                id="retry-race",
                filename="sample.wav",
                source="local",
                audio_path="uploads/sample.wav",
                status="failed",
                progress=100,
                options_json="{}",
            )
        )
        setup_db.commit()
    finally:
        setup_db.close()

    first_at_commit = threading.Event()
    second_at_commit = threading.Event()
    release_first = threading.Event()
    enqueued: list[str] = []
    outcomes: list[str] = []
    outcomes_lock = threading.Lock()

    monkeypatch.setattr(task_service, "start_task_in_background", enqueued.append)

    def run_retry(*, block_commit: bool) -> None:
        db = db_session.SessionLocal()
        original_commit = db.commit
        if block_commit:
            def delayed_commit() -> None:
                first_at_commit.set()
                assert release_first.wait(timeout=5)
                original_commit()

            db.commit = delayed_commit  # type: ignore[method-assign]
        else:
            def observed_commit() -> None:
                second_at_commit.set()
                original_commit()

            db.commit = observed_commit  # type: ignore[method-assign]
        try:
            task_service.retry_task(db, "retry-race")
        except task_service.TaskActiveError:
            result = "conflict"
        else:
            result = "queued"
        finally:
            db.close()
        with outcomes_lock:
            outcomes.append(result)

    first = threading.Thread(target=run_retry, kwargs={"block_commit": True})
    second = threading.Thread(target=run_retry, kwargs={"block_commit": False})
    first.start()
    assert first_at_commit.wait(timeout=5)
    second.start()
    # Without the transition lock the second request reaches commit while the
    # first one is paused. With the lock it waits until the first finishes.
    second_at_commit.wait(timeout=0.25)
    release_first.set()
    second.join(timeout=5)
    first.join(timeout=5)

    assert not first.is_alive() and not second.is_alive()
    assert sorted(outcomes) == ["conflict", "queued"]
    assert enqueued == ["retry-race"]


def test_task_worker_continues_after_run_task_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_db(tmp_path, monkeypatch)
    db = db_session.SessionLocal()
    try:
        for task_id in ("worker-first", "worker-second"):
            db.add(
                TranscriptionTask(
                    id=task_id,
                    filename="sample.wav",
                    source="local",
                    audio_path="uploads/sample.wav",
                    status="queued",
                    progress=0,
                    options_json="{}",
                )
            )
        db.commit()
    finally:
        db.close()

    isolated_queue: queue.Queue[str] = queue.Queue()
    isolated_queue.put("worker-first")
    isolated_queue.put("worker-second")
    monkeypatch.setitem(task_service._task_queues, task_service.LOCAL_QUEUE, isolated_queue)
    seen: list[str] = []

    def fake_run_task(_db, row) -> None:
        seen.append(row.id)
        if row.id == "worker-first":
            raise RuntimeError("escaped normal handler")
        raise SystemExit(0)

    monkeypatch.setattr(task_service, "run_task", fake_run_task)
    worker = threading.Thread(target=task_service._task_worker, args=(task_service.LOCAL_QUEUE,))
    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert seen == ["worker-first", "worker-second"]
    check_db = db_session.SessionLocal()
    try:
        first = check_db.get(TranscriptionTask, "worker-first")
        assert first is not None
        assert first.status == "failed"
        assert first.error_code == "WORKER_RUNTIME_ERROR"
    finally:
        check_db.close()
        task_runtime.mark_finished("worker-second")


def test_task_worker_exit_reclaims_live_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_db(tmp_path, monkeypatch)
    db = db_session.SessionLocal()
    try:
        db.add(
            TranscriptionTask(
                id="worker-exit",
                filename="sample.wav",
                source="local",
                audio_path="uploads/sample.wav",
                status="queued",
                progress=0,
                options_json="{}",
            )
        )
        db.commit()
    finally:
        db.close()

    isolated_queue: queue.Queue[str] = queue.Queue()
    isolated_queue.put("worker-exit")
    monkeypatch.setitem(task_service._task_queues, task_service.LOCAL_QUEUE, isolated_queue)
    monkeypatch.setitem(task_service._worker_counts, task_service.LOCAL_QUEUE, 1)
    if hasattr(task_service, "_worker_targets"):
        monkeypatch.setitem(task_service._worker_targets, task_service.LOCAL_QUEUE, 0)
    monkeypatch.setattr(task_service, "run_task", lambda _db, _row: (_ for _ in ()).throw(SystemExit(0)))

    worker = threading.Thread(target=task_service._task_worker, args=(task_service.LOCAL_QUEUE,))
    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert task_service._worker_counts[task_service.LOCAL_QUEUE] == 0
    task_runtime.mark_finished("worker-exit")


def test_auth_query_is_removed_before_downstream_and_access_logging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_db(tmp_path, monkeypatch)
    monkeypatch.setenv("BEEBOX_API_TOKEN", "query-token")
    from backend.app import create_app

    app = create_app()

    @app.get("/test/query-scope")
    async def query_scope(request: Request):
        return {"query": request.scope["query_string"].decode("utf-8")}

    client = TestClient(app)
    response = client.get("/test/query-scope?keep=1&api_token=query-token&keep=2")

    assert response.status_code == 200
    parsed = parse_qs(response.json()["query"])
    assert parsed == {"keep": ["1", "2"]}
    assert "api_token" not in response.json()["query"]


def test_probe_media_treats_na_and_non_finite_duration_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        media,
        "resolve_tools",
        lambda **_kwargs: {
            "ffprobe": ToolStatus(True, "/tmp/ffprobe", "manual", "test"),
            "ffmpeg": ToolStatus(True, "/tmp/ffmpeg", "manual", "test"),
        },
    )
    durations = iter(["N/A", "NaN", "-1"])

    def fake_run(_command, **kwargs):
        assert kwargs["timeout"] > 0
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "format": {"duration": next(durations)},
                    "streams": [
                        {
                            "codec_type": "audio",
                            "sample_rate": "N/A",
                            "channels": 1,
                        }
                    ],
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    for _ in range(3):
        metadata = media.probe_media(Path("sample.mp4"))
        assert metadata["duration_ms"] is None
        assert metadata["sample_rate"] is None
        assert metadata["has_audio_stream"] is True


def test_media_timeout_is_diagnostic_and_removes_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"video")
    target = tmp_path / "sample.wav"
    monkeypatch.setattr(
        media,
        "resolve_tools",
        lambda **_kwargs: {
            "ffprobe": ToolStatus(True, "/tmp/ffprobe", "manual", "test"),
            "ffmpeg": ToolStatus(True, "/tmp/ffmpeg", "manual", "test"),
        },
    )
    monkeypatch.setattr(
        media,
        "probe_media",
        lambda _path: {"has_audio_stream": True, "duration_ms": 1_000},
    )

    def timeout_run(command, **kwargs):
        assert kwargs["timeout"] >= 60
        Path(command[-1]).write_bytes(b"partial")
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(media.subprocess, "run", timeout_run)
    with pytest.raises(BeeBoxError, match="timed out"):
        media.prepare_media_for_asr(source)
    assert not target.exists()


def test_runtime_probe_runs_in_short_lived_process_and_is_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.services import platform as platform_service

    calls: list[list[str]] = []
    probe = {
        "python_version": "3.test",
        "platform": "test-platform",
        "ffmpeg_available": True,
        "ffprobe_available": True,
        "ffmpeg_path": "/tools/ffmpeg",
        "ffprobe_path": "/tools/ffprobe",
        "ffmpeg_source": "system",
        "ffprobe_source": "system",
        "ffmpeg_version": "test",
        "ffprobe_version": "test",
        "ffmpeg_error": None,
        "ffprobe_error": None,
        "torch_available": True,
        "torch_cuda_available": False,
        "torch_mps_available": True,
        "cuda_device_name": None,
        "cuda_capability": None,
        "ctranslate2_available": True,
        "faster_whisper_available": True,
        "funasr_available": True,
        "torchaudio_available": True,
        "modelscope_available": True,
        "huggingface_hub_available": True,
        "pyannote_available": False,
        "mlx_available": True,
        "mlx_whisper_available": True,
        "mlx_import_error": None,
        "mlx_whisper_import_error": None,
        "qwen3_asr_available": True,
        "transformers_qwen3_asr_available": True,
        "moss_transcribe_diarize_available": True,
        "warnings": [],
    }

    def fake_run(command, **kwargs):
        calls.append(command)
        assert command[-2:] == ["--runtime-check", "all"]
        assert kwargs["timeout"] > 0
        assert kwargs["env"]["BEEBOX_RUNTIME_PROBE_CHILD"] == "1"
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(probe),
            stderr="",
        )

    monkeypatch.setattr(platform_service.subprocess, "run", fake_run)
    platform_service.runtime_probe_snapshot.cache_clear()
    try:
        assert platform_service.detect_runtime()["funasr_available"] is True
        assert platform_service.torchaudio_available() is True
        assert platform_service.runtime_mlx_import_error() is None
        assert len(calls) == 1
    finally:
        platform_service.runtime_probe_snapshot.cache_clear()


def test_detect_runtime_does_not_mutate_cached_probe_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.services import platform as platform_service

    cached = {"warnings": ["original warning"]}
    monkeypatch.setattr(platform_service, "runtime_probe_snapshot", lambda: cached)

    first = platform_service.detect_runtime()
    first["warnings"].append("request-local warning")
    second = platform_service.detect_runtime()

    assert cached["warnings"] == ["original warning"]
    assert second["warnings"] == ["original warning"]


def test_retry_failed_chunks_completes_and_clears_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_db(tmp_path, monkeypatch)
    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    db = db_session.SessionLocal()
    try:
        row = TranscriptionTask(
            id="chunk-recovery",
            filename="long.wav",
            source="local",
            audio_path="uploads/long.wav",
            status="failed",
            progress=80,
            model_name="whisper-base",
            error="chunk failed",
            error_code="CHUNK_FAILED",
            options_json='{"error_code":"CHUNK_FAILED","diarization":false}',
        )
        db.add(row)
        for index in range(2):
            path = chunks_dir / f"chunk-{index}.wav"
            path.write_bytes(b"audio")
            db.add(
                TranscriptionChunk(
                    task_id=row.id,
                    idx=index,
                    audio_path=f"chunks/{path.name}",
                    start_ms=index * 1_000,
                    end_ms=(index + 1) * 1_000,
                    status="failed",
                    error="temporary",
                    error_code="CHUNK_FAILED",
                )
            )
        db.commit()

        monkeypatch.setattr(
            task_service,
            "_transcribe_path_for_row",
            lambda _db, _row, path, **_kwargs: TranscriptionResult(
                text=path.stem,
                segments=[],
            ),
        )
        enqueued: list[str] = []
        monkeypatch.setattr(task_service, "start_task_in_background", lambda task_id: enqueued.append(task_id))
        queued = task_service.retry_failed_chunks(db, row.id)

        assert queued is not None
        assert queued.status == "queued"
        assert enqueued == [row.id]

        task_service.run_task(db, db.get(type(row), row.id))
        recovered = task_service.get_task(db, row.id)
        assert recovered is not None
        assert recovered.status == "completed"
        assert recovered.error is None
        assert recovered.error_code is None
        assert recovered.text == "chunk-0 chunk-1"
        assert [chunk.status for chunk in db.query(TranscriptionChunk).order_by(TranscriptionChunk.idx)] == [
            "completed",
            "completed",
        ]
    finally:
        db.close()

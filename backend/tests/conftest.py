"""Shared test fixtures.

The suite drives the public HTTP seam end to end: a real upload, a queued
task, polling, and the result/export endpoints. Two fixtures make that
possible without model files or a GPU:

* ``wav_bytes`` / ``make_wav`` produce a genuinely decodable 16 kHz mono PCM
  WAV. ``validate_upload_media`` and ``prepare_media_for_asr`` therefore run
  for real instead of being stubbed.
* ``fake_speech`` selects the configurable fake speech engine through
  ``BEEBOX_SPEECH_ENGINE`` / ``BEEBOX_SPEECH_FAKE_RESULT``. Only the model call
  is replaced; chunking, merging, postprocessing, paragraph grouping and
  exports all run the production code on the scripted output.

Nothing here monkeypatches a pipeline function.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from support import DEFAULT_FIXTURE_SECONDS, make_wav_bytes


@pytest.fixture(autouse=True)
def _isolated_speech_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts on the production engine unless it opts into the fake."""
    from backend.services import speech_engine

    monkeypatch.delenv("BEEBOX_SPEECH_ENGINE", raising=False)
    monkeypatch.delenv("BEEBOX_SPEECH_FAKE_RESULT", raising=False)
    speech_engine.reset_speech_engine_state()
    yield
    speech_engine.reset_speech_engine_state()


@pytest.fixture(autouse=True)
def _isolated_gpu_queue() -> None:
    """The GPU queue is process-wide; never let pause state or waiting jobs leak.

    ``reset`` drops waiting jobs but deliberately leaves the running job alone,
    so wait for it to settle before and after each test. Otherwise a slow CI
    runner can carry one test's running job into the next, which then observes
    a phantom current job or a non-zero queue length.
    """
    import time

    from backend.services import gpu_queue

    def wait_for_idle() -> None:
        deadline = time.monotonic() + 30.0
        while gpu_queue.snapshot().current_job_id is not None and time.monotonic() < deadline:
            time.sleep(0.02)

    wait_for_idle()
    gpu_queue.reset()
    yield
    gpu_queue.reset()
    wait_for_idle()


@pytest.fixture
def wav_bytes() -> bytes:
    return make_wav_bytes()


@pytest.fixture
def make_wav(tmp_path: Path):
    def _make(duration_seconds: float = DEFAULT_FIXTURE_SECONDS, name: str = "fixture.wav") -> Path:
        path = tmp_path / name
        path.write_bytes(make_wav_bytes(duration_seconds))
        return path

    return _make


@pytest.fixture
def fake_speech(monkeypatch: pytest.MonkeyPatch):
    """Configure the fake speech engine through environment variables only."""

    def configure(**config: Any) -> None:
        monkeypatch.setenv("BEEBOX_SPEECH_ENGINE", "fake")
        monkeypatch.setenv("BEEBOX_SPEECH_FAKE_RESULT", json.dumps(config, ensure_ascii=False))

    return configure

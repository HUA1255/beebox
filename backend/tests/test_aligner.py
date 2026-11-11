from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from backend.services.errors import BeeBoxError


def _write_aligner_files(model_dir, weight_name: str = "model.safetensors") -> None:
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    (model_dir / "processor_config.json").write_text("{}", encoding="utf-8")
    (model_dir / weight_name).write_bytes(b"weights")


class _FakeInputs(dict):
    def __init__(self, payload: dict, calls: list[tuple[str, dict]]) -> None:
        super().__init__(payload)
        self._calls = calls

    def to(self, *args):
        self._calls.append(("to", {"args": args}))
        return self


class _FakeProcessor:
    def __init__(self, calls: list[tuple[str, dict]], decoded: list[dict] | None = None) -> None:
        self._calls = calls
        self._decoded = decoded

    def prepare_forced_aligner_inputs(self, **kwargs):
        self._calls.append(("prepare", kwargs))
        return _FakeInputs({"input_ids": [[1, 2, 3]]}, self._calls), [["你", "好"]]

    def decode_forced_alignment(self, **kwargs):
        self._calls.append(("decode", kwargs))
        if self._decoded is not None:
            return [self._decoded]
        return [[
            {"text": "你", "start_time": 0.1, "end_time": 0.2},
            {"text": "好", "start_time": 0.2, "end_time": 0.3},
        ]]


class _FakeModel:
    device = "cuda:0"
    dtype = "float16"
    config = SimpleNamespace(timestamp_token_id=99)

    def __init__(self, calls: list[tuple[str, dict]], error: Exception | None = None) -> None:
        self._calls = calls
        self._error = error

    def __call__(self, **kwargs):
        self._calls.append(("infer", kwargs))
        if self._error is not None:
            raise self._error
        return SimpleNamespace(logits="logits")


def _fake_aligner(monkeypatch, tmp_path, *, decoded: list[dict] | None = None, error: Exception | None = None):
    from backend.services import aligner

    _write_aligner_files(tmp_path)
    calls: list[tuple[str, dict]] = []
    processor = _FakeProcessor(calls, decoded)
    model = _FakeModel(calls, error)
    monkeypatch.setattr(aligner, "aligner_model_dir", lambda: tmp_path)
    monkeypatch.setattr(aligner, "_load_aligner", lambda model_dir: (processor, model))
    aligner.unload_aligner()
    return aligner, calls, model


def test_align_transcript_calls_the_forced_aligner_and_returns_items(tmp_path, monkeypatch) -> None:
    aligner, calls, model = _fake_aligner(monkeypatch, tmp_path)
    audio = np.zeros(16000, dtype=np.float32)

    items = aligner.align_transcript(audio, "你好", "Chinese")

    assert items == [
        {"text": "你", "start_time": 0.1, "end_time": 0.2},
        {"text": "好", "start_time": 0.2, "end_time": 0.3},
    ]
    prepare = next(kwargs for name, kwargs in calls if name == "prepare")
    assert prepare["audio"] is audio
    assert prepare["transcript"] == "你好"
    assert prepare["language"] == "Chinese"
    to_call = next(kwargs for name, kwargs in calls if name == "to")
    assert to_call["args"] == (model.device, model.dtype)
    decode = next(kwargs for name, kwargs in calls if name == "decode")
    assert decode["timestamp_token_id"] == 99
    assert decode["word_lists"] == [["你", "好"]]


def test_align_transcript_returns_nothing_for_blank_text(tmp_path, monkeypatch) -> None:
    aligner, calls, _model = _fake_aligner(monkeypatch, tmp_path)

    assert aligner.align_transcript(np.zeros(10, dtype=np.float32), "   ", None) == []
    assert calls == []


def test_align_transcript_fails_when_the_model_directory_is_missing(tmp_path, monkeypatch) -> None:
    from backend.services import aligner

    monkeypatch.setattr(aligner, "aligner_model_dir", lambda: tmp_path / "missing")
    aligner.unload_aligner()

    with pytest.raises(BeeBoxError) as excinfo:
        aligner.align_transcript(np.zeros(100, dtype=np.float32), "你好", "Chinese")

    assert excinfo.value.code == "ALIGNMENT_FAILED"
    assert excinfo.value.stage == "align"
    assert "aligner" in excinfo.value.message.lower()


def test_align_transcript_wraps_inference_errors(tmp_path, monkeypatch) -> None:
    aligner, _calls, _model = _fake_aligner(
        monkeypatch,
        tmp_path,
        error=RuntimeError("CUDA out of memory"),
    )

    with pytest.raises(BeeBoxError) as excinfo:
        aligner.align_transcript(np.zeros(100, dtype=np.float32), "你好", "Chinese")

    assert excinfo.value.code == "ALIGNMENT_FAILED"
    assert excinfo.value.stage == "align"
    assert "CUDA out of memory" in excinfo.value.message
    assert isinstance(excinfo.value.__cause__, RuntimeError)


def test_align_transcript_rejects_token_text_mismatch(tmp_path, monkeypatch) -> None:
    aligner, _calls, _model = _fake_aligner(
        monkeypatch,
        tmp_path,
        decoded=[{"text": "你", "start_time": 0.1, "end_time": 0.2}],
    )

    with pytest.raises(BeeBoxError) as excinfo:
        aligner.align_transcript(np.zeros(100, dtype=np.float32), "你好", "Chinese")

    assert excinfo.value.code == "ALIGNMENT_FAILED"
    assert excinfo.value.stage == "align"


def test_align_transcript_rejects_empty_alignment(tmp_path, monkeypatch) -> None:
    aligner, _calls, _model = _fake_aligner(monkeypatch, tmp_path, decoded=[])

    with pytest.raises(BeeBoxError) as excinfo:
        aligner.align_transcript(np.zeros(100, dtype=np.float32), "你好", "Chinese")

    assert excinfo.value.code == "ALIGNMENT_FAILED"


def test_is_aligner_ready_requires_model_files(tmp_path, monkeypatch) -> None:
    from backend.services import aligner

    model_dir = tmp_path / "qwen3-forced-aligner-0.6b"
    monkeypatch.setattr(aligner, "aligner_model_dir", lambda: model_dir)
    assert aligner.is_aligner_ready() is False

    model_dir.mkdir(parents=True)
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    assert aligner.is_aligner_ready() is False

    _write_aligner_files(model_dir)
    assert aligner.is_aligner_ready() is True


def test_aligner_load_kwargs_force_fp16_cuda_and_sdpa() -> None:
    from backend.services import aligner

    class Availability:
        def __init__(self, available: bool) -> None:
            self._available = available

        def is_available(self) -> bool:
            return self._available

    gpu = SimpleNamespace(cuda=Availability(True), float16="float16", float32="float32")
    assert aligner._load_kwargs(gpu) == {
        "dtype": "float16",
        "device_map": "cuda:0",
        "attn_implementation": "sdpa",
    }

    cpu = SimpleNamespace(cuda=Availability(False), float16="float16", float32="float32")
    kwargs = aligner._load_kwargs(cpu)
    assert kwargs["dtype"] == "float32"
    assert kwargs["attn_implementation"] == "sdpa"
    assert "device_map" not in kwargs

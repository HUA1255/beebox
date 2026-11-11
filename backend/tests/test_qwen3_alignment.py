from __future__ import annotations

from pathlib import Path

import pytest

from backend.backends.registry import ASRModelConfig
from backend.services.errors import BeeBoxError


class _FakeAudio:
    def __init__(self, samples: int) -> None:
        self._samples = samples

    def __len__(self) -> int:
        return self._samples

    def __getitem__(self, key):
        return 0.0


class _FakeInputs(dict):
    def __init__(self) -> None:
        super().__init__({"input_ids": _FakeIds()})

    def to(self, *args, **kwargs):
        return self


class _FakeIds:
    shape = (1, 3)


class _FakeProcessor:
    def __init__(self, transcription: str = "你好", language: str = "Chinese") -> None:
        self._transcription = transcription
        self._language = language

    def apply_transcription_request(self, **kwargs):
        return _FakeInputs()

    def decode(self, ids, return_format=None):
        if return_format == "parsed":
            return [{"transcription": self._transcription, "language": self._language}]
        return [self._transcription]


class _FakeOutput:
    def __getitem__(self, key):
        return self


class _FakeModel:
    device = "cpu"
    dtype = "float32"

    def generate(self, **kwargs):
        return _FakeOutput()


def _model_config() -> ASRModelConfig:
    return ASRModelConfig(
        model_name="test-qwen3",
        display_name="test",
        engine="transformers_speech_lm",
        adapter="qwen3_asr",
        source="test",
        repo_id=None,
        model_size="small",
        size_mb=1,
        supported_devices=[],
    )


def _adapter(monkeypatch, processor=None, model=None):
    from backend.backends import local_asr

    monkeypatch.setattr(
        local_asr.Qwen3SpeechLMAdapter,
        "load_audio_16k",
        staticmethod(lambda path: _FakeAudio(16000)),
    )
    return local_asr.Qwen3SpeechLMAdapter(), processor or _FakeProcessor(), model or _FakeModel()


def test_qwen3_transcribe_returns_aligned_segments(monkeypatch) -> None:
    from backend.backends import local_asr

    adapter, processor, model = _adapter(monkeypatch)
    aligned = [
        {"text": "你", "start_time": 0.1, "end_time": 0.5},
        {"text": "好", "start_time": 0.5, "end_time": 1.0},
    ]
    calls: list[tuple] = []

    def fake_align(audio, text, language):
        calls.append((audio, text, language))
        return aligned

    monkeypatch.setattr(local_asr, "align_transcript", fake_align)

    result = adapter.transcribe(processor, model, "audio.wav", _model_config(), {"language": "zh"})

    assert calls and calls[0][1] == "你好" and calls[0][2] == "Chinese"
    assert result.text == "你好"
    assert result.language == "Chinese"
    assert [(segment.start, segment.end) for segment in result.segments] == [(0.1, 1.0)]
    assert [segment.text for segment in result.segments] == ["你好"]
    assert result.duration == pytest.approx(1.0)
    assert result.raw_result_summary["aligned"] is True
    assert result.raw_result_summary["alignment_items"] == 2
    assert result.words == [
        {"word": "你", "start": 0.1, "end": 0.5},
        {"word": "好", "start": 0.5, "end": 1.0},
    ]
    assert all(segment.end > segment.start for segment in result.segments)


def test_qwen3_transcribe_uses_sentence_splitting_not_the_display_budget(monkeypatch) -> None:
    from backend.backends import local_asr
    from backend.utils.transcript_text import transcript_text_from_segments

    text = "测" * 70 + "。"
    adapter, processor, model = _adapter(monkeypatch, processor=_FakeProcessor(transcription=text))
    aligned = [
        {"text": char, "start_time": index * 0.1, "end_time": (index + 1) * 0.1}
        for index, char in enumerate(char for char in text if char != "。")
    ]
    monkeypatch.setattr(local_asr, "align_transcript", lambda *args: aligned)

    result = adapter.transcribe(
        processor,
        model,
        "audio.wav",
        _model_config(),
        {"language": "zh", "max_chars_per_line": 42, "max_lines": 2},
    )

    assert [len(segment.text) for segment in result.segments] == [60, 11]
    assert transcript_text_from_segments(result.segments) == text


def test_qwen3_transcribe_skips_alignment_for_empty_transcript(monkeypatch) -> None:
    from backend.backends import local_asr

    adapter, processor, model = _adapter(monkeypatch, processor=_FakeProcessor(transcription=""))
    monkeypatch.setattr(
        local_asr,
        "align_transcript",
        lambda *args: pytest.fail("alignment must not run without a transcript"),
    )

    result = adapter.transcribe(processor, model, "audio.wav", _model_config(), {"language": "zh"})

    assert result.text == ""
    assert result.segments == []


def test_qwen3_transcribe_propagates_alignment_failure(monkeypatch) -> None:
    from backend.backends import local_asr

    adapter, processor, model = _adapter(monkeypatch)

    def fake_align(*args):
        raise BeeBoxError("ALIGNMENT_FAILED", "forced aligner exploded", stage="align")

    monkeypatch.setattr(local_asr, "align_transcript", fake_align)

    with pytest.raises(BeeBoxError) as excinfo:
        adapter.transcribe(processor, model, "audio.wav", _model_config(), {"language": "zh"})

    assert excinfo.value.code == "ALIGNMENT_FAILED"
    assert excinfo.value.stage == "align"


def test_qwen3_transcribe_rejects_token_text_mismatch_as_alignment_failure(monkeypatch) -> None:
    from backend.backends import local_asr

    adapter, processor, model = _adapter(monkeypatch)
    monkeypatch.setattr(
        local_asr,
        "align_transcript",
        lambda *args: [{"text": "你", "start_time": 0.0, "end_time": 0.5}],
    )

    with pytest.raises(BeeBoxError) as excinfo:
        adapter.transcribe(processor, model, "audio.wav", _model_config(), {"language": "zh"})

    assert excinfo.value.code == "ALIGNMENT_FAILED"
    assert excinfo.value.stage == "align"


def test_qwen3_load_forces_fp16_cuda_and_sdpa(monkeypatch, tmp_path: Path) -> None:
    import torch

    from backend.backends import local_asr

    captured: dict = {}

    def fake_loader(model_dir, kwargs):
        captured["model_dir"] = model_dir
        captured["kwargs"] = kwargs
        return object(), object()

    monkeypatch.setattr(local_asr, "_load_qwen3_speech_models", fake_loader)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)

    local_asr.Qwen3SpeechLMAdapter().load(tmp_path)

    assert captured["model_dir"] == tmp_path
    assert captured["kwargs"]["dtype"] is torch.float16
    assert captured["kwargs"]["device_map"] == "cuda:0"
    assert captured["kwargs"]["attn_implementation"] == "sdpa"


def test_qwen3_load_uses_float32_on_cpu(monkeypatch, tmp_path: Path) -> None:
    import torch

    from backend.backends import local_asr

    captured: dict = {}

    def fake_loader(model_dir, kwargs):
        captured["kwargs"] = kwargs
        return object(), object()

    monkeypatch.setattr(local_asr, "_load_qwen3_speech_models", fake_loader)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)

    local_asr.Qwen3SpeechLMAdapter().load(tmp_path)

    assert captured["kwargs"]["dtype"] is torch.float32
    assert captured["kwargs"]["attn_implementation"] == "sdpa"
    assert "device_map" not in captured["kwargs"]


def test_qwen3_registry_entry_advertises_timestamps() -> None:
    from backend.backends.registry import get_model_config

    config = get_model_config("qwen3-asr-1.7b")

    assert config is not None
    assert config.supports_timestamps is True
    assert config.supports_word_timestamps is True

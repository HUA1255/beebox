"""Backend engine and process contracts.

The legacy ``/transcriptions``/``/tasks`` surface was removed by ticket #56;
upload, job lifecycle and export behaviour now lives in the ``test_v1_*``
files. What remains here are the model-engine adapters, the process helpers and
the startup recovery path, which are independent of a route prefix.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.models import TranscriptSegment, TranscriptionResult


def create_downloaded_model(tmp_path: Path, model_name: str, weight_name: str = "model.safetensors") -> Path:
    model_dir = tmp_path / "models" / model_name
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "model.json").write_text("{}", encoding="utf-8")
    (model_dir / weight_name).write_bytes(b"weights")
    if model_name.startswith("sensevoice"):
        (model_dir / "config.yaml").write_text("model: sensevoice", encoding="utf-8")
        (model_dir / "model.pt").write_bytes(b"weights")
        (model_dir / "am.mvn").write_bytes(b"mvn")
    else:
        (model_dir / "config.json").write_text("{}", encoding="utf-8")
        (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")
        (model_dir / "preprocessor_config.json").write_text("{}", encoding="utf-8")
    return model_dir


def test_running_tasks_are_marked_interrupted_on_startup(tmp_path: Path) -> None:
    os.environ["BEEBOX_DATA_DIR"] = str(tmp_path)
    from backend.database.models import TranscriptionTask
    from backend.database import session as db_session
    from backend.services.tasks import mark_interrupted_tasks

    db_session.init_db()
    db = db_session.SessionLocal()
    try:
        db.add(
            TranscriptionTask(
                id="stale-task",
                filename="stale.mp4",
                source="local",
                audio_path="uploads/stale.mp4",
                status="transcribing",
                progress=60,
                model_name="whisper-base",
            )
        )
        # A job waiting in the (in-memory) GPU queue is not interrupted work: a
        # restart must leave it queued for the new process (ticket #64).
        db.add(
            TranscriptionTask(
                id="waiting-task",
                filename="waiting.mp4",
                source="local",
                audio_path="uploads/waiting.mp4",
                status="queued",
                progress=0,
            )
        )
        db.commit()

        assert mark_interrupted_tasks(db) == 1

        row = db.query(TranscriptionTask).filter(TranscriptionTask.id == "stale-task").one()
        assert row.status == "interrupted"
        assert row.progress == 60
        assert row.error == "Task was interrupted before the server restarted"
        assert row.error_code == "TASK_INTERRUPTED"
        waiting = db.query(TranscriptionTask).filter(TranscriptionTask.id == "waiting-task").one()
        assert waiting.status == "queued"
        assert waiting.progress == 0
        assert waiting.error is None
        assert waiting.error_code is None
    finally:
        db.close()


def test_local_asr_backend_dispatches_transformers_and_faster_whisper(tmp_path: Path, monkeypatch) -> None:
    from backend.backends.local_asr import transcribe_local
    from backend.backends.registry import get_model_config

    calls: list[tuple[str, str, dict]] = []

    class FakeBackend:
        def transcribe(self, audio_path: str, model_config, options: dict) -> TranscriptionResult:
            calls.append((model_config.engine, audio_path, options))
            return TranscriptionResult(
                text=f"{model_config.engine} result",
                duration=1.0,
                model_name=model_config.model_name,
                segments=[TranscriptSegment(id=1, start=0.0, end=1.0, text="ok")],
            )

        def is_loaded(self, model_name: str) -> bool:
            return False

        def unload(self, model_name: str) -> bool:
            return False

    monkeypatch.setattr("backend.backends.local_asr._backends", {"whisper_transformers": FakeBackend(), "faster_whisper": FakeBackend()})

    whisper = transcribe_local("audio.wav", get_model_config("whisper-base"), {"language": "zh"})
    faster = transcribe_local("audio.wav", get_model_config("faster-whisper-small"), {"word_timestamps": True})

    assert whisper.text == "whisper_transformers result"
    assert faster.text == "faster_whisper result"
    assert calls == [
        ("whisper_transformers", "audio.wav", {"language": "zh"}),
        ("faster_whisper", "audio.wav", {"word_timestamps": True}),
    ]


def test_faster_whisper_backend_maps_segments_and_words(tmp_path: Path, monkeypatch) -> None:
    from backend.backends.local_asr import FasterWhisperBackend
    from backend.backends.registry import get_model_config

    created: list[tuple[str, str, str]] = []

    class Word:
        start = 0.1
        end = 0.3
        word = "你"
        probability = 0.9

    class Segment:
        id = 7
        start = 0.0
        end = 1.2
        text = "你好"
        avg_logprob = -0.2
        words = [Word()]

    class FakeWhisperModel:
        def __init__(self, model_path, device, compute_type):
            created.append((model_path, device, compute_type))

        def transcribe(self, audio_path, **kwargs):
            return [Segment()], type("Info", (), {"language": "zh", "duration": 1.2})()

    monkeypatch.setattr("backend.backends.local_asr.WhisperModel", FakeWhisperModel)
    monkeypatch.setattr("backend.backends.local_asr._faster_whisper_device", lambda: ("cpu", "int8"))

    model_dir = create_downloaded_model(tmp_path, "faster-whisper-small", "model.bin")
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))

    result = FasterWhisperBackend().transcribe(
        "audio.wav",
        get_model_config("faster-whisper-small"),
        {"language": "auto", "vad": True, "word_timestamps": True},
    )

    assert created == [(str(model_dir), "cpu", "int8")]
    assert result.text == "你好"
    assert result.language == "zh"
    assert result.duration == 1.2
    assert result.segments[0].confidence == -0.2
    assert result.words == [{"start": 0.1, "end": 0.3, "word": "你", "probability": 0.9}]


def test_transformers_whisper_backend_decodes_audio_without_torchcodec(monkeypatch) -> None:
    from backend.backends.local_asr import TransformersWhisperBackend
    from backend.backends.registry import get_model_config

    calls: list[tuple[str, object]] = []
    audio_samples = object()

    def fake_load_audio(audio, sampling_rate, backend):
        calls.append(("load_audio", (audio, sampling_rate, backend)))
        return audio_samples

    def fake_pipeline(audio, **kwargs):
        calls.append(("pipeline", (audio, kwargs)))
        return {"text": "你好", "chunks": [{"text": "你好", "timestamp": (0.0, 1.0)}]}

    monkeypatch.setattr("transformers.audio_utils.load_audio", fake_load_audio)
    backend = TransformersWhisperBackend()
    backend._pipelines["whisper-base"] = fake_pipeline

    result = backend.transcribe("audio.wav", get_model_config("whisper-base"), {"language": "zh"})

    assert result.text == "你好"
    assert calls[0] == ("load_audio", ("audio.wav", 16000, "librosa"))
    assert calls[1][0] == "pipeline"
    assert calls[1][1][0] is audio_samples


def test_funasr_backend_cleans_sensevoice_tags_and_maps_segments(monkeypatch) -> None:
    from backend.backends.local_asr import FunASRBackend
    from backend.backends.registry import get_model_config

    created: list[dict] = []
    generated: list[dict] = []

    class FakeAutoModel:
        def __init__(self, **kwargs):
            created.append(kwargs)

        def generate(self, **kwargs):
            generated.append(kwargs)
            return [
                {
                    "text": "<|zh|><|NEUTRAL|>你好 世界",
                    "sentence_info": [
                        {"start": 0, "end": 1200, "text": "<|zh|>你好"},
                        {"start": 1200, "end": 2300, "text": "<|HAPPY|>世界"},
                    ],
                }
            ]

    monkeypatch.setattr("backend.backends.local_asr.AutoModel", FakeAutoModel)

    result = FunASRBackend().transcribe("audio.wav", get_model_config("sensevoice-small"), {"language": "yue"})

    assert created[0]["disable_update"] is True
    assert generated[0]["language"] == "yue"
    assert result.text == "你好 世界"
    assert [segment.text for segment in result.segments] == ["你好", "世界"]
    assert result.segments[1].end == 2.3
    assert result.raw_result_summary["items"] == 1


def test_funasr_backend_defers_frozen_registry_preload_until_first_use(monkeypatch) -> None:
    import builtins
    from backend.backends import local_asr

    calls: list[str] = []

    class FakeAutoModel:
        pass

    def preload() -> None:
        calls.append("preload")
        local_asr.AutoModel = FakeAutoModel

    monkeypatch.setattr(local_asr, "AutoModel", None)
    monkeypatch.setattr(local_asr, "FUNASR_IMPORT_ERROR", None)
    monkeypatch.setattr(local_asr, "_FUNASR_IMPORT_ATTEMPTED", False)
    monkeypatch.setattr(builtins, "_beebox_preload_funasr_submodules", preload, raising=False)

    local_asr._ensure_funasr_auto_model()
    local_asr._ensure_funasr_auto_model()

    assert local_asr.AutoModel is FakeAutoModel
    assert calls == ["preload"]


class _FakeLength:
    def __init__(self, length: int) -> None:
        self._length = length

    def __len__(self) -> int:
        return self._length


def test_qwen3_asr_backend_uses_processor_and_maps_transcription(tmp_path: Path, monkeypatch) -> None:
    from backend.backends.local_asr import TransformersSpeechLMBackend
    from backend.backends.registry import get_model_config

    model_dir = create_downloaded_model(tmp_path, "qwen3-asr-0.6b")
    (model_dir / "processor_config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))

    calls: list[tuple[str, dict]] = []
    audio_samples = _FakeLength(16000)

    def fake_load_audio(audio, sampling_rate, backend):
        calls.append(("load_audio", {"audio": audio, "sampling_rate": sampling_rate, "backend": backend}))
        return audio_samples

    class FakeInputs(dict):
        def __init__(self):
            super().__init__({"input_ids": type("Ids", (), {"shape": (1, 3)})()})

        def to(self, *args):
            calls.append(("to", {"args": args}))
            return self

    class FakeProcessor:
        @classmethod
        def from_pretrained(cls, path):
            calls.append(("processor", {"path": path}))
            return cls()

        def apply_transcription_request(self, **kwargs):
            calls.append(("request", kwargs))
            return FakeInputs()

        def decode(self, generated_ids, return_format=None):
            calls.append(("decode", {"return_format": return_format}))
            if return_format == "parsed":
                return [{"language": "Chinese", "transcription": "你好 Qwen"}]
            return ["你好 Qwen"]

    class FakeModel:
        device = "cpu"
        dtype = "float32"

        @classmethod
        def from_pretrained(cls, path, **kwargs):
            calls.append(("model", {"path": path, **kwargs}))
            return cls()

        def generate(self, **kwargs):
            calls.append(("generate", kwargs))

            class Output:
                def __getitem__(self, _key):
                    return self

            return Output()

    monkeypatch.setattr("transformers.audio_utils.load_audio", fake_load_audio)
    monkeypatch.setattr(
        "backend.backends.local_asr.align_transcript",
        lambda audio, text, language: [
            {"text": char, "start_time": index * 0.1, "end_time": index * 0.1 + 0.1}
            for index, char in enumerate(char for char in text if not char.isspace())
        ],
    )

    backend = TransformersSpeechLMBackend()
    backend._processors["qwen3-asr-0.6b"] = FakeProcessor()
    backend._models["qwen3-asr-0.6b"] = FakeModel()
    result = backend.transcribe(
        "audio.wav",
        get_model_config("qwen3-asr-0.6b"),
        {"language": "zh"},
    )

    assert result.text == "你好 Qwen"
    assert result.language == "Chinese"
    assert result.segments[0].text == "你好 Qwen"
    assert all(segment.start < segment.end for segment in result.segments)
    assert ("load_audio", {"audio": "audio.wav", "sampling_rate": 16000, "backend": "librosa"}) in calls
    request = next(kwargs for name, kwargs in calls if name == "request")
    assert request["audio"] is audio_samples
    assert request["language"] == "zh"


def test_moss_transcribe_diarize_backend_parses_speaker_segments(monkeypatch) -> None:
    from backend.backends.local_asr import TransformersSpeechLMBackend
    from backend.backends.registry import get_model_config

    calls: dict[str, object] = {}

    def fake_build_messages(audio_path):
        calls["audio_path"] = audio_path
        return [{"role": "user", "content": []}]

    def fake_generate(model, processor, messages, **kwargs):
        calls["max_new_tokens"] = kwargs.get("max_new_tokens")
        calls["do_sample"] = kwargs.get("do_sample")
        return {"text": "[0.48][S01]Welcome everyone[1.66][12.26][S02]The pipeline is ready[13.81]"}

    monkeypatch.setattr("moss_transcribe_diarize.inference_utils.build_transcription_messages", fake_build_messages)
    monkeypatch.setattr("moss_transcribe_diarize.inference_utils.generate_transcription", fake_generate)

    backend = TransformersSpeechLMBackend()
    backend._processors["moss-transcribe-diarize"] = object()
    backend._models["moss-transcribe-diarize"] = object()
    result = backend.transcribe("audio.wav", get_model_config("moss-transcribe-diarize"), {})

    assert calls["audio_path"] == "audio.wav"
    assert calls["do_sample"] is False
    # soundfile cannot stat the missing file, so the duration heuristic falls back.
    assert calls["max_new_tokens"] == 8192
    assert [segment.speaker for segment in result.segments] == ["S01", "S02"]
    assert result.segments[0].start == 0.48
    assert result.segments[0].end == 1.66
    assert result.segments[1].text == "The pipeline is ready"
    assert result.raw_result_summary["parsed_segments"] == 2

    backend.transcribe("audio.wav", get_model_config("moss-transcribe-diarize"), {"max_new_tokens": 65536})
    assert calls["max_new_tokens"] == 65536


def test_moss_max_new_tokens_scales_with_audio_duration(monkeypatch) -> None:
    from backend.backends import local_asr

    class FakeInfo:
        def __init__(self, duration: float) -> None:
            self.duration = duration

    monkeypatch.setattr("soundfile.info", lambda path: FakeInfo(120.0))
    assert local_asr._moss_max_new_tokens("clip.wav", {}) == 4096
    monkeypatch.setattr("soundfile.info", lambda path: FakeInfo(600.0))
    assert local_asr._moss_max_new_tokens("clip.wav", {}) == 8000
    monkeypatch.setattr("soundfile.info", lambda path: FakeInfo(5400.0))
    assert local_asr._moss_max_new_tokens("clip.wav", {}) == 65536



def test_mlx_runtime_import_errors_are_actionable(monkeypatch) -> None:
    from backend.services import platform as platform_service

    monkeypatch.setattr(platform_service, "module_available", lambda _name: True)
    monkeypatch.setattr(
        platform_service,
        "module_import_error",
        lambda name: "ImportError: Library not loaded: @rpath/libjaccl.dylib" if name == "mlx.core" else None,
    )

    assert platform_service.mlx_runtime_import_error() == (
        "mlx.core import failed: ImportError: Library not loaded: @rpath/libjaccl.dylib"
    )


def test_mlx_model_compatibility_uses_runtime_import(monkeypatch) -> None:
    from backend.services import models as model_service

    monkeypatch.setattr(model_service.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(model_service.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(model_service, "mlx_runtime_import_error", lambda: "mlx.core import failed: missing libjaccl")
    model = model_service.get_model_config("mlx-whisper-turbo")

    assert model is not None
    assert model_service._model_runtime_error(model) == "mlx.core import failed: missing libjaccl"


def test_server_disables_xet_for_controllable_downloads(tmp_path: Path, monkeypatch) -> None:
    from backend import server

    freeze_support_calls = []
    monkeypatch.delenv("HF_HUB_DISABLE_XET", raising=False)
    monkeypatch.setattr(server.multiprocessing, "freeze_support", lambda: freeze_support_calls.append(True))
    monkeypatch.setattr(server.uvicorn, "run", lambda *args, **kwargs: None)

    server.main(["--port", "17495", "--data-dir", str(tmp_path)])

    assert freeze_support_calls == [True]
    assert os.environ["HF_HUB_DISABLE_XET"] == "1"


def test_pid_alive_detects_live_and_exited_processes() -> None:
    from backend.server import _pid_alive

    assert _pid_alive(os.getpid()) is True
    assert _pid_alive(-1) is False
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    assert _pid_alive(proc.pid) is False

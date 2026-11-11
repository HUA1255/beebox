from __future__ import annotations

import json
import os
import platform
import queue
import shutil
import threading
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend import config
from backend.backends.local_asr import speech_lm_compat_spec
from backend.backends import ASRModelConfig, ModelSourceCandidate, get_all_model_configs, get_model_config
from backend.models import ASRModelStatus
from backend.services.errors import BeeBoxError
from backend.services import model_storage
from backend.services.speech_engine import get_speech_engine
from backend.services.platform import (
    cohere_asr_import_error,
    firered_asr_available,
    funasr_available,
    granite_speech_import_error,
    granite_speech_plus_import_error,
    moss_transcribe_diarize_available,
    nemo_available,
    qwen3_asr_available,
    runtime_mlx_import_error as mlx_runtime_import_error,
    torchaudio_available,
    voxtral_import_error,
)
from backend.utils.hf_progress import track_hf_download
from backend.utils.progress import get_progress_manager

_active_downloads: set[str] = set()
_cancelled_downloads: set[str] = set()
_download_errors: dict[str, str] = {}
_download_error_codes: dict[str, str] = {}
_state_lock = threading.Lock()
_download_queue: queue.Queue[ASRModelConfig] = queue.Queue()
_download_worker_started = False
DOWNLOAD_PROGRESS_POLL_SECONDS = 0.5
HF_DUPLICATE_WEIGHT_IGNORE_PATTERNS = ["pytorch_model*.bin", "*.fp32.*", "*.fp32-*"]
# A local model archive is a first-class download source: offline deployments
# and the test suite point this at a directory shaped like <root>/<model-name>/.
LOCAL_MODEL_SOURCE_ENV = "BEEBOX_MODEL_SOURCE_DIR"
LOCAL_SOURCE_NAME = "local"
LOCAL_COPY_CHUNK_BYTES = 1024 * 1024
# Test seam: a local copy is usually instant, so the console smoke test cannot
# observe progress or cancel it. A per-chunk delay makes the copy behave like a
# network download. Defaults to 0 (no delay); real sources never set it.
LOCAL_COPY_DELAY_ENV = "BEEBOX_MODEL_SOURCE_COPY_DELAY_MS"


class DownloadStopped(RuntimeError):
    pass


def _local_copy_delay_seconds() -> float:
    raw = os.environ.get(LOCAL_COPY_DELAY_ENV, "").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw)) / 1000.0
    except ValueError:
        return 0.0


def _download_checkpoint(model_name: str) -> None:
    with _state_lock:
        if model_name in _cancelled_downloads:
            raise DownloadStopped("Download stopped")

WEIGHT_EXTENSIONS = (
    ".safetensors",
    ".bin",
    ".pt",
    ".pth",
    ".npz",
    ".model",
    ".onnx",
    ".mvn",
)


def _model_dir(model_name: str) -> Path:
    return config.get_models_dir() / model_name


def _has_incomplete_files(model_dir: Path) -> bool:
    return model_dir.exists() and any(
        ".cache" not in path.relative_to(model_dir).parts
        for path in model_dir.rglob("*.incomplete")
    )


def _has_weight_files(model_dir: Path) -> bool:
    if not model_dir.exists():
        return False
    return any(path.is_file() and path.suffix.lower() in WEIGHT_EXTENSIONS for path in model_dir.rglob("*"))


def _has_any(model_dir: Path, names: tuple[str, ...]) -> bool:
    return any((model_dir / name).exists() for name in names) or any(path.name in names for path in model_dir.rglob("*"))


def _has_glob(model_dir: Path, patterns: tuple[str, ...]) -> bool:
    return any(any(model_dir.rglob(pattern)) for pattern in patterns)


def _directory_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return total
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _integrity_missing_files(model_config: ASRModelConfig, model_dir: Path) -> list[str]:
    """File-level verification only: every required artefact is on disk.

    Runtime imports are deliberately excluded so the catalog can be listed
    without loading torch or probing optional engines. The returned labels
    match :func:`check_model_compatibility` where the rules are the same.
    """
    missing: list[str] = []
    if not (model_dir / "model.json").is_file():
        missing.append("model.json")
    if _has_incomplete_files(model_dir):
        missing.append("incomplete files")
    engine = model_config.engine
    if engine == "whisper_transformers":
        if not _has_any(model_dir, ("config.json",)):
            missing.append("config.json")
        if not _has_any(model_dir, ("tokenizer.json", "vocab.json", "merges.txt")):
            missing.append("tokenizer")
        if not _has_any(model_dir, ("preprocessor_config.json", "feature_extractor_config.json")):
            missing.append("preprocessor")
        if not _has_weight_files(model_dir):
            missing.append("weights")
    elif engine == "faster_whisper":
        if not _has_any(model_dir, ("model.bin",)) and not _has_glob(model_dir, ("*.safetensors", "*.bin")):
            missing.append("model.bin")
        if not _has_any(model_dir, ("tokenizer.json", "vocabulary.json")):
            missing.append("tokenizer")
        if not _has_any(model_dir, ("config.json",)):
            missing.append("config.json")
    elif engine == "funasr":
        for required in model_config.required_files or ("config.yaml", "model.pt", "am.mvn"):
            if not _has_any(model_dir, (required,)):
                missing.append(required)
    elif engine == "mlx_whisper":
        if not _has_weight_files(model_dir):
            missing.append("mlx weights")
        if not _has_any(model_dir, ("config.json", "tokenizer.json")):
            missing.append("config/tokenizer")
    elif engine == "firered_asr":
        for required in model_config.required_files or ("model.pth.tar",):
            if not _has_any(model_dir, (required,)):
                missing.append(required)
    elif engine == "nemo":
        for required in model_config.required_files or ("*.nemo",):
            if not _has_glob(model_dir, (required,)):
                missing.append(required)
    elif engine == "transformers_speech_lm":
        spec = speech_lm_compat_spec(model_config.adapter)
        if spec is None:
            missing.append(f"unknown speech-LM adapter: {model_config.adapter}")
        else:
            if not _has_any(model_dir, ("config.json",)):
                missing.append("config.json")
            if not _has_any(model_dir, tuple(spec["tokenizer_files"])):
                missing.append("tokenizer")
            if not _has_any(
                model_dir,
                ("processor_config.json", "preprocessor_config.json", "chat_template.json", "chat_template.jinja"),
            ):
                missing.append("processor/chat_template")
            if spec["requires_remote_code_files"] and not _has_glob(model_dir, ("*.py",)):
                missing.append("remote code")
            if not _has_weight_files(model_dir):
                missing.append("weights")
    else:
        missing.append(f"unsupported engine: {engine}")
    return missing


def _utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def local_model_source_dir() -> Path | None:
    """The configured offline model archive, or ``None`` when unset."""
    raw = os.environ.get(LOCAL_MODEL_SOURCE_ENV, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().absolute()


def _local_source_candidate(model_config: ASRModelConfig) -> ModelSourceCandidate | None:
    root = local_model_source_dir()
    if root is None:
        return None
    source_dir = root / model_config.model_name
    if not source_dir.is_dir():
        return None
    return ModelSourceCandidate(LOCAL_SOURCE_NAME, str(source_dir), priority=-100, verified=True)


def _source_candidates(model_config: ASRModelConfig) -> list[ModelSourceCandidate]:
    local = _local_source_candidate(model_config)
    if local is not None:
        # An archive entry is authoritative: a broken local copy must surface as
        # a download error instead of silently reaching for the network.
        return [local]
    candidates = model_config.source_candidates or []
    if not candidates and model_config.source and model_config.repo_id:
        candidates = [ModelSourceCandidate(model_config.source, model_config.repo_id, priority=100, verified=True)]
    return sorted(candidates, key=lambda item: item.priority)


def _candidate_dicts(model_config: ASRModelConfig) -> list[dict[str, Any]]:
    return [asdict(candidate) for candidate in _source_candidates(model_config)]


def _preferred_source(model_config: ASRModelConfig) -> str | None:
    candidates = _source_candidates(model_config)
    return candidates[0].source if candidates else model_config.source


def _read_model_marker(model_name: str) -> dict[str, Any]:
    marker_path = _model_dir(model_name) / "model.json"
    if not marker_path.exists():
        return {}
    try:
        parsed = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _marker_size_bytes(marker: dict[str, Any]) -> int:
    try:
        return max(0, round(float(marker.get("size_on_disk_mb") or 0) * 1024 * 1024))
    except (TypeError, ValueError):
        return 0


def _shallow_directory_size(path: Path) -> int:
    """Best-effort fallback for legacy or partial installs without marker size."""

    total = 0
    try:
        for item in path.iterdir():
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
    except OSError:
        return total
    return total


def _catalog_snapshot(model_name: str) -> tuple[bool, dict[str, Any], int]:
    """Return the last verified install snapshot without walking model files.

    The marker is written only after a completed download. Full recursive
    validation remains available through ``verify_models`` and before task
    execution.
    """

    model_dir = _model_dir(model_name)
    marker_path = model_dir / "model.json"
    marker = _read_model_marker(model_name)
    downloaded = marker_path.is_file() and model_dir.is_dir()
    size_bytes = _marker_size_bytes(marker)
    if size_bytes == 0:
        size_bytes = _shallow_directory_size(model_dir)
    return downloaded, marker, size_bytes


def _cache_info(model_config: ASRModelConfig) -> dict[str, Any]:
    candidates = _source_candidates(model_config)
    if candidates:
        for candidate in candidates:
            candidate_config = replace(model_config, source=candidate.source, repo_id=candidate.repo_id)
            cache = _cache_info_for_source(candidate_config)
            if cache["detected"]:
                return cache
        return {"detected": False, "size_mb": None, "path": None}
    return _cache_info_for_source(model_config)


def _cache_info_for_source(model_config: ASRModelConfig) -> dict[str, Any]:
    if not model_config.repo_id:
        return {"detected": False, "size_mb": None, "path": None}
    if model_config.source == "modelscope":
        return _modelscope_cache_info(model_config)
    return _huggingface_cache_info(model_config)


def _huggingface_cache_info(model_config: ASRModelConfig) -> dict[str, Any]:
    try:
        from huggingface_hub import scan_cache_dir

        cache_info = scan_cache_dir(config.get_model_cache_dir("huggingface") / "hub")
    except Exception:
        return {"detected": False, "size_mb": None, "path": None}
    for repo in cache_info.repos:
        if repo.repo_id != model_config.repo_id:
            continue
        has_weight = False
        has_incomplete = False
        for revision in repo.revisions:
            for file_info in revision.files:
                name = file_info.file_name.lower()
                if name.endswith(".incomplete"):
                    has_incomplete = True
                if name.endswith(WEIGHT_EXTENSIONS):
                    has_weight = True
        if has_weight and not has_incomplete:
            try:
                size_mb = sum(revision.size_on_disk for revision in repo.revisions) / (1024 * 1024)
            except Exception:
                size_mb = None
            path = None
            try:
                path = str(next(iter(repo.revisions)).snapshot_path)
            except Exception:
                pass
            return {"detected": True, "size_mb": size_mb, "path": path}
    return {"detected": False, "size_mb": None, "path": None}


def _modelscope_cache_info(model_config: ASRModelConfig) -> dict[str, Any]:
    candidates: list[Path] = []
    repo_id = model_config.repo_id or ""
    safe_parts = repo_id.split("/")
    env_cache = Path(str(Path.home() / ".cache" / "modelscope"))
    if "MODELSCOPE_CACHE" in os.environ:
        env_cache = Path(os.environ["MODELSCOPE_CACHE"]).expanduser()
    for root in (env_cache, Path.home() / ".cache" / "modelscope" / "hub"):
        if len(safe_parts) == 2:
            candidates.append(root / safe_parts[0] / safe_parts[1])
            candidates.append(root / safe_parts[1])
        candidates.append(root / repo_id.replace("/", "___"))
    for path in candidates:
        if path.exists() and _has_weight_files(path) and not _has_incomplete_files(path):
            return {"detected": True, "size_mb": _directory_size(path) / (1024 * 1024), "path": str(path)}
    return {"detected": False, "size_mb": None, "path": None}


def _start_directory_progress_tracker(model_config: ASRModelConfig, model_dir: Path, stop_event: threading.Event) -> threading.Thread:
    progress = get_progress_manager()
    estimated_total = max(model_config.size_mb * 1024 * 1024, 1)

    def poll() -> None:
        while not stop_event.wait(DOWNLOAD_PROGRESS_POLL_SECONDS):
            with _state_lock:
                if model_config.model_name in _cancelled_downloads:
                    return
            current = _directory_size(model_dir)
            if current <= 0:
                continue
            progress.update_progress(
                model_config.model_name,
                min(current, estimated_total - 1),
                estimated_total,
                filename="Downloading files...",
                status="downloading",
            )

    thread = threading.Thread(target=poll, daemon=True)
    thread.start()
    return thread


def is_model_downloaded(model_name: str) -> bool:
    if _has_incomplete_files(_model_dir(model_name)):
        return False
    if _is_local_model_downloaded(model_name):
        return True
    with _state_lock:
        if model_name in _download_errors:
            return False
    return False


def _is_local_model_downloaded(model_name: str) -> bool:
    model_dir = _model_dir(model_name)
    return (model_dir / "model.json").exists() and _has_weight_files(model_dir) and not _has_incomplete_files(model_dir)


def is_model_directory_valid(model_name: str, model_dir: Path) -> bool:
    return get_model_config(model_name) is not None and (model_dir / "model.json").exists() and _has_weight_files(model_dir) and not _has_incomplete_files(model_dir)


_BLOCKING_RUNTIME_PROBE = object()


def _runtime_capability(
    runtime_snapshot: dict[str, Any] | None | object,
    key: str,
    blocking_probe,
) -> bool | None:
    if runtime_snapshot is _BLOCKING_RUNTIME_PROBE:
        return bool(blocking_probe())
    if runtime_snapshot is None:
        return None
    return bool(runtime_snapshot.get(key))


def _probe_fallback(probe_key: str):
    if probe_key == "qwen3_asr_available":
        return qwen3_asr_available
    if probe_key == "moss_transcribe_diarize_available":
        return moss_transcribe_diarize_available
    import_error_by_key = {
        "granite_speech_available": granite_speech_import_error,
        "granite_speech_plus_available": granite_speech_plus_import_error,
        "cohere_asr_available": cohere_asr_import_error,
        "voxtral_available": voxtral_import_error,
    }
    import_error = import_error_by_key.get(probe_key)
    if import_error is not None:
        return lambda: import_error() is None
    return None


def check_model_compatibility(
    model_name: str,
    *,
    runtime_snapshot: dict[str, Any] | None | object = _BLOCKING_RUNTIME_PROBE,
) -> dict[str, Any]:
    model_config = get_model_config(model_name)
    model_dir = _model_dir(model_name)
    downloaded = is_model_downloaded(model_name)
    if model_config is None:
        return {"model_name": model_name, "downloaded": False, "compatible": False, "missing": ["model registry entry"], "message": f"Unknown model: {model_name}", "code": "unknown_model"}
    runtime_error = _model_runtime_error(model_config, runtime_snapshot=runtime_snapshot)
    if runtime_error:
        return {"model_name": model_name, "downloaded": downloaded, "compatible": False, "missing": ["compatible runtime"], "message": runtime_error, "code": "runtime_incompatible"}
    if not downloaded:
        return {"model_name": model_name, "downloaded": False, "compatible": False, "missing": ["model.json", "weights"], "message": f"Model {model_name} is not downloaded", "code": "model_not_downloaded"}

    missing: list[str] = []
    runtime_pending = False
    engine = model_config.engine
    if engine == "whisper_transformers":
        if not _has_any(model_dir, ("config.json",)):
            missing.append("config.json")
        if not _has_any(model_dir, ("tokenizer.json", "vocab.json", "merges.txt")):
            missing.append("tokenizer")
        if not _has_any(model_dir, ("preprocessor_config.json", "feature_extractor_config.json")):
            missing.append("preprocessor")
        if not _has_weight_files(model_dir):
            missing.append("weights")
    elif engine == "faster_whisper":
        if not _has_any(model_dir, ("model.bin",)) and not _has_glob(model_dir, ("*.safetensors", "*.bin")):
            missing.append("model.bin")
        if not _has_any(model_dir, ("tokenizer.json", "vocabulary.json")):
            missing.append("tokenizer")
        if not _has_any(model_dir, ("config.json",)):
            missing.append("config.json")
    elif engine == "funasr":
        for required in model_config.required_files or ("config.yaml", "model.pt", "am.mvn"):
            if not _has_any(model_dir, (required,)):
                missing.append(required)
        torchaudio_ready = _runtime_capability(runtime_snapshot, "torchaudio_available", torchaudio_available)
        funasr_ready = _runtime_capability(runtime_snapshot, "funasr_available", funasr_available)
        runtime_pending = torchaudio_ready is None or funasr_ready is None
        if torchaudio_ready is False:
            missing.append("torchaudio runtime")
        if funasr_ready is False:
            missing.append("funasr runtime")
    elif engine == "mlx_whisper":
        if not _has_weight_files(model_dir):
            missing.append("mlx weights")
        if not _has_any(model_dir, ("config.json", "tokenizer.json")):
            missing.append("config/tokenizer")
    elif engine == "firered_asr":
        for required in model_config.required_files or ("model.pth.tar",):
            if not _has_any(model_dir, (required,)):
                missing.append(required)
        probe_ready = _runtime_capability(runtime_snapshot, "firered_asr_available", firered_asr_available)
        runtime_pending = probe_ready is None
        if probe_ready is False:
            missing.append("firered runtime")
    elif engine == "nemo":
        for required in model_config.required_files or ("*.nemo",):
            if not _has_glob(model_dir, (required,)):
                missing.append(required)
        probe_ready = _runtime_capability(runtime_snapshot, "nemo_available", nemo_available)
        runtime_pending = probe_ready is None
        if probe_ready is False:
            missing.append("nemo runtime (optional NeMo engine not installed)")

        def _cuda_probe() -> bool:
            try:
                import torch

                return bool(torch.cuda.is_available())
            except Exception:
                return False

        cuda_ready = _runtime_capability(runtime_snapshot, "torch_cuda_available", _cuda_probe)
        runtime_pending = runtime_pending or cuda_ready is None
        if cuda_ready is False:
            missing.append("CUDA device")
    elif engine == "transformers_speech_lm":
        spec = speech_lm_compat_spec(model_config.adapter)
        if spec is None:
            missing.append(f"unknown speech-LM adapter: {model_config.adapter}")
        else:
            if not _has_any(model_dir, ("config.json",)):
                missing.append("config.json")
            if not _has_any(model_dir, tuple(spec["tokenizer_files"])):
                missing.append("tokenizer")
            if not _has_any(model_dir, ("processor_config.json", "preprocessor_config.json", "chat_template.json", "chat_template.jinja")):
                missing.append("processor/chat_template")
            if spec["requires_remote_code_files"] and not _has_glob(model_dir, ("*.py",)):
                missing.append("remote code")
            if not _has_weight_files(model_dir):
                missing.append("weights")
            probe_key = spec["runtime_probe_key"]
            if probe_key:
                probe_ready = _runtime_capability(runtime_snapshot, probe_key, _probe_fallback(probe_key))
                runtime_pending = probe_ready is None
                if probe_ready is False:
                    missing.append(spec["runtime_missing_label"])
    else:
        missing.append(f"unsupported engine: {engine}")

    compatible = None if runtime_pending and not missing else not missing
    return {
        "model_name": model_name,
        "downloaded": downloaded,
        "compatible": compatible,
        "missing": missing,
        "message": "Runtime compatibility check pending" if compatible is None else "Compatible" if compatible else f"Missing required files: {', '.join(missing)}",
        "code": None if compatible is not False else "missing_files",
    }


def _model_runtime_error(
    model_config: ASRModelConfig,
    *,
    runtime_snapshot: dict[str, Any] | None | object = _BLOCKING_RUNTIME_PROBE,
) -> str | None:
    if model_config.engine != "mlx_whisper":
        return None
    if platform.system() != "Darwin" or platform.machine().lower() not in {"arm64", "aarch64"}:
        return "MLX Whisper requires the macOS Apple Silicon desktop runtime and is unavailable in Linux containers"
    if runtime_snapshot is _BLOCKING_RUNTIME_PROBE:
        return mlx_runtime_import_error()
    if runtime_snapshot is None:
        return None
    core_error = runtime_snapshot.get("mlx_import_error")
    whisper_error = runtime_snapshot.get("mlx_whisper_import_error")
    if core_error:
        return f"mlx.core import failed: {core_error}"
    if whisper_error:
        return f"mlx_whisper import failed: {whisper_error}"
    return None


def _catalog_runtime_compatibility(
    model_config: ASRModelConfig,
    *,
    downloaded: bool,
    runtime_snapshot: dict[str, Any] | None,
) -> tuple[bool | None, str | None]:
    runtime_error = _model_runtime_error(model_config, runtime_snapshot=runtime_snapshot)
    if runtime_error:
        return False, runtime_error
    if not downloaded:
        return None, None
    engine = model_config.engine
    if engine in {"whisper_transformers", "faster_whisper"}:
        return True, None
    if runtime_snapshot is None:
        return None, None
    missing: list[str] = []
    if engine == "funasr":
        if not runtime_snapshot.get("torchaudio_available"):
            missing.append("torchaudio runtime")
        if not runtime_snapshot.get("funasr_available"):
            missing.append("funasr runtime")
    elif engine == "transformers_speech_lm":
        spec = speech_lm_compat_spec(model_config.adapter)
        probe_key = spec["runtime_probe_key"] if spec else None
        if probe_key and not runtime_snapshot.get(probe_key):
            missing.append(spec["runtime_missing_label"])
    if missing:
        return False, f"Missing required files: {', '.join(missing)}"
    return True, None


def list_model_statuses() -> list[ASRModelStatus]:
    statuses: list[ASRModelStatus] = []
    storage = model_storage.inspect_storage(include_usage=False)
    storage_available = bool(storage["available"])
    # Catalog rendering must stay independent from heavyweight runtime imports.
    # Exact compatibility is checked by explicit diagnostics and before use.
    runtime_snapshot = None
    progress = get_progress_manager()
    engine = get_speech_engine()
    for item in get_all_model_configs():
        progress_state = progress.get_progress(item.model_name)
        downloaded, marker, size_on_disk_bytes = _catalog_snapshot(item.model_name) if storage_available else (None, {}, 0)
        error = None if downloaded else _download_errors.get(item.model_name)
        if progress_state and progress_state.get("status") == "error":
            error = None if downloaded else progress_state.get("error") or error
        size_on_disk_mb = round(size_on_disk_bytes / (1024 * 1024), 2)
        compatible, compatibility_error = _catalog_runtime_compatibility(
            item,
            downloaded=bool(downloaded),
            runtime_snapshot=runtime_snapshot,
        ) if storage_available else (None, "模型存储位置不可用")
        compatibility_error_code = None
        if compatible is False and compatibility_error:
            compatibility_error_code = "runtime_incompatible"
        elif downloaded is False:
            compatibility_error = f"Model {item.model_name} is not downloaded"
            compatibility_error_code = "model_not_downloaded"
        statuses.append(
            ASRModelStatus(
                model_name=item.model_name,
                display_name=item.display_name,
                engine=item.engine,
                source=item.source,
                repo_id=item.repo_id,
                model_size=item.model_size,
                size_mb=item.size_mb,
                languages=item.languages,
                runtime=item.runtime,
                supported_devices=item.supported_devices,
                supports_timestamps=item.supports_timestamps,
                supports_word_timestamps=item.supports_word_timestamps,
                supports_diarization=item.supports_diarization,
                supports_streaming=item.supports_streaming,
                downloaded=downloaded,
                downloading=item.model_name in _active_downloads,
                loaded=engine.is_model_loaded(item.model_name),
                error=error,
                size_on_disk_mb=size_on_disk_mb,
                download_error=error,
                download_error_code=None if downloaded else _download_error_codes.get(item.model_name),
                license=item.license or None,
                attribution=item.attribution,
                compatible=compatible,
                compatibility_error=compatibility_error,
                compatibility_error_code=compatibility_error_code,
                cache_detected=False,
                cache_size_mb=None,
                cache_path=None,
                preferred_source=_preferred_source(item),
                source_candidates=_candidate_dicts(item),
                installed_source=marker.get("source"),
                installed_repo_id=marker.get("repo_id"),
                last_verified_at=marker.get("last_verified_at") or marker.get("installed_at"),
                storage_status=storage["status"],
                storage_error="模型存储位置不可用" if not storage_available else None,
            )
        )
    return statuses


def inspect_model(model_name: str) -> dict[str, Any]:
    """File-level truth for one model: install marker, integrity, disk usage.

    ``downloaded`` records that a completed install was recorded; ``complete``
    additionally verifies every artefact the registered engine needs. Both are
    filesystem facts, so this stays cheap enough for the catalog.
    """
    model_config = get_model_config(model_name)
    if model_config is None:
        raise ValueError(f"Unknown model: {model_name}")
    model_dir = _model_dir(model_name)
    missing = _integrity_missing_files(model_config, model_dir)
    downloaded = model_dir.is_dir() and (model_dir / "model.json").is_file()
    return {
        "model_name": model_name,
        "downloaded": downloaded,
        "complete": downloaded and not missing,
        "missing_files": missing,
        "size_bytes": model_storage.directory_size(model_dir),
    }


def download_state(model_name: str) -> dict[str, Any]:
    """Project the in-process download worker onto the public state machine.

    ``idle``, ``downloading``, ``cancelling``, ``succeeded``, ``failed`` and
    ``cancelled`` are the only states a caller has to understand.
    """
    with _state_lock:
        active = model_name in _active_downloads
        remembered_error = _download_errors.get(model_name)
        error_code = _download_error_codes.get(model_name)
    progress = get_progress_manager().get_progress(model_name) or {}
    raw_state = str(progress.get("status") or "")
    if raw_state == "cancelling":
        state = "cancelling" if active else "cancelled"
    elif active or raw_state == "downloading":
        state = "downloading"
    elif raw_state == "complete":
        state = "succeeded"
    elif raw_state == "cancelled":
        state = "cancelled"
    elif raw_state == "error" or remembered_error:
        state = "failed"
    else:
        state = "idle"

    current = int(progress.get("current") or 0)
    total = int(progress.get("total") or 0)
    if state == "succeeded":
        fraction = 1.0
    elif total > 0:
        fraction = min(1.0, max(0.0, current / total))
    else:
        fraction = 0.0
    return {
        "state": state,
        "progress": round(fraction, 4),
        "bytes_downloaded": current,
        "total_bytes": total or None,
        "filename": progress.get("filename"),
        "error": progress.get("error") or remembered_error,
        "error_code": error_code,
    }


def _write_model_marker(model_config: ASRModelConfig, snapshot_path: str) -> None:
    size_on_disk_mb = round(_directory_size(_model_dir(model_config.model_name)) / (1024 * 1024), 6)
    marker = {
        "model_name": model_config.model_name,
        "repo_id": model_config.repo_id,
        "source": model_config.source,
        "engine": model_config.engine,
        "snapshot_path": snapshot_path,
        "installed_at": _utc_iso(),
        "last_verified_at": _utc_iso(),
        "size_on_disk_mb": size_on_disk_mb,
    }
    (_model_dir(model_config.model_name) / "model.json").write_text(
        json.dumps(marker, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _download_huggingface_snapshot(model_config: ASRModelConfig, model_dir: Path) -> str:
    if not model_config.repo_id:
        raise ValueError(f"Model {model_config.model_name} does not define a Hugging Face repo")

    from huggingface_hub import snapshot_download

    with track_hf_download(
        model_config.model_name,
        get_progress_manager(),
        checkpoint=lambda: _download_checkpoint(model_config.model_name),
    ):
        return snapshot_download(
            repo_id=model_config.repo_id,
            cache_dir=str(config.get_model_cache_dir("huggingface") / "hub"),
            local_dir=str(model_dir),
            allow_patterns=model_config.allow_patterns,
            ignore_patterns=HF_DUPLICATE_WEIGHT_IGNORE_PATTERNS,
        )


def _download_modelscope_snapshot(model_config: ASRModelConfig, model_dir: Path) -> str:
    if not model_config.repo_id:
        raise ValueError(f"Model {model_config.model_name} does not define a ModelScope model id")

    from modelscope import snapshot_download

    get_progress_manager().update_progress(
        model_config.model_name,
        0,
        0,
        filename="Connecting to ModelScope...",
        status="downloading",
    )
    with track_hf_download(
        model_config.model_name,
        get_progress_manager(),
        checkpoint=lambda: _download_checkpoint(model_config.model_name),
    ):
        return snapshot_download(
            model_id=model_config.repo_id,
            cache_dir=str(config.get_model_cache_dir("modelscope")),
            local_dir=str(model_dir),
        )


def _download_local_snapshot(model_config: ASRModelConfig, model_dir: Path) -> str:
    """Copy a model from the configured offline archive, byte by byte.

    The copy reports exact byte progress and checks for cancellation between
    1 MiB chunks, so it behaves like a network download from the caller's
    point of view. ``BEEBOX_MODEL_SOURCE_COPY_DELAY_MS`` adds a per-chunk sleep
    for the console smoke test, which needs a download that stays observable.
    """
    root = local_model_source_dir()
    if root is None:
        raise RuntimeError("No local model source is configured")
    source_dir = root / model_config.model_name
    if not source_dir.is_dir():
        raise RuntimeError(f"Local model source is missing: {source_dir}")

    files = [
        path
        for path in sorted(source_dir.rglob("*"))
        if path.is_file() and path.relative_to(source_dir).as_posix() != "model.json"
    ]
    if not files:
        raise RuntimeError(f"Local model source is empty: {source_dir}")
    total = sum(path.stat().st_size for path in files)
    copied = 0
    chunk_delay = _local_copy_delay_seconds()
    progress = get_progress_manager()
    for path in files:
        relative = path.relative_to(source_dir)
        destination = model_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        with path.open("rb") as reader, destination.open("wb") as writer:
            while chunk := reader.read(LOCAL_COPY_CHUNK_BYTES):
                _download_checkpoint(model_config.model_name)
                writer.write(chunk)
                copied += len(chunk)
                progress.update_progress(
                    model_config.model_name,
                    copied,
                    total,
                    filename=relative.as_posix(),
                    status="downloading",
                )
                if chunk_delay:
                    time.sleep(chunk_delay)
    return str(model_dir)


def _progress_counters(model_name: str) -> tuple[int, int]:
    snapshot = get_progress_manager().get_progress(model_name) or {}
    return int(snapshot.get("current") or 0), int(snapshot.get("total") or 0)


def _mark_download_cancelled(model_name: str, message: str) -> None:
    current, total = _progress_counters(model_name)
    get_progress_manager().update_progress(
        model_name,
        current,
        total,
        status="cancelled",
        error=message,
    )


def _is_gated_repo_error(exc: BaseException) -> bool:
    try:
        from huggingface_hub.utils import GatedRepoError, HfHubHTTPError
    except Exception:
        return False
    if isinstance(exc, GatedRepoError):
        return True
    if isinstance(exc, HfHubHTTPError):
        response = getattr(exc, "response", None)
        return getattr(response, "status_code", None) in (401, 403)
    return False


def _run_download(model_config: ASRModelConfig) -> None:
    model_name = model_config.model_name
    model_dir = _model_dir(model_name)
    progress = get_progress_manager()
    stop_progress = threading.Event()
    progress_thread: threading.Thread | None = None
    gated_hit = False
    try:
        model_storage.require_storage(writable=True)
        _download_checkpoint(model_name)
        model_dir.mkdir(parents=True, exist_ok=True)
        candidates = _source_candidates(model_config)
        exact_progress = bool(candidates) and candidates[0].source == LOCAL_SOURCE_NAME
        if not exact_progress:
            # Local copies report exact bytes themselves; the directory poller
            # would overwrite them with the catalog estimate.
            progress_thread = _start_directory_progress_tracker(model_config, model_dir, stop_progress)
        errors: list[str] = []
        snapshot_path = ""
        installed_config = model_config
        for index, candidate in enumerate(candidates):
            _download_checkpoint(model_name)
            candidate_config = replace(model_config, source=candidate.source, repo_id=candidate.repo_id)
            progress.update_progress(
                model_name,
                0,
                0,
                filename=f"Connecting to {candidate.source}...",
                status="downloading",
                source=candidate.source,
                repo_id=candidate.repo_id,
                fallback_from=candidates[index - 1].source if index else None,
            )
            try:
                if candidate.source == "modelscope":
                    snapshot_path = _download_modelscope_snapshot(candidate_config, model_dir)
                elif candidate.source == "huggingface":
                    snapshot_path = _download_huggingface_snapshot(candidate_config, model_dir)
                elif candidate.source == LOCAL_SOURCE_NAME:
                    snapshot_path = _download_local_snapshot(candidate_config, model_dir)
                else:
                    raise RuntimeError(f"Unsupported model source: {candidate.source}")
                installed_config = candidate_config
                break
            except DownloadStopped:
                raise
            except Exception as exc:
                if candidate.source == "huggingface" and _is_gated_repo_error(exc):
                    gated_hit = True
                errors.append(str(exc) if len(candidates) == 1 else f"{candidate.source}:{candidate.repo_id}: {exc}")
                if index < len(candidates) - 1:
                    progress.update_progress(
                        model_name,
                        0,
                        0,
                        filename=f"Falling back from {candidate.source}...",
                        status="downloading",
                        error=str(exc),
                        source=candidate.source,
                        repo_id=candidate.repo_id,
                    )
                    continue
                raise RuntimeError("; ".join(errors)) from exc

        _download_checkpoint(model_name)

        if not _has_weight_files(model_dir):
            raise RuntimeError("Model download finished but no model weight files were found")
        if _has_incomplete_files(model_dir):
            raise RuntimeError("Model download has incomplete files")

        with _state_lock:
            cancelled = model_name in _cancelled_downloads
        if cancelled:
            _mark_download_cancelled(model_name, "Download cancelled")
            return

        _write_model_marker(installed_config, snapshot_path)
        stop_progress.set()
        if progress_thread:
            progress_thread.join(timeout=1)
        current, total = _progress_counters(model_name)
        progress.mark_complete(model_name, current, total)
    except DownloadStopped:
        stop_progress.set()
        if progress_thread:
            progress_thread.join(timeout=1)
        _mark_download_cancelled(model_name, "Download stopped")
    except Exception as exc:
        stop_progress.set()
        if progress_thread:
            progress_thread.join(timeout=1)
        error = str(exc)
        with _state_lock:
            _download_errors[model_name] = error
            if gated_hit:
                _download_error_codes[model_name] = "GATED_REPO_ACCESS"
        progress.mark_error(model_name, error)
    finally:
        stop_progress.set()
        if progress_thread:
            progress_thread.join(timeout=1)
        with _state_lock:
            _active_downloads.discard(model_name)
            _cancelled_downloads.discard(model_name)
    

def _download_worker() -> None:
    while True:
        model_config = _download_queue.get()
        try:
            _run_download(model_config)
        finally:
            _download_queue.task_done()


def _ensure_download_worker() -> None:
    global _download_worker_started
    with _state_lock:
        if _download_worker_started:
            return
        thread = threading.Thread(target=_download_worker, daemon=True)
        thread.start()
        _download_worker_started = True


def download_model(model_name: str) -> str:
    model_storage.require_storage(writable=True)
    model_config = get_model_config(model_name)
    if model_config is None:
        raise ValueError(f"Unknown model: {model_name}")
    runtime_error = _model_runtime_error(model_config)
    if runtime_error:
        raise ValueError(runtime_error)

    if _is_local_model_downloaded(model_name):
        return f"Model {model_name} is already downloaded"

    with _state_lock:
        if model_name in _active_downloads:
            return f"Model {model_name} download already running"
        _active_downloads.add(model_name)
        _cancelled_downloads.discard(model_name)
        _download_errors.pop(model_name, None)
        _download_error_codes.pop(model_name, None)

    progress = get_progress_manager()
    progress.update_progress(
        model_name,
        0,
        0,
        filename="Connecting to model hub...",
        status="downloading",
    )

    _ensure_download_worker()
    _download_queue.put(model_config)
    return f"Model {model_name} download started"


def redownload_model(model_name: str) -> str:
    model_storage.require_storage(writable=True)
    model_config = get_model_config(model_name)
    if model_config is None:
        raise ValueError(f"Unknown model: {model_name}")
    delete_model(model_name)
    return download_model(model_name)


def unload_model(model_name: str) -> bool:
    if get_model_config(model_name) is None:
        return False
    return get_speech_engine().unload(model_name)


def is_model_loaded(model_name: str) -> bool:
    if get_model_config(model_name) is None:
        return False
    return get_speech_engine().is_model_loaded(model_name)


def is_model_downloading(model_name: str) -> bool:
    with _state_lock:
        return model_name in _active_downloads


def delete_model(model_name: str) -> None:
    model_storage.require_storage(writable=True)
    if get_model_config(model_name) is None:
        raise ValueError(f"Unknown model: {model_name}")
    target = _model_dir(model_name).resolve()
    if not target.is_relative_to(config.get_models_dir().resolve()):
        raise ValueError(f"Unknown model: {model_name}")
    unload_model(model_name)
    shutil.rmtree(target, ignore_errors=True)
    with _state_lock:
        _active_downloads.discard(model_name)
        _cancelled_downloads.discard(model_name)
        _download_errors.pop(model_name, None)
        _download_error_codes.pop(model_name, None)
    get_progress_manager().clear_progress(model_name)


def active_downloads() -> list[dict[str, Any]]:
    progress = get_progress_manager()
    items = []
    with _state_lock:
        names = set(_active_downloads)
    for model_name in names:
        state = progress.get_progress(model_name) or {"model_name": model_name, "status": "downloading"}
        items.append(state)
    return items


def cancel_download(model_name: str) -> bool:
    with _state_lock:
        if model_name not in _active_downloads:
            return False
        _cancelled_downloads.add(model_name)
    current, total = _progress_counters(model_name)
    get_progress_manager().update_progress(
        model_name,
        current,
        total,
        status="cancelling",
        error="Download cancellation requested",
    )
    return True


def retry_download(model_name: str) -> str:
    return download_model(model_name)


def storage_summary() -> dict[str, Any]:
    storage = model_storage.inspect_storage(include_usage=False)
    items = []
    total = 0
    for item in get_all_model_configs():
        model_dir = _model_dir(item.model_name)
        downloaded, _marker, size_bytes = _catalog_snapshot(item.model_name) if storage["available"] else (False, {}, 0)
        total += size_bytes
        items.append(
            {
                "model_name": item.model_name,
                "path": str(model_dir),
                "exists": model_dir.exists(),
                "size_bytes": size_bytes,
                "size_on_disk_mb": round(size_bytes / (1024 * 1024), 2),
                "downloaded": downloaded if storage["available"] else None,
            }
        )
    free_disk_bytes = storage["free_bytes"]
    total_disk_bytes = storage["total_bytes"]
    cache_usage = [
        {
            "name": name,
            "path": storage["cache_dirs"][name],
            "size_bytes": size,
            "shared": False,
            "selected": True,
            "warning": None,
        }
        for name, size in storage["cache_usage"].items()
    ]
    return {
        "root": storage["root"],
        "models_dir": str(config.get_models_dir()),
        "models": items,
        "status": storage["status"],
        "reason": storage["reason"],
        "detail": storage["detail"],
        "available": storage["available"],
        "writable": storage["writable"],
        "cache_dirs": storage["cache_dirs"],
        "cache_usage": cache_usage,
        "cache_bytes": storage["cache_bytes"],
        "allowed_roots": storage["allowed_roots"],
        "root_locked": storage["root_locked"],
        "network_filesystem": storage["network_filesystem"],
        "filesystem_type": storage["filesystem_type"],
        "used_bytes": total + storage["cache_bytes"],
        "free_bytes": free_disk_bytes,
        "total_bytes": total_disk_bytes,
        "total_size_mb": round(total / (1024 * 1024), 2),
        "free_disk_bytes": free_disk_bytes,
    }


def cleanup_incomplete() -> dict[str, Any]:
    model_storage.require_storage(writable=True)
    removed: list[str] = []
    errors: list[str] = []
    models_dir = config.get_models_dir()
    if not models_dir.exists():
        return {"removed": removed, "errors": errors}
    for item in models_dir.iterdir():
        if not item.is_dir():
            continue
        should_remove = _has_incomplete_files(item) or not (item / "model.json").exists() or not _has_weight_files(item)
        if not should_remove:
            continue
        try:
            shutil.rmtree(item)
            removed.append(item.name)
        except OSError as exc:
            errors.append(f"{item.name}: {exc}")
    return {"removed": removed, "errors": errors}


def ensure_model_ready(model_name: str) -> None:
    model_storage.require_storage()
    if get_model_config(model_name) is None:
        raise ValueError(f"Unknown model: {model_name}")
    if not is_model_downloaded(model_name):
        raise BeeBoxError("MODEL_NOT_DOWNLOADED", f"Model {model_name} is not downloaded", stage="waiting_model")
    compatibility = check_model_compatibility(model_name)
    if not compatibility["compatible"]:
        raise BeeBoxError("MODEL_COMPATIBILITY_FAILED", compatibility["message"], stage="waiting_model")


def verify_models() -> list[dict[str, Any]]:
    model_storage.require_storage()
    return [check_model_compatibility(item.model_name) for item in get_all_model_configs()]

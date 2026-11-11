from __future__ import annotations

import math
import os
import threading
from pathlib import Path

import numpy as np

from backend.services.errors import BeeBoxError
from backend.services.model_loading import transformer_load_kwargs
from backend.services.model_paths import resolve_model_dir
from backend.services.segments import build_char_timeline

ALIGNER_MODEL_NAME = "qwen3-forced-aligner-0.6b"

_REQUIRED_FILES = ("config.json",)
_PROCESSOR_FILES = ("processor_config.json", "preprocessor_config.json")
_WEIGHT_FILES = ("model.safetensors", "pytorch_model.bin")

# Local copies so tests can exercise the kwargs helpers without torch installed.
_load_kwargs = transformer_load_kwargs

_lock = threading.Lock()
_cached_dir: Path | None = None
_cached_aligner: tuple[object, object] | None = None


def aligner_model_dir() -> Path:
    override = os.environ.get("BEEBOX_ALIGNER_MODEL_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return resolve_model_dir(ALIGNER_MODEL_NAME)


def _model_files_present(model_dir: Path) -> bool:
    if not model_dir.exists():
        return False
    if not all((model_dir / name).exists() for name in _REQUIRED_FILES):
        return False
    if not any((model_dir / name).exists() for name in _PROCESSOR_FILES):
        return False
    return any((model_dir / name).exists() for name in _WEIGHT_FILES)


def is_aligner_ready() -> bool:
    """True when the preseeded forced-aligner directory can be loaded."""
    try:
        return _model_files_present(aligner_model_dir())
    except Exception:
        return False


def unload_aligner() -> bool:
    """Drop the cached processor/model (used by tests and stage teardown)."""
    global _cached_dir, _cached_aligner
    with _lock:
        removed = _cached_aligner is not None
        _cached_dir = None
        _cached_aligner = None
    return removed


def _load_aligner(model_dir: Path) -> tuple[object, object]:
    import torch
    from transformers import AutoModelForTokenClassification, AutoProcessor

    kwargs = _load_kwargs(torch)
    processor = AutoProcessor.from_pretrained(str(model_dir))
    model = AutoModelForTokenClassification.from_pretrained(str(model_dir), **kwargs)
    model.eval()
    return processor, model


def _get_aligner() -> tuple[object, object]:
    global _cached_dir, _cached_aligner
    model_dir = aligner_model_dir()
    with _lock:
        if _cached_aligner is not None and _cached_dir == model_dir:
            return _cached_aligner
        if not _model_files_present(model_dir):
            raise BeeBoxError(
                "ALIGNMENT_FAILED",
                f"Forced aligner model is not available at {model_dir}",
                stage="align",
            )
        try:
            processor, model = _load_aligner(model_dir)
        except Exception as exc:
            raise BeeBoxError(
                "ALIGNMENT_FAILED",
                f"Failed to load forced aligner from {model_dir}: {type(exc).__name__}: {exc}",
                stage="align",
            ) from exc
        _cached_dir = model_dir
        _cached_aligner = (processor, model)
        return _cached_aligner


def _normalise_items(decoded) -> list[dict]:
    if isinstance(decoded, dict):
        decoded = [decoded]
    items: list[dict] = []
    for entry in decoded or []:
        if not isinstance(entry, dict):
            raise ValueError(f"unexpected forced-alignment item: {entry!r}")
        token = entry.get("text")
        if token is None:
            raise ValueError(f"forced-alignment item is missing text: {entry!r}")
        start = float(entry.get("start_time"))
        end = float(entry.get("end_time"))
        if not (math.isfinite(start) and math.isfinite(end)):
            raise ValueError(f"forced-alignment item has a non-finite timestamp: {entry!r}")
        items.append({"text": str(token), "start_time": start, "end_time": end})
    if not items:
        raise ValueError("forced aligner returned no timestamp items")
    return items


def align_transcript(audio: np.ndarray, text: str, language: str | None) -> list[dict]:
    """Align ``text`` against 16 kHz audio and return token timestamps.

    Every failure (missing model, incompatible token count, inference error)
    raises ``BeeBoxError("ALIGNMENT_FAILED")``. There is deliberately no
    fallback to evenly spaced timestamps: a wrong timeline is worse than a
    failed task.
    """
    if not text or not text.strip():
        return []
    try:
        processor, model = _get_aligner()
        import torch

        inputs, word_lists = processor.prepare_forced_aligner_inputs(
            audio=audio,
            transcript=text,
            language=language,
        )
        device = getattr(model, "device", None)
        dtype = getattr(model, "dtype", None)
        if hasattr(inputs, "to") and device is not None:
            inputs = inputs.to(device, dtype) if dtype is not None else inputs.to(device)
        with torch.inference_mode():
            outputs = model(**inputs)
        decoded = processor.decode_forced_alignment(
            logits=outputs.logits,
            input_ids=inputs["input_ids"],
            word_lists=word_lists,
            timestamp_token_id=model.config.timestamp_token_id,
        )
        items = _normalise_items(decoded[0] if isinstance(decoded, list) and decoded else decoded)
        # Fail here, at the alignment seam, when the aligner dropped or added
        # tokens instead of letting downstream grouping shift the transcript.
        build_char_timeline(text, items)
        return items
    except BeeBoxError:
        raise
    except Exception as exc:
        raise BeeBoxError(
            "ALIGNMENT_FAILED",
            f"Forced alignment failed: {type(exc).__name__}: {exc}",
            stage="align",
        ) from exc

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from backend.services.aligner import ALIGNER_MODEL_NAME
from backend.services.diarization import DIARIZATION_MODEL_NAME
from backend.services.media import CHUNK_OVERLAP_MS, CHUNK_WINDOW_MS

if TYPE_CHECKING:
    from backend.database.models import TranscriptionTask

DEFAULT_OUTPUT_FORMATS = ["txt", "md", "srt"]
SUPPORTED_OUTPUT_FORMATS = frozenset(DEFAULT_OUTPUT_FORMATS)
DEFAULT_POSTPROCESS_MODE = "safe"


def read_task_options(row: TranscriptionTask) -> dict[str, Any]:
    """Return a task row's persisted option snapshot.

    One reader for every module that projects or mutates ``options_json``:
    corrupt legacy rows degrade to ``{}`` instead of breaking a status request.
    """
    try:
        options = json.loads(row.options_json or "{}")
    except json.JSONDecodeError:
        return {}
    return options if isinstance(options, dict) else {}


class UnsupportedOutputFormat(ValueError):
    def __init__(self, formats: list[str]) -> None:
        self.formats = formats
        super().__init__(f"Unsupported output format(s): {', '.join(sorted(formats))}")


def _canonical_formats(values: Any) -> list[str]:
    """Alias-normalise, validate and de-duplicate one format sequence."""
    normalized = [("md" if str(item).strip().lower() == "markdown" else str(item).strip().lower()) for item in values]
    normalized = [item for item in normalized if item]
    unknown = sorted({item for item in normalized if item not in SUPPORTED_OUTPUT_FORMATS})
    if unknown:
        raise UnsupportedOutputFormat(unknown)
    # Preserve request order but drop duplicates.
    return list(dict.fromkeys(normalized))


def parse_output_formats(value: str | None) -> list[str]:
    """Parse the multipart ``output_formats`` field into canonical formats.

    The contract accepts both a JSON array and a comma-separated list. Unknown
    formats raise before any upload is written so a rejected request leaves no
    orphan file or task row.
    """
    if value is None or not str(value).strip():
        return list(DEFAULT_OUTPUT_FORMATS)
    text = str(value).strip()
    parsed: list[str] | None = None
    if text.startswith("["):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, list):
            parsed = list(decoded)
    if parsed is None:
        parsed = [item for item in text.split(",") if item.strip()]
    if not any(str(item).strip() for item in parsed):
        return list(DEFAULT_OUTPUT_FORMATS)
    return _canonical_formats(parsed)


def validate_output_formats(formats: list[str] | None) -> list[str]:
    if formats is None:
        return list(DEFAULT_OUTPUT_FORMATS)
    return _canonical_formats(formats)


def build_task_options(
    *,
    num_speakers: int | None,
    diarization: bool | None,
    vad: bool | None,
    word_timestamps: bool | None,
    postprocess_mode: str | None,
    traditional_to_simplified: bool | None,
) -> dict[str, Any]:
    """The option snapshot persisted on upload and exposed by ``GET /tasks``."""
    options: dict[str, Any] = {
        "diarization": False if diarization is None else bool(diarization),
        "num_speakers": num_speakers,
        "aligner_model": ALIGNER_MODEL_NAME,
        "diarization_model": DIARIZATION_MODEL_NAME,
        "chunk_window_ms": CHUNK_WINDOW_MS,
        "chunk_overlap_ms": CHUNK_OVERLAP_MS,
        "postprocess_mode": postprocess_mode or DEFAULT_POSTPROCESS_MODE,
        "traditional_to_simplified": bool(traditional_to_simplified),
        "vad": True if vad is None else bool(vad),
    }
    if word_timestamps is not None:
        options["word_timestamps"] = bool(word_timestamps)
    return options

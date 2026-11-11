from __future__ import annotations

import json
from pathlib import Path

from backend import config


def resolve_model_dir(model_name: str) -> Path:
    """Resolve a preseeded/downloaded model directory.

    Model markers written by the model manager store an absolute
    ``snapshot_path`` for the storage root active at download time. When that
    root moved, the path is stale, so the portion below the model directory is
    re-anchored under the current storage root before falling back to
    ``<models_dir>/<model_name>``.
    """
    model_dir = config.get_models_dir() / model_name
    marker = model_dir / "model.json"
    if marker.exists():
        try:
            snapshot_path = json.loads(marker.read_text(encoding="utf-8")).get("snapshot_path")
        except (json.JSONDecodeError, OSError):
            snapshot_path = None
        if snapshot_path:
            recorded = Path(snapshot_path)
            if recorded.exists():
                return recorded
            if model_name in recorded.parts:
                suffix = recorded.parts[recorded.parts.index(model_name) + 1 :]
                if suffix:
                    reanchored = model_dir.joinpath(*suffix)
                    if reanchored.exists():
                        return reanchored
    return model_dir

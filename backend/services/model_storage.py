"""Model storage inspection shared by the health, status and download paths."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
from typing import Any

from backend import config

NETWORK_FILESYSTEMS = {
    "9p",
    "afpfs",
    "cifs",
    "davfs",
    "fuse.sshfs",
    "nfs",
    "nfs4",
    "smbfs",
}


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _filesystem_type(path: Path) -> str | None:
    mounts = Path("/proc/mounts")
    if not mounts.is_file():
        return None
    best: tuple[int, str] | None = None
    try:
        lines = mounts.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue
        mount = Path(fields[1].replace("\\040", " "))
        try:
            path.relative_to(mount)
        except ValueError:
            continue
        candidate = (len(mount.parts), fields[2].lower())
        if best is None or candidate[0] > best[0]:
            best = candidate
    return best[1] if best else None


def inspect_storage(*, include_usage: bool = True) -> dict[str, Any]:
    root = config.get_model_storage_root()
    models_dir = root / "models"
    cache_dirs = config.get_model_cache_dirs()
    status = "available"
    reason: str | None = None
    detail: str | None = None
    writable = False
    total_bytes: int | None = None
    free_bytes: int | None = None

    try:
        if not root.exists():
            status, reason = "unavailable", "path_missing"
        elif not root.is_dir():
            status, reason = "unavailable", "not_a_directory"
        else:
            writable = os.access(root, os.W_OK | os.X_OK)
            if not writable:
                status, reason = "read_only", "not_writable"
            usage = shutil.disk_usage(root)
            total_bytes, free_bytes = usage.total, usage.free
    except OSError as exc:
        status, reason, detail = "unavailable", "io_error", str(exc)

    should_scan_usage = include_usage and status != "unavailable"
    cache_usage = {name: directory_size(path) if should_scan_usage else 0 for name, path in cache_dirs.items()}
    model_bytes = directory_size(models_dir) if should_scan_usage else 0
    filesystem_type = _filesystem_type(root) if status != "unavailable" else None
    return {
        "root": str(root),
        "models_dir": str(models_dir),
        "cache_dirs": {name: str(path) for name, path in cache_dirs.items()},
        "status": status,
        "reason": reason,
        "detail": detail,
        "available": status != "unavailable",
        "writable": writable,
        "model_bytes": model_bytes,
        "cache_bytes": sum(cache_usage.values()),
        "cache_usage": cache_usage,
        "used_bytes": model_bytes + sum(cache_usage.values()),
        "total_bytes": total_bytes,
        "free_bytes": free_bytes,
        "filesystem_type": filesystem_type,
        "network_filesystem": filesystem_type in NETWORK_FILESYSTEMS if filesystem_type else False,
        "allowed_roots": [str(path) for path in config.get_allowed_model_storage_roots()],
        "root_locked": config.model_storage_root_is_locked(),
    }


def require_storage(*, writable: bool = False) -> dict[str, Any]:
    from backend.services.errors import BeeBoxError

    status = inspect_storage()
    if not status["available"]:
        raise BeeBoxError("MODEL_STORAGE_UNAVAILABLE", "模型存储位置不可用", stage="waiting_model")
    if writable and config.is_dev_instance():
        # The dev instance shares the production models directory read-only
        # (ADR-0010); downloading, deleting or cleaning models there would
        # corrupt production. Read-only use stays available.
        raise BeeBoxError(
            "MODEL_STORAGE_READ_ONLY",
            "The dev instance shares production models read-only; model storage changes are disabled",
            stage="waiting_model",
        )
    if writable and not status["writable"]:
        raise BeeBoxError("MODEL_STORAGE_READ_ONLY", "Model storage location is read-only", stage="waiting_model")
    return status

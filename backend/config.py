from __future__ import annotations

import json
import ipaddress
import os
from pathlib import Path
import tempfile
import sys
from typing import Any

DEFAULT_PORT = 17494
# v1 exposes exactly one local ASR model; every implicit default resolves here.
DEFAULT_LOCAL_MODEL_NAME = "qwen3-asr-1.7b"
DEFAULT_RETENTION_DAYS = 30
# ADR-0012: the Bilibili cookie is checked and, when needed, refreshed daily.
DEFAULT_BILIBILI_REFRESH_INTERVAL_SECONDS = 24 * 60 * 60
MODEL_STORAGE_CONFIG_VERSION = 1
MODEL_STORAGE_CONFIG_FILENAME = "model-storage.json"
MODEL_CACHE_NAMES = ("huggingface", "modelscope", "torch", "xdg")
MEDIA_STORAGE_CONFIG_VERSION = 1
MEDIA_STORAGE_CONFIG_FILENAME = "media-storage.json"
INGEST_MODE_REFERENCE = "reference"
INGEST_MODE_COPY = "copy"
INGEST_MODES = (INGEST_MODE_REFERENCE, INGEST_MODE_COPY)
DEFAULT_INGEST_MODE = INGEST_MODE_REFERENCE


def is_loopback_address(value: str) -> bool:
    host = value.strip().strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_bind_security(host: str) -> None:
    """Refuse to serve a non-loopback address without a Bearer token (ADR-0011).

    The native Windows deployment passes ``--host 0.0.0.0`` to
    :mod:`backend.server`; only loopback binds may start without a token. The
    dev instance is local-only by design (ADR-0010), so it never binds a LAN
    address even with a token.
    """
    if is_dev_instance() and not is_loopback_address(host):
        raise RuntimeError(
            f"The dev instance must bind a loopback address, not {host!r}. Use "
            "deploy/windows/dev-instance.ps1, which binds 127.0.0.1."
        )
    if is_loopback_address(host):
        return
    if os.environ.get("BEEBOX_API_TOKEN", "").strip():
        return
    raise RuntimeError(
        f"Refusing to start on {host!r} without BEEBOX_API_TOKEN. "
        "Set BEEBOX_API_TOKEN (the Windows installer writes it to "
        "secrets\\settings.env) or bind 127.0.0.1 for local-only access."
    )


def get_root_dir() -> Path | None:
    """Return the fixed install root (``C:\\BeeBox`` by default on the server)."""
    raw = os.environ.get("BEEBOX_ROOT", "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().absolute()


def resolve_secrets_dir() -> Path:
    """Return the credential directory without creating it (ADR-0011/0012).

    ``BEEBOX_SECRETS_DIR`` is the explicit override the tests and a custom
    deployment use. Otherwise the install root's ``secrets`` directory is the
    home for ``settings.env`` and the Bilibili cookie file; a developer
    checkout falls back to ``<data dir>/secrets``.
    """
    enforced = os.environ.get("BEEBOX_SECRETS_DIR", "").strip()
    if enforced:
        return Path(enforced).expanduser().absolute()
    root = get_root_dir()
    if root is not None:
        return root / "secrets"
    return get_data_dir() / "secrets"


def get_secrets_dir() -> Path:
    path = resolve_secrets_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_log_dir() -> Path:
    """Return the log directory used by :mod:`backend.logging_setup`.

    ``BEEBOX_LOG_DIR`` is the explicit override the installer writes into
    ``secrets\\settings.env``. Without it the install root's ``logs`` directory
    is used; a plain developer checkout falls back to ``<data dir>/logs``.
    """
    enforced = os.environ.get("BEEBOX_LOG_DIR", "").strip()
    if enforced:
        return Path(enforced).expanduser().absolute()
    root = get_root_dir()
    if root is not None:
        return root / "logs"
    return get_data_dir() / "logs"


def get_data_dir() -> Path:
    path = Path(os.environ.get("BEEBOX_DATA_DIR", "data")).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_db_path() -> Path:
    return get_data_dir() / "beebox.db"


def get_retention_days() -> int:
    """How long a settled job and its result are kept (ADR-0011).

    ``BEEBOX_RETENTION_DAYS`` is the single knob; invalid or missing values
    fall back to the contract default of 30 days. ``0`` is allowed and means a
    settled job is eligible on the next cleanup run.
    """
    raw = os.environ.get("BEEBOX_RETENTION_DAYS", "").strip()
    if not raw:
        return DEFAULT_RETENTION_DAYS
    try:
        days = int(raw)
    except ValueError:
        return DEFAULT_RETENTION_DAYS
    return max(0, days)


def get_bilibili_refresh_interval_seconds() -> float:
    """How often the Bilibili login is checked (ADR-0012, ticket #47).

    ``BEEBOX_BILIBILI_REFRESH_INTERVAL_SECONDS`` is the single knob; invalid,
    missing or non-positive values fall back to one day. A positive fractional
    value is allowed so a smoke run can observe a refresh quickly.
    """
    raw = os.environ.get("BEEBOX_BILIBILI_REFRESH_INTERVAL_SECONDS", "").strip()
    if not raw:
        return float(DEFAULT_BILIBILI_REFRESH_INTERVAL_SECONDS)
    try:
        seconds = float(raw)
    except ValueError:
        return float(DEFAULT_BILIBILI_REFRESH_INTERVAL_SECONDS)
    if seconds <= 0:
        return float(DEFAULT_BILIBILI_REFRESH_INTERVAL_SECONDS)
    return seconds


def _ensure_dir(name: str) -> Path:
    path = get_data_dir() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_media_storage_config_path() -> Path:
    return get_data_dir() / MEDIA_STORAGE_CONFIG_FILENAME


def _read_media_storage_config() -> dict[str, Any]:
    path = get_media_storage_config_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != MEDIA_STORAGE_CONFIG_VERSION:
        return {}
    return payload


def _write_media_storage_config(payload: dict[str, Any]) -> None:
    path = get_media_storage_config_path()
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _optional_configured_path(value: Any) -> Path | None:
    if isinstance(value, str) and value.strip():
        return Path(value).expanduser().absolute()
    return None


def get_media_ingest_mode() -> str:
    mode = _read_media_storage_config().get("ingest_mode")
    return mode if mode in INGEST_MODES else DEFAULT_INGEST_MODE


def uploads_dir_is_locked() -> bool:
    return bool(os.environ.get("BEEBOX_UPLOADS_DIR", "").strip())


def derived_audio_dir_is_locked() -> bool:
    return bool(os.environ.get("BEEBOX_DERIVED_AUDIO_DIR", "").strip())


def resolve_uploads_dir() -> Path:
    """Return the effective managed-uploads directory without creating it."""
    enforced = os.environ.get("BEEBOX_UPLOADS_DIR", "").strip()
    if enforced:
        return Path(enforced).expanduser().absolute()
    configured = _optional_configured_path(_read_media_storage_config().get("uploads_dir"))
    if configured is not None:
        return configured
    return get_data_dir() / "uploads"


def resolve_derived_audio_dir() -> Path:
    """Return the effective derived-audio directory without creating it."""
    enforced = os.environ.get("BEEBOX_DERIVED_AUDIO_DIR", "").strip()
    if enforced:
        return Path(enforced).expanduser().absolute()
    configured = _optional_configured_path(_read_media_storage_config().get("derived_audio_dir"))
    if configured is not None:
        return configured
    return get_data_dir() / "derived-audio"


def _env_flag(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return None


def is_dev_instance() -> bool:
    """True when this process is the isolated development instance (ADR-0010)."""
    return _env_flag("BEEBOX_DEV_INSTANCE") is True


def get_prod_root() -> Path | None:
    """Production install root, used by the dev instance to reject prod paths."""
    raw = os.environ.get("BEEBOX_PROD_ROOT", "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().absolute()


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().absolute()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        return path.resolve().is_relative_to(parent.resolve())
    except (OSError, ValueError):
        return False


def validate_dev_instance_paths() -> None:
    """Refuse a dev instance that could write the production instance (ADR-0010).

    The dev instance is allowed to reference the production models directory
    read-only; every path it *writes* -- data, logs, uploads, derived audio and
    model caches -- must live outside the production root.
    """
    if not is_dev_instance():
        return
    prod_root = get_prod_root()
    if prod_root is None:
        raise RuntimeError(
            "BEEBOX_DEV_INSTANCE=1 requires BEEBOX_PROD_ROOT (the production install root, e.g. C:\\BeeBox)."
        )
    data_dir = _env_path("BEEBOX_DATA_DIR")
    if data_dir is None:
        raise RuntimeError(
            "BEEBOX_DEV_INSTANCE=1 requires an explicit BEEBOX_DATA_DIR outside the production root."
        )
    writable_paths = {
        "BEEBOX_DATA_DIR": data_dir,
        "BEEBOX_LOG_DIR": _env_path("BEEBOX_LOG_DIR") or data_dir / "logs",
        "BEEBOX_UPLOADS_DIR": _env_path("BEEBOX_UPLOADS_DIR") or data_dir / "uploads",
        "BEEBOX_DERIVED_AUDIO_DIR": _env_path("BEEBOX_DERIVED_AUDIO_DIR") or data_dir / "derived-audio",
        "BEEBOX_MODEL_CACHE_ROOT": get_model_cache_root(),
    }
    for name, path in writable_paths.items():
        if _is_within(path, prod_root):
            raise RuntimeError(
                f"{name} resolves to {path}, which is inside the production root {prod_root}. "
                "The dev instance must keep every writable path outside production (ADR-0010)."
            )
    model_root = get_model_storage_root()
    for forbidden_name, forbidden in (
        ("data", prod_root / "data"),
        ("secrets", prod_root / "secrets"),
    ):
        if _is_within(model_root, forbidden):
            raise RuntimeError(
                f"BEEBOX_MODEL_STORAGE_ROOT resolves to {model_root}, which is inside production {forbidden_name} "
                f"({forbidden}). Point it at the production model storage root (read-only) or outside production."
            )


def delete_derived_audio_on_complete() -> bool:
    """Gate 2 v1 default: derived audio is removed after a successful task.

    Precedence: ``BEEBOX_DELETE_DERIVED_ON_COMPLETE`` (explicit true/false)
    overrides the persisted media-storage setting; when neither exists the
    contract default is on.
    """
    override = _env_flag("BEEBOX_DELETE_DERIVED_ON_COMPLETE")
    if override is not None:
        return override
    stored = _read_media_storage_config()
    if "delete_derived_on_complete" in stored:
        return bool(stored["delete_derived_on_complete"])
    return True


def update_media_storage_config(
    *,
    ingest_mode: str | None = None,
    uploads_dir: str | Path | None = None,
    derived_audio_dir: str | Path | None = None,
    delete_derived_on_complete: bool | None = None,
) -> None:
    payload = _read_media_storage_config()
    payload["version"] = MEDIA_STORAGE_CONFIG_VERSION
    if ingest_mode is not None:
        if ingest_mode not in INGEST_MODES:
            raise ValueError(f"Unsupported ingest mode: {ingest_mode}")
        payload["ingest_mode"] = ingest_mode
    if uploads_dir is not None:
        if uploads_dir_is_locked():
            raise ValueError("Uploads directory is locked by BEEBOX_UPLOADS_DIR")
        payload["uploads_dir"] = str(Path(uploads_dir).expanduser().absolute())
    if derived_audio_dir is not None:
        if derived_audio_dir_is_locked():
            raise ValueError("Derived-audio directory is locked by BEEBOX_DERIVED_AUDIO_DIR")
        payload["derived_audio_dir"] = str(Path(derived_audio_dir).expanduser().absolute())
    if delete_derived_on_complete is not None:
        payload["delete_derived_on_complete"] = bool(delete_derived_on_complete)
    _write_media_storage_config(payload)


def get_uploads_dir() -> Path:
    path = resolve_uploads_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_derived_audio_dir() -> Path:
    path = resolve_derived_audio_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_audio_dir() -> Path:
    return _ensure_dir("audio")


def get_exports_dir() -> Path:
    return _ensure_dir("exports")


def get_cache_dir() -> Path:
    return _ensure_dir("cache")


def get_model_storage_config_path() -> Path:
    return get_data_dir() / MODEL_STORAGE_CONFIG_FILENAME


def _read_model_storage_config() -> dict[str, Any]:
    path = get_model_storage_config_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != MODEL_STORAGE_CONFIG_VERSION:
        return {}
    return payload


def get_model_storage_root() -> Path:
    enforced = os.environ.get("BEEBOX_MODEL_STORAGE_ROOT", "").strip()
    if enforced:
        return Path(enforced).expanduser().absolute()
    configured = _read_model_storage_config().get("root")
    if isinstance(configured, str) and configured.strip():
        return Path(configured).expanduser().absolute()
    return get_data_dir()


def model_storage_root_is_locked() -> bool:
    return bool(os.environ.get("BEEBOX_MODEL_STORAGE_ROOT", "").strip())


def set_model_storage_root(root: str | Path) -> Path:
    """Persist the model storage root outside the movable root itself."""
    resolved = Path(root).expanduser().absolute()
    path = get_model_storage_config_path()
    payload = {"version": MODEL_STORAGE_CONFIG_VERSION, "root": str(resolved)}
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return resolved


def get_allowed_model_storage_roots() -> list[Path]:
    configured = os.environ.get("BEEBOX_MODEL_STORAGE_ROOTS", "")
    return [Path(value.strip()).expanduser().absolute() for value in configured.split(",") if value.strip()]


def get_models_dir() -> Path:
    return get_model_storage_root() / "models"


def get_model_cache_root() -> Path:
    """Root for framework caches; the dev instance points it away from prod."""
    override = _env_path("BEEBOX_MODEL_CACHE_ROOT")
    if override is not None:
        return override
    return get_model_storage_root()


def get_model_cache_dir(name: str) -> Path:
    if name not in MODEL_CACHE_NAMES:
        raise ValueError(f"Unsupported model cache: {name}")
    return get_model_cache_root() / "cache" / name


def get_model_cache_dirs() -> dict[str, Path]:
    return {name: get_model_cache_dir(name) for name in MODEL_CACHE_NAMES}


def configure_model_cache_environment() -> None:
    cache_dirs = get_model_cache_dirs()
    os.environ["HF_HOME"] = str(cache_dirs["huggingface"])
    os.environ["HUGGINGFACE_HUB_CACHE"] = str(cache_dirs["huggingface"] / "hub")
    os.environ["MODELSCOPE_CACHE"] = str(cache_dirs["modelscope"])
    os.environ["TORCH_HOME"] = str(cache_dirs["torch"])
    os.environ["XDG_CACHE_HOME"] = str(cache_dirs["xdg"])
    torch = sys.modules.get("torch")
    if torch is not None and hasattr(torch, "hub"):
        torch.hub.set_dir(str(cache_dirs["torch"] / "hub"))


def to_storage_path(path: str | Path) -> str:
    resolved = Path(path).resolve()
    try:
        return str(resolved.relative_to(get_data_dir()))
    except ValueError:
        return str(resolved)


def resolve_storage_path(path: str | Path | None) -> Path | None:
    if path is None:
        return None
    stored = Path(path)
    if stored.is_absolute():
        return stored
    return (get_data_dir() / stored).resolve()

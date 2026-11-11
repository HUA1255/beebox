"""UTF-8 file logging with rotation for the hosted Windows service.

``backend.server`` (the entry point WinSW starts) and ``backend.main`` (the
development entry point) call :func:`configure_logging` before serving. The
file lives below ``BEEBOX_LOG_DIR`` (default ``<BEEBOX_ROOT>\\logs``) and is
rotated by size, so a long-running service never fills the disk.

The console handler stays attached: when the service runs under WinSW stdout
and stderr are redirected into the same logs directory, and in development the
records remain visible in the terminal.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from backend import config

LOG_FILE_PREFIX = "beebox"
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_HANDLER_MARKER = "_beebox_logging_handler"


def _positive_int_setting(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _console_enabled() -> bool:
    raw = os.environ.get("BEEBOX_LOG_CONSOLE", "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _safe_component(component: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", component.strip()).strip("-")
    return cleaned or "server"


def _prepare_console_stream() -> None:
    stream = sys.stderr
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, ValueError, OSError):
        # pytest capture objects and already-detached streams cannot be
        # reconfigured; the file handler below is the durable record anyway.
        pass


def configure_logging(
    component: str = "server",
    *,
    log_dir: str | Path | None = None,
    max_bytes: int | None = None,
    backup_count: int | None = None,
    console: bool | None = None,
) -> Path:
    """Attach a rotating UTF-8 file handler (and console handler) to the root logger.

    Idempotent for the same target file so both ``backend.main`` and a reload
    supervisor can call it without duplicating records. Returns the log file
    path, whether or not the filesystem allowed creating it.
    """
    directory = Path(log_dir).expanduser().absolute() if log_dir is not None else config.get_log_dir()
    path = directory / f"{LOG_FILE_PREFIX}-{_safe_component(component)}.log"

    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, _HANDLER_MARKER, None) == str(path):
            return path

    root.setLevel(logging.INFO)
    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    use_console = _console_enabled() if console is None else console

    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=max_bytes if max_bytes is not None else _positive_int_setting("BEEBOX_LOG_MAX_BYTES", DEFAULT_MAX_BYTES),
            backupCount=backup_count
            if backup_count is not None
            else _positive_int_setting("BEEBOX_LOG_BACKUP_COUNT", DEFAULT_BACKUP_COUNT),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"BeeBox logging: cannot write {path}: {exc}", file=sys.stderr)
        handler = None
    if handler is not None:
        handler.setFormatter(formatter)
        setattr(handler, _HANDLER_MARKER, str(path))
        root.addHandler(handler)

    if use_console:
        _prepare_console_stream()
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        setattr(console_handler, _HANDLER_MARKER, "console")
        root.addHandler(console_handler)

    return path


def reset_logging() -> None:
    """Detach every handler installed by :func:`configure_logging` (tests, reloads)."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARKER, None) is None:
            continue
        root.removeHandler(handler)
        handler.close()

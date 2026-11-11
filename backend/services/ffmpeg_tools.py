from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from backend.services.process_utils import no_window_kwargs

ToolSource = Literal["manual", "bundled", "system", "missing"]
ToolName = Literal["ffmpeg", "ffprobe", "deno"]

_TOOL_ENV_NAMES: dict[str, str] = {
    "ffmpeg": "BEEBOX_FFMPEG_PATH",
    "ffprobe": "BEEBOX_FFPROBE_PATH",
    "deno": "BEEBOX_DENO_PATH",
}
_TOOL_VERSION_ARGUMENTS: dict[str, str] = {
    "ffmpeg": "-version",
    "ffprobe": "-version",
    "deno": "--version",
}


@dataclass(frozen=True)
class ToolStatus:
    available: bool
    path: str | None
    source: ToolSource
    version: str | None = None
    error: str | None = None


def _probe_version(path: str, argument: str) -> tuple[str | None, str | None]:
    try:
        completed = subprocess.run([path, argument], capture_output=True, check=True, encoding="utf-8", errors="replace", timeout=8, **no_window_kwargs())
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    line = (completed.stdout or completed.stderr or "").splitlines()
    return (line[0] if line else None), None


def _version(path: str) -> tuple[str | None, str | None]:
    return _probe_version(path, "-version")


def _candidate(
    path: str | None,
    source: ToolSource,
    *,
    check_version: bool,
    version_argument: str = "-version",
) -> ToolStatus | None:
    if not path:
        return None
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        return ToolStatus(False, str(resolved), source, error="file does not exist")
    if not os.access(resolved, os.X_OK):
        return ToolStatus(False, str(resolved), source, error="file is not executable")
    version = None
    if check_version:
        if version_argument == "-version":
            version, error = _version(str(resolved))
        else:
            version, error = _probe_version(str(resolved), version_argument)
        if error:
            return ToolStatus(False, str(resolved), source, error=error)
    return ToolStatus(True, str(resolved), source, version=version)


def _manual_paths() -> tuple[str | None, str | None]:
    try:
        from backend.database.session import SessionLocal, init_db
        from backend.services import settings as settings_service

        init_db()
        db = SessionLocal()
        try:
            settings = settings_service.get_settings(db)
            return settings.ffmpeg_path, settings.ffprobe_path
        finally:
            db.close()
    except Exception:
        return None, None


def get_tools_dir() -> Path | None:
    """Return the fixed tools directory (``BEEBOX_TOOLS_DIR`` or ``<root>\\tools``).

    The Windows installer places ffmpeg/ffprobe and Deno below this directory
    and also writes their absolute paths into ``secrets\\settings.env``. This
    fallback keeps the service working when only the root directory is
    configured, and never depends on winget's versioned install paths.
    """
    explicit = os.environ.get("BEEBOX_TOOLS_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser().absolute()
    root = os.environ.get("BEEBOX_ROOT", "").strip()
    if root:
        return Path(root).expanduser().absolute() / "tools"
    return None


def _tools_dir_candidates(name: str) -> list[Path]:
    base = get_tools_dir()
    if base is None:
        return []
    filenames = (f"{name}.exe", name)
    directories = [base / name, base / name / "bin"]
    if name in {"ffmpeg", "ffprobe"}:
        # The official ffmpeg archive ships both binaries in one directory,
        # which the installer unpacks to <root>\tools\ffmpeg.
        directories.extend([base / "ffmpeg", base / "ffmpeg" / "bin"])
    directories.append(base / "bin")
    candidates: list[Path] = []
    for directory in directories:
        for filename in filenames:
            candidates.append(directory / filename)
    return candidates


def _attempts(name: ToolName, manual_path: str | None, *, check_version: bool) -> list[ToolStatus]:
    version_argument = _TOOL_VERSION_ARGUMENTS[name]
    statuses: list[ToolStatus] = []
    manual = _candidate(manual_path, "manual", check_version=check_version, version_argument=version_argument)
    if manual is not None:
        statuses.append(manual)
    bundled = _candidate(
        os.environ.get(_TOOL_ENV_NAMES[name]),
        "bundled",
        check_version=check_version,
        version_argument=version_argument,
    )
    if bundled is not None:
        statuses.append(bundled)
    for candidate in _tools_dir_candidates(name):
        status = _candidate(str(candidate), "bundled", check_version=check_version, version_argument=version_argument)
        if status is not None:
            statuses.append(status)
    system = _candidate(shutil.which(name), "system", check_version=check_version, version_argument=version_argument)
    if system is not None:
        statuses.append(system)
    return statuses


def _first_usable(name: ToolName, attempts: list[ToolStatus]) -> ToolStatus:
    first_error: ToolStatus | None = None
    for attempt in attempts:
        if attempt.available:
            return attempt
        if first_error is None:
            first_error = attempt
    if first_error is not None:
        return ToolStatus(False, first_error.path, first_error.source, error=first_error.error)
    return ToolStatus(False, None, "missing", error=f"{name} was not found")


def resolve_tool(
    name: Literal["ffmpeg", "ffprobe"],
    manual_path: str | None = None,
    *,
    check_version: bool = True,
) -> ToolStatus:
    return _first_usable(name, _attempts(name, manual_path, check_version=check_version))


def resolve_deno(manual_path: str | None = None, *, check_version: bool = True) -> ToolStatus:
    """Resolve the Deno executable used by yt-dlp's JavaScript runtime.

    Resolution order matches ffmpeg: manual path, ``BEEBOX_DENO_PATH``, the
    fixed tools directory, then ``PATH``. Deno is optional for transcription;
    the downloader (a later ticket) decides whether to require it.
    """
    return _first_usable("deno", _attempts("deno", manual_path, check_version=check_version))


def resolve_tools(*, check_version: bool = True) -> dict[str, ToolStatus]:
    ffmpeg_path, ffprobe_path = _manual_paths()
    return {
        "ffmpeg": resolve_tool("ffmpeg", ffmpeg_path, check_version=check_version),
        "ffprobe": resolve_tool("ffprobe", ffprobe_path, check_version=check_version),
    }

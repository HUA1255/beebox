from __future__ import annotations

from pathlib import Path

from backend.services import ffmpeg_tools
from backend.services.ffmpeg_tools import resolve_deno, resolve_tool


def _tool(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return str(path)


def test_deno_path_env_is_used(tmp_path: Path, monkeypatch):
    deno = _tool(tmp_path / "deno" / "deno.exe")
    monkeypatch.setenv("BEEBOX_DENO_PATH", deno)

    status = resolve_deno(check_version=False)

    assert status.available is True
    assert status.path == deno
    assert status.source == "bundled"


def test_ffmpeg_is_found_in_the_tools_directory(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("BEEBOX_FFMPEG_PATH", raising=False)
    monkeypatch.delenv("BEEBOX_TOOLS_DIR", raising=False)
    monkeypatch.delenv("BEEBOX_ROOT", raising=False)
    ffmpeg = _tool(tmp_path / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe")
    monkeypatch.setenv("BEEBOX_TOOLS_DIR", str(tmp_path / "tools"))
    monkeypatch.setattr(ffmpeg_tools.shutil, "which", lambda _name: None)

    status = resolve_tool("ffmpeg", check_version=False)

    assert status.available is True
    assert status.path == ffmpeg
    assert status.source == "bundled"


def test_tools_directory_defaults_to_beebox_root(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("BEEBOX_FFPROBE_PATH", raising=False)
    monkeypatch.delenv("BEEBOX_TOOLS_DIR", raising=False)
    ffprobe = _tool(tmp_path / "tools" / "ffmpeg" / "bin" / "ffprobe.exe")
    monkeypatch.setenv("BEEBOX_ROOT", str(tmp_path))
    monkeypatch.setattr(ffmpeg_tools.shutil, "which", lambda _name: None)

    status = resolve_tool("ffprobe", check_version=False)

    assert status.available is True
    assert status.path == ffprobe


def test_deno_is_found_in_the_tools_directory(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("BEEBOX_DENO_PATH", raising=False)
    monkeypatch.delenv("BEEBOX_ROOT", raising=False)
    deno = _tool(tmp_path / "tools" / "deno" / "deno.exe")
    monkeypatch.setenv("BEEBOX_TOOLS_DIR", str(tmp_path / "tools"))
    monkeypatch.setattr(ffmpeg_tools.shutil, "which", lambda _name: None)

    status = resolve_deno(check_version=False)

    assert status.available is True
    assert status.path == deno
    assert status.source == "bundled"


def test_explicit_env_path_wins_over_tools_directory(tmp_path: Path, monkeypatch):
    explicit = _tool(tmp_path / "custom" / "ffmpeg.exe")
    _tool(tmp_path / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe")
    monkeypatch.setenv("BEEBOX_TOOLS_DIR", str(tmp_path / "tools"))
    monkeypatch.setenv("BEEBOX_FFMPEG_PATH", explicit)
    monkeypatch.setattr(ffmpeg_tools.shutil, "which", lambda _name: None)

    status = resolve_tool("ffmpeg", check_version=False)

    assert status.path == explicit


def test_missing_deno_reports_missing(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("BEEBOX_DENO_PATH", raising=False)
    monkeypatch.delenv("BEEBOX_TOOLS_DIR", raising=False)
    monkeypatch.delenv("BEEBOX_ROOT", raising=False)
    monkeypatch.setattr(ffmpeg_tools.shutil, "which", lambda _name: None)

    status = resolve_deno(check_version=False)

    assert status.available is False
    assert status.source == "missing"

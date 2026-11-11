from __future__ import annotations

import logging
from pathlib import Path

from backend import logging_setup


def _emit(name: str, message: str) -> None:
    logging.getLogger(name).info(message)


def test_configure_logging_writes_utf8_to_log_dir(tmp_path: Path):
    log_path = logging_setup.configure_logging("server", log_dir=tmp_path, console=False)

    _emit("beebox.test", "转写任务完成：中文日志")
    logging_setup.reset_logging()

    assert log_path.parent == tmp_path
    assert log_path.name == "beebox-server.log"
    assert "转写任务完成：中文日志" in log_path.read_text(encoding="utf-8")
    raw = log_path.read_bytes()
    assert "转写任务完成：中文日志".encode("utf-8") in raw
    assert not raw.startswith(b"\xef\xbb\xbf")


def test_rotating_file_handler_rolls_over(tmp_path: Path):
    log_path = logging_setup.configure_logging(
        "worker",
        log_dir=tmp_path,
        max_bytes=400,
        backup_count=2,
        console=False,
    )

    for index in range(30):
        _emit("beebox.test", f"rotation-line-{index:02d}-" + "x" * 40)
    logging_setup.reset_logging()

    rotated = list(tmp_path.glob("beebox-worker.log.*"))
    assert rotated, "expected at least one rotated log file"
    assert log_path.exists()
    assert log_path.stat().st_size <= 400
    assert not any(path.name.endswith(".3") for path in rotated)
    assert "rotation-line-29-" in log_path.read_text(encoding="utf-8")


def test_log_dir_defaults_to_beebox_log_dir_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BEEBOX_LOG_DIR", str(tmp_path / "service-logs"))

    log_path = logging_setup.configure_logging("server", console=False)
    logging_setup.reset_logging()

    assert log_path == tmp_path / "service-logs" / "beebox-server.log"
    assert log_path.exists()


def test_log_dir_defaults_to_root_logs_when_only_root_is_set(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("BEEBOX_LOG_DIR", raising=False)
    monkeypatch.setenv("BEEBOX_ROOT", str(tmp_path))

    log_path = logging_setup.configure_logging("server", console=False)
    logging_setup.reset_logging()

    assert log_path == tmp_path / "logs" / "beebox-server.log"
    assert log_path.exists()

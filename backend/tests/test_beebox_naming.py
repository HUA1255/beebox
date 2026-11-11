"""BeeBox naming contract: public title, database filename, env prefix."""

from __future__ import annotations

import os

from fastapi.testclient import TestClient

from backend import config


def _clear_env_prefix(monkeypatch, prefix: str) -> None:
    for name in list(os.environ):
        if name.startswith(prefix):
            monkeypatch.delenv(name, raising=False)


def test_api_is_presented_as_beebox(monkeypatch, tmp_path) -> None:
    _clear_env_prefix(monkeypatch, "BEEBOX_")
    _clear_env_prefix(monkeypatch, "ASRBOX_")
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))

    from backend.app import create_app

    app = create_app()
    assert app.title == "BeeBox API"
    assert "BeeBox" in app.description

    with TestClient(app) as client:
        assert client.get("/v1/health").json()["online"] is True


def test_default_database_filename_is_beebox_db(monkeypatch, tmp_path) -> None:
    _clear_env_prefix(monkeypatch, "BEEBOX_")
    _clear_env_prefix(monkeypatch, "ASRBOX_")
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))

    assert config.get_db_path() == tmp_path / "beebox.db"


def test_legacy_asrbox_data_dir_is_not_read(monkeypatch, tmp_path) -> None:
    _clear_env_prefix(monkeypatch, "BEEBOX_")
    _clear_env_prefix(monkeypatch, "ASRBOX_")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASRBOX_DATA_DIR", str(tmp_path / "legacy-data"))

    assert config.get_data_dir() == (tmp_path / "data").resolve()
    assert not (tmp_path / "legacy-data").exists()


def test_legacy_asrbox_api_token_is_not_read(monkeypatch, tmp_path) -> None:
    _clear_env_prefix(monkeypatch, "BEEBOX_")
    _clear_env_prefix(monkeypatch, "ASRBOX_")
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ASRBOX_API_TOKEN", "legacy-token")

    from backend.app import create_app

    with TestClient(create_app()) as client:
        assert client.get("/v1/files").status_code == 200

    monkeypatch.delenv("ASRBOX_API_TOKEN")
    monkeypatch.setenv("BEEBOX_API_TOKEN", "current-token")
    with TestClient(create_app()) as client:
        assert client.get("/v1/files").status_code == 401

"""Ticket #49: BeeBox serves the console build itself under ``/console``.

The assertions are all over HTTP: the built shell is public (the login page
must load before a token exists), client-side routes fall back to
``index.html``, and every ``/v1`` call keeps its Bearer requirement.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

INDEX_HTML = "<!doctype html><html><body><div id=\"root\"></div></body></html>"
CONSOLE_TOKEN = "console-static-token"


def _write_build(root: Path) -> Path:
    dist = root / "console-dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log('beebox console');", encoding="utf-8")
    return dist


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BEEBOX_MODEL_STORAGE_ROOT", str(tmp_path / "models"))
    monkeypatch.setenv("BEEBOX_API_TOKEN", CONSOLE_TOKEN)
    monkeypatch.setenv("BEEBOX_CONSOLE_DIR", str(_write_build(tmp_path)))
    from backend.app import create_app

    return TestClient(create_app())


def test_console_root_serves_the_built_shell_without_a_token(client: TestClient) -> None:
    response = client.get("/console")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    assert 'id="root"' in response.text


def test_console_assets_are_served_with_their_own_content_type(client: TestClient) -> None:
    response = client.get("/console/assets/app.js")

    assert response.status_code == 200, response.text
    assert "javascript" in response.headers["content-type"]
    assert "beebox console" in response.text


def test_console_client_route_falls_back_to_the_shell(client: TestClient) -> None:
    response = client.get("/console/jobs/6b0f1a2c")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    assert 'id="root"' in response.text


def test_console_cannot_escape_the_build_directory(client: TestClient, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("outside the build", encoding="utf-8")

    response = client.get("/console/%2e%2e/secret.txt")

    assert response.status_code == 404
    assert "outside the build" not in response.text


def test_v1_keeps_requiring_the_bearer_token_while_console_is_public(client: TestClient) -> None:
    assert client.get("/console").status_code == 200
    assert client.get("/v1/jobs").status_code == 401
    authorized = client.get("/v1/jobs", headers={"Authorization": f"Bearer {CONSOLE_TOKEN}"})
    assert authorized.status_code == 200


def test_missing_build_explains_how_to_build_the_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BEEBOX_CONSOLE_DIR", str(tmp_path / "not-built"))
    from backend.app import create_app

    response = TestClient(create_app()).get("/console")

    assert response.status_code == 404
    assert "npm run build" in response.text

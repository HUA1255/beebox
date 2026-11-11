"""开发实例隔离（ticket #55）：路径守卫与只读共享模型目录。

断言只走两个公开边界：

* ``create_app()`` 的启动校验 —— 开发实例配置越界时拒绝启动；
* ``/v1/models`` 的 HTTP 行为 —— 开发实例不能改动共享的生产模型目录。

环境变量是文档化的配置契约（与 ``BEEBOX_DATA_DIR`` 等一致），不是对内部函数的替身。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.app import create_app


def _dev_instance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    prod_root = tmp_path / "BeeBox"
    dev_root = tmp_path / "BeeBoxDev"
    (prod_root / "models").mkdir(parents=True)
    (prod_root / "data").mkdir(parents=True)
    (dev_root / "data").mkdir(parents=True)
    monkeypatch.setenv("BEEBOX_DEV_INSTANCE", "1")
    monkeypatch.setenv("BEEBOX_PROD_ROOT", str(prod_root))
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(dev_root / "data"))
    monkeypatch.setenv("BEEBOX_LOG_DIR", str(dev_root / "logs"))
    monkeypatch.setenv("BEEBOX_MODEL_STORAGE_ROOT", str(prod_root / "models"))
    monkeypatch.setenv("BEEBOX_MODEL_CACHE_ROOT", str(dev_root / "model-cache"))
    monkeypatch.delenv("BEEBOX_API_TOKEN", raising=False)
    return prod_root, dev_root


def test_dev_instance_starts_with_isolated_data_and_shared_models(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prod_root, dev_root = _dev_instance(monkeypatch, tmp_path)

    client = TestClient(create_app())
    response = client.get("/v1/health")

    assert response.status_code == 200
    assert (dev_root / "data").is_dir()
    assert (prod_root / "data").is_dir()


def test_dev_instance_refuses_production_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    prod_root, _dev_root = _dev_instance(monkeypatch, tmp_path)
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(prod_root / "data"))

    with pytest.raises(RuntimeError, match="BEEBOX_DATA_DIR"):
        create_app()


def test_dev_instance_refuses_model_root_inside_production_data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    prod_root, _dev_root = _dev_instance(monkeypatch, tmp_path)
    monkeypatch.setenv("BEEBOX_MODEL_STORAGE_ROOT", str(prod_root / "data" / "models"))

    with pytest.raises(RuntimeError, match="BEEBOX_MODEL_STORAGE_ROOT"):
        create_app()


def test_dev_instance_refuses_cache_root_inside_production_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    prod_root, _dev_root = _dev_instance(monkeypatch, tmp_path)
    monkeypatch.setenv("BEEBOX_MODEL_CACHE_ROOT", str(prod_root / "models" / "cache"))

    with pytest.raises(RuntimeError, match="BEEBOX_MODEL_CACHE_ROOT"):
        create_app()


def test_dev_instance_requires_an_explicit_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _dev_instance(monkeypatch, tmp_path)
    monkeypatch.delenv("BEEBOX_DATA_DIR")

    with pytest.raises(RuntimeError, match="BEEBOX_DATA_DIR"):
        create_app()


def test_dev_instance_refuses_a_non_loopback_bind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _dev_instance(monkeypatch, tmp_path)

    with pytest.raises(RuntimeError, match="loopback"):
        config.validate_bind_security("0.0.0.0")


def test_dev_instance_refuses_model_download_into_shared_storage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _dev_instance(monkeypatch, tmp_path)

    response = TestClient(create_app()).post("/v1/models/whisper-base/download")

    assert response.status_code == 409
    assert response.json()["error_code"] == "MODEL_STORAGE_READ_ONLY"


def test_dev_instance_refuses_model_deletion_from_shared_storage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _dev_instance(monkeypatch, tmp_path)
    shared_model = tmp_path / "BeeBox" / "models" / "models" / "whisper-base"
    shared_model.mkdir(parents=True)
    (shared_model / "config.json").write_text("{}", encoding="utf-8")

    response = TestClient(create_app()).delete("/v1/models/whisper-base")

    assert response.status_code == 409
    assert response.json()["error_code"] == "MODEL_STORAGE_READ_ONLY"
    assert shared_model.is_dir()

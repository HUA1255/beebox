"""``/v1/models``: model catalog, download lifecycle and VRAM release (#41).

Every registered model is reported with the same facts, whatever its engine:

* ``downloaded`` -- a completed install is recorded (``model.json``);
* ``complete`` -- every file the engine needs is on disk;
* ``loaded`` -- inference state is resident and holding VRAM;
* ``size_bytes`` -- what the model directory currently occupies;
* ``download`` -- the in-process download state machine, including byte
  progress, so a caller can poll instead of subscribing to an event stream.

Downloads, cancellation, retry, deletion and unloading reuse the existing
storage and loading services. Unloading goes through
:mod:`backend.services.speech_engine`, the same boundary transcription uses,
so the engine drops its resident state instead of only being asked to.

``BEEBOX_MODEL_SOURCE_DIR`` optionally names a local archive laid out as
``<root>/<model-id>/``; a download then copies from it instead of the network.
That is what offline deployments and the test suite use.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Response

from backend.backends import ASRModelConfig, get_all_model_configs, get_model_config
from backend.services import model_storage
from backend.services import models as model_service
from backend.services import speech_engine
from backend.v1.errors import ApiError, ErrorResponse
from backend.v1.schemas import ModelDownloadState, ModelInfo, ModelListResponse

router = APIRouter(prefix="/v1/models", tags=["v1"])

SETTLE_TIMEOUT_SECONDS = 5.0
SETTLE_POLL_SECONDS = 0.02


def _model_config(model_id: str) -> ASRModelConfig:
    model_config = get_model_config(model_id)
    if model_config is None:
        raise ApiError(404, f"Model {model_id} not found", "MODEL_NOT_FOUND", "model")
    return model_config


def _model_info(model_config: ASRModelConfig, storage_status: str) -> ModelInfo:
    report = model_service.inspect_model(model_config.model_name)
    return ModelInfo(
        id=model_config.model_name,
        name=model_config.display_name,
        engine=model_config.engine,
        source=model_config.source,
        repo_id=model_config.repo_id,
        size_bytes=report["size_bytes"],
        downloaded=report["downloaded"],
        complete=report["complete"],
        missing_files=report["missing_files"],
        loaded=speech_engine.get_speech_engine().is_model_loaded(model_config.model_name),
        download=ModelDownloadState(**model_service.download_state(model_config.model_name)),
        storage_status=storage_status,
    )


def _current_info(model_config: ASRModelConfig) -> ModelInfo:
    storage = model_storage.inspect_storage(include_usage=False)
    return _model_info(model_config, storage["status"])


def _wait_for_download_to_settle(model_id: str) -> bool:
    """Wait out a cancellation; ``False`` when the worker is still running."""
    deadline = time.monotonic() + SETTLE_TIMEOUT_SECONDS
    while model_service.is_model_downloading(model_id):
        if time.monotonic() >= deadline:
            return False
        time.sleep(SETTLE_POLL_SECONDS)
    return True


def _reject_downloading(model_id: str) -> None:
    if model_service.is_model_downloading(model_id):
        raise ApiError(
            409,
            f"Model {model_id} is already downloading",
            "MODEL_DOWNLOAD_IN_PROGRESS",
            "model",
        )


def _drop_stale_install(model_id: str) -> None:
    """A recorded but incomplete install must be replaced, not skipped."""
    report = model_service.inspect_model(model_id)
    if report["downloaded"] and not report["complete"]:
        model_service.delete_model(model_id)


@router.get("", response_model=ModelListResponse)
def list_models() -> ModelListResponse:
    storage = model_storage.inspect_storage(include_usage=False)
    items = [_model_info(config, storage["status"]) for config in get_all_model_configs()]
    return ModelListResponse(items=items, total=len(items))


@router.get(
    "/{model_id}",
    response_model=ModelInfo,
    responses={404: {"model": ErrorResponse}},
)
def get_model(model_id: str) -> ModelInfo:
    return _current_info(_model_config(model_id))


@router.post(
    "/{model_id}/download",
    response_model=ModelInfo,
    status_code=202,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def start_download(model_id: str) -> ModelInfo:
    model_config = _model_config(model_id)
    report = model_service.inspect_model(model_id)
    if report["downloaded"] and report["complete"]:
        raise ApiError(
            409,
            f"Model {model_id} is already downloaded",
            "MODEL_ALREADY_DOWNLOADED",
            "model",
        )
    _reject_downloading(model_id)
    _drop_stale_install(model_id)
    try:
        model_service.download_model(model_id)
    except ValueError as exc:
        raise ApiError(409, str(exc), "MODEL_NOT_DOWNLOADABLE", "model") from exc
    return _current_info(model_config)


@router.post(
    "/{model_id}/download/cancel",
    response_model=ModelInfo,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def cancel_download(model_id: str) -> ModelInfo:
    model_config = _model_config(model_id)
    if not model_service.cancel_download(model_id):
        raise ApiError(
            409,
            f"Model {model_id} is not downloading",
            "MODEL_NOT_DOWNLOADING",
            "model",
        )
    return _current_info(model_config)


@router.post(
    "/{model_id}/download/retry",
    response_model=ModelInfo,
    status_code=202,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def retry_download(model_id: str) -> ModelInfo:
    model_config = _model_config(model_id)
    report = model_service.inspect_model(model_id)
    if report["downloaded"] and report["complete"]:
        raise ApiError(
            409,
            f"Model {model_id} is already downloaded",
            "MODEL_ALREADY_DOWNLOADED",
            "model",
        )
    # A cancellation is asynchronous: wait for the worker to drop the model
    # before starting it again, otherwise the start is silently ignored.
    if model_service.download_state(model_id)["state"] in {"cancelling", "cancelled"}:
        if not _wait_for_download_to_settle(model_id):
            _reject_downloading(model_id)
    else:
        _reject_downloading(model_id)
    _drop_stale_install(model_id)
    try:
        model_service.retry_download(model_id)
    except ValueError as exc:
        raise ApiError(409, str(exc), "MODEL_NOT_DOWNLOADABLE", "model") from exc
    return _current_info(model_config)


@router.post(
    "/{model_id}/unload",
    response_model=ModelInfo,
    responses={404: {"model": ErrorResponse}},
)
def unload_model(model_id: str) -> ModelInfo:
    model_config = _model_config(model_id)
    model_service.unload_model(model_id)
    return _current_info(model_config)


@router.delete(
    "/{model_id}",
    status_code=204,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def delete_model(model_id: str) -> Response:
    _model_config(model_id)
    if model_service.is_model_downloading(model_id):
        model_service.cancel_download(model_id)
        if not _wait_for_download_to_settle(model_id):
            _reject_downloading(model_id)
    model_service.delete_model(model_id)
    return Response(status_code=204)


__all__ = ["router"]

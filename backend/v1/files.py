"""``/v1/files``: stream uploads to disk and serve them back.

The upload path never holds the whole body in memory: Starlette spools
multipart parts to disk, and this module copies them in 4 MiB reads while
hashing and enforcing the configured upload limit. File metadata lives in the
``v1_files`` table so query/delete do not depend on directory listings.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import BinaryIO

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from backend import config
from backend.database import get_db
from backend.database.models import TranscriptionTask, V1File
from backend.services.tasks import ACTIVE_TASK_STATUSES
from backend.v1.errors import ApiError, ErrorResponse
from backend.v1.schemas import FileListResponse, FileMetadata
from backend.services.uploads import UploadLimitExceeded, max_upload_bytes

router = APIRouter(
    prefix="/v1/files",
    tags=["v1"],
    responses={401: {"model": ErrorResponse}},
)

STREAM_CHUNK_SIZE = 4 * 1024 * 1024
_MAX_SUFFIX_LENGTH = 16


def _safe_suffix(filename: str | None) -> str:
    suffix = Path(filename or "").suffix
    if len(suffix) > _MAX_SUFFIX_LENGTH or not suffix.startswith("."):
        return ""
    if not all(character.isalnum() or character == "." for character in suffix):
        return ""
    return suffix


def _download_url(file_id: str) -> str:
    return f"/v1/files/{file_id}/content"


def _metadata(row: V1File) -> FileMetadata:
    return FileMetadata(
        id=row.id,
        filename=row.filename,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        created_at=row.created_at,
        download_url=_download_url(row.id),
    )


def _store_stream(file_obj: BinaryIO, destination: Path) -> tuple[int, str]:
    """Copy ``file_obj`` to ``destination`` in chunks, hashing as it goes."""
    limit = max_upload_bytes()
    digest = hashlib.sha256()
    written = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("xb") as output:
            while chunk := file_obj.read(STREAM_CHUNK_SIZE):
                written += len(chunk)
                if written > limit:
                    raise UploadLimitExceeded(f"Upload exceeds the {limit} byte file limit")
                digest.update(chunk)
                output.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    if written == 0:
        destination.unlink(missing_ok=True)
        raise ValueError("Uploaded file is empty")
    return written, digest.hexdigest()


def _file_row(db: Session, file_id: str) -> V1File:
    row = db.get(V1File, file_id)
    if row is None:
        raise ApiError(404, f"File {file_id} not found", "FILE_NOT_FOUND", "file")
    return row


def _stored_path(row: V1File) -> Path:
    path = config.resolve_storage_path(row.storage_path)
    if path is None or not path.is_file():
        raise ApiError(404, f"File {row.id} content is no longer stored", "FILE_NOT_FOUND", "file")
    return path


@router.post(
    "",
    response_model=FileMetadata,
    status_code=201,
    responses={
        400: {"model": ErrorResponse},
        413: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
)
def upload_file(file: UploadFile = File(...), db: Session = Depends(get_db)) -> FileMetadata:
    file_id = str(uuid.uuid4())
    destination = config.get_uploads_dir() / "v1" / f"{file_id}{_safe_suffix(file.filename)}"
    try:
        size_bytes, sha256 = _store_stream(file.file, destination)
    except UploadLimitExceeded as exc:
        raise ApiError(413, str(exc), "FILE_TOO_LARGE", "file") from exc
    except ValueError as exc:
        raise ApiError(400, str(exc), "EMPTY_FILE", "file") from exc

    row = V1File(
        id=file_id,
        filename=file.filename or "upload",
        content_type=file.content_type,
        size_bytes=size_bytes,
        sha256=sha256,
        storage_path=config.to_storage_path(destination),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _metadata(row)


@router.get("", response_model=FileListResponse, responses={422: {"model": ErrorResponse}})
def list_files(
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> FileListResponse:
    query = db.query(V1File).order_by(V1File.created_at.desc(), V1File.id.desc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    return FileListResponse(items=[_metadata(row) for row in rows], total=total)


@router.get("/{file_id}", response_model=FileMetadata, responses={404: {"model": ErrorResponse}})
def get_file(file_id: str, db: Session = Depends(get_db)) -> FileMetadata:
    return _metadata(_file_row(db, file_id))


def _download(file_id: str, db: Session) -> FileResponse:
    row = _file_row(db, file_id)
    path = _stored_path(row)
    return FileResponse(path, media_type=row.content_type or "application/octet-stream", filename=row.filename)


@router.get(
    "/{file_id}/content",
    response_class=FileResponse,
    responses={404: {"model": ErrorResponse}},
)
def download_file(file_id: str, db: Session = Depends(get_db)) -> FileResponse:
    return _download(file_id, db)


@router.head(
    "/{file_id}/content",
    response_class=FileResponse,
    responses={404: {"model": ErrorResponse}},
)
def download_file_head(file_id: str, db: Session = Depends(get_db)) -> FileResponse:
    # HEAD lets a headerless client probe size/type before requesting a
    # download ticket for GET.
    return _download(file_id, db)


@router.delete("/{file_id}", status_code=204, responses={404: {"model": ErrorResponse}})
def delete_file(file_id: str, db: Session = Depends(get_db)) -> Response:
    row = _file_row(db, file_id)
    if _file_has_active_job(db, file_id):
        raise ApiError(
            409,
            f"File {file_id} is still used by a queued or running job",
            "FILE_IN_USE",
            "file",
        )
    path = config.resolve_storage_path(row.storage_path)
    db.delete(row)
    db.commit()
    if path is not None:
        path.unlink(missing_ok=True)
    return Response(status_code=204)


def _file_has_active_job(db: Session, file_id: str) -> bool:
    """A file may only disappear once every job using it has settled."""
    candidates = (
        db.query(TranscriptionTask)
        .filter(TranscriptionTask.options_json.contains(file_id))
        .all()
    )
    for task in candidates:
        try:
            options = json.loads(task.options_json or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(options, dict) and options.get("_v1_file_id") == file_id:
            if task.status in ACTIVE_TASK_STATUSES:
                return True
    return False

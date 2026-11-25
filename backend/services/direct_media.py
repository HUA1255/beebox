"""Direct media links (``source.type = "direct"``, ticket #32).

A direct source is an absolute http(s) URL that points straight at an audio or
video file instead of at a platform page. The job downloads it outside the GPU
queue, validates it, and then re-enters the ordinary transcription queue with
the downloaded file as BeeBox-owned temporary media (ADR-0011), exactly like
the audio path of a video-link job.

Ticket #32 established the security baseline: every resolved address must be
globally routable. Ticket #33 follows a bounded number of redirects (CDN and
signed temporary links rely on them) while applying the same check to every
hop: the scheme, host and every resolved address are validated before the hop
is requested, relative locations are resolved against the current URL, and a
loop or an exhausted hop budget fails the job without further requests.

Two module-level seams keep every test offline:

``resolve_host(host, port)``
    Returns every address the host resolves to. Production uses
    :func:`socket.getaddrinfo`; tests replace it.
``get_http_transport()``
    Returns the object that performs one HTTP GET. Production uses
    :class:`RequestsHttpTransport`; tests replace it.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import uuid
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

import requests
from sqlalchemy.orm import Session

from backend import config
from backend.database import session as db_session
from backend.database.models import TranscriptionTask
from backend.services import task_logs
from backend.services import task_runtime
from backend.services import tasks as task_service
from backend.services import uploads
from backend.services import video_jobs
from backend.services.errors import BeeBoxError
from backend.services.media import probe_media
from backend.services.task_options import DEFAULT_OUTPUT_FORMATS, read_task_options
from backend.services.video_platform import (
    TEXT_PATH_ASR,
    TEXT_PATH_SUBTITLE,
    TEXT_SOURCE_ASR,
)
from backend.services.video_platform.config import download_options
from backend.services.video_platform.errors import (
    DownloadNetworkError,
    DownloadTimeoutError,
    InvalidUrlError,
)

logger = logging.getLogger(__name__)

DIRECT_SOURCE_TYPE = "direct"
DOWNLOAD_CHUNK_SIZE = 64 * 1024
#: How many redirect hops a direct download may follow before it fails.
MAX_REDIRECTS = 5

_PORTS = {"http": 80, "https": 443}
_MEDIA_SUFFIXES = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/wave": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/ogg": ".ogg",
    "audio/opus": ".ogg",
    "audio/mp4": ".m4a",
    "audio/m4a": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".m4a",
    "audio/webm": ".webm",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "video/mkv": ".mkv",
}
_SUPPORTED_SUFFIXES = (".mp4", ".mov", ".mkv", ".webm", ".mp3", ".wav", ".m4a", ".flac", ".ogg")
_EXPLICIT_NON_MEDIA_TYPES = frozenset(
    {
        "application/javascript",
        "application/pdf",
        "application/xhtml+xml",
        "application/xml",
        "application/json",
    }
)


class DirectSourceError(BeeBoxError):
    """Base class for direct-source failures; subclasses carry public codes."""

    code = "DIRECT_URL_NOT_ALLOWED"

    def __init__(self, message: str, *, stage: str = "download") -> None:
        super().__init__(self.code, message, stage=stage)


class DirectUrlNotAllowedError(DirectSourceError):
    """A hop resolves to a non-public address or the redirect chain is invalid."""

    code = "DIRECT_URL_NOT_ALLOWED"


class DirectMediaFormatError(DirectSourceError):
    """The response is not audio or video."""

    code = "UNSUPPORTED_MEDIA_FORMAT"


class DirectFileTooLargeError(DirectSourceError):
    """The response exceeds ``BEEBOX_MAX_UPLOAD_BYTES``."""

    code = "FILE_TOO_LARGE"


class HttpTransportError(RuntimeError):
    """The HTTP request failed below the status-code layer."""


class HttpTimeout(HttpTransportError):
    """The HTTP request exceeded its deadline."""


class HttpResponse(Protocol):
    """One HTTP response as the download code sees it."""

    status_code: int
    headers: Mapping[str, str]

    def iter_bytes(self) -> Iterator[bytes]: ...

    def close(self) -> None: ...


class HttpTransport(Protocol):
    """One HTTP GET with redirects disabled."""

    def get(self, url: str, *, timeout: float) -> HttpResponse: ...


class _RequestsResponse:
    """Adapt a streamed :class:`requests.Response` to :class:`HttpResponse`."""

    def __init__(self, response: requests.Response) -> None:
        self._response = response
        self.status_code = response.status_code
        self.headers = response.headers

    def iter_bytes(self) -> Iterator[bytes]:
        # A read timeout or a connection reset can surface *after* the status
        # line was received; translate it here so a mid-body failure keeps the
        # same public classification as a failure before the response.
        try:
            yield from self._response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE)
        except requests.Timeout as exc:
            raise HttpTimeout("The direct media download timed out") from exc
        except requests.RequestException as exc:
            raise HttpTransportError("The direct media download failed") from exc

    def close(self) -> None:
        self._response.close()


class RequestsHttpTransport:
    """The production transport: one streamed GET, no redirect following."""

    def __init__(self, *, proxy: str | None = None) -> None:
        self._proxy = proxy

    def get(self, url: str, *, timeout: float) -> HttpResponse:
        proxies = {"http": self._proxy, "https": self._proxy} if self._proxy else None
        try:
            response = requests.get(
                url,
                stream=True,
                allow_redirects=False,
                timeout=timeout,
                proxies=proxies,
                # Only BEEBOX_DOWNLOAD_PROXY decides a proxy, matching the
                # video platform boundary; ambient HTTP(S)_PROXY is ignored.
                trust_env=False,
            )
        except requests.Timeout as exc:
            raise HttpTimeout("The direct media request timed out") from exc
        except requests.RequestException as exc:
            raise HttpTransportError("The direct media request failed") from exc
        return _RequestsResponse(response)


def resolve_host(host: str, port: int) -> tuple[str, ...]:
    """Every address ``host`` resolves to, deduplicated, in resolver order."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for info in infos:
        address = info[4][0]
        if address not in addresses:
            addresses.append(address)
    return tuple(addresses)


def get_http_transport() -> HttpTransport:
    """The transport for one download; tests replace this function."""
    return RequestsHttpTransport(proxy=download_options().proxy)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name:
            return str(value)
    return None


def _content_type(headers: Mapping[str, str]) -> str | None:
    value = _header(headers, "content-type")
    if value is None:
        return None
    media_type = value.split(";", 1)[0].strip().lower()
    return media_type or None


def _media_suffix(content_type: str | None, url: str) -> str:
    if content_type in _MEDIA_SUFFIXES:
        return _MEDIA_SUFFIXES[content_type]
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix in _SUPPORTED_SUFFIXES:
        return suffix
    return ".mp4"


def _is_media_type(content_type: str | None) -> bool:
    return bool(content_type) and content_type.split("/", 1)[0] in {"audio", "video"}


def _is_explicit_non_media(content_type: str | None) -> bool:
    """True for content types that are unambiguously not audio or video.

    Generic types such as ``application/octet-stream`` are not refused here:
    they are downloaded and must pass the ffprobe audio check instead.
    """
    if not content_type or _is_media_type(content_type):
        return False
    if content_type.split("/", 1)[0] in {"text", "image", "multipart", "message"}:
        return True
    return content_type in _EXPLICIT_NON_MEDIA_TYPES or content_type.endswith(
        ("+json", "+xml")
    )


def _require_audio_stream(path: Path) -> None:
    """A response without a media content type must prove it carries audio."""
    try:
        metadata = probe_media(path)
    except BeeBoxError as exc:
        if exc.code == "FFPROBE_FAILED" and "is required" in exc.message:
            # A missing local tool is an environment problem, not a rejection.
            raise
        raise DirectMediaFormatError(
            "The direct link did not return decodable audio or video"
        ) from exc
    if not metadata.get("has_audio_stream"):
        raise DirectMediaFormatError(
            "The direct link returned media without an audio stream"
        )


def parse_direct_url(url: str) -> tuple[str, str, int]:
    """Return ``(scheme, host, port)`` for an absolute http(s) link.

    Raises :class:`InvalidUrlError` (``INVALID_URL``) for everything else; the
    caller answers 400 without writing a job row. No name is resolved here.
    """
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        # urlsplit() rejects malformed IPv6 brackets itself; that is still a
        # caller mistake, not a server failure.
        raise InvalidUrlError(
            "The direct media link is not an absolute http(s) URL",
            stage="download",
        ) from exc
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").strip()
    if scheme not in _PORTS or not host:
        raise InvalidUrlError(
            "The direct media link is not an absolute http(s) URL",
            stage="download",
        )
    try:
        port = parts.port or _PORTS[scheme]
    except ValueError as exc:
        raise InvalidUrlError(
            "The direct media link has an invalid port", stage="download"
        ) from exc
    return scheme, host, port


def _ensure_public_addresses(host: str, port: int) -> None:
    try:
        addresses = resolve_host(host, port)
    except OSError as exc:
        # A resolution failure is a transport problem, not a policy refusal.
        raise HttpTransportError("The direct media host could not be resolved") from exc
    if not addresses:
        raise HttpTransportError("The direct media host could not be resolved")
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise DirectUrlNotAllowedError(
                "The direct media link resolves to an address that is not public"
            ) from exc
        if not parsed.is_global or parsed.is_multicast:
            # ``is_global`` alone still reports some multicast ranges as
            # global; a direct download must never target one.
            raise DirectUrlNotAllowedError(
                "The direct media link resolves to an address that is not public"
            )


def _require_public_host(host: str, port: int) -> None:
    """Resolve ``host`` and refuse it unless every answer is public.

    A resolution failure is a transport problem the caller may retry; a
    non-public answer is a policy refusal that a retry cannot fix.
    """
    try:
        _ensure_public_addresses(host, port)
    except HttpTransportError as exc:
        raise DownloadNetworkError(str(exc), stage="download") from exc


def _redirect_target(current_url: str, location: str | None) -> str:
    """Resolve one ``Location`` header against the URL that answered it."""
    value = (location or "").strip()
    if not value:
        raise DirectUrlNotAllowedError(
            "The direct link redirected without a usable Location header"
        )
    try:
        return urljoin(current_url, value)
    except ValueError as exc:
        raise DirectUrlNotAllowedError(
            "The direct link redirected to a location that is not a valid URL"
        ) from exc


def _parse_redirect_target(target: str) -> tuple[str, str, int]:
    """A redirect target must be absolute http(s) before it is requested."""
    try:
        return parse_direct_url(target)
    except InvalidUrlError as exc:
        raise DirectUrlNotAllowedError(
            "The direct link redirected to a location that is not an absolute "
            "http(s) URL"
        ) from exc


def _open_response(
    url: str,
    transport: HttpTransport,
    *,
    timeout: float,
    is_cancelled: Callable[[], bool] | None,
) -> tuple[HttpResponse, str]:
    """GET ``url``, following at most :data:`MAX_REDIRECTS` validated hops.

    Every hop is parsed, resolved and checked before it is requested, so a
    redirect that points at a non-public address is refused without a request
    to that address. Redirect responses are closed as soon as their target is
    known. The returned response is the first non-3xx answer, paired with the
    URL that served it.
    """
    current_url = url
    visited = {url}
    redirects = 0
    while True:
        if is_cancelled is not None and is_cancelled():
            raise BeeBoxError("TASK_CANCELLED", "Task was cancelled", stage="cancel")
        response = transport.get(current_url, timeout=timeout)
        status = response.status_code
        if not 300 <= status < 400:
            return response, current_url
        location = _header(response.headers, "location")
        response.close()
        if redirects >= MAX_REDIRECTS:
            raise DirectUrlNotAllowedError(
                f"The direct link redirected more than {MAX_REDIRECTS} times"
            )
        target = _redirect_target(current_url, location)
        if target in visited:
            raise DirectUrlNotAllowedError("The direct link redirects form a loop")
        _, host, port = _parse_redirect_target(target)
        _require_public_host(host, port)
        visited.add(target)
        current_url = target
        redirects += 1


def download_direct_media(
    url: str,
    destination: Path,
    *,
    is_cancelled: Callable[[], bool] | None = None,
) -> Path:
    """Download and validate one direct media link; return the stored path.

    Redirects are followed manually with a small cap; every hop is checked
    before it is requested and the final answer goes through the same media
    checks as a redirect-free response. ``is_cancelled`` is polled between
    hops and once per streamed chunk so a cancel stops a large transfer
    instead of waiting for the whole body.
    """
    _, host, port = parse_direct_url(url)
    limit = uploads.max_upload_bytes()
    _require_public_host(host, port)
    transport = get_http_transport()
    response: HttpResponse | None = None
    final_url = url
    try:
        response, final_url = _open_response(
            url,
            transport,
            timeout=download_options().timeout_seconds,
            is_cancelled=is_cancelled,
        )
        status = response.status_code
        if not 200 <= status < 300:
            raise DownloadNetworkError(
                f"The direct media download failed with HTTP {status}",
                stage="download",
            )
        content_type = _content_type(response.headers)
        if _is_explicit_non_media(content_type):
            raise DirectMediaFormatError(
                f"The direct link returned {content_type}, not audio or video"
            )
        declared = _header(response.headers, "content-length")
        if declared is not None:
            try:
                declared_bytes = int(declared)
            except ValueError:
                declared_bytes = None
            if declared_bytes is not None and declared_bytes > limit:
                raise DirectFileTooLargeError(
                    f"The direct media exceeds the {limit} byte limit"
                )
        written = 0
        with destination.open("wb") as output:
            for chunk in response.iter_bytes():
                if is_cancelled is not None and is_cancelled():
                    raise BeeBoxError(
                        "TASK_CANCELLED", "Task was cancelled", stage="cancel"
                    )
                written += len(chunk)
                if written > limit:
                    raise DirectFileTooLargeError(
                        f"The direct media exceeds the {limit} byte limit"
                    )
                output.write(chunk)
        if not _is_media_type(content_type):
            _require_audio_stream(destination)
    except HttpTimeout as exc:
        # A read timeout or reset can arrive mid-body; keep the same
        # classification as a failure before the response.
        destination.unlink(missing_ok=True)
        raise DownloadTimeoutError(
            "The direct media download timed out", stage="download"
        ) from exc
    except HttpTransportError as exc:
        destination.unlink(missing_ok=True)
        raise DownloadNetworkError(
            "The direct media download failed for a network reason", stage="download"
        ) from exc
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        if response is not None:
            response.close()
    final = destination.with_name(
        destination.name + _media_suffix(content_type, final_url)
    )
    destination.replace(final)
    return final


# ---------------------------------------------------------------------------
# Job lifecycle
# ---------------------------------------------------------------------------


def _write_options(row: TranscriptionTask, options: dict[str, Any]) -> None:
    row.options_json = json.dumps(options, ensure_ascii=False)


def _source(options: dict[str, Any]) -> dict[str, Any]:
    value = options.get("_v1_source")
    return dict(value) if isinstance(value, dict) else {}


def _request(options: dict[str, Any]) -> dict[str, Any]:
    value = options.get("_video_request")
    return dict(value) if isinstance(value, dict) else {}


def _display_name(url: str, title: str | None) -> str:
    """A caller-friendly filename: the given title, else the media file name."""
    if isinstance(title, str) and title.strip():
        return title
    parts = urlsplit(url)
    return Path(parts.path).name or parts.hostname or "direct media"


def runs_outside_gpu_queue(row: TranscriptionTask) -> bool:
    """A direct job downloads outside the GPU slot until it has ASR media.

    After a validated download the job sets ``_video_text_path`` to ASR and
    re-enters the GPU queue like any other transcription, so a retried ASR
    failure never re-downloads the link.
    """
    options = read_task_options(row)
    return (
        _source(options).get("type") == DIRECT_SOURCE_TYPE
        and options.get("_video_text_path") != TEXT_PATH_ASR
    )


def _base_options(
    source: dict[str, Any], request: dict[str, Any]
) -> dict[str, Any]:
    return {
        "output_formats": list(DEFAULT_OUTPUT_FORMATS),
        # No audio to diarize until the download hands off; replaced then.
        "diarization": False,
        # Until the download stores one, delete/retention must not unlink a path.
        "_source_kind": "external",
        "_video_text_path": TEXT_PATH_SUBTITLE,
        "_video_request": request,
        "_v1_source": source,
    }


def create_direct_job(
    db: Session,
    *,
    url: str,
    title: str | None = None,
    author: str | None = None,
    platform: str | None = None,
    page_url: str | None = None,
    options: dict[str, Any] | None = None,
) -> TranscriptionTask:
    """Create a queued direct media job; only the URL is required.

    The link is validated offline: a non-absolute or non-http(s) value raises
    ``INVALID_URL`` and no row is written. Nothing is resolved or fetched here;
    the job resolves and downloads it outside the GPU queue.
    """
    value = (url or "").strip()
    parse_direct_url(value)
    request = video_jobs.requested_asr_options(options)
    filename = _display_name(value, title)
    source = {
        "type": DIRECT_SOURCE_TYPE,
        "url": value,
        "title": title,
        "author": author,
        "platform": platform,
        "page_url": page_url,
        "filename": filename,
        # A direct link never has platform subtitles.
        "text_source": TEXT_SOURCE_ASR,
    }
    row = TranscriptionTask(
        id=str(uuid.uuid4()),
        filename=filename,
        source=DIRECT_SOURCE_TYPE,
        audio_path="",
        status="queued",
        progress=0,
        options_json=json.dumps(
            _base_options(source, request), ensure_ascii=False
        ),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    task_service.start_task_in_background(row.id)
    return row


def _is_cancelled(row: TranscriptionTask) -> bool:
    return row.status == "cancelled" or task_runtime.is_cancelled(row.id)


def _check_cancelled(row: TranscriptionTask) -> None:
    # Same semantics as the video-link executor: a cancel between steps must
    # stop the download instead of handing cancelled work to the GPU queue.
    if _is_cancelled(row):
        raise BeeBoxError("TASK_CANCELLED", "Task was cancelled", stage="cancel")


def _run_direct_job(db: Session, row: TranscriptionTask) -> bool:
    """Download and validate the media, then report that the GPU queue owns it."""
    options = read_task_options(row)
    source = _source(options)
    request = _request(options)
    url = str(source.get("url") or "")
    _, host, _ = parse_direct_url(url)
    # Resolve the model before spending a download on a job that cannot run.
    model_name = video_jobs.resolve_asr_model_name(db, request)
    task_logs.add_log(
        db,
        row.id,
        "download",
        "info",
        "Downloading direct media",
        {"host": host},
    )
    task_service.set_task_state(db, row, "preprocessing", 10)
    _check_cancelled(row)

    destination = config.get_uploads_dir() / row.id
    stored: Path | None = None
    try:
        stored = download_direct_media(
            url, destination, is_cancelled=lambda: _is_cancelled(row)
        )
        _check_cancelled(row)
    except BaseException:
        # A failed or cancelled download leaves no temporary media behind.
        destination.unlink(missing_ok=True)
        if stored is not None:
            stored.unlink(missing_ok=True)
        raise

    options.update(
        {
            "_source_kind": video_jobs.SOURCE_KIND_DOWNLOAD,
            "_video_text_path": TEXT_PATH_ASR,
            "diarization": bool(request.get("diarization", True)),
        }
    )
    if request.get("num_speakers") is not None:
        options["num_speakers"] = request["num_speakers"]
    source.update({"text_source": TEXT_SOURCE_ASR, "subtitle_language": None})
    options["_v1_source"] = source
    _write_options(row, options)
    row.audio_path = config.to_storage_path(stored)
    row.source = "local"
    row.model_name = model_name
    if request.get("language") is not None:
        row.language = request["language"]
    db.commit()
    task_logs.add_log(
        db,
        row.id,
        "download",
        "info",
        "Direct media download completed; queued for transcription",
        {"model": row.model_name, "bytes": stored.stat().st_size},
    )
    # The job re-enters through the normal seam: the GPU queue now owns
    # ordering, pausing and the one-at-a-time rule.
    task_service.set_task_state(db, row, "queued", 0)
    return True


def _hand_off_to_gpu_queue(job_id: str) -> None:
    """Submit the downloaded job as an ordinary transcription (ADR-0009)."""
    try:
        task_service.start_task_in_background(job_id)
    except Exception as exc:
        logger.exception("Failed to hand direct job %s to the GPU queue", job_id)
        db = db_session.SessionLocal()
        try:
            video_jobs.record_job_failure(db, job_id, exc)
        finally:
            db.close()


def execute_direct_job(job_id: str) -> None:
    """Resolve one direct media job outside the GPU queue.

    The executor owns the download and validation only; once the media is
    ASR-ready it hands the job to the GPU queue, so it never holds the GPU
    slot while fetching the link.
    """
    db = db_session.SessionLocal()
    handoff = False
    try:
        row = task_service.get_task_row(db, job_id)
        if row is None or row.status == "cancelled" or task_runtime.is_cancelled(job_id):
            return
        task_runtime.mark_running(job_id)
        handoff = _run_direct_job(db, row)
    except Exception as exc:
        video_jobs.record_job_failure(db, job_id, exc)
    finally:
        try:
            db.close()
        finally:
            task_runtime.mark_finished(job_id)
            if handoff:
                # Marking this executor finished first prevents its exit from
                # clearing the queue bookkeeping the hand-off just created.
                _hand_off_to_gpu_queue(job_id)


__all__ = [
    "DIRECT_SOURCE_TYPE",
    "MAX_REDIRECTS",
    "DirectFileTooLargeError",
    "DirectMediaFormatError",
    "DirectSourceError",
    "DirectUrlNotAllowedError",
    "HttpResponse",
    "HttpTimeout",
    "HttpTransport",
    "HttpTransportError",
    "RequestsHttpTransport",
    "create_direct_job",
    "download_direct_media",
    "execute_direct_job",
    "get_http_transport",
    "parse_direct_url",
    "resolve_host",
    "runs_outside_gpu_queue",
]

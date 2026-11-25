"""Unit tests for the direct media download seam (ticket #32).

Every test injects both halves of the seam: ``direct_media.resolve_host``
answers with scripted addresses and ``direct_media.get_http_transport``
answers with scripted responses. No socket is opened and no name is resolved
against real DNS, so the security baseline is asserted deterministically.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from support import FakeHttpResponse, FakeHttpTransport, make_wav_bytes

GLOBAL_ADDRESS = "93.184.216.34"
MEDIA_URL = "https://media.example/clip.wav?signature=sentinel-value"


def install_seams(
    monkeypatch: pytest.MonkeyPatch,
    *,
    addresses: tuple[str, ...] = (GLOBAL_ADDRESS,),
    transport: FakeHttpTransport | None = None,
) -> tuple[list[tuple[str, int]], FakeHttpTransport]:
    """Replace the resolver and transport; return what the job saw."""
    from backend.services import direct_media

    resolved: list[tuple[str, int]] = []

    def resolve(host: str, port: int) -> tuple[str, ...]:
        resolved.append((host, port))
        return addresses

    fake = transport or FakeHttpTransport()
    monkeypatch.setattr(direct_media, "resolve_host", resolve)
    monkeypatch.setattr(direct_media, "get_http_transport", lambda: fake)
    return resolved, fake


def download(tmp_path: Path, url: str = MEDIA_URL) -> Path:
    from backend.services import direct_media

    return direct_media.download_direct_media(url, tmp_path / "job")


def test_a_public_media_url_is_downloaded_to_a_media_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = make_wav_bytes()
    transport = FakeHttpTransport(
        FakeHttpResponse(headers={"Content-Type": "audio/wav"}, chunks=(body,))
    )
    resolved, _ = install_seams(monkeypatch, transport=transport)

    path = download(tmp_path)

    assert path == tmp_path / "job.wav"
    assert path.read_bytes() == body
    assert resolved == [("media.example", 443)]
    assert transport.requests == [{"url": MEDIA_URL, "timeout": 30}]
    assert transport.served[0].closed


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "192.168.1.10",
        "169.254.1.1",
        "192.0.2.10",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fc00::1",
        "fe80::1",
    ],
)
def test_any_non_public_resolved_address_is_refused_before_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    # A single non-public answer poisons the host even when another answer is
    # public: the request must never be sent.
    transport = FakeHttpTransport()
    install_seams(
        monkeypatch, addresses=(GLOBAL_ADDRESS, address), transport=transport
    )

    with pytest.raises(Exception) as excinfo:
        download(tmp_path)

    assert getattr(excinfo.value, "code", None) == "DIRECT_URL_NOT_ALLOWED"
    assert transport.requests == []
    assert list(tmp_path.iterdir()) == []


def test_a_redirect_is_refused_without_requesting_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = FakeHttpTransport(
        FakeHttpResponse(
            302, headers={"Location": "https://elsewhere.example/payload"}
        )
    )
    install_seams(monkeypatch, transport=transport)

    with pytest.raises(Exception) as excinfo:
        download(tmp_path)

    assert getattr(excinfo.value, "code", None) == "DIRECT_URL_NOT_ALLOWED"
    assert "redirect" in str(excinfo.value).lower()
    assert "302" in str(excinfo.value)
    # Only the original URL was requested; the Location target was not.
    assert transport.requests == [{"url": MEDIA_URL, "timeout": 30}]
    assert list(tmp_path.iterdir()) == []
    assert transport.served[0].closed


@pytest.mark.parametrize("status", [400, 403, 404, 410, 500, 503])
def test_http_error_statuses_are_retryable_download_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    transport = FakeHttpTransport(FakeHttpResponse(status))
    install_seams(monkeypatch, transport=transport)

    with pytest.raises(Exception) as excinfo:
        download(tmp_path)

    assert getattr(excinfo.value, "code", None) == "DOWNLOAD_NETWORK_ERROR"
    assert str(status) in str(excinfo.value)
    assert list(tmp_path.iterdir()) == []


def test_a_transport_timeout_is_a_download_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.services import direct_media

    transport = FakeHttpTransport(direct_media.HttpTimeout("scripted"))
    install_seams(monkeypatch, transport=transport)

    with pytest.raises(Exception) as excinfo:
        download(tmp_path)

    assert getattr(excinfo.value, "code", None) == "DOWNLOAD_TIMEOUT"
    assert list(tmp_path.iterdir()) == []


def test_an_ordinary_transport_failure_is_a_download_network_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.services import direct_media

    transport = FakeHttpTransport(direct_media.HttpTransportError("scripted"))
    install_seams(monkeypatch, transport=transport)

    with pytest.raises(Exception) as excinfo:
        download(tmp_path)

    assert getattr(excinfo.value, "code", None) == "DOWNLOAD_NETWORK_ERROR"
    assert list(tmp_path.iterdir()) == []


def test_a_resolution_failure_is_a_retryable_download_network_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.services import direct_media

    def broken_resolver(host: str, port: int) -> tuple[str, ...]:
        raise socket.gaierror("scripted resolution failure")

    monkeypatch.setattr(direct_media, "resolve_host", broken_resolver)
    transport = FakeHttpTransport()
    monkeypatch.setattr(direct_media, "get_http_transport", lambda: transport)

    with pytest.raises(Exception) as excinfo:
        download(tmp_path)

    assert getattr(excinfo.value, "code", None) == "DOWNLOAD_NETWORK_ERROR"
    assert transport.requests == []


@pytest.mark.parametrize(
    "content_type",
    [
        "text/html",
        "text/html; charset=utf-8",
        "text/plain",
        "application/json",
        "application/problem+json",
        "image/png",
        "application/pdf",
    ],
)
def test_an_explicit_non_media_response_is_rejected_before_downloading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content_type: str
) -> None:
    response = FakeHttpResponse(
        headers={"Content-Type": content_type}, chunks=(b"<html>not media</html>",)
    )
    transport = FakeHttpTransport(response)
    install_seams(monkeypatch, transport=transport)

    with pytest.raises(Exception) as excinfo:
        download(tmp_path)

    assert getattr(excinfo.value, "code", None) == "UNSUPPORTED_MEDIA_FORMAT"
    # The rejection happens on the headers: no body byte was read or written.
    assert response.bytes_read == 0
    assert response.closed
    assert list(tmp_path.iterdir()) == []


def test_content_length_over_the_limit_is_rejected_before_streaming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEEBOX_MAX_UPLOAD_BYTES", "1024")
    response = FakeHttpResponse(
        headers={"Content-Type": "audio/wav", "Content-Length": "2048"},
        chunks=(b"x" * 2048,),
    )
    install_seams(monkeypatch, transport=FakeHttpTransport(response))

    with pytest.raises(Exception) as excinfo:
        download(tmp_path)

    assert getattr(excinfo.value, "code", None) == "FILE_TOO_LARGE"
    assert response.bytes_read == 0
    assert response.closed
    assert list(tmp_path.iterdir()) == []


def test_a_stream_over_the_limit_is_detected_and_the_partial_file_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEEBOX_MAX_UPLOAD_BYTES", "1024")
    # No Content-Length: the cap must be enforced while writing.
    response = FakeHttpResponse(
        headers={"Content-Type": "audio/wav"},
        chunks=(b"x" * 700, b"x" * 700),
    )
    install_seams(monkeypatch, transport=FakeHttpTransport(response))

    with pytest.raises(Exception) as excinfo:
        download(tmp_path)

    assert getattr(excinfo.value, "code", None) == "FILE_TOO_LARGE"
    assert response.bytes_read > 1024
    assert list(tmp_path.iterdir()) == []


def test_a_generic_content_type_is_accepted_when_ffprobe_finds_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = make_wav_bytes()
    transport = FakeHttpTransport(
        FakeHttpResponse(
            headers={"Content-Type": "application/octet-stream"}, chunks=(body,)
        )
    )
    install_seams(monkeypatch, transport=transport)

    path = download(tmp_path)

    assert path == tmp_path / "job.wav"
    assert path.read_bytes() == body


def test_a_generic_content_type_without_audio_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = FakeHttpTransport(
        FakeHttpResponse(
            headers={"Content-Type": "application/octet-stream"},
            chunks=(b"<html>not media at all</html>",),
        )
    )
    install_seams(monkeypatch, transport=transport)

    with pytest.raises(Exception) as excinfo:
        download(tmp_path)

    assert getattr(excinfo.value, "code", None) == "UNSUPPORTED_MEDIA_FORMAT"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("url", ["ftp://media.example/clip.wav", "file:///etc/passwd", "not a url"])
def test_a_non_http_link_is_rejected_at_download_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    transport = FakeHttpTransport()
    install_seams(monkeypatch, transport=transport)

    with pytest.raises(Exception) as excinfo:
        download(tmp_path, url)

    assert getattr(excinfo.value, "code", None) == "INVALID_URL"
    assert transport.requests == []


@pytest.mark.parametrize("url", ["http://[::1", "https://[bad/", "http://[]/x"])
def test_a_malformed_url_is_rejected_without_a_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    transport = FakeHttpTransport()
    install_seams(monkeypatch, transport=transport)

    with pytest.raises(Exception) as excinfo:
        download(tmp_path, url)

    assert getattr(excinfo.value, "code", None) == "INVALID_URL"
    assert transport.requests == []


class _ExplodingResponse:
    """A streamed requests.Response whose body raises mid-transfer."""

    status_code = 200
    headers = {"Content-Type": "audio/wav"}

    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.closed = False

    def iter_content(self, chunk_size: int):
        yield b"partial-body"
        raise self._error

    def close(self) -> None:
        self.closed = True


def test_a_read_timeout_from_the_requests_body_becomes_an_http_timeout() -> None:
    import requests

    from backend.services import direct_media

    adapter = direct_media._RequestsResponse(
        _ExplodingResponse(requests.Timeout("read timed out"))
    )

    with pytest.raises(direct_media.HttpTimeout):
        list(adapter.iter_bytes())


def test_a_connection_reset_from_the_requests_body_becomes_a_transport_error() -> None:
    import requests

    from backend.services import direct_media

    adapter = direct_media._RequestsResponse(
        _ExplodingResponse(requests.ConnectionError("connection reset"))
    )

    with pytest.raises(direct_media.HttpTransportError):
        list(adapter.iter_bytes())


class _BodyErrorResponse(FakeHttpResponse):
    """A scripted response that fails after the first body chunk."""

    def __init__(self, error: BaseException) -> None:
        super().__init__(headers={"Content-Type": "audio/wav"})
        self.error = error

    def iter_bytes(self):
        yield b"partial-body"
        raise self.error


def test_a_timeout_while_streaming_the_body_is_a_download_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.services import direct_media

    response = _BodyErrorResponse(direct_media.HttpTimeout("scripted"))
    install_seams(monkeypatch, transport=FakeHttpTransport(response))

    with pytest.raises(Exception) as excinfo:
        download(tmp_path)

    assert getattr(excinfo.value, "code", None) == "DOWNLOAD_TIMEOUT"
    assert response.closed
    assert list(tmp_path.iterdir()) == []


def test_a_reset_while_streaming_the_body_is_a_download_network_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.services import direct_media

    response = _BodyErrorResponse(direct_media.HttpTransportError("scripted"))
    install_seams(monkeypatch, transport=FakeHttpTransport(response))

    with pytest.raises(Exception) as excinfo:
        download(tmp_path)

    assert getattr(excinfo.value, "code", None) == "DOWNLOAD_NETWORK_ERROR"
    assert response.closed
    assert list(tmp_path.iterdir()) == []


def test_a_cancel_during_the_body_stops_the_download_and_deletes_the_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.services import direct_media

    state = {"cancelled": False}

    class _CancellingBody(FakeHttpResponse):
        def iter_bytes(self):
            yield b"x" * 64
            state["cancelled"] = True
            yield b"x" * 64

    install_seams(
        monkeypatch,
        transport=FakeHttpTransport(
            _CancellingBody(headers={"Content-Type": "audio/wav"})
        ),
    )

    with pytest.raises(Exception) as excinfo:
        direct_media.download_direct_media(
            MEDIA_URL, tmp_path / "job", is_cancelled=lambda: state["cancelled"]
        )

    assert getattr(excinfo.value, "code", None) == "TASK_CANCELLED"
    assert list(tmp_path.iterdir()) == []


def test_failure_messages_never_echo_the_signed_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failures = [
        FakeHttpResponse(302, headers={"Location": "https://elsewhere.example/x"}),
        FakeHttpResponse(403),
        FakeHttpResponse(headers={"Content-Type": "text/html"}, chunks=(b"x",)),
        FakeHttpResponse(headers={"Content-Length": "999999999999"}),
    ]
    for response in failures:
        install_seams(monkeypatch, transport=FakeHttpTransport(response))
        with pytest.raises(Exception) as excinfo:
            download(tmp_path / response.status_code.__str__())
        message = str(excinfo.value)
        assert MEDIA_URL not in message
        assert "sentinel-value" not in message
        assert "media.example" not in message


def test_the_default_resolver_returns_literal_addresses_without_dns() -> None:
    from backend.services import direct_media

    assert direct_media.resolve_host("127.0.0.1", 80) == ("127.0.0.1",)
    assert "::1" in direct_media.resolve_host("::1", 80)


def test_a_cancel_after_the_download_deletes_the_downloaded_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    import uuid

    from backend import config
    from backend.database import session as db_session
    from backend.database.models import TranscriptionTask
    from backend.services import direct_media, task_runtime
    from backend.services.video_platform import TEXT_PATH_SUBTITLE

    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path / "data"))
    db_session.init_db()
    transport = FakeHttpTransport(
        FakeHttpResponse(headers={"Content-Type": "audio/wav"}, chunks=(make_wav_bytes(),))
    )
    install_seams(monkeypatch, transport=transport)

    state = {"cancelled": False}
    real_download = direct_media.download_direct_media

    def download_then_cancel(url: str, destination: Path, **kwargs) -> Path:
        path = real_download(url, destination, **kwargs)
        # The cancel lands between the download and the hand-off.
        state["cancelled"] = True
        return path

    monkeypatch.setattr(direct_media, "download_direct_media", download_then_cancel)
    monkeypatch.setattr(
        task_runtime, "is_cancelled", lambda task_id: state["cancelled"]
    )

    job_id = str(uuid.uuid4())
    options = {
        "output_formats": ["txt"],
        "diarization": False,
        "_source_kind": "external",
        "_video_text_path": TEXT_PATH_SUBTITLE,
        "_video_request": {},
        "_v1_source": {
            "type": "direct",
            "url": MEDIA_URL,
            "title": None,
            "author": None,
            "platform": None,
            "page_url": None,
            "filename": "clip.wav",
            "text_source": "asr",
        },
    }
    db = db_session.SessionLocal()
    try:
        db.add(
            TranscriptionTask(
                id=job_id,
                filename="clip.wav",
                source="direct",
                audio_path="",
                status="queued",
                progress=0,
                options_json=json.dumps(options),
            )
        )
        db.commit()
    finally:
        db.close()

    direct_media.execute_direct_job(job_id)

    uploads_dir = config.get_uploads_dir()
    assert [path for path in uploads_dir.iterdir() if job_id in path.name] == []
    assert transport.served[0].closed
    db = db_session.SessionLocal()
    try:
        row = db.get(TranscriptionTask, job_id)
        assert row is not None
        assert row.status == "cancelled", row.status
        assert row.error_code == "TASK_CANCELLED"
        # No media was ever persisted, so delete/retention has nothing to own.
        assert row.audio_path == ""
    finally:
        db.close()

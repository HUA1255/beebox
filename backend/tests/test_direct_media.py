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
REDIRECT_URL = "https://cdn.example/files/clip.wav?token=another-sentinel"


def install_seams(
    monkeypatch: pytest.MonkeyPatch,
    *,
    addresses: tuple[str, ...] | dict[str, tuple[str, ...]] = (GLOBAL_ADDRESS,),
    transport: FakeHttpTransport | None = None,
) -> tuple[list[tuple[str, int]], FakeHttpTransport]:
    """Replace the resolver and transport; return what the job saw.

    ``addresses`` is either one answer for every host or a mapping from host to
    answers, so a redirect hop can resolve differently from the submitted host.
    """
    from backend.services import direct_media

    resolved: list[tuple[str, int]] = []

    def resolve(host: str, port: int) -> tuple[str, ...]:
        resolved.append((host, port))
        if isinstance(addresses, dict):
            return addresses[host]
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


def test_a_redirect_to_a_public_media_file_is_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = make_wav_bytes()
    transport = FakeHttpTransport(
        FakeHttpResponse(302, headers={"Location": REDIRECT_URL}),
        FakeHttpResponse(headers={"Content-Type": "audio/wav"}, chunks=(body,)),
    )
    resolved, _ = install_seams(
        monkeypatch,
        addresses={
            "media.example": (GLOBAL_ADDRESS,),
            "cdn.example": (GLOBAL_ADDRESS,),
        },
        transport=transport,
    )

    path = download(tmp_path)

    assert path == tmp_path / "job.wav"
    assert path.read_bytes() == body
    # Every hop was requested once, in order, with the same deadline.
    assert [request["url"] for request in transport.requests] == [
        MEDIA_URL,
        REDIRECT_URL,
    ]
    assert {request["timeout"] for request in transport.requests} == {30}
    assert resolved == [("media.example", 443), ("cdn.example", 443)]
    # The redirect response was closed before the target was fetched.
    assert all(response.closed for response in transport.served)


def test_a_relative_location_is_resolved_against_the_current_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = make_wav_bytes()
    transport = FakeHttpTransport(
        FakeHttpResponse(302, headers={"Location": "/files/clip.wav"}),
        FakeHttpResponse(headers={"Content-Type": "audio/wav"}, chunks=(body,)),
    )
    install_seams(monkeypatch, transport=transport)

    path = download(tmp_path)

    assert path.read_bytes() == body
    assert [request["url"] for request in transport.requests] == [
        MEDIA_URL,
        "https://media.example/files/clip.wav",
    ]


@pytest.mark.parametrize(
    ("location", "target_host", "target_address", "target_port"),
    [
        ("http://localhost/clip.wav", "localhost", "127.0.0.1", 80),
        ("http://internal.example/clip.wav", "internal.example", "10.1.2.3", 80),
        ("https://lan.example/clip.wav", "lan.example", "192.168.1.10", 443),
        ("http://link-local.example/clip.wav", "link-local.example", "169.254.1.1", 80),
        ("https://reserved.example/clip.wav", "reserved.example", "192.0.2.10", 443),
    ],
)
def test_a_redirect_to_a_non_public_target_is_refused_without_requesting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
    target_host: str,
    target_address: str,
    target_port: int,
) -> None:
    redirect = FakeHttpResponse(302, headers={"Location": location})
    transport = FakeHttpTransport(redirect)
    resolved, _ = install_seams(
        monkeypatch,
        addresses={
            "media.example": (GLOBAL_ADDRESS,),
            target_host: (target_address,),
        },
        transport=transport,
    )

    with pytest.raises(Exception) as excinfo:
        download(tmp_path)

    assert getattr(excinfo.value, "code", None) == "DIRECT_URL_NOT_ALLOWED"
    # The hop was resolved and rejected; its target was never requested.
    assert transport.requests == [{"url": MEDIA_URL, "timeout": 30}]
    assert resolved == [("media.example", 443), (target_host, target_port)]
    assert redirect.closed
    assert list(tmp_path.iterdir()) == []


def test_a_redirect_hop_with_public_and_private_addresses_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One public answer is not enough on a hop either: the whole host is
    # poisoned and the target is never requested.
    transport = FakeHttpTransport(
        FakeHttpResponse(
            302, headers={"Location": "https://mixed.example/clip.wav"}
        )
    )
    resolved, _ = install_seams(
        monkeypatch,
        addresses={
            "media.example": (GLOBAL_ADDRESS,),
            "mixed.example": (GLOBAL_ADDRESS, "10.1.2.3"),
        },
        transport=transport,
    )

    with pytest.raises(Exception) as excinfo:
        download(tmp_path)

    assert getattr(excinfo.value, "code", None) == "DIRECT_URL_NOT_ALLOWED"
    assert transport.requests == [{"url": MEDIA_URL, "timeout": 30}]
    assert ("mixed.example", 443) in resolved
    assert list(tmp_path.iterdir()) == []


def test_the_hop_limit_fails_fast_without_requesting_the_next_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.services import direct_media
    from backend.v1.status import NON_RETRYABLE_ERROR_CODES

    urls = [MEDIA_URL]
    responses = []
    for hop in range(direct_media.MAX_REDIRECTS + 1):
        target = f"https://hop{hop}.example/clip.wav"
        urls.append(target)
        responses.append(FakeHttpResponse(302, headers={"Location": target}))
    transport = FakeHttpTransport(*responses)
    install_seams(monkeypatch, transport=transport)

    with pytest.raises(Exception) as excinfo:
        download(tmp_path)

    assert getattr(excinfo.value, "code", None) == "DIRECT_URL_NOT_ALLOWED"
    assert getattr(excinfo.value, "code", None) in NON_RETRYABLE_ERROR_CODES
    assert "redirect" in str(excinfo.value).lower()
    assert str(direct_media.MAX_REDIRECTS) in str(excinfo.value)
    # MAX_REDIRECTS hops were followed; the last Location was never requested.
    assert [request["url"] for request in transport.requests] == urls[:-1]
    assert all(response.closed for response in transport.served)
    assert list(tmp_path.iterdir()) == []


def test_a_redirect_loop_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.v1.status import NON_RETRYABLE_ERROR_CODES

    first = "https://first.example/clip.wav"
    second = "https://second.example/clip.wav"
    transport = FakeHttpTransport(
        FakeHttpResponse(302, headers={"Location": first}),
        FakeHttpResponse(302, headers={"Location": second}),
        FakeHttpResponse(302, headers={"Location": first}),
    )
    install_seams(
        monkeypatch,
        addresses={
            "media.example": (GLOBAL_ADDRESS,),
            "first.example": (GLOBAL_ADDRESS,),
            "second.example": (GLOBAL_ADDRESS,),
        },
        transport=transport,
    )

    with pytest.raises(Exception) as excinfo:
        download(tmp_path)

    assert getattr(excinfo.value, "code", None) == "DIRECT_URL_NOT_ALLOWED"
    assert getattr(excinfo.value, "code", None) in NON_RETRYABLE_ERROR_CODES
    assert "loop" in str(excinfo.value).lower()
    # Detection happens on the repeated target, before a fourth request.
    assert [request["url"] for request in transport.requests] == [
        MEDIA_URL,
        first,
        second,
    ]
    assert all(response.closed for response in transport.served)
    assert list(tmp_path.iterdir()) == []


def test_a_redirect_to_the_same_url_is_a_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = FakeHttpTransport(
        FakeHttpResponse(302, headers={"Location": MEDIA_URL})
    )
    install_seams(monkeypatch, transport=transport)

    with pytest.raises(Exception) as excinfo:
        download(tmp_path)

    assert getattr(excinfo.value, "code", None) == "DIRECT_URL_NOT_ALLOWED"
    assert "loop" in str(excinfo.value).lower()
    assert transport.requests == [{"url": MEDIA_URL, "timeout": 30}]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("headers", [{}, {"Location": ""}, {"Location": "   "}])
def test_a_redirect_without_a_location_is_an_invalid_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]
) -> None:
    redirect = FakeHttpResponse(302, headers=headers)
    transport = FakeHttpTransport(redirect)
    install_seams(monkeypatch, transport=transport)

    with pytest.raises(Exception) as excinfo:
        download(tmp_path)

    assert getattr(excinfo.value, "code", None) == "DIRECT_URL_NOT_ALLOWED"
    assert "location" in str(excinfo.value).lower()
    assert transport.requests == [{"url": MEDIA_URL, "timeout": 30}]
    assert redirect.closed
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "location",
    ["ftp://files.example/clip.wav", "file:///etc/passwd", "data:text/plain,hello"],
)
def test_a_redirect_to_a_non_http_location_is_an_invalid_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    redirect = FakeHttpResponse(302, headers={"Location": location})
    transport = FakeHttpTransport(redirect)
    install_seams(monkeypatch, transport=transport)

    with pytest.raises(Exception) as excinfo:
        download(tmp_path)

    assert getattr(excinfo.value, "code", None) == "DIRECT_URL_NOT_ALLOWED"
    assert "absolute http(s) url" in str(excinfo.value).lower()
    assert transport.requests == [{"url": MEDIA_URL, "timeout": 30}]
    assert redirect.closed
    assert list(tmp_path.iterdir()) == []


def test_a_final_redirect_response_is_still_media_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = FakeHttpResponse(
        headers={"Content-Type": "text/html"}, chunks=(b"<html>not media</html>",)
    )
    transport = FakeHttpTransport(
        FakeHttpResponse(302, headers={"Location": REDIRECT_URL}), page
    )
    install_seams(monkeypatch, transport=transport)

    with pytest.raises(Exception) as excinfo:
        download(tmp_path)

    assert getattr(excinfo.value, "code", None) == "UNSUPPORTED_MEDIA_FORMAT"
    # The rejection happens on the final headers: no body byte was read.
    assert page.bytes_read == 0
    assert page.closed
    assert list(tmp_path.iterdir()) == []


def test_a_redirected_generic_response_without_audio_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = FakeHttpTransport(
        FakeHttpResponse(302, headers={"Location": REDIRECT_URL}),
        FakeHttpResponse(
            headers={"Content-Type": "application/octet-stream"},
            chunks=(b"<html>not media at all</html>",),
        ),
    )
    install_seams(monkeypatch, transport=transport)

    with pytest.raises(Exception) as excinfo:
        download(tmp_path)

    assert getattr(excinfo.value, "code", None) == "UNSUPPORTED_MEDIA_FORMAT"
    assert list(tmp_path.iterdir()) == []


def test_a_cancel_between_hops_stops_before_the_next_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.services import direct_media

    state = {"cancelled": False}

    class _CancelOnClose(FakeHttpResponse):
        def close(self) -> None:
            super().close()
            state["cancelled"] = True

    transport = FakeHttpTransport(
        _CancelOnClose(302, headers={"Location": REDIRECT_URL}),
        FakeHttpResponse(
            headers={"Content-Type": "audio/wav"}, chunks=(make_wav_bytes(),)
        ),
    )
    install_seams(monkeypatch, transport=transport)

    with pytest.raises(Exception) as excinfo:
        direct_media.download_direct_media(
            MEDIA_URL, tmp_path / "job", is_cancelled=lambda: state["cancelled"]
        )

    assert getattr(excinfo.value, "code", None) == "TASK_CANCELLED"
    assert [request["url"] for request in transport.requests] == [MEDIA_URL]
    assert list(tmp_path.iterdir()) == []


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


class _FakeRequestsSession:
    """Stands in for requests.Session so the production transport is offline."""

    def __init__(self) -> None:
        self.trust_env = True
        self.closed = False
        self.url: str | None = None
        self.kwargs: dict | None = None
        self.response = _FakeRequestsStream()

    def get(self, url: str, **kwargs):
        self.url = url
        self.kwargs = kwargs
        if "trust_env" in kwargs:
            # requests.Session.request() does not accept this keyword; the
            # production failure it used to cause is what this test pins.
            raise TypeError(
                "Session.request() got an unexpected keyword argument 'trust_env'"
            )
        return self.response

    def close(self) -> None:
        self.closed = True


class _FakeRequestsStream:
    status_code = 200
    headers = {"Content-Type": "audio/mpeg"}

    def __init__(self) -> None:
        self.closed = False

    def iter_content(self, chunk_size: int):
        yield b"media-bytes"

    def close(self) -> None:
        self.closed = True


def test_the_production_transport_sets_trust_env_on_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.services import direct_media

    session = _FakeRequestsSession()
    monkeypatch.setattr(direct_media.requests, "Session", lambda: session)
    monkeypatch.setattr(
        direct_media.requests,
        "get",
        lambda *args, **kwargs: pytest.fail(
            "requests.get cannot carry trust_env; use a Session"
        ),
    )

    transport = direct_media.RequestsHttpTransport(proxy="http://127.0.0.1:3128")
    response = transport.get("https://media.example/clip.mp3", timeout=7)

    assert session.url == "https://media.example/clip.mp3"
    assert session.trust_env is False
    assert session.kwargs == {
        "stream": True,
        "allow_redirects": False,
        "timeout": 7,
        "proxies": {
            "http": "http://127.0.0.1:3128",
            "https": "http://127.0.0.1:3128",
        },
        "headers": {"User-Agent": direct_media.USER_AGENT},
    }
    assert direct_media.USER_AGENT.startswith("BeeBox/")
    assert list(response.iter_bytes()) == [b"media-bytes"]

    response.close()

    assert session.response.closed is True
    assert session.closed is True



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
        # An invalid redirect chain (a 3xx without a Location header).
        FakeHttpResponse(302),
        FakeHttpResponse(403),
        FakeHttpResponse(headers={"Content-Type": "text/html"}, chunks=(b"x",)),
        FakeHttpResponse(headers={"Content-Length": "999999999999"}),
    ]
    for index, response in enumerate(failures):
        install_seams(monkeypatch, transport=FakeHttpTransport(response))
        with pytest.raises(Exception) as excinfo:
            download(tmp_path / str(index))
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
    from backend.services.video_platform import TEXT_PATH_DOWNLOAD

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
        "_video_text_path": TEXT_PATH_DOWNLOAD,
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

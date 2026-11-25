"""The short-link expansion seam (ticket #29), tested without a network.

The job hands the expansion to
:func:`backend.services.video_platform.short_links.expand_short_link`; HTTP
level tests replace that callable, and these cases pin the real callable's
transport contract with a fake ``requests`` module. No socket is opened and no
host is contacted.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.services.video_platform import (
    PLATFORM_X,
    DownloadNetworkError,
    DownloadOptions,
    DownloadTimeoutError,
    short_links,
)

TCO_URL = "https://t.co/BeeBoxFake29"
X_POST_URL = "https://x.com/BeeBoxTest/status/1234567890123456789"
X_POST_ID = "1234567890123456789"
PROXY = "http://127.0.0.1:3128"
MAX_REDIRECTS = 10


class FakeTimeout(Exception):
    """Stands in for ``requests.exceptions.Timeout``."""


class FakeRequestException(Exception):
    """Stands in for ``requests.exceptions.RequestException``."""


class FakeResponse:
    def __init__(self, *, url: str, status_code: int = 200) -> None:
        self.url = url
        self.status_code = status_code
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1


class FakeSession:
    """Records the request the expander sends through ``requests``."""

    def __init__(
        self,
        *,
        response: FakeResponse | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.max_redirects: int | None = None
        self.requests: list[tuple[str, dict]] = []
        self.closed = False

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.closed = True

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.requests.append((url, kwargs))
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def install_fake_requests(
    monkeypatch: pytest.MonkeyPatch, session: FakeSession
) -> FakeSession:
    stub = SimpleNamespace(
        Session=lambda: session,
        exceptions=SimpleNamespace(
            Timeout=FakeTimeout,
            RequestException=FakeRequestException,
        ),
    )
    monkeypatch.setattr(short_links, "requests", stub)
    return session


def test_expansion_returns_the_final_url_with_the_job_proxy(monkeypatch) -> None:
    session = install_fake_requests(
        monkeypatch,
        FakeSession(response=FakeResponse(url=X_POST_URL)),
    )

    final_url = short_links.expand_short_link(
        TCO_URL,
        options=DownloadOptions(proxy=PROXY, timeout_seconds=7),
    )

    assert final_url == X_POST_URL
    [(requested_url, kwargs)] = session.requests
    assert requested_url == TCO_URL
    assert kwargs["allow_redirects"] is True
    assert kwargs["timeout"] == 7
    assert kwargs["proxies"] == {"http": PROXY, "https": PROXY}
    # Redirect following is bounded, and the response is released.
    assert session.max_redirects == MAX_REDIRECTS
    assert session.closed is True
    assert session.response is not None
    assert session.response.close_count == 1


def test_expansion_sends_no_proxy_when_none_is_configured(monkeypatch) -> None:
    session = install_fake_requests(
        monkeypatch,
        FakeSession(response=FakeResponse(url=X_POST_URL)),
    )

    short_links.expand_short_link(TCO_URL, options=DownloadOptions())

    [(_, kwargs)] = session.requests
    assert kwargs["proxies"] is None


@pytest.mark.parametrize(
    ("error_type", "expected"),
    [
        (FakeTimeout, DownloadTimeoutError),
        (FakeRequestException, DownloadNetworkError),
    ],
)
def test_transport_failures_map_to_the_retryable_download_codes(
    monkeypatch, error_type, expected
) -> None:
    install_fake_requests(monkeypatch, FakeSession(error=error_type("boom")))

    with pytest.raises(expected) as error:
        short_links.expand_short_link(TCO_URL, options=DownloadOptions())

    assert error.value.code == expected.code


def test_an_error_status_is_a_download_network_error(monkeypatch) -> None:
    install_fake_requests(
        monkeypatch,
        FakeSession(response=FakeResponse(url=TCO_URL, status_code=404)),
    )

    with pytest.raises(DownloadNetworkError):
        short_links.expand_short_link(TCO_URL, options=DownloadOptions())


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/i/flow/login",
        "https://x.com/i/flow/login?redirect_after_login=%2FBeeBoxTest",
        "https://x.com/i/flow/signup",
        "https://x.com/i/flow/consent",
        "https://x.com/login",
        "https://x.com/login/",
        "https://www.x.com/account/access",
        "https://x.com/account/verify",
        "https://x.com/i/verify",
        "https://x.com/verify",
        "https://twitter.com/i/flow/login",
        "https://twitter.com/login",
        "https://twitter.com/account/verify",
        "https://mobile.twitter.com/account/access",
    ],
)
def test_login_and_verification_pages_are_recognized(url: str) -> None:
    assert short_links.is_login_page(url, platform=PLATFORM_X) is True


@pytest.mark.parametrize(
    "url",
    [
        X_POST_URL,
        "https://x.com/BeeBoxTest",
        "https://x.com/home",
        "https://example.com/i/flow/login",
        TCO_URL,
        # The signed-in permalinks are posts, not login pages.
        f"https://x.com/i/web/status/{X_POST_ID}",
        f"https://x.com/i/status/{X_POST_ID}",
    ],
)
def test_content_pages_are_not_login_pages(url: str) -> None:
    assert short_links.is_login_page(url, platform=PLATFORM_X) is False


def test_a_platform_without_login_paths_has_no_login_pages() -> None:
    # Only X declares login paths in its platform rule; a page on another
    # platform is never classified as a login wall.
    assert short_links.is_login_page("https://x.com/i/flow/login", platform="zhihu") is False
    assert short_links.is_login_page("https://x.com/i/flow/login", platform="unknown") is False

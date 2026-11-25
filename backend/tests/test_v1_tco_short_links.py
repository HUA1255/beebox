"""t.co short-link jobs through ``/v1`` (ticket #29).

Submit accepts ``https://t.co/<token>`` offline. The job resolves the link at
task start through the same :class:`DownloadOptions` as the platform calls and
continues only when the target is an X single post; every other target,
login page and transport failure fails before anything is downloaded. These
cases replace the expansion seam with a scripted callable and drive the fake
video platform and fake speech engine: nothing contacts t.co or any real host.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.services.video_platform import (
    PLATFORM_X,
    DownloadOptions,
    short_links,
)
from test_v1_link_errors import (
    assert_background_link_failure,
    configure_client,
    create_url_job,
    read_calls,
    wait_for,
)

TCO_URL = "https://t.co/BeeBoxFake29"
X_POST_URL = "https://x.com/BeeBoxTest/status/1234567890123456789"
X_POST_ID = "1234567890123456789"
ASR_TEXT = "语音识别得到的句子。"
PROXY = "http://127.0.0.1:3128"


def fake_x_post(**extra) -> dict:
    """The post the fake platform exposes once the short link is expanded."""
    entry = {
        "platform": PLATFORM_X,
        "id": X_POST_ID,
        "urls": [X_POST_URL],
        "title": "Spoken X post from a short link",
        "author": "BeeBoxTest",
        "duration_seconds": 1.5,
        "tracks": [],
        "audio_seconds": 1.5,
    }
    entry.update(extra)
    return entry


def install_expander(
    monkeypatch: pytest.MonkeyPatch,
    target: str | None = None,
    *,
    error: BaseException | None = None,
) -> list[dict]:
    """Replace the expansion seam and record the options it receives."""
    calls: list[dict] = []

    def expand(url: str, *, options: DownloadOptions | None = None) -> str:
        calls.append({"url": url, "options": options})
        if error is not None:
            raise error
        return target or X_POST_URL

    monkeypatch.setattr(short_links, "expand_short_link", expand)
    return calls


def test_a_tco_link_expands_to_an_x_post_and_is_transcribed_from_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, root = configure_client(
        tmp_path,
        monkeypatch,
        [fake_x_post()],
        asr=True,
        # An X post needs no Bilibili login; the gate must not apply to it.
        bilibili_login="absent",
    )
    calls = install_expander(monkeypatch)

    created = create_url_job(client, TCO_URL)
    assert created.status_code == 201, created.text
    body = created.json()
    # Submit stored the short link and platform without expanding anything.
    assert body["source"]["url"] == TCO_URL
    assert body["source"]["platform"] == PLATFORM_X

    finished = wait_for(client, body["id"])
    assert finished["status"] == "succeeded", finished
    source = finished["source"]
    assert source["url"] == TCO_URL
    assert source["platform"] == PLATFORM_X
    assert source["text_source"] == "asr"
    assert source["subtitle_language"] is None
    assert source["title"] == "Spoken X post from a short link"

    # Expansion ran once, on the submitted short link.
    assert [call["url"] for call in calls] == [TCO_URL]
    # The job re-recognized the final URL and downloaded only audio.
    platform_calls = read_calls(root)
    assert [call["stage"] for call in platform_calls] == ["metadata", "audio"]
    assert platform_calls[0]["url"] == X_POST_URL
    assert platform_calls[0]["video_id"] == X_POST_ID

    result = client.get(f"/v1/jobs/{body['id']}/result").json()
    assert ASR_TEXT in result["text"]


def test_the_download_proxy_reaches_expansion_and_the_platform_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, root = configure_client(
        tmp_path,
        monkeypatch,
        [fake_x_post()],
        proxy=PROXY,
        asr=True,
        bilibili_login="absent",
    )
    calls = install_expander(monkeypatch)

    created = create_url_job(client, TCO_URL)
    assert created.status_code == 201, created.text
    finished = wait_for(client, created.json()["id"])
    assert finished["status"] == "succeeded", finished

    [expansion] = calls
    assert expansion["url"] == TCO_URL
    assert expansion["options"] is not None
    assert expansion["options"].proxy == PROXY

    platform_calls = read_calls(root)
    assert [call["stage"] for call in platform_calls] == ["metadata", "audio"]
    assert all(call["proxy"] == PROXY for call in platform_calls)
    # The proxy is configuration, never part of the public result.
    assert PROXY not in client.get(f"/v1/jobs/{finished['id']}/result").text


@pytest.mark.parametrize(
    "target",
    [
        # A profile, a collection-style page and a media sub-page are not posts.
        "https://x.com/BeeBoxTest",
        "https://x.com/BeeBoxTest/likes",
        "https://x.com/i/lists/123456",
        f"{X_POST_URL}/photo/1",
        # A short link that resolves outside X is refused as well.
        "https://www.bilibili.com/video/BV1Short29",
    ],
)
def test_a_short_link_that_is_not_an_x_post_fails_before_downloading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    client, root = configure_client(
        tmp_path, monkeypatch, [fake_x_post()], asr=True, bilibili_login="absent"
    )
    install_expander(monkeypatch, target)

    created = create_url_job(client, TCO_URL)
    assert created.status_code == 201, created.text
    finished = assert_background_link_failure(
        client, created.json()["id"], "UNSUPPORTED_URL"
    )

    # The caller can tell which submission failed, and retrying cannot help.
    assert TCO_URL in finished["error"], finished
    assert finished["retryable"] is False, finished
    # Nothing was fetched or downloaded for any target.
    assert read_calls(root) == []


@pytest.mark.parametrize(
    "target",
    [
        "https://x.com/i/flow/login",
        "https://x.com/i/flow/login?redirect_after_login=%2FBeeBoxTest",
        "https://x.com/i/flow/signup",
        "https://x.com/login",
        "https://x.com/account/access",
        "https://twitter.com/i/flow/login",
    ],
)
def test_a_short_link_that_lands_on_a_login_page_requires_a_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    client, root = configure_client(
        tmp_path, monkeypatch, [fake_x_post()], asr=True, bilibili_login="absent"
    )
    install_expander(monkeypatch, target)

    created = create_url_job(client, TCO_URL)
    assert created.status_code == 201, created.text
    finished = assert_background_link_failure(
        client, created.json()["id"], "VIDEO_LOGIN_REQUIRED"
    )

    assert finished["retryable"] is True, finished
    assert TCO_URL in finished["error"], finished
    assert read_calls(root) == []


@pytest.mark.parametrize(
    ("error_type", "code"),
    [
        ("DownloadNetworkError", "DOWNLOAD_NETWORK_ERROR"),
        ("DownloadTimeoutError", "DOWNLOAD_TIMEOUT"),
    ],
)
def test_expansion_transport_failures_are_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: str, code: str
) -> None:
    from backend.services import video_platform

    client, root = configure_client(
        tmp_path, monkeypatch, [fake_x_post()], asr=True, bilibili_login="absent"
    )
    error = getattr(video_platform, error_type)(f"Injected expansion failure: {TCO_URL}")
    install_expander(monkeypatch, error=error)

    created = create_url_job(client, TCO_URL)
    assert created.status_code == 201, created.text
    finished = assert_background_link_failure(client, created.json()["id"], code)

    assert finished["retryable"] is True, finished
    assert TCO_URL in finished["error"], finished
    # The failure happened before the platform was asked for anything.
    assert read_calls(root) == []

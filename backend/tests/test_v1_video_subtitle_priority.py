"""HTTP contracts for subtitle selection and automatic-caption cleaning.

Ticket #43 replaces the basic track choice with the full priority order and
adds ``options.subtitle_language`` / ``options.prefer=asr``. These tests use
the fake video platform: every track combination is a local fixture, no
network, no GPU and no speech engine. The subtitle-path client's fake speech
engine fails if ASR is ever called, so a succeeded subtitle job proves the
text came from the platform; the ASR-path client scripts a successful engine
so the ticket #44 fallback can be asserted end to end.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from support import fake_speech_env

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "video_platform"
TERMINAL = {"succeeded", "failed", "cancelled"}
ASR_TEXT = "语音识别得到的句子。"

MULTI_LANGUAGE_URL = "https://www.youtube.com/watch?v=MultiLang43"
CHINESE_AUTO_URL = "https://www.youtube.com/watch?v=AutoZhOnly43"
ENGLISH_ORIGINAL_URL = "https://www.youtube.com/watch?v=EnglishOriginal43"
JAPANESE_ORIGINAL_URL = "https://www.youtube.com/watch?v=JapaneseOriginal43"
TRANSLATED_ONLY_URL = "https://www.youtube.com/watch?v=TranslatedOnly43"
BILIBILI_AI_ZH_ONLY_URL = "https://www.bilibili.com/video/BV1AiZh065"
BILIBILI_AI_AND_MANUAL_URL = "https://www.bilibili.com/video/BV1AiManual065"
BILIBILI_AI_TRANSLATION_URL = "https://www.bilibili.com/video/BV1AiEn065"


def _seed_valid_bilibili_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Store a valid cookie so link jobs survive ticket #66's login gate.

    Ticket #66 is landing concurrently and may require a stored valid login
    for Bilibili link jobs. Seeding one now keeps these fake-platform tests
    about subtitle selection independent of that lifecycle. The fake refresh
    implementation keeps the file local: no test touches the network.
    """
    monkeypatch.setenv("BEEBOX_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv("BEEBOX_BILIBILI_REFRESH", "fake")
    from backend.services.bilibili_login import write_cookie_file

    write_cookie_file(
        {"SESSDATA": "t65-sessdata", "bili_jct": "t65-bili-jct", "DedeUserID": "650065"},
        expires_at=int(time.time()) + 86_400,
    )


def _build_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, asr_succeeds: bool
) -> TestClient:
    monkeypatch.setenv("BEEBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BEEBOX_MODEL_STORAGE_ROOT", str(tmp_path / "models-root"))
    monkeypatch.delenv("BEEBOX_API_TOKEN", raising=False)
    monkeypatch.setenv("BEEBOX_VIDEO_PLATFORM", "fake")
    monkeypatch.setenv("BEEBOX_VIDEO_FAKE_ROOT", str(FIXTURE_ROOT))
    monkeypatch.setenv("BEEBOX_INLINE_LOCAL_TRANSCRIPTION", "1")
    monkeypatch.setenv("BEEBOX_GIT_COMMIT", "0123456789abcdef0123456789abcdef01234567")
    _seed_valid_bilibili_login(tmp_path, monkeypatch)
    if asr_succeeds:
        fake_speech_env(
            monkeypatch,
            segments=[{"start": 0.0, "end": 2.0, "text": ASR_TEXT}],
            text=ASR_TEXT,
            duration=2.0,
            language="zh",
            aligned=True,
        )
    else:
        fake_speech_env(
            monkeypatch,
            fail={
                "stage": "any",
                "code": "ASR_SHOULD_NOT_RUN",
                "message": "ASR must not run for platform-subtitle jobs",
            },
        )
    from backend.app import create_app

    return TestClient(create_app())


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Subtitle path: any ASR run fails the job."""
    return _build_client(tmp_path, monkeypatch, asr_succeeds=False)


@pytest.fixture
def asr_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """ASR path: the fake engine succeeds with a recognisable transcript."""
    return _build_client(tmp_path, monkeypatch, asr_succeeds=True)


def create_url_job(
    client: TestClient,
    url: str,
    *,
    options: dict | None = None,
):
    payload: dict = {"kind": "transcription", "source": {"type": "url", "url": url}}
    if options is not None:
        payload["options"] = options
    return client.post("/v1/jobs", json=payload)


def wait_for(client: TestClient, job_id: str, *, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/v1/jobs/{job_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] in TERMINAL:
            return body
        time.sleep(0.05)
    pytest.fail(f"job {job_id} did not reach a terminal status: {body}")


def wait_for_gpu_queue_entry(client: TestClient, *, timeout: float = 10.0) -> None:
    """Block until a job waits in the (paused) GPU queue."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        queue = client.get("/v1/queue").json()
        if queue["length"] == 1:
            return
        time.sleep(0.02)
    pytest.fail(f"no job entered the GPU queue: {client.get('/v1/queue').json()}")


def run_job(client: TestClient, url: str, *, options: dict | None = None) -> dict:
    created = create_url_job(client, url, options=options)
    assert created.status_code == 201, created.text
    return wait_for(client, created.json()["id"])


# ---------------------------------------------------------------------------
# Priority order
# ---------------------------------------------------------------------------


def test_simplified_chinese_manual_beats_every_other_track(client: TestClient) -> None:
    finished = run_job(client, MULTI_LANGUAGE_URL)

    assert finished["status"] == "succeeded", finished
    assert finished["source"]["subtitle_language"] == "zh-Hans"
    assert finished["source"]["text_source"] == "platform_manual"
    assert finished["options"]["subtitle_language"] is None

    result = client.get(f"/v1/jobs/{finished['id']}/result").json()
    assert "欢迎来到 BeeBox。" in result["text"]
    assert "繁體人工字幕。" not in result["text"]
    assert "机器翻译" not in result["text"]
    assert [segment["text"] for segment in result["segments"]] == [
        "欢迎来到 BeeBox。",
        "这是平台人工字幕。",
        "最后一句。",
    ]


def test_chinese_auto_captions_are_used_and_cleaned(client: TestClient) -> None:
    finished = run_job(client, CHINESE_AUTO_URL)

    assert finished["status"] == "succeeded", finished
    assert finished["source"]["subtitle_language"] == "zh-Hans"
    assert finished["source"]["text_source"] == "platform_auto"

    result = client.get(f"/v1/jobs/{finished['id']}/result").json()
    # Rolling duplicates are gone and the fragments are merged into two
    # readable cues; punctuation is not restored.
    assert [segment["text"] for segment in result["segments"]] == [
        "大家好欢迎来到我的频道今天我们聊聊字幕选择",
        "先看人工字幕最后一句。",
    ]
    assert [(segment["start"], segment["end"]) for segment in result["segments"]] == [
        (0.32, 5.21),
        (5.21, 10.5),
    ]
    assert "大家好 欢迎来到我的频道 今天" not in result["text"]

    srt = client.get(f"/v1/jobs/{finished['id']}/exports/srt")
    assert srt.text == (
        "1\n"
        "00:00:00,320 --> 00:00:05,210\n"
        "大家好欢迎来到我的频道今天我们聊聊字幕选择\n"
        "\n"
        "2\n"
        "00:00:05,210 --> 00:00:10,500\n"
        "先看人工字幕最后一句。\n"
    )


def test_original_language_manual_beats_other_language_auto(client: TestClient) -> None:
    finished = run_job(client, ENGLISH_ORIGINAL_URL)

    assert finished["status"] == "succeeded", finished
    assert finished["source"]["subtitle_language"] == "en"
    assert finished["source"]["text_source"] == "platform_manual"

    result = client.get(f"/v1/jobs/{finished['id']}/result").json()
    assert result["segments"][0]["text"] == "This is the English manual subtitle."


def test_original_language_auto_is_used_when_no_original_manual_track_exists(client: TestClient) -> None:
    finished = run_job(client, JAPANESE_ORIGINAL_URL)

    assert finished["status"] == "succeeded", finished
    assert finished["source"]["subtitle_language"] == "ja"
    assert finished["source"]["text_source"] == "platform_auto"

    result = client.get(f"/v1/jobs/{finished['id']}/result").json()
    assert result["segments"][0]["text"] == "これは日本語の自動字幕です。"


def test_only_machine_translated_tracks_are_never_used_and_fall_back_to_asr(
    asr_client: TestClient,
) -> None:
    # A paused queue proves the translated tracks were rejected: the job takes
    # the ticket #44 audio/ASR path instead of completing from subtitles.
    assert asr_client.post("/v1/queue/pause").status_code == 200

    created = create_url_job(asr_client, TRANSLATED_ONLY_URL)
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]

    wait_for_gpu_queue_entry(asr_client)
    assert asr_client.get(f"/v1/jobs/{job_id}").json()["status"] == "queued"
    assert asr_client.post("/v1/queue/resume").status_code == 200
    finished = wait_for(asr_client, job_id)

    assert finished["status"] == "succeeded", finished
    assert finished["source"]["text_source"] == "asr"
    assert finished["source"]["subtitle_language"] is None

    result = asr_client.get(f"/v1/jobs/{job_id}/result").json()
    assert result["text"] == ASR_TEXT
    # Neither translated track supplied any text.
    assert "欢迎来到 BeeBox。" not in result["text"]
    assert "This is the English" not in result["text"]


# ---------------------------------------------------------------------------
# options.subtitle_language
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("requested", "expected_language", "expected_text_source", "expected_text"),
    [
        ("zh-Hant", "zh-Hant", "platform_manual", "繁體人工字幕。"),
        ("zh", "zh-Hans", "platform_manual", "欢迎来到 BeeBox。"),
        ("en", "en", "platform_manual", "This is the English manual subtitle."),
    ],
)
def test_subtitle_language_restricts_the_choice(
    client: TestClient,
    requested: str,
    expected_language: str,
    expected_text_source: str,
    expected_text: str,
) -> None:
    finished = run_job(
        client, MULTI_LANGUAGE_URL, options={"subtitle_language": requested}
    )

    assert finished["status"] == "succeeded", finished
    assert finished["source"]["subtitle_language"] == expected_language
    assert finished["source"]["text_source"] == expected_text_source
    assert finished["options"]["subtitle_language"] == requested

    result = client.get(f"/v1/jobs/{finished['id']}/result").json()
    assert expected_text in result["text"]


def test_subtitle_language_without_a_matching_track_fails_instead_of_falling_back(
    client: TestClient,
) -> None:
    finished = run_job(client, MULTI_LANGUAGE_URL, options={"subtitle_language": "fr"})

    assert finished["status"] == "failed", finished
    assert finished["error_code"] == "VIDEO_SUBTITLE_UNAVAILABLE"
    assert finished["options"]["subtitle_language"] == "fr"
    # The zh/en tracks existed, but a caller-specified language never falls
    # back to a different one.
    assert finished["source"]["subtitle_language"] is None


def test_malformed_subtitle_language_is_rejected_by_the_contract(client: TestClient) -> None:
    response = create_url_job(
        client, MULTI_LANGUAGE_URL, options={"subtitle_language": "not a language!"}
    )

    assert response.status_code == 422, response.text
    assert response.json()["error_code"] == "INVALID_REQUEST"


# ---------------------------------------------------------------------------
# options.prefer=asr
# ---------------------------------------------------------------------------


def test_prefer_asr_routes_to_the_asr_path_even_when_subtitles_exist(
    asr_client: TestClient,
) -> None:
    # A paused queue proves the decision took the ASR route rather than
    # silently completing from the available zh-Hans manual track.
    assert asr_client.post("/v1/queue/pause").status_code == 200

    created = create_url_job(asr_client, MULTI_LANGUAGE_URL, options={"prefer": "asr"})
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]

    wait_for_gpu_queue_entry(asr_client)
    assert asr_client.get(f"/v1/jobs/{job_id}").json()["status"] == "queued"
    assert asr_client.post("/v1/queue/resume").status_code == 200
    finished = wait_for(asr_client, job_id)

    assert finished["status"] == "succeeded", finished
    assert finished["options"]["prefer"] == "asr"
    assert finished["source"]["subtitle_language"] is None
    assert finished["source"]["text_source"] == "asr"

    result = asr_client.get(f"/v1/jobs/{job_id}/result").json()
    assert result["text"] == ASR_TEXT
    assert "欢迎来到 BeeBox。" not in result["text"]


def test_unknown_prefer_value_is_rejected_by_the_contract(client: TestClient) -> None:
    response = create_url_job(client, MULTI_LANGUAGE_URL, options={"prefer": "subtitle"})

    assert response.status_code == 422, response.text
    assert response.json()["error_code"] == "INVALID_REQUEST"


# ---------------------------------------------------------------------------
# Bilibili AI captions (ticket #65)
# ---------------------------------------------------------------------------


def test_bilibili_ai_chinese_caption_is_used_as_platform_auto(client: TestClient) -> None:
    finished = run_job(client, BILIBILI_AI_ZH_ONLY_URL)

    assert finished["status"] == "succeeded", finished
    assert finished["source"]["subtitle_language"] == "zh"
    assert finished["source"]["text_source"] == "platform_auto"

    result = client.get(f"/v1/jobs/{finished['id']}/result").json()
    assert "这是 B 站 AI 中文字幕。" in result["text"]
    assert "第二句。" in result["text"]


def test_manual_chinese_subtitles_beat_the_bilibili_ai_caption(client: TestClient) -> None:
    finished = run_job(client, BILIBILI_AI_AND_MANUAL_URL)

    assert finished["status"] == "succeeded", finished
    assert finished["source"]["subtitle_language"] == "zh-Hans"
    assert finished["source"]["text_source"] == "platform_manual"

    result = client.get(f"/v1/jobs/{finished['id']}/result").json()
    assert "欢迎来到 BeeBox。" in result["text"]
    assert "这是 B 站 AI 中文字幕。" not in result["text"]


def test_subtitle_language_zh_matches_the_bilibili_ai_caption(client: TestClient) -> None:
    finished = run_job(
        client, BILIBILI_AI_ZH_ONLY_URL, options={"subtitle_language": "zh"}
    )

    assert finished["status"] == "succeeded", finished
    assert finished["options"]["subtitle_language"] == "zh"
    assert finished["source"]["subtitle_language"] == "zh"
    assert finished["source"]["text_source"] == "platform_auto"

    result = client.get(f"/v1/jobs/{finished['id']}/result").json()
    assert "这是 B 站 AI 中文字幕。" in result["text"]


def test_bilibili_ai_translation_is_never_selected(asr_client: TestClient) -> None:
    # The video is Chinese and its only AI track is the machine-translated
    # English one, so the job must fall through to speech recognition.
    finished = run_job(asr_client, BILIBILI_AI_TRANSLATION_URL)

    assert finished["status"] == "succeeded", finished
    assert finished["source"]["text_source"] == "asr"
    assert finished["source"]["subtitle_language"] is None

    result = asr_client.get(f"/v1/jobs/{finished['id']}/result").json()
    assert result["text"] == ASR_TEXT
    assert "This is the English manual subtitle." not in result["text"]

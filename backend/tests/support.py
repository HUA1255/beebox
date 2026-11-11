"""Test support: a real tiny WAV and fake-engine configuration helpers.

Importable from test modules because pytest puts ``backend/tests`` on
``sys.path`` (the tests are not a package). Keeping it out of ``conftest.py``
lets module-level helpers seed a valid upload payload.
"""

from __future__ import annotations

import io
import json
import math
import struct
import wave
from typing import Any

SAMPLE_RATE = 16_000
DEFAULT_FIXTURE_SECONDS = 1.0


def make_wav_bytes(duration_seconds: float = DEFAULT_FIXTURE_SECONDS, *, sample_rate: int = SAMPLE_RATE) -> bytes:
    """A real, ffprobe-decodable mono PCM WAV (a quiet 440 Hz tone)."""
    frame_count = max(1, int(sample_rate * max(0.05, duration_seconds)))
    frames = bytearray()
    for index in range(frame_count):
        sample = int(1200 * math.sin(2 * math.pi * 440 * index / sample_rate))
        frames += struct.pack("<h", sample)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(bytes(frames))
    return buffer.getvalue()


def fake_speech_payload(**config: Any) -> dict[str, Any]:
    """The JSON object accepted by ``BEEBOX_SPEECH_FAKE_RESULT``."""
    return config


def fake_speech_env(monkeypatch, **config: Any) -> None:
    """Select the fake engine and script its output via environment variables."""
    monkeypatch.setenv("BEEBOX_SPEECH_ENGINE", "fake")
    monkeypatch.setenv("BEEBOX_SPEECH_FAKE_RESULT", json.dumps(fake_speech_payload(**config), ensure_ascii=False))


BILIBILI_COOKIE_FILENAME = "bilibili-cookies.txt"
#: A valid-looking credential. It is a test sentinel, never a real one.
BILIBILI_TEST_SESSDATA = "test-sessdata-value"
BILIBILI_LOGIN_TTL_SECONDS = 30 * 24 * 3600


def seed_bilibili_login(
    secrets_dir: Path, *, expires_at: int | None = None
) -> Path:
    """Write the Netscape cookie file that makes a Bilibili login ``valid``.

    Ticket #66 refuses Bilibili jobs while the stored login is absent or
    expired (ADR-0012), so any test that expects a Bilibili link to proceed
    seeds this file first. The values are sentinels, not credentials.
    """
    import time

    secrets_dir.mkdir(parents=True, exist_ok=True)
    expiry = int(expires_at if expires_at is not None else time.time() + BILIBILI_LOGIN_TTL_SECONDS)
    path = secrets_dir / BILIBILI_COOKIE_FILENAME
    path.write_text(
        "# Netscape HTTP Cookie File\n"
        f"#HttpOnly_.bilibili.com\tTRUE\t/\tTRUE\t{expiry}\tSESSDATA\t{BILIBILI_TEST_SESSDATA}\n"
        f".bilibili.com\tTRUE\t/\tTRUE\t{expiry}\tbili_jct\ttest-bili-jct-value\n",
        encoding="utf-8",
    )
    return path

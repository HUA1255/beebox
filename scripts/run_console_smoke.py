"""Start BeeBox for the console Playwright smoke test (ticket #49).

The console smoke test must not need a GPU, model files or the network. This
launcher assembles exactly that backend:

* the fake speech engine (``BEEBOX_SPEECH_ENGINE=fake``);
* the fake video platform rooted at the checked-in fixtures;
* a throwaway data directory;
* a local model archive (``BEEBOX_MODEL_SOURCE_DIR``) holding one small
  whisper-base model, so the models page can download without the network;
* the fake Bilibili QR-login client and fake refresh client, rooted at the
  throwaway secrets directory, plus an expired cookie file so the credentials
  page starts in the ``expired`` state the console must warn about (the fake
  refresh also reports the login expired, so the startup check cannot renew
  the seeded file);
* a known Bearer token;
* the built console from ``console/dist``.

The seed copy sleeps between 1 MiB chunks (``BEEBOX_MODEL_SOURCE_COPY_DELAY_MS``)
so the smoke test can watch progress and cancel an in-flight download. The
fake QR payload carries a generated PNG so the credentials page renders a real
image without a network round trip.

Usage (from the repository root)::

    python scripts/run_console_smoke.py --port 17497

``BEEBOX_CONSOLE_SMOKE_TOKEN`` overrides the token; Playwright reads the same
variable. The process stays in the foreground until Playwright stops it.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import struct
import sys
import tempfile
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOKEN = "console-smoke-token"
FAKE_VIDEO_ROOT = REPO_ROOT / "backend" / "tests" / "fixtures" / "video_platform"
CONSOLE_DIST = REPO_ROOT / "console" / "dist"
SMOKE_MODEL_ID = "whisper-base"
SMOKE_MODEL_WEIGHT_BYTES = 24 * 1024 * 1024
SMOKE_MODEL_COPY_DELAY_MS = 120
BILIBILI_COOKIE_FILENAME = "bilibili-cookies.txt"
# Distinctive values the Playwright spec scans the page, responses and console
# log for; neither may ever be rendered.
EXPIRED_SESSDATA = "smoke-expired-sessdata-secret-52"
FAKE_LOGIN_SECRETS = {
    "SESSDATA": "smoke-login-sessdata-secret-52",
    "bili_jct": "smoke-login-bili-jct-secret-52",
    "DedeUserID": "10052",
    "ac_time_value": "smoke-login-ac-time-secret-52",
}

FAKE_SPEECH_RESULT = {
    "language": "zh",
    "text": "这是冒烟测试转写结果。第二段用于验证结果预览。",
    "segments": [
        {"start": 0.0, "end": 1.0, "text": "这是冒烟测试转写结果。"},
        {"start": 1.0, "end": 2.0, "text": "第二段用于验证结果预览。"},
    ],
    "duration": 2.0,
    "aligned": True,
    # Keep the file job in `running` long enough for the smoke test to observe
    # the list/detail views before polling moves them to `succeeded`.
    "delay_seconds": 6,
}


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def qr_like_png(modules: int = 25, scale: int = 8) -> str:
    """Build a deterministic QR-looking PNG as a data URL.

    The fake login client needs no image bytes, but the console must render an
    ``<img>``; a generated black-and-white grid keeps the smoke test offline.
    """
    pixels = modules * scale
    rows = []
    for y in range(pixels):
        row = bytearray()
        for x in range(pixels):
            column = x // scale
            line = y // scale
            dark = (
                column in (0, modules - 1)
                or line in (0, modules - 1)
                or (column * 7 + line * 13) % 5 < 2
            )
            row.extend(b"\x00\x00\x00" if dark else b"\xff\xff\xff")
        rows.append(b"\x00" + bytes(row))
    header = struct.pack(">IIBBBBB", pixels, pixels, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + _png_chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def fake_login_config() -> dict:
    """The scripted QR login: pending, scanned, then confirmed."""
    return {
        "states": ["pending", "scanned", "confirmed"],
        "cookies": dict(FAKE_LOGIN_SECRETS),
        "qr_url": "https://fake.bilibili.test/qr/console-smoke",
        "qr_image": qr_like_png(),
    }


def seed_expired_bilibili_cookie(secrets_dir: Path) -> None:
    """Start the credentials page in the ``expired`` state.

    The console must call out an expired Bilibili login. The QR flow later
    replaces this file with a valid credential, so the same smoke run covers
    both states. This value is a sentinel the tests scan for.
    """
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / BILIBILI_COOKIE_FILENAME).write_text(
        "# Netscape HTTP Cookie File\n"
        f"#HttpOnly_.bilibili.com\tTRUE\t/\tTRUE\t1000\tSESSDATA\t{EXPIRED_SESSDATA}\n",
        encoding="utf-8",
    )


def seed_model_source(data_dir: Path) -> Path:
    """Write a downloadable whisper-base into the offline model archive.

    The file set satisfies the engine's integrity check (config, tokenizer,
    preprocessor, weights); the weight file is spread over many 1 MiB chunks so
    the seeded per-chunk delay keeps the download observable.
    """
    source = data_dir / "model-source" / SMOKE_MODEL_ID
    source.mkdir(parents=True, exist_ok=True)
    (source / "config.json").write_text('{"model_type": "whisper"}', encoding="utf-8")
    (source / "tokenizer.json").write_text('{"version": "1.0"}', encoding="utf-8")
    (source / "preprocessor_config.json").write_text("{}", encoding="utf-8")
    chunk = b"\0" * (1024 * 1024)
    with (source / "model.safetensors").open("wb") as handle:
        for _ in range(SMOKE_MODEL_WEIGHT_BYTES // len(chunk)):
            handle.write(chunk)
    return data_dir / "model-source"


def configure_environment(data_dir: Path, token: str, model_source: Path) -> None:
    os.environ.update(
        {
            "BEEBOX_DATA_DIR": str(data_dir),
            # Explicit so the seeded expired credential and the confirmed QR
            # login live in the throwaway directory, never in a checkout.
            "BEEBOX_SECRETS_DIR": str(data_dir / "secrets"),
            "BEEBOX_MODEL_STORAGE_ROOT": str(data_dir / "models"),
            "BEEBOX_MODEL_SOURCE_DIR": str(model_source),
            "BEEBOX_MODEL_SOURCE_COPY_DELAY_MS": str(SMOKE_MODEL_COPY_DELAY_MS),
            "BEEBOX_API_TOKEN": token,
            "BEEBOX_SPEECH_ENGINE": "fake",
            "BEEBOX_SPEECH_FAKE_RESULT": json.dumps(FAKE_SPEECH_RESULT, ensure_ascii=False),
            "BEEBOX_VIDEO_PLATFORM": "fake",
            "BEEBOX_VIDEO_FAKE_ROOT": str(FAKE_VIDEO_ROOT),
            "BEEBOX_BILIBILI_LOGIN": "fake",
            "BEEBOX_BILIBILI_LOGIN_FAKE": json.dumps(
                fake_login_config(), ensure_ascii=False
            ),
            # The startup refresh then keeps the seeded credential expired
            # instead of renewing it with a fresh TTL (see #47).
            "BEEBOX_BILIBILI_REFRESH_FAKE": json.dumps({"outcome": "expired"}),
            "BEEBOX_CONSOLE_DIR": str(CONSOLE_DIST),
            "BEEBOX_INLINE_LOCAL_TRANSCRIPTION": "1",
            "BEEBOX_DELETE_DERIVED_ON_COMPLETE": "1",
            "BEEBOX_GIT_COMMIT": "console-smoke",
            "PYTHONUTF8": "1",
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="BeeBox console smoke-test server")
    parser.add_argument("--port", type=int, default=17497)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--data-dir", default=None)
    args = parser.parse_args()

    if not (CONSOLE_DIST / "index.html").is_file():
        raise SystemExit(
            f"Console build not found at {CONSOLE_DIST}. Run `npm ci && npm run build` in console/."
        )

    data_dir = Path(args.data_dir).expanduser() if args.data_dir else Path(
        tempfile.mkdtemp(prefix="beebox-console-smoke-")
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    # A configured storage root must exist: downloads refuse to invent one.
    (data_dir / "models").mkdir(parents=True, exist_ok=True)
    model_source = seed_model_source(data_dir)
    seed_expired_bilibili_cookie(data_dir / "secrets")
    token = os.environ.get("BEEBOX_CONSOLE_SMOKE_TOKEN", DEFAULT_TOKEN).strip() or DEFAULT_TOKEN
    configure_environment(data_dir, token, model_source)

    # The script lives in scripts/, so the repository root is not on sys.path.
    sys.path.insert(0, str(REPO_ROOT))

    import uvicorn

    from backend.app import create_app

    print(f"BeeBox console smoke server: http://{args.host}:{args.port}/console (data={data_dir})")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

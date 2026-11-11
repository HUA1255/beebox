"""Production yt-dlp adapter contracts for ticket #45.

yt-dlp is replaced by an in-process scripted module (the same seam yt-dlp
occupies in production), so the tests exercise the real adapter code without
a network. They pin how yt-dlp's failures become typed platform errors, how a
Bilibili multi-part playlist is narrowed to one part, and that the download
proxy reaches yt-dlp options.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

import pytest

from backend.services.video_platform import (
    DownloadNetworkError,
    DownloadOptions,
    DownloadTimeoutError,
    InvalidUrlError,
    LocalToolMissingError,
    LoginRequiredError,
    SubtitleTrack,
    UnsupportedUrlError,
    VideoUnavailableError,
    parse_video_url,
)
from backend.services.video_platform.yt_dlp import YtDlpVideoPlatform


class ScriptedYoutubeDL:
    """Stand-in for ``yt_dlp.YoutubeDL`` with one scripted answer."""

    instances: list["ScriptedYoutubeDL"] = []

    def __init__(self, options: dict) -> None:
        self.options = options
        self.__class__.instances.append(self)

    def __enter__(self) -> "ScriptedYoutubeDL":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    def extract_info(self, url: str, download: bool = False):
        raise NotImplementedError


class AudioYoutubeDL(ScriptedYoutubeDL):
    """Stand-in for the audio download plus the WAV postprocessing step."""

    def extract_info(self, url: str, download: bool = False):
        target = Path(self.options["outtmpl"].replace("%(ext)s", "wav"))
        target.write_bytes(b"RIFFfake")
        return {"ext": "m4a"}

    def prepare_filename(self, info) -> str:
        # Real yt-dlp reports the pre-postprocessing name, which no longer exists.
        return str(self.options["outtmpl"].replace("%(ext)s", str(info["ext"])))


@pytest.fixture(autouse=True)
def _reset_scripted_instances() -> None:
    ScriptedYoutubeDL.instances.clear()
    yield
    ScriptedYoutubeDL.instances.clear()


def install(monkeypatch: pytest.MonkeyPatch, klass: type) -> None:
    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = klass
    monkeypatch.setitem(sys.modules, "yt_dlp", module)


def raising(message: str) -> type:
    class RaisingYoutubeDL(ScriptedYoutubeDL):
        def extract_info(self, url: str, download: bool = False):
            raise RuntimeError(message)

    return RaisingYoutubeDL


def returning(payload: dict) -> type:
    class ReturningYoutubeDL(ScriptedYoutubeDL):
        def extract_info(self, url: str, download: bool = False):
            return payload

    return ReturningYoutubeDL


@pytest.mark.parametrize(
    ("message", "error_type", "code"),
    [
        (
            "Unable to download webpage: <urlopen error [WinError 10061] "
            "No connection could be made because the target machine actively refused it>",
            DownloadNetworkError,
            "DOWNLOAD_NETWORK_ERROR",
        ),
        (
            "Unable to connect to proxy http://127.0.0.1:9: <urlopen error [Errno 111] "
            "Connection refused>",
            DownloadNetworkError,
            "DOWNLOAD_NETWORK_ERROR",
        ),
        ("The read operation timed out", DownloadTimeoutError, "DOWNLOAD_TIMEOUT"),
        (
            "Sign in to confirm you're not a bot. Use --cookies-from-browser",
            LoginRequiredError,
            "VIDEO_LOGIN_REQUIRED",
        ),
        ("This video is private", VideoUnavailableError, "VIDEO_NOT_ACCESSIBLE"),
        (
            "Video unavailable. This video is not available in your country",
            VideoUnavailableError,
            "VIDEO_NOT_ACCESSIBLE",
        ),
        (
            "This video is available to this channel's members only",
            VideoUnavailableError,
            "VIDEO_NOT_ACCESSIBLE",
        ),
        ("HTTP Error 404: Not Found", VideoUnavailableError, "VIDEO_NOT_ACCESSIBLE"),
        ("Unsupported URL: https://example.com/nope", UnsupportedUrlError, "UNSUPPORTED_URL"),
        ("'nope' is not a valid URL", InvalidUrlError, "INVALID_URL"),
        (
            "Postprocessing: ffprobe and ffmpeg not found. "
            "Please install or provide the path using --ffmpeg-location",
            LocalToolMissingError,
            "LOCAL_TOOL_MISSING",
        ),
        ("Something unexpected broke", DownloadNetworkError, "DOWNLOAD_NETWORK_ERROR"),
    ],
)
def test_yt_dlp_failures_become_typed_platform_errors(
    monkeypatch: pytest.MonkeyPatch, message: str, error_type: type, code: str
) -> None:
    install(monkeypatch, raising(message))
    platform = YtDlpVideoPlatform()
    ref = parse_video_url("https://www.bilibili.com/video/BV1Gt411c7mD")

    with pytest.raises(error_type) as error:
        platform.fetch_video_info(ref)

    assert error.value.code == code
    assert message in error.value.message


def test_yt_dlp_timeout_exception_is_a_download_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TimeoutYoutubeDL(ScriptedYoutubeDL):
        def extract_info(self, url: str, download: bool = False):
            raise TimeoutError("connection timed out")

    install(monkeypatch, TimeoutYoutubeDL)
    platform = YtDlpVideoPlatform()

    with pytest.raises(DownloadTimeoutError):
        platform.fetch_video_info(parse_video_url("https://youtu.be/dQw4w9WgXcQ"))


def test_yt_dlp_playlist_metadata_is_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    install(
        monkeypatch,
        returning(
            {
                "_type": "playlist",
                "entries": [{"id": "a", "title": "A"}],
            }
        ),
    )
    platform = YtDlpVideoPlatform()

    with pytest.raises(UnsupportedUrlError):
        platform.fetch_video_info(
            parse_video_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        )


def test_yt_dlp_live_metadata_is_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, returning({"is_live": True, "title": "Live"}))
    platform = YtDlpVideoPlatform()

    with pytest.raises(UnsupportedUrlError):
        platform.fetch_video_info(
            parse_video_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        )


def test_yt_dlp_bilibili_multi_part_playlist_keeps_only_the_selected_part(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(
        monkeypatch,
        returning(
            {
                "_type": "multi_video",
                "entries": [
                    {
                        "id": "BV1Part1",
                        "title": "第一集",
                        "duration": 1.0,
                        "subtitles": {
                            "zh-Hans": [
                                {"ext": "srt", "url": "https://example.test/first.srt"}
                            ]
                        },
                    },
                    {
                        "id": "BV1Part2",
                        "title": "第二集",
                        "duration": 2.0,
                        "subtitles": {
                            "zh-Hans": [
                                {"ext": "srt", "url": "https://example.test/second.srt"}
                            ]
                        },
                    },
                ],
            }
        ),
    )
    platform = YtDlpVideoPlatform()
    ref = parse_video_url("https://www.bilibili.com/video/BV1Gt411c7mD?p=2")

    info = platform.fetch_video_info(ref)

    assert info.title == "第二集"
    assert info.part_title == "第二集"
    assert info.duration_ms == 2000
    assert [track.url for track in info.tracks] == ["https://example.test/second.srt"]
    [instance] = ScriptedYoutubeDL.instances
    assert instance.options["playlist_items"] == "2"


def test_yt_dlp_already_narrowed_multi_part_playlist_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(
        monkeypatch,
        returning(
            {
                "_type": "multi_video",
                "entries": [
                    {
                        "id": "BV1Part2",
                        "title": "第二集",
                        "duration": 2.0,
                        "subtitles": {},
                    }
                ],
            }
        ),
    )
    platform = YtDlpVideoPlatform()

    info = platform.fetch_video_info(
        parse_video_url("https://www.bilibili.com/video/BV1Gt411c7mD?p=2")
    )

    assert info.title == "第二集"
    assert info.part_title == "第二集"


def test_yt_dlp_gets_the_configured_proxy_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(
        monkeypatch,
        returning({"title": "Sample", "subtitles": {}}),
    )
    platform = YtDlpVideoPlatform()

    platform.fetch_video_info(
        parse_video_url("https://www.bilibili.com/video/BV1Gt411c7mD"),
        options=DownloadOptions(proxy="http://127.0.0.1:3128", timeout_seconds=12),
    )

    [instance] = ScriptedYoutubeDL.instances
    assert instance.options["proxy"] == "http://127.0.0.1:3128"
    assert instance.options["socket_timeout"] == 12
    assert instance.options["noplaylist"] is True


def test_yt_dlp_falls_back_to_the_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BEEBOX_DOWNLOAD_PROXY", "http://127.0.0.1:7890")
    install(
        monkeypatch,
        returning({"title": "Sample", "subtitles": {}}),
    )
    platform = YtDlpVideoPlatform()

    platform.fetch_video_info(parse_video_url("https://www.bilibili.com/video/BV1Gt411c7mD"))

    [instance] = ScriptedYoutubeDL.instances
    assert instance.options["proxy"] == "http://127.0.0.1:7890"


def test_yt_dlp_subtitle_download_receives_the_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SubtitleYoutubeDL(ScriptedYoutubeDL):
        def urlopen(self, request):
            return types.SimpleNamespace(
                read=lambda: b"1\n00:00:00,000 --> 00:00:01,000\nHi.\n"
            )

    install(monkeypatch, SubtitleYoutubeDL)
    platform = YtDlpVideoPlatform()
    track = SubtitleTrack(
        language="en",
        kind="manual",
        format="srt",
        url="https://example.test/captions.srt",
    )

    document = platform.download_subtitle(
        track, options=DownloadOptions(proxy="http://127.0.0.1:3128")
    )

    assert [cue.text for cue in document.cues] == ["Hi."]
    [instance] = ScriptedYoutubeDL.instances
    assert instance.options["proxy"] == "http://127.0.0.1:3128"


def test_bilibili_ai_tracks_are_reported_as_automatic() -> None:
    """Bilibili publishes AI captions under "subtitles" with an ai- prefix."""
    from backend.services.video_platform.yt_dlp import _tracks

    info = {
        "subtitles": {
            "ai-zh": [{"ext": "srt", "url": "https://example.test/ai-zh.srt"}],
            "zh-CN": [{"ext": "srt", "url": "https://example.test/zh-CN.srt"}],
        }
    }

    tracks = {track.language: track.kind for track in _tracks(info, video_id="BV1")}

    assert tracks == {"ai-zh": "auto", "zh-CN": "manual"}


def test_metadata_options_request_subtitle_listing() -> None:
    """yt-dlp only fills info["subtitles"] when subtitle output is requested."""
    from backend.services.video_platform.yt_dlp import YtDlpVideoPlatform

    assert YtDlpVideoPlatform().yt_dlp_options()["writesubtitles"] is True


def test_inline_subtitle_payload_is_parsed_without_a_url() -> None:
    """Bilibili returns the caption file inline instead of a download URL."""
    platform = _platform()
    track = SubtitleTrack(
        language="ai-zh",
        kind="auto",
        format="srt",
        content="1\n00:00:00,000 --> 00:00:01,000\n你好\n",
    )

    document = platform.download_subtitle(track)

    assert [cue.text for cue in document.cues] == ["你好"]
    assert document.kind == "auto"


def test_yt_dlp_audio_download_receives_the_proxy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install(monkeypatch, AudioYoutubeDL)
    platform = YtDlpVideoPlatform()
    destination = tmp_path / "audio.wav"

    platform.download_audio(
        parse_video_url("https://www.bilibili.com/video/BV1Gt411c7mD?p=2"),
        destination,
        options=DownloadOptions(proxy="http://127.0.0.1:3128"),
    )

    assert destination.read_bytes() == b"RIFFfake"
    [instance] = ScriptedYoutubeDL.instances
    assert instance.options["proxy"] == "http://127.0.0.1:3128"
    assert instance.options["format"] == "bestaudio"
    assert instance.options["playlist_items"] == "2"


def test_audio_download_tells_yt_dlp_where_ffmpeg_lives(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ticket #85: the native install keeps ffmpeg under <root>\\tools, not PATH.

    ``FFmpegExtractAudio`` would otherwise look only on ``PATH`` and fail the
    whole job even though BeeBox already resolved the binaries itself. The
    value must be the executable, not its directory: yt-dlp resolves a
    directory to extensionless ``ffmpeg``/``ffprobe`` names on every platform.
    """
    from backend.services import ffmpeg_tools
    from backend.services.ffmpeg_tools import ToolStatus

    bin_dir = tmp_path / "tools" / "ffmpeg" / "bin"
    bin_dir.mkdir(parents=True)
    ffmpeg = bin_dir / "ffmpeg.exe"
    ffprobe = bin_dir / "ffprobe.exe"
    ffmpeg.write_bytes(b"stub")
    ffprobe.write_bytes(b"stub")
    monkeypatch.setattr(
        ffmpeg_tools,
        "resolve_tools",
        lambda **_kwargs: {
            "ffmpeg": ToolStatus(True, str(ffmpeg), "bundled", "ffmpeg version test"),
            "ffprobe": ToolStatus(True, str(ffprobe), "bundled", "ffprobe version test"),
        },
    )

    install(monkeypatch, AudioYoutubeDL)
    platform = YtDlpVideoPlatform()

    platform.download_audio(
        parse_video_url("https://www.bilibili.com/video/BV1Gt411c7mD"),
        tmp_path / "audio.wav",
    )

    [instance] = ScriptedYoutubeDL.instances
    assert instance.options["ffmpeg_location"] == str(ffmpeg)


def test_audio_download_omits_ffmpeg_location_when_no_tool_is_resolved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing tool must be reported by yt-dlp, not hidden behind a bogus path."""
    from backend.services import ffmpeg_tools
    from backend.services.ffmpeg_tools import ToolStatus

    monkeypatch.setattr(
        ffmpeg_tools,
        "resolve_tools",
        lambda **_kwargs: {
            "ffmpeg": ToolStatus(False, None, "missing", error="ffmpeg was not found"),
            "ffprobe": ToolStatus(False, None, "missing", error="ffprobe was not found"),
        },
    )

    install(monkeypatch, AudioYoutubeDL)
    platform = YtDlpVideoPlatform()

    platform.download_audio(
        parse_video_url("https://www.bilibili.com/video/BV1Gt411c7mD"),
        tmp_path / "audio.wav",
    )

    [instance] = ScriptedYoutubeDL.instances
    assert "ffmpeg_location" not in instance.options


def test_audio_download_does_not_request_subtitle_sidecars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ticket #85: subtitle output belongs to metadata, not the audio temp dir."""
    install(monkeypatch, AudioYoutubeDL)
    platform = YtDlpVideoPlatform()

    platform.download_audio(
        parse_video_url("https://www.bilibili.com/video/BV1Gt411c7mD"),
        tmp_path / "audio.wav",
    )

    [instance] = ScriptedYoutubeDL.instances
    assert instance.options["writesubtitles"] is False


def test_audio_download_selects_the_wav_among_leftover_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bilibili writes a danmaku subtitle next to the audio in the same directory."""

    class NoisyAudioYoutubeDL(AudioYoutubeDL):
        def extract_info(self, url: str, download: bool = False):
            info = super().extract_info(url, download=download)
            parent = Path(self.options["outtmpl"]).parent
            (parent / "audio.m4a").write_bytes(b"compressed")
            (parent / "audio.danmaku.xml").write_bytes(b"<i/>")
            return info

    install(monkeypatch, NoisyAudioYoutubeDL)
    platform = YtDlpVideoPlatform()
    destination = tmp_path / "audio.wav"

    platform.download_audio(
        parse_video_url("https://www.bilibili.com/video/BV1Gt411c7mD"),
        destination,
    )

    assert destination.read_bytes() == b"RIFFfake"


def test_audio_download_maps_a_missing_ffmpeg_to_a_local_tool_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The exact ticket #85 failure happens in the audio postprocessor."""
    install(
        monkeypatch,
        raising(
            "Postprocessing: ffprobe and ffmpeg not found. "
            "Please install or provide the path using --ffmpeg-location"
        ),
    )
    platform = YtDlpVideoPlatform()

    with pytest.raises(LocalToolMissingError) as error:
        platform.download_audio(
            parse_video_url("https://www.bilibili.com/video/BV1Gt411c7mD"),
            tmp_path / "audio.wav",
        )

    assert error.value.code == "LOCAL_TOOL_MISSING"


# ---------------------------------------------------------------------------
# Ticket #48: YouTube PO token provider and the uploaded cookie jar
# ---------------------------------------------------------------------------

YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
COOKIE_CONTENT = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tsecret-value\n"
)


class RecordingYoutubeDL:
    """Stand-in for yt-dlp's ``YoutubeDL`` that records the options."""

    instances: list["RecordingYoutubeDL"] = []
    error: Exception | None = None

    def __init__(self, options: dict) -> None:
        self.options = options
        # Ticket #67: the session sees a temporary copy, not the real file;
        # remember what it saw because the adapter drops the key on close.
        self.cookiefile = Path(options["cookiefile"]) if options.get("cookiefile") else None
        self.cookie_bytes = self.cookiefile.read_bytes() if self.cookiefile else None
        self.calls: list[tuple[str, bool]] = []
        RecordingYoutubeDL.instances.append(self)

    def __enter__(self) -> "RecordingYoutubeDL":
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def extract_info(self, url: str, download: bool = False):
        self.calls.append((url, download))
        if RecordingYoutubeDL.error is not None:
            raise RecordingYoutubeDL.error
        return {"title": "Sample", "duration": 1.0, "subtitles": {}}


@pytest.fixture(autouse=True)
def _reset_recorder() -> None:
    RecordingYoutubeDL.instances = []
    RecordingYoutubeDL.error = None
    yield
    RecordingYoutubeDL.instances = []
    RecordingYoutubeDL.error = None


def _platform() -> YtDlpVideoPlatform:
    return YtDlpVideoPlatform(ydl_factory=RecordingYoutubeDL)


def _provider_server_home(tmp_path: Path) -> Path:
    server_home = tmp_path / "tools" / "bgutil-ytdlp-pot-provider" / "server"
    (server_home / "src").mkdir(parents=True)
    (server_home / "src" / "generate_once.ts").write_text("// stub", encoding="utf-8")
    return server_home


def _deno(tmp_path: Path) -> Path:
    deno = tmp_path / "tools" / "deno" / "deno.exe"
    deno.parent.mkdir(parents=True)
    deno.write_bytes(b"stub")
    return deno


def _cookie_file(tmp_path: Path) -> Path:
    cookie = tmp_path / "secrets" / "youtube-cookies.txt"
    cookie.parent.mkdir(parents=True)
    cookie.write_text(COOKIE_CONTENT, encoding="utf-8")
    return cookie


def test_default_youtube_options_enable_bgutil_script_and_deno(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BEEBOX_YOUTUBE_PO_TOKEN_PROVIDER", raising=False)
    monkeypatch.setenv("BEEBOX_BGUTIL_SERVER_HOME", str(_provider_server_home(tmp_path)))
    monkeypatch.setenv("BEEBOX_DENO_PATH", str(_deno(tmp_path)))
    cookie_file = _cookie_file(tmp_path)
    monkeypatch.setenv("BEEBOX_YOUTUBE_COOKIE_FILE", str(cookie_file))

    _platform().fetch_video_info(parse_video_url(YOUTUBE_URL))

    options = RecordingYoutubeDL.instances[-1].options
    assert options["js_runtimes"] == {"deno": {"path": str(tmp_path / "tools" / "deno" / "deno.exe")}}
    assert options["extractor_args"]["youtubepot-bgutilscript"]["server_home"] == [
        str(tmp_path / "tools" / "bgutil-ytdlp-pot-provider" / "server")
    ]
    instance = RecordingYoutubeDL.instances[-1]
    # Ticket #67: the session reads a temporary copy, never the real jar.
    assert instance.cookiefile is not None
    assert instance.cookiefile != cookie_file
    assert instance.cookie_bytes == cookie_file.read_bytes()


def test_http_provider_mode_uses_the_configured_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEEBOX_YOUTUBE_PO_TOKEN_PROVIDER", "bgutil-http")
    monkeypatch.setenv("BEEBOX_BGUTIL_HTTP_BASE_URL", "http://127.0.0.1:5516")
    monkeypatch.setenv("BEEBOX_DENO_PATH", str(_deno(tmp_path)))

    _platform().fetch_video_info(parse_video_url(YOUTUBE_URL))

    options = RecordingYoutubeDL.instances[-1].options
    assert options["extractor_args"]["youtubepot-bgutilhttp"]["base_url"] == [
        "http://127.0.0.1:5516"
    ]
    assert "youtubepot-bgutilscript" not in options["extractor_args"]


def test_provider_can_be_disabled_and_then_no_provider_options_are_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEEBOX_YOUTUBE_PO_TOKEN_PROVIDER", "off")
    monkeypatch.setenv("BEEBOX_DENO_PATH", str(_deno(tmp_path)))
    monkeypatch.delenv("BEEBOX_YOUTUBE_COOKIE_FILE", raising=False)
    monkeypatch.delenv("BEEBOX_BGUTIL_SERVER_HOME", raising=False)

    _platform().fetch_video_info(parse_video_url(YOUTUBE_URL))

    options = RecordingYoutubeDL.instances[-1].options
    assert "extractor_args" not in options
    assert "cookiefile" not in options


@pytest.mark.parametrize(
    "message",
    [
        "Sign in to confirm you're not a bot",
        "This video is age-restricted",
        "Sign in to confirm your age. This video may be inappropriate for some users.",
    ],
)
def test_age_and_bot_checks_map_to_video_login_required(
    message: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    RecordingYoutubeDL.error = Exception(message)

    with pytest.raises(LoginRequiredError) as error:
        _platform().fetch_video_info(parse_video_url(YOUTUBE_URL))

    assert error.value.code == "VIDEO_LOGIN_REQUIRED"


def test_missing_provider_files_do_not_break_metadata_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BEEBOX_YOUTUBE_PO_TOKEN_PROVIDER", raising=False)
    monkeypatch.setenv("BEEBOX_BGUTIL_SERVER_HOME", str(tmp_path / "missing"))
    monkeypatch.delenv("BEEBOX_DENO_PATH", raising=False)
    monkeypatch.delenv("BEEBOX_TOOLS_DIR", raising=False)
    monkeypatch.delenv("BEEBOX_ROOT", raising=False)

    info = _platform().fetch_video_info(parse_video_url(YOUTUBE_URL))

    assert info.title == "Sample"
    options = RecordingYoutubeDL.instances[-1].options
    assert "extractor_args" not in options


def test_youtube_never_receives_the_bilibili_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    bilibili_cookie = secrets / "bilibili-cookies.txt"
    bilibili_cookie.write_text(
        "# Netscape HTTP Cookie File\n"
        ".bilibili.com\tTRUE\t/\tTRUE\t2147483647\tSESSDATA\tsecret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BEEBOX_SECRETS_DIR", str(secrets))
    monkeypatch.delenv("BEEBOX_YOUTUBE_COOKIE_FILE", raising=False)
    monkeypatch.setenv("BEEBOX_YOUTUBE_PO_TOKEN_PROVIDER", "off")

    _platform().fetch_video_info(parse_video_url(YOUTUBE_URL))
    youtube_session = RecordingYoutubeDL.instances[-1]
    assert youtube_session.cookiefile is None
    assert "cookiefile" not in youtube_session.options

    _platform().fetch_video_info(
        parse_video_url("https://www.bilibili.com/video/BV1Gt411c7mD")
    )
    bilibili_session = RecordingYoutubeDL.instances[-1]
    # Ticket #67: the session gets a throwaway copy of the Bilibili credential,
    # never the real file and never the YouTube jar.
    assert bilibili_session.cookiefile is not None
    assert bilibili_session.cookiefile != bilibili_cookie
    assert bilibili_session.cookie_bytes == bilibili_cookie.read_bytes()


# ---------------------------------------------------------------------------
# Ticket #67: a download session must never modify the credential files
# ---------------------------------------------------------------------------

BILIBILI_VIDEO_URL = "https://www.bilibili.com/video/BV1Gt411c7mD"
BILIBILI_COOKIE_CONTENT = (
    "# Netscape HTTP Cookie File\n"
    "#HttpOnly_.bilibili.com\tTRUE\t/\tTRUE\t2147483647\tSESSDATA\tbilibili-secret\n"
)
YOUTUBE_COOKIE_CONTENT = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tyoutube-secret\n"
)
DOWNLOADER_WRITE_BACK = (
    "# Netscape HTTP Cookie File\n"
    ".example.test\tTRUE\t/\tTRUE\t1\tWRITTEN_BY_DOWNLOADER\tjunk\n"
)


class CookieWritingYoutubeDL:
    """Stand-in that rewrites ``cookiefile`` like yt-dlp's cookie jar does."""

    instances: list["CookieWritingYoutubeDL"] = []
    error: BaseException | None = None

    def __init__(self, options: dict) -> None:
        self.options = options
        cookiefile = options.get("cookiefile")
        self.cookiefile = Path(cookiefile) if cookiefile else None
        self.cookie_snapshot = self.cookiefile.read_bytes() if self.cookiefile else None
        self.downloaded: Path | None = None
        self.__class__.instances.append(self)

    def __enter__(self) -> "CookieWritingYoutubeDL":
        return self

    def __exit__(self, *exc_info) -> bool:
        # Real yt-dlp saves its cookie jar when the session closes.
        self.write_back()
        return False

    def write_back(self) -> None:
        if self.cookiefile is not None:
            self.cookiefile.write_text(DOWNLOADER_WRITE_BACK, encoding="utf-8")

    def extract_info(self, url: str, download: bool = False):
        self.write_back()
        if CookieWritingYoutubeDL.error is not None:
            raise CookieWritingYoutubeDL.error
        if download:
            target = Path(str(self.options["outtmpl"]).replace("%(ext)s", "wav"))
            target.write_bytes(b"RIFFfake")
            self.downloaded = target
            return {"ext": "wav"}
        return {"title": "Sample", "duration": 1.0, "subtitles": {}}

    def urlopen(self, request):
        self.write_back()
        return types.SimpleNamespace(
            read=lambda: b"1\n00:00:00,000 --> 00:00:01,000\nHi.\n"
        )

    def prepare_filename(self, info) -> str:
        assert self.downloaded is not None
        return str(self.downloaded)


@pytest.fixture(autouse=True)
def _reset_cookie_writers() -> None:
    CookieWritingYoutubeDL.instances.clear()
    CookieWritingYoutubeDL.error = None
    yield
    CookieWritingYoutubeDL.instances.clear()
    CookieWritingYoutubeDL.error = None


def _write_bilibili_cookie(secrets: Path) -> Path:
    secrets.mkdir(parents=True, exist_ok=True)
    cookie = secrets / "bilibili-cookies.txt"
    cookie.write_bytes(BILIBILI_COOKIE_CONTENT.encode("utf-8"))
    return cookie


def _configure_bilibili_secrets(
    monkeypatch: pytest.MonkeyPatch, secrets: Path
) -> None:
    monkeypatch.setenv("BEEBOX_SECRETS_DIR", str(secrets))
    monkeypatch.delenv("BEEBOX_YOUTUBE_COOKIE_FILE", raising=False)
    monkeypatch.setenv("BEEBOX_YOUTUBE_PO_TOKEN_PROVIDER", "off")


def _assert_throwaway_copy(
    instance: CookieWritingYoutubeDL,
    credential: Path,
    secrets: Path,
    expected: bytes,
) -> None:
    assert instance.cookiefile is not None
    assert instance.cookiefile != credential
    assert instance.cookiefile.is_relative_to(Path(tempfile.gettempdir()))
    assert not instance.cookiefile.is_relative_to(secrets)
    assert instance.cookie_snapshot == expected
    assert not instance.cookiefile.exists()


def _assert_credential_untouched(
    credential: Path, expected: bytes, expected_mtime_ns: int
) -> None:
    assert credential.read_bytes() == expected
    assert credential.stat().st_mtime_ns == expected_mtime_ns


def test_every_session_gets_a_fresh_throwaway_copy_and_the_credential_is_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = tmp_path / "secrets"
    credential = _write_bilibili_cookie(secrets)
    _configure_bilibili_secrets(monkeypatch, secrets)
    platform = YtDlpVideoPlatform(ydl_factory=CookieWritingYoutubeDL)

    before = credential.read_bytes()
    before_mtime_ns = credential.stat().st_mtime_ns

    platform.fetch_video_info(parse_video_url(BILIBILI_VIDEO_URL))
    metadata = CookieWritingYoutubeDL.instances[-1]
    _assert_throwaway_copy(metadata, credential, secrets, before)
    _assert_credential_untouched(credential, before, before_mtime_ns)

    document = platform.download_subtitle(
        SubtitleTrack(
            language="en",
            kind="manual",
            format="srt",
            url="https://example.test/captions.srt",
        )
    )
    assert [cue.text for cue in document.cues] == ["Hi."]
    subtitle = CookieWritingYoutubeDL.instances[-1]
    _assert_throwaway_copy(subtitle, credential, secrets, before)
    _assert_credential_untouched(credential, before, before_mtime_ns)

    destination = tmp_path / "audio.wav"
    platform.download_audio(parse_video_url(BILIBILI_VIDEO_URL), destination)
    assert destination.read_bytes() == b"RIFFfake"
    audio = CookieWritingYoutubeDL.instances[-1]
    _assert_throwaway_copy(audio, credential, secrets, before)
    _assert_credential_untouched(credential, before, before_mtime_ns)

    copies = {metadata.cookiefile, subtitle.cookiefile, audio.cookiefile}
    assert len(copies) == 3, "each session must get its own temporary copy"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RuntimeError("download exploded"), DownloadNetworkError),
        (asyncio.CancelledError(), asyncio.CancelledError),
    ],
    ids=["failure", "cancellation"],
)
def test_failed_or_cancelled_session_still_removes_the_copy_and_leaves_the_credential_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    expected: type,
) -> None:
    secrets = tmp_path / "secrets"
    credential = _write_bilibili_cookie(secrets)
    _configure_bilibili_secrets(monkeypatch, secrets)
    platform = YtDlpVideoPlatform(ydl_factory=CookieWritingYoutubeDL)
    before = credential.read_bytes()
    before_mtime_ns = credential.stat().st_mtime_ns
    CookieWritingYoutubeDL.error = error

    with pytest.raises(expected):
        platform.fetch_video_info(parse_video_url(BILIBILI_VIDEO_URL))

    session = CookieWritingYoutubeDL.instances[-1]
    _assert_throwaway_copy(session, credential, secrets, before)
    _assert_credential_untouched(credential, before, before_mtime_ns)


def test_credential_paths_and_values_never_reach_the_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    secrets = tmp_path / "secrets"
    credential = _write_bilibili_cookie(secrets)
    _configure_bilibili_secrets(monkeypatch, secrets)
    platform = YtDlpVideoPlatform(ydl_factory=CookieWritingYoutubeDL)
    before = credential.read_bytes()
    before_mtime_ns = credential.stat().st_mtime_ns

    platform.fetch_video_info(parse_video_url(BILIBILI_VIDEO_URL))
    platform.download_subtitle(
        SubtitleTrack(
            language="en",
            kind="manual",
            format="srt",
            url="https://example.test/captions.srt",
        )
    )

    for session in CookieWritingYoutubeDL.instances:
        assert session.cookiefile is not None
        assert str(session.cookiefile) not in caplog.text
    assert "bilibili-secret" not in caplog.text
    assert "WRITTEN_BY_DOWNLOADER" not in caplog.text
    _assert_credential_untouched(credential, before, before_mtime_ns)


@pytest.mark.parametrize("state", ["missing", "empty"])
def test_missing_or_empty_credential_creates_no_copy_and_no_cookiefile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    if state == "empty":
        (secrets / "bilibili-cookies.txt").write_bytes(b"")
    _configure_bilibili_secrets(monkeypatch, secrets)

    def forbidden_temporary_directory(*args, **kwargs):
        raise AssertionError("an absent credential must not be copied")

    monkeypatch.setattr(tempfile, "TemporaryDirectory", forbidden_temporary_directory)
    platform = YtDlpVideoPlatform(ydl_factory=CookieWritingYoutubeDL)

    platform.fetch_video_info(parse_video_url(BILIBILI_VIDEO_URL))

    session = CookieWritingYoutubeDL.instances[-1]
    assert session.cookiefile is None
    assert "cookiefile" not in session.options


def test_each_platform_session_only_gets_a_copy_of_its_own_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = tmp_path / "secrets"
    bilibili = _write_bilibili_cookie(secrets)
    youtube = secrets / "youtube-cookies.txt"
    youtube.write_bytes(YOUTUBE_COOKIE_CONTENT.encode("utf-8"))
    monkeypatch.setenv("BEEBOX_SECRETS_DIR", str(secrets))
    monkeypatch.setenv("BEEBOX_YOUTUBE_COOKIE_FILE", str(youtube))
    monkeypatch.setenv("BEEBOX_YOUTUBE_PO_TOKEN_PROVIDER", "off")
    platform = YtDlpVideoPlatform(ydl_factory=CookieWritingYoutubeDL)

    platform.fetch_video_info(parse_video_url(YOUTUBE_URL))
    youtube_session = CookieWritingYoutubeDL.instances[-1]
    _assert_throwaway_copy(
        youtube_session, youtube, secrets, YOUTUBE_COOKIE_CONTENT.encode("utf-8")
    )
    assert youtube_session.cookie_snapshot != BILIBILI_COOKIE_CONTENT.encode("utf-8")
    assert youtube.read_bytes() == YOUTUBE_COOKIE_CONTENT.encode("utf-8")

    platform.fetch_video_info(parse_video_url(BILIBILI_VIDEO_URL))
    bilibili_session = CookieWritingYoutubeDL.instances[-1]
    _assert_throwaway_copy(
        bilibili_session, bilibili, secrets, BILIBILI_COOKIE_CONTENT.encode("utf-8")
    )
    assert bilibili_session.cookie_snapshot != YOUTUBE_COOKIE_CONTENT.encode("utf-8")
    assert bilibili.read_bytes() == BILIBILI_COOKIE_CONTENT.encode("utf-8")


def test_daily_refresh_and_a_running_session_do_not_overwrite_each_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.services.bilibili_login import cookies as cookies_module
    from backend.services.bilibili_login import write_cookie_file

    secrets = tmp_path / "secrets"
    credential = _write_bilibili_cookie(secrets)
    _configure_bilibili_secrets(monkeypatch, secrets)
    stale = credential.read_bytes()
    stale_mtime_ns = credential.stat().st_mtime_ns
    refreshed = {"SESSDATA": "refreshed-sessdata", "bili_jct": "refreshed-bili-jct"}

    class RefreshingYoutubeDL(CookieWritingYoutubeDL):
        def extract_info(self, url: str, download: bool = False):
            # A daily refresh lands while the download session is running.
            write_cookie_file(dict(refreshed), expires_at=4_102_444_800)
            return super().extract_info(url, download=download)

    platform = YtDlpVideoPlatform(ydl_factory=RefreshingYoutubeDL)
    with mock.patch.object(cookies_module.os, "replace", wraps=os.replace) as replace:
        platform.fetch_video_info(parse_video_url(BILIBILI_VIDEO_URL))

    session = RefreshingYoutubeDL.instances[-1]
    # The job read its own pre-refresh snapshot and deleted it afterwards.
    assert session.cookie_snapshot == stale
    assert session.cookiefile is not None
    assert not session.cookiefile.exists()
    # The downloader's write-back never reached the real file, and the daily
    # refresh's atomic replace installed the new credential.
    stored = credential.read_bytes()
    assert b"WRITTEN_BY_DOWNLOADER" not in stored
    assert b"refreshed-sessdata" in stored
    assert b"bilibili-secret" not in stored
    assert credential.stat().st_mtime_ns >= stale_mtime_ns
    replace.assert_called_once()
    assert Path(replace.call_args.args[1]) == credential

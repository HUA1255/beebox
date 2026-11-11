"""Production video platform: yt-dlp behind the boundary (tickets #42, #45).

yt-dlp is imported lazily so tests and deployments on the fake platform do not
need it installed. The pinned version lives in ``pyproject.toml``; the adapter
is intentionally thin: yt-dlp extracts metadata and downloads bytes, while
this module turns them into the platform-neutral value objects and classifies
its failures into typed errors, so the public ``error_code`` never depends on
matching downloader prose. Bilibili multi-part links are narrowed to the one
part named by ``?p=N`` even when yt-dlp returns the whole multi-video playlist.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

from backend.services import ffmpeg_tools, youtube_cookies
from backend.services.bilibili_login import cookie_file_path, login_expired, login_status
from backend.services.video_platform.config import (
    DEFAULT_TIMEOUT_SECONDS,
    DOWNLOAD_PROXY_ENV,
    DownloadOptions,
    download_options,
)
from backend.services.video_platform.errors import (
    AudioDownloadError,
    DownloadNetworkError,
    DownloadTimeoutError,
    InvalidUrlError,
    LocalToolMissingError,
    LoginRequiredError,
    SubtitleUnavailableError,
    UnsupportedUrlError,
    VideoPlatformConfigError,
    VideoPlatformError,
    VideoUnavailableError,
)
from backend.services.video_platform.models import (
    PLATFORM_BILIBILI,
    PLATFORM_YOUTUBE,
    TRACK_KIND_AUTO,
    TRACK_KIND_MANUAL,
    LoginStatus,
    SubtitleDocument,
    SubtitleTrack,
    VideoInfo,
    VideoRef,
)
from backend.services.video_platform.subtitles import parse_subtitle
from backend.services.video_platform.youtube import apply_youtube_options

FORMAT_PREFERENCE = ("srt", "vtt", "json3", "json")


def _format_priority(fmt: str) -> int:
    try:
        return FORMAT_PREFERENCE.index(fmt)
    except ValueError:
        return len(FORMAT_PREFERENCE)


def _ffmpeg_location() -> str | None:
    """The resolved ffmpeg executable handed to yt-dlp (ticket #85).

    yt-dlp's ``FFmpegExtractAudio`` postprocessor searches ``PATH`` only. The
    native Windows installer keeps ffmpeg and ffprobe under ``<root>\\tools``
    and reports them through ``BEEBOX_FFMPEG_PATH``/``BEEBOX_FFPROBE_PATH``, so
    the same resolution BeeBox uses for its own media work must be handed to
    yt-dlp.

    The executable path is what yt-dlp needs, not its directory: for a
    directory yt-dlp joins ``ffmpeg``/``ffprobe`` without the ``.exe`` suffix,
    which never exists on Windows. For an executable it also derives
    ``ffprobe.exe`` in the same folder. A missing tool stays unset so yt-dlp
    reports it and ``_classify_download_error`` can surface
    ``LOCAL_TOOL_MISSING``.
    """
    try:
        status = ffmpeg_tools.resolve_tools(check_version=False)["ffmpeg"]
    except (OSError, ValueError):
        # Tool resolution must never break metadata or subtitle extraction.
        return None
    if not status.available or not status.path:
        return None
    return status.path


_YOUTUBE_HOST_SUFFIXES = (".youtube.com", ".youtu.be", ".googlevideo.com", ".youtube-nocookie.com")


def _is_youtube_url(url: str | None) -> bool:
    if not url:
        return False
    host = (urlsplit(url).hostname or "").lower()
    return host == "youtube.com" or host == "youtu.be" or host.endswith(_YOUTUBE_HOST_SUFFIXES)


def _is_translated(format_payload: dict) -> bool:
    if format_payload.get("tlang"):
        return True
    url = str(format_payload.get("url") or "")
    if not url:
        return False
    query = parse_qs(urlsplit(url).query)
    return "tlang" in query


def _best_format(format_list) -> dict | None:
    candidates = [
        item
        for item in format_list or []
        if isinstance(item, dict) and str(item.get("ext") or "").lower() in FORMAT_PREFERENCE
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda item: _format_priority(str(item.get("ext") or "").lower()))


def _tracks(
    info: dict, *, video_id: str | None = None, platform: str | None = None
) -> tuple[SubtitleTrack, ...]:
    tracks: list[SubtitleTrack] = []
    for kind, key in ((TRACK_KIND_MANUAL, "subtitles"), (TRACK_KIND_AUTO, "automatic_captions")):
        languages = info.get(key)
        if not isinstance(languages, dict):
            continue
        for language, formats in languages.items():
            best = _best_format(formats)
            if best is None:
                continue
            headers = best.get("http_headers") if isinstance(best.get("http_headers"), dict) else {}
            # Bilibili publishes its AI-generated tracks under "subtitles" with
            # an ai-<lang> name; report them as platform automatic captions.
            track_kind = kind
            if kind == TRACK_KIND_MANUAL and str(language).lower().startswith("ai-"):
                track_kind = TRACK_KIND_AUTO
            tracks.append(
                SubtitleTrack(
                    language=str(language),
                    kind=track_kind,
                    format=str(best.get("ext") or "").lower(),
                    url=best.get("url"),
                    content=best.get("data") if isinstance(best.get("data"), str) else None,
                    name=best.get("name") if isinstance(best.get("name"), str) else None,
                    translated=_is_translated(best),
                    headers={str(key): str(value) for key, value in headers.items()},
                    video_id=video_id,
                    platform=platform,
                )
            )
    return tuple(tracks)


def _classify_download_error(
    exc: Exception, *, login_expired: bool = False
) -> VideoPlatformError:
    """Best-effort mapping of a yt-dlp failure onto the public codes.

    Order matters: a proxy failure can mention ``authentication`` and a login
    page can mention ``404``, so transport markers win over account and
    availability markers. ``login_expired`` marks a Bilibili request whose
    stored login is stale: the useful outcome is then ``VIDEO_LOGIN_REQUIRED``
    rather than a silent downgrade (ADR-0012).
    """
    message = str(exc)
    lowered = message.lower()
    if isinstance(exc, TimeoutError) or "timed out" in lowered or "timeout" in lowered:
        return DownloadTimeoutError(message)
    # A missing local tool is not a network failure and retrying cannot fix it,
    # so it must win before the transport markers (ticket #85). yt-dlp reports
    # the postprocessor's own wording here rather than raising a distinct type.
    if any(
        marker in lowered
        for marker in (
            "ffprobe and ffmpeg not found",
            "ffmpeg not found",
            "ffprobe not found",
            "ffmpeg is not installed",
        )
    ):
        return LocalToolMissingError(
            f"{message} ffmpeg and ffprobe are required for audio extraction; "
            "configure BEEBOX_FFMPEG_PATH and BEEBOX_FFPROBE_PATH (or the tool "
            "paths in settings)."
        )
    # Proxy and transport failures are network errors, including HTTP 407.
    if any(
        marker in lowered
        for marker in (
            "proxy",
            "connection refused",
            "connection reset",
            "connection aborted",
            "no connection could be made",
            "network is unreachable",
            "temporary failure in name resolution",
            "getaddrinfo",
            "ssl",
            "http error 407",
            "http error 429",
            "http error 500",
            "http error 502",
            "http error 503",
            "http error 504",
        )
    ):
        return DownloadNetworkError(message)
    if "not a valid url" in lowered or "invalid url" in lowered:
        return InvalidUrlError(message)
    if "unsupported url" in lowered:
        return UnsupportedUrlError(message)
    if any(
        marker in lowered
        for marker in (
            "login",
            "sign in",
            "sign-in",
            "cookies",
            "not a bot",
            "age-restricted",
            "age restricted",
            "confirm your age",
            "inappropriate for some users",
            "登录",
            "需要登录",
        )
    ):
        return LoginRequiredError(message)
    if login_expired:
        # Public Bilibili videos still succeed without a login; this branch
        # only runs after a request already failed.
        return LoginRequiredError(message)
    if any(
        marker in lowered
        for marker in (
            "private",
            "unavailable",
            "not available",
            "removed",
            "premium",
            "members-only",
            "members only",
            "paid",
            "geo restrict",
            "not permitted",
            "forbidden",
            "http error 403",
            "404",
            "大会员",
            "地区",
            "区域",
        )
    ):
        return VideoUnavailableError(message)
    return DownloadNetworkError(message)


class YtDlpVideoPlatform:
    """yt-dlp implementation of the video platform boundary."""

    name = "yt-dlp"

    def __init__(
        self,
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        cookie_file: Path | str | None = None,
        ydl_factory=None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._configured_cookie_file = Path(cookie_file) if cookie_file else None
        self._ydl_factory = ydl_factory

    def cookie_file(self) -> Path:
        """The Bilibili cookie file the downloader reads (ticket #46)."""
        return cookie_file_path()

    def yt_dlp_options(self) -> dict:
        """The base options for metadata, subtitle and audio calls.

        When a credential exists, ``cookiefile`` names the file to read. A
        yt-dlp session never receives that path itself: ``_credential_session``
        hands it a temporary copy, because yt-dlp writes its cookie jar back to
        the file it was given when the session closes.
        """
        return self._base_options()

    # -- boundary ----------------------------------------------------------

    def fetch_video_info(
        self, ref: VideoRef, *, options: DownloadOptions | None = None
    ) -> VideoInfo:
        info = self._extract(ref, options)
        if self._is_live(info):
            raise UnsupportedUrlError("Live streams are not supported")
        entry, part_title = self._select_part(info, ref)
        if self._is_live(entry):
            raise UnsupportedUrlError("Live streams are not supported")
        duration = entry.get("duration")
        reported_language = (
            entry.get("language")
            or entry.get("default_audio_language")
            or info.get("language")
            or info.get("default_audio_language")
        )
        return VideoInfo(
            ref=ref,
            title=str(entry.get("title") or ref.video_id),
            author=(
                str(entry.get("uploader") or entry.get("channel") or entry.get("creator"))
                if (entry.get("uploader") or entry.get("channel") or entry.get("creator"))
                else None
            ),
            duration_ms=int(round(float(duration) * 1000)) if duration is not None else None,
            tracks=_tracks(entry, video_id=ref.video_id, platform=ref.platform),
            part_title=part_title,
            original_language=(
                str(reported_language)
                if isinstance(reported_language, str) and reported_language.strip()
                else None
            ),
        )

    def download_subtitle(
        self,
        track: SubtitleTrack,
        *,
        options: DownloadOptions | None = None,
    ) -> SubtitleDocument:
        if track.content is not None:
            # Bilibili hands the whole caption file back inline.
            text = track.content
        elif track.url:
            try:
                platform_options = self._base_options(options)
                if _is_youtube_url(track.url):
                    self._apply_youtube_options(platform_options)
                with self._credential_session(platform_options):
                    with self._youtube_dl(platform_options) as ydl:
                        request = Request(track.url, headers=track.headers or {})
                        response = ydl.urlopen(request)
                        raw = response.read()
            except VideoPlatformError:
                raise
            except Exception as exc:
                raise _classify_download_error(
                    exc, login_expired=self._login_expired_for(track.platform)
                ) from exc
            text = raw.decode("utf-8-sig", errors="replace")
        else:
            raise SubtitleUnavailableError(
                f"The {track.language!r} subtitle track has no download URL"
            )
        return SubtitleDocument(
            language=track.language,
            kind=track.kind,
            cues=tuple(parse_subtitle(text, track.format)),
        )

    def download_audio(
        self,
        ref: VideoRef,
        destination: Path,
        *,
        options: DownloadOptions | None = None,
    ) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="beebox-audio-") as temporary:
            output_template = str(Path(temporary) / "audio.%(ext)s")
            platform_options = self._with_part(self._base_options(options), ref)
            if ref.platform == PLATFORM_YOUTUBE:
                self._apply_youtube_options(platform_options)
            platform_options.update(
                {
                    "skip_download": False,
                    # Ticket #44 downloads audio only: never fetch a video
                    # stream. When a platform offers no audio-only format the
                    # download fails loudly instead of silently pulling the
                    # muxed video stream.
                    "format": "bestaudio",
                    "outtmpl": output_template,
                    # Ticket #85: the base session lists subtitles for
                    # metadata, but the audio temp directory must not collect
                    # Bilibili's danmaku/subtitle sidecar files.
                    "writesubtitles": False,
                    "postprocessors": [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "wav",
                            "preferredquality": "0",
                        }
                    ],
                }
            )
            try:
                with self._credential_session(platform_options):
                    with self._youtube_dl(platform_options) as ydl:
                        ydl.extract_info(ref.canonical_url, download=True)
            except VideoPlatformError:
                raise
            except Exception as exc:
                raise _classify_download_error(
                    exc, login_expired=self._login_expired_for(ref.platform)
                ) from exc
            # The postprocessor renames the downloaded file, so select the WAV
            # explicitly: prepare_filename reports the pre-extraction name and
            # a subtitle sidecar may share the temporary directory.
            wavs = [
                item
                for item in Path(temporary).iterdir()
                if item.is_file() and item.suffix.lower() == ".wav"
            ]
            if not wavs:
                raise AudioDownloadError("yt-dlp did not produce a WAV audio file")
            shutil.move(str(wavs[0]), destination)
        return destination

    def check_login(self) -> LoginStatus:
        # Ticket #46 stores the credential as a cookie file; the daily refresh
        # lifecycle is ticket #47. No network call is needed to report state.
        return login_status()

    def tool_version(self) -> str | None:
        try:
            import yt_dlp.version
        except Exception:
            return None
        return getattr(yt_dlp.version, "__version__", None)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _is_live(info: dict) -> bool:
        return bool(
            info.get("is_live")
            or info.get("live_status") in {"is_live", "is_upcoming"}
        )

    @staticmethod
    def _part_title(info: dict) -> str | None:
        value = info.get("part")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def _select_part(self, info: dict, ref: VideoRef) -> tuple[dict, str | None]:
        """Narrow a Bilibili multi-video playlist to the requested part."""
        entries = info.get("entries")
        is_multi = info.get("_type") in {"playlist", "multi_video"} or bool(entries)
        if not is_multi:
            return info, self._part_title(info)
        if ref.platform != PLATFORM_BILIBILI:
            raise UnsupportedUrlError("Playlist links are not supported")
        selected = [entry for entry in entries or [] if isinstance(entry, dict)]
        if not selected:
            raise UnsupportedUrlError("The playlist contains no videos")
        if len(selected) == 1:
            # yt-dlp may already have narrowed ?p=N to the one requested part
            # instead of returning the whole multi-video list.
            entry = selected[0]
            return entry, self._part_title(entry) or str(entry.get("title") or "") or None
        index = (ref.part or 1) - 1
        if index < 0 or index >= len(selected):
            raise VideoUnavailableError(
                f"Part {ref.part or 1} does not exist for {ref.original_url}"
            )
        entry = selected[index]
        return entry, self._part_title(entry) or str(entry.get("title") or "") or None

    def _base_options(self, options: DownloadOptions | None = None) -> dict:
        resolved = options or download_options(timeout_seconds=self._timeout_seconds)
        platform_options: dict = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            # yt-dlp only populates info["subtitles"] when subtitle output is
            # requested; without this, Bilibili videos look subtitle-less and
            # always fall back to ASR.
            "writesubtitles": True,
            "socket_timeout": resolved.timeout_seconds,
            "retries": 2,
        }
        if resolved.proxy:
            platform_options["proxy"] = resolved.proxy
        ffmpeg_location = _ffmpeg_location()
        if ffmpeg_location:
            platform_options["ffmpeg_location"] = ffmpeg_location
        cookie_file = cookie_file_path()
        try:
            if cookie_file.is_file() and cookie_file.stat().st_size > 0:
                # Ticket #46: this is the credential to read. Ticket #67: a
                # yt-dlp session gets a temporary copy (``_credential_session``)
                # because yt-dlp rewrites the file it was given on close; the
                # login and daily-refresh flows are the only writers of the
                # real file.
                platform_options["cookiefile"] = str(cookie_file)
        except OSError:
            pass
        return platform_options

    @staticmethod
    def _with_part(options: dict, ref: VideoRef) -> dict:
        # Tell yt-dlp which page of a Bilibili multi-video list to take; the
        # canonical URL also carries ?p=N, but yt-dlp may still return the
        # whole list.
        if ref.platform == PLATFORM_BILIBILI and ref.part and ref.part > 1:
            options["playlist_items"] = str(ref.part)
        return options

    @staticmethod
    @contextmanager
    def _credential_session(options: dict) -> Iterator[dict]:
        """Run one yt-dlp session against a throwaway copy of the credential.

        ``options["cookiefile"]`` names the file to read (the Bilibili login
        file or the uploaded YouTube jar). yt-dlp rewrites the cookie file it
        was given when the session closes, so no session may receive the real
        secrets file: the login and daily-refresh flows remain its only writers
        (ADR-0012). The copy lives in the system temporary directory for this
        session only and is removed on success, failure and cancellation.

        A missing or empty credential yields the options without a
        ``cookiefile`` and never falls back to the live file. No path or cookie
        value is logged.
        """
        source = options.pop("cookiefile", None)
        if not source:
            yield options
            return
        try:
            content = Path(source).read_bytes()
        except OSError:
            # An unreadable credential behaves like an absent one; using the
            # live file would let the downloader rewrite it.
            yield options
            return
        if not content:
            yield options
            return
        with tempfile.TemporaryDirectory(prefix="beebox-credential-") as temporary:
            copy = Path(temporary) / "cookies.txt"
            try:
                copy.write_bytes(content)
            except OSError:
                yield options
                return
            options["cookiefile"] = str(copy)
            try:
                yield options
            finally:
                options.pop("cookiefile", None)

    def _extract(self, ref: VideoRef, options: DownloadOptions | None = None) -> dict:
        platform_options = self._with_part(self._base_options(options), ref)
        if ref.platform == PLATFORM_YOUTUBE:
            self._apply_youtube_options(platform_options)
        try:
            with self._credential_session(platform_options):
                with self._youtube_dl(platform_options) as ydl:
                    info = ydl.extract_info(ref.canonical_url, download=False)
        except VideoPlatformError:
            raise
        except Exception as exc:
            raise _classify_download_error(
                exc, login_expired=self._login_expired_for(ref.platform)
            ) from exc
        if not isinstance(info, dict):
            raise VideoUnavailableError(f"yt-dlp returned no metadata for {ref.original_url}")
        return info

    def _stored_cookie_file(self) -> Path | None:
        if self._configured_cookie_file is not None:
            try:
                if (
                    self._configured_cookie_file.is_file()
                    and self._configured_cookie_file.stat().st_size > 0
                ):
                    return self._configured_cookie_file
            except OSError:
                pass
        return youtube_cookies.cookie_file()

    def _apply_youtube_options(self, options: dict) -> None:
        """Attach the PO token provider and the uploaded cookie jar, if any.

        The shared base options may already carry the Bilibili credential; a
        YouTube request must only ever see the YouTube cookie, so an absent
        YouTube jar explicitly removes the inherited ``cookiefile``. The path
        recorded here is the read source; ``_credential_session`` replaces it
        with a temporary copy before yt-dlp is constructed.
        """
        cookie_file = self._stored_cookie_file()
        if cookie_file is not None:
            options["cookiefile"] = str(cookie_file)
        else:
            options.pop("cookiefile", None)
        apply_youtube_options(options)

    @staticmethod
    def _login_expired_for(platform: str | None) -> bool:
        """Whether a failed Bilibili request is evidence of a stale credential.

        The caller passes the parsed platform, so no URL or header text decides
        it (ticket #66). Only ``expired`` upgrades the failure: an absent login
        is refused by the job gate before any download, unless the caller chose
        the anonymous ``prefer=asr`` path.
        """
        return platform == PLATFORM_BILIBILI and login_expired()

    def _youtube_dl(self, options: dict):
        if self._ydl_factory is not None:
            return self._ydl_factory(options)
        try:
            from yt_dlp import YoutubeDL
        except Exception as exc:  # pragma: no cover - dependency is pinned
            raise VideoPlatformConfigError(
                "yt-dlp is not installed; install the locked dependencies"
            ) from exc
        return YoutubeDL(options)


__all__ = ["DOWNLOAD_PROXY_ENV", "YtDlpVideoPlatform"]

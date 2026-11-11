"""Fake video platform backed by local sample files (tickets #42 and #45).

Selection is configuration only, matching the fake speech engine::

    BEEBOX_VIDEO_PLATFORM=fake
    BEEBOX_VIDEO_FAKE_ROOT=<directory with videos.json and subtitle files>

``videos.json`` lists the videos, their metadata and their subtitle tracks.
An ``audio`` entry names a local sample file; ``audio_seconds`` instead makes
the fake generate a silent WAV of that length for the ticket #44 audio path.

A video may also script failures and Bilibili multi-part metadata:

* ``parts``: ``[{"title", "duration_seconds", "tracks", "audio"}, ...]``; the
  requested part (``?p=N``, default 1) selects exactly one entry.
* ``login_required`` / ``unavailable``: report the matching typed error.
* ``live`` / ``playlist``: refuse the link as unsupported.
* ``fail``: ``{"stage": "metadata"|"subtitle"|"audio", "error_code": ...,
  "message": ...}`` raises the typed error for that stage.

Every boundary call records the settings it received, including the download
proxy. Top-level ``"record_calls": "calls.jsonl"`` additionally appends one
JSON object per call under the fake root, which lets HTTP tests assert that
``BEEBOX_DOWNLOAD_PROXY`` reached every fetch/download. No test or
development instance using this platform touches the network.
"""

from __future__ import annotations

import json
import shutil
import wave
from pathlib import Path
from typing import Any

from backend.services.video_platform.config import DownloadOptions
from backend.services.video_platform.errors import (
    AudioDownloadError,
    DownloadNetworkError,
    DownloadTimeoutError,
    InvalidUrlError,
    LoginRequiredError,
    SubtitleUnavailableError,
    UnsupportedUrlError,
    VideoPlatformConfigError,
    VideoPlatformError,
    VideoUnavailableError,
)
from backend.services.video_platform.models import (
    LOGIN_UNKNOWN,
    TRACK_KIND_AUTO,
    TRACK_KIND_MANUAL,
    LoginStatus,
    SubtitleDocument,
    SubtitleTrack,
    VideoInfo,
    VideoRef,
)
from backend.services.video_platform.subtitles import parse_subtitle

FAKE_PLATFORM_ROOT_ENV = "BEEBOX_VIDEO_FAKE_ROOT"
FAKE_PLATFORM_NAME = "fake"
_CONFIG_FILENAME = "videos.json"

_FAILURE_TYPES: dict[str, type[VideoPlatformError]] = {
    "INVALID_URL": InvalidUrlError,
    "UNSUPPORTED_URL": UnsupportedUrlError,
    "VIDEO_NOT_ACCESSIBLE": VideoUnavailableError,
    "VIDEO_LOGIN_REQUIRED": LoginRequiredError,
    "DOWNLOAD_NETWORK_ERROR": DownloadNetworkError,
    "DOWNLOAD_TIMEOUT": DownloadTimeoutError,
    "VIDEO_SUBTITLE_UNAVAILABLE": SubtitleUnavailableError,
    "VIDEO_AUDIO_DOWNLOAD_FAILED": AudioDownloadError,
}


def _write_silent_wav(destination: Path, seconds: float, *, sample_rate: int = 16_000) -> None:
    """Generate a valid mono PCM WAV so tests never need an audio fixture."""
    frame_count = max(1, int(sample_rate * max(0.05, float(seconds))))
    try:
        with wave.open(str(destination), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(b"\x00\x00" * frame_count)
    except OSError as exc:
        raise AudioDownloadError(f"Cannot write fake audio sample {destination}: {exc}") from exc


class FakeVideoPlatform:
    """Read scripted video metadata and subtitle samples from a directory."""

    name = FAKE_PLATFORM_NAME

    def __init__(self, root: Path, *, cookie_file: Path | str | None = None) -> None:
        self._root = Path(root).expanduser().resolve()
        self._cookie_file = Path(cookie_file) if cookie_file else None
        config_path = self._root / _CONFIG_FILENAME
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VideoPlatformConfigError(
                f"Cannot read fake video platform config {config_path}: {exc}"
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("videos"), list):
            raise VideoPlatformConfigError(
                f"{config_path} must contain a 'videos' list"
            )
        self._videos: list[dict[str, Any]] = [
            item for item in payload["videos"] if isinstance(item, dict)
        ]
        self._tool_version = str(payload.get("tool_version") or "fake")
        login = payload.get("login") if isinstance(payload.get("login"), dict) else {}
        self._login = LoginStatus(
            state=str(login.get("state") or LOGIN_UNKNOWN),
            detail=login.get("detail") if isinstance(login.get("detail"), str) else None,
        )
        record_calls = payload.get("record_calls")
        self._calls_path: Path | None = None
        if isinstance(record_calls, str) and record_calls.strip():
            self._calls_path = self._resolve_sample(record_calls.strip())
        self.calls: list[dict[str, Any]] = []

    # -- boundary ----------------------------------------------------------

    def fetch_video_info(
        self, ref: VideoRef, *, options: DownloadOptions | None = None
    ) -> VideoInfo:
        self._record(
            "metadata",
            options,
            video_id=ref.video_id,
            part=ref.part,
            url=ref.original_url,
        )
        entry = self._entry(ref)
        self._raise_injected(entry, "metadata")
        if entry.get("playlist"):
            raise UnsupportedUrlError("Playlist links are not supported")
        if entry.get("live"):
            raise UnsupportedUrlError("Live streams are not supported")
        if entry.get("login_required"):
            raise LoginRequiredError(f"Login is required for {ref.original_url}")
        if entry.get("age_restricted") and not self._has_cookie():
            raise LoginRequiredError(
                f"An uploaded YouTube cookie is required for {ref.original_url}"
            )
        reason = entry.get("unavailable")
        if reason:
            detail = reason if isinstance(reason, str) else "unavailable"
            raise VideoUnavailableError(
                f"Video is not accessible ({detail}): {ref.original_url}"
            )
        part = self._part(entry, ref)
        tracks = tuple(
            self._track(item, ref.video_id, ref.platform)
            for item in part.get("tracks") or []
            if isinstance(item, dict)
        )
        duration = part.get("duration_seconds")
        part_title = part.get("part_title")
        original_language = part.get("original_language", entry.get("original_language"))
        return VideoInfo(
            ref=ref,
            title=str(part.get("video_title") or entry.get("title") or ref.video_id),
            author=str(entry["author"]) if entry.get("author") else None,
            duration_ms=int(round(float(duration) * 1000)) if duration is not None else None,
            tracks=tracks,
            part_title=str(part_title) if part_title else None,
            original_language=(
                str(original_language)
                if isinstance(original_language, str) and original_language
                else None
            ),
        )

    def download_subtitle(
        self,
        track: SubtitleTrack,
        *,
        options: DownloadOptions | None = None,
    ) -> SubtitleDocument:
        self._record(
            "subtitle",
            options,
            video_id=track.video_id,
            language=track.language,
            kind=track.kind,
            path=track.path,
        )
        entry = self._entry_by_id(track.video_id)
        if entry is not None:
            self._raise_injected(entry, "subtitle")
        if not track.path:
            raise SubtitleUnavailableError(
                f"The fake subtitle track {track.language!r} has no sample file"
            )
        path = self._resolve_sample(track.path)
        try:
            content = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise SubtitleUnavailableError(f"Cannot read subtitle sample {path}: {exc}") from exc
        return SubtitleDocument(
            language=track.language,
            kind=track.kind,
            cues=tuple(parse_subtitle(content, track.format)),
        )

    def download_audio(
        self,
        ref: VideoRef,
        destination: Path,
        *,
        options: DownloadOptions | None = None,
    ) -> Path:
        self._record(
            "audio",
            options,
            video_id=ref.video_id,
            part=ref.part,
            url=ref.original_url,
        )
        entry = self._entry(ref)
        if entry.get("age_restricted") and not self._has_cookie():
            raise LoginRequiredError(
                f"An uploaded YouTube cookie is required for {ref.original_url}"
            )
        self._raise_injected(entry, "audio")
        part = self._part(entry, ref)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        sample = part.get("audio")
        if sample:
            source = self._resolve_sample(str(sample))
            try:
                shutil.copyfile(source, destination)
            except OSError as exc:
                raise AudioDownloadError(f"Cannot read audio sample {source}: {exc}") from exc
            return destination
        seconds = part.get("audio_seconds")
        if seconds is None:
            raise AudioDownloadError(f"The fake video {ref.video_id!r} has no audio sample")
        try:
            duration = float(seconds)
        except (TypeError, ValueError) as exc:
            raise VideoPlatformConfigError(
                f"The fake video {ref.video_id!r} has an invalid audio_seconds value"
            ) from exc
        _write_silent_wav(destination, duration)
        return destination

    def check_login(self) -> LoginStatus:
        return self._login

    def tool_version(self) -> str:
        return self._tool_version

    # -- internals ---------------------------------------------------------

    def _has_cookie(self) -> bool:
        if self._cookie_file is None:
            return False
        try:
            return self._cookie_file.is_file() and self._cookie_file.stat().st_size > 0
        except OSError:
            return False

    def _record(
        self,
        stage: str,
        options: DownloadOptions | None,
        **fields: Any,
    ) -> None:
        # Record exactly what the caller handed over: the job pipeline must
        # pass the proxy itself, so an omitted option stays visible here.
        payload = {"stage": stage, "proxy": options.proxy if options else None, **fields}
        self.calls.append(payload)
        if self._calls_path is not None:
            self._calls_path.parent.mkdir(parents=True, exist_ok=True)
            with self._calls_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _raise_injected(self, entry: dict[str, Any], stage: str) -> None:
        failure = entry.get("fail")
        if not isinstance(failure, dict) or failure.get("stage") != stage:
            return
        code = str(failure.get("error_code") or failure.get("code") or "").strip().upper()
        error_type = _FAILURE_TYPES.get(code)
        if error_type is None:
            raise VideoPlatformConfigError(
                f"Unsupported injected fake platform error code: {code!r}"
            )
        message = failure.get("message")
        raise error_type(str(message) if message else f"Injected {code}")

    def _part(self, entry: dict[str, Any], ref: VideoRef) -> dict[str, Any]:
        """Merge the requested part into the video entry.

        ``parts`` describes a Bilibili multi-part video: the job must only ever
        see the selected part's title, duration, tracks and audio. Entries
        without ``parts`` are single videos and only accept part 1.
        """
        parts = entry.get("parts")
        if not isinstance(parts, list) or not parts:
            if ref.part not in (None, 1):
                raise VideoUnavailableError(
                    f"Part {ref.part} does not exist for {ref.original_url}"
                )
            return entry
        index = (ref.part or 1) - 1
        if index < 0 or index >= len(parts) or not isinstance(parts[index], dict):
            raise VideoUnavailableError(
                f"Part {ref.part} does not exist for {ref.original_url}"
            )
        payload = dict(entry)
        selected = parts[index]
        for key in ("duration_seconds", "tracks", "audio", "audio_seconds", "original_language"):
            if key in selected:
                payload[key] = selected[key]
        payload["video_title"] = entry.get("title")
        payload["part_title"] = selected.get("title")
        return payload

    def _entry(self, ref: VideoRef) -> dict[str, Any]:
        for entry in self._videos:
            if ref.original_url in (entry.get("urls") or []):
                return entry
        for entry in self._videos:
            if entry.get("platform") == ref.platform and str(entry.get("id")) == ref.video_id:
                return entry
        raise VideoUnavailableError(f"The fake platform has no video for {ref.original_url}")

    def _entry_by_id(self, video_id: str | None) -> dict[str, Any] | None:
        if not video_id:
            return None
        for entry in self._videos:
            if str(entry.get("id")) == video_id:
                return entry
        return None

    def _track(
        self, payload: dict[str, Any], video_id: str, platform: str
    ) -> SubtitleTrack:
        kind = str(payload.get("kind") or TRACK_KIND_MANUAL).lower()
        if kind not in {TRACK_KIND_MANUAL, TRACK_KIND_AUTO}:
            raise VideoPlatformConfigError(f"Unsupported fake track kind: {kind}")
        fmt = str(payload.get("format") or Path(str(payload.get("path") or "")).suffix.lstrip(".") or "srt")
        return SubtitleTrack(
            language=str(payload.get("language") or "und"),
            kind=kind,
            format=fmt.lower(),
            path=str(payload["path"]) if payload.get("path") else None,
            translated=bool(payload.get("translated", False)),
            name=payload.get("name") if isinstance(payload.get("name"), str) else None,
            video_id=video_id,
            platform=platform,
        )

    def _resolve_sample(self, relative: str) -> Path:
        candidate = (self._root / relative).resolve()
        if candidate != self._root and self._root not in candidate.parents:
            raise VideoPlatformConfigError(
                f"Fake platform sample {relative!r} escapes {self._root}"
            )
        return candidate


__all__ = ["FAKE_PLATFORM_NAME", "FAKE_PLATFORM_ROOT_ENV", "FakeVideoPlatform"]

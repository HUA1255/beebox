from __future__ import annotations

ERROR_CODES: dict[str, str] = {
    "MODEL_STORAGE_UNAVAILABLE": "The configured model storage location is unavailable.",
    "MODEL_STORAGE_READ_ONLY": "The configured model storage location is not writable.",
    "MODEL_NOT_DOWNLOADED": "Model files are not downloaded.",
    "MODEL_COMPATIBILITY_FAILED": "Downloaded model files are incomplete or incompatible.",
    "MODEL_LOAD_FAILED": "Model runtime failed to load the model.",
    "LOCAL_WORKER_STALLED": "The local transcription worker stopped producing progress and was terminated.",
    "FFMPEG_FAILED": "ffmpeg failed while preparing media.",
    "FFPROBE_FAILED": "ffprobe failed while inspecting media.",
    "LOCAL_TOOL_MISSING": (
        "ffmpeg or ffprobe is required but was not found. Configure "
        "BEEBOX_FFMPEG_PATH / BEEBOX_FFPROBE_PATH (or the tool paths in "
        "settings) and retry."
    ),
    "NO_AUDIO_STREAM": "The media file does not contain an audio stream.",
    "UNSUPPORTED_MEDIA_FORMAT": "The media file format is not supported.",
    "CHUNK_FAILED": "A long-audio chunk failed to transcribe.",
    "ALIGNMENT_FAILED": "Forced alignment failed to produce timestamps for the transcript.",
    "DIARIZATION_FAILED": "Speaker diarization failed to label the transcript.",
    "TASK_CANCELLED": "The task was cancelled.",
    "INVALID_URL": "The video link is not a valid absolute URL.",
    "UNSUPPORTED_URL": (
        "The link is not a supported single Bilibili or YouTube video "
        "(other hosts, playlists, collections and live streams are refused)."
    ),
    "VIDEO_NOT_ACCESSIBLE": (
        "The video is unavailable, private, removed, region-locked or paid."
    ),
    "VIDEO_LOGIN_REQUIRED": (
        "A valid platform login is required. For Bilibili the stored login is "
        "absent or expired, so the job is refused instead of silently using "
        "anonymous access; sign in from the console and retry."
    ),
    "DOWNLOAD_NETWORK_ERROR": (
        "The video download failed for a network or transport reason, "
        "including an unreachable proxy."
    ),
    "DOWNLOAD_TIMEOUT": "The video download exceeded its deadline.",
    "VIDEO_SUBTITLE_UNAVAILABLE": "The video has no usable platform subtitle track.",
    "VIDEO_AUDIO_DOWNLOAD_FAILED": "The audio-only download for a video failed.",
    "VIDEO_PLATFORM_CONFIG_INVALID": "The video platform implementation is misconfigured.",
    "VIDEO_PLATFORM_FAILED": "The video platform failed for an unclassified reason.",
}


class BeeBoxError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str | None = None,
        command: str | None = None,
        stderr_excerpt: str | None = None,
        audio_metadata: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.message = message
        self.command = command
        self.stderr_excerpt = stderr_excerpt
        self.audio_metadata = audio_metadata

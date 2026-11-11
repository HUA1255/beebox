"""yt-dlp options for YouTube without login (ticket #48).

YouTube's "Sign in to confirm you're not a bot" check needs a proof-of-origin
token. The bgutil provider generates it; BeeBox uses the provider's script mode
with Deno, so no Docker image and no extra Windows service are required.

Configuration (see ``AGENTS.md`` and ``deploy/windows/README.md``):

``BEEBOX_YOUTUBE_PO_TOKEN_PROVIDER``
    ``bgutil-script`` (default), ``bgutil-http`` or ``off``.
``BEEBOX_BGUTIL_SERVER_HOME``
    The provider's ``server`` checkout. Defaults to
    ``<tools>\\bgutil-ytdlp-pot-provider\\server``.
``BEEBOX_BGUTIL_HTTP_BASE_URL``
    Base URL for the optional HTTP provider (``bgutil-http`` mode).
``BEEBOX_DENO_PATH``
    Deno executable, passed to yt-dlp as its JavaScript runtime.

A missing provider checkout degrades to plain yt-dlp behaviour instead of
failing every YouTube job; the cookie upload and the operator documentation
are what make age-restricted videos work in that case.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from backend.services.ffmpeg_tools import get_tools_dir, resolve_deno
from backend.services.video_platform.errors import VideoPlatformConfigError

PO_TOKEN_PROVIDER_ENV = "BEEBOX_YOUTUBE_PO_TOKEN_PROVIDER"
BGUTIL_SERVER_HOME_ENV = "BEEBOX_BGUTIL_SERVER_HOME"
BGUTIL_HTTP_BASE_URL_ENV = "BEEBOX_BGUTIL_HTTP_BASE_URL"

PROVIDER_BGUTIL_SCRIPT = "bgutil-script"
PROVIDER_BGUTIL_HTTP = "bgutil-http"
PROVIDER_OFF = "off"
PROVIDERS = (PROVIDER_BGUTIL_SCRIPT, PROVIDER_BGUTIL_HTTP, PROVIDER_OFF)
DEFAULT_PROVIDER = PROVIDER_BGUTIL_SCRIPT
DEFAULT_HTTP_BASE_URL = "http://127.0.0.1:4416"
PROVIDER_DIRECTORY_NAME = "bgutil-ytdlp-pot-provider"
SCRIPT_ENTRYPOINT = ("src", "generate_once.ts")


def po_token_provider() -> str:
    """The configured provider mode; an unknown value is a configuration error."""
    value = os.environ.get(PO_TOKEN_PROVIDER_ENV, "").strip().lower()
    if not value:
        return DEFAULT_PROVIDER
    if value not in PROVIDERS:
        raise VideoPlatformConfigError(
            f"{PO_TOKEN_PROVIDER_ENV} must be one of {', '.join(PROVIDERS)}; got {value!r}"
        )
    return value


def bgutil_server_home() -> Path | None:
    """The provider checkout: explicit env first, then the fixed tools directory."""
    explicit = os.environ.get(BGUTIL_SERVER_HOME_ENV, "").strip()
    if explicit:
        return Path(explicit).expanduser().absolute()
    tools = get_tools_dir()
    if tools is not None:
        return tools / PROVIDER_DIRECTORY_NAME / "server"
    return None


def deno_path() -> str | None:
    """The Deno executable used by yt-dlp and the bgutil script provider."""
    status = resolve_deno(check_version=False)
    return status.path if status.available else None


def apply_youtube_options(options: dict[str, Any]) -> dict[str, Any]:
    """Add the PO token provider and JavaScript runtime to yt-dlp options."""
    provider = po_token_provider()
    if provider == PROVIDER_OFF:
        return options

    deno = deno_path()
    if deno:
        options["js_runtimes"] = {"deno": {"path": deno}}

    extractor_args = options.setdefault("extractor_args", {})
    if provider == PROVIDER_BGUTIL_HTTP:
        base_url = (
            os.environ.get(BGUTIL_HTTP_BASE_URL_ENV, "").strip() or DEFAULT_HTTP_BASE_URL
        )
        extractor_args["youtubepot-bgutilhttp"] = {"base_url": [base_url]}
        return options

    server_home = bgutil_server_home()
    if server_home is None:
        # No checkout to run the script from; leave yt-dlp's plugin defaults
        # alone rather than passing a path that cannot work.
        if not extractor_args:
            options.pop("extractor_args", None)
        return options
    entrypoint = server_home.joinpath(*SCRIPT_ENTRYPOINT)
    if not entrypoint.is_file():
        if not extractor_args:
            options.pop("extractor_args", None)
        return options
    extractor_args["youtubepot-bgutilscript"] = {"server_home": [str(server_home)]}
    return options


__all__ = [
    "BGUTIL_HTTP_BASE_URL_ENV",
    "BGUTIL_SERVER_HOME_ENV",
    "DEFAULT_HTTP_BASE_URL",
    "DEFAULT_PROVIDER",
    "PO_TOKEN_PROVIDER_ENV",
    "PROVIDERS",
    "PROVIDER_BGUTIL_HTTP",
    "PROVIDER_BGUTIL_SCRIPT",
    "PROVIDER_DIRECTORY_NAME",
    "PROVIDER_OFF",
    "apply_youtube_options",
    "bgutil_server_home",
    "deno_path",
    "po_token_provider",
]

"""Recognize the video links BeeBox accepts (tickets #27, #42 and #45).

Only single videos on the platforms listed in
:data:`backend.services.video_platform.platforms.PLATFORM_RULES` are supported.
Recognition is pure and offline: the caller gets a :class:`VideoRef` or a typed
:class:`UnsupportedUrlError`/``InvalidUrlError`` without any network request.
Playlists, collections and live streams are refused here, before any download;
Bilibili multi-part links keep only the requested part (``?p=N``, part 1 when
absent) so a job never turns into a list download.

Platform knowledge (hosts, URL forms, canonical URLs, capabilities) lives in
:mod:`backend.services.video_platform.platforms`; this module only validates
the value, finds the rule for its host and applies the first matching form.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from backend.services.video_platform import platforms
from backend.services.video_platform.errors import InvalidUrlError, UnsupportedUrlError
from backend.services.video_platform.models import VideoRef


def _hostname(parts) -> str:
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise InvalidUrlError("The URL has no host")
    return host


def _query(parts) -> dict[str, list[str]]:
    return parse_qs(parts.query or "", keep_blank_values=True)


def _host_rule(rule: platforms.PlatformRule, host: str) -> platforms.HostRule | None:
    for host_rule in rule.hosts:
        if host == host_rule.host or host.endswith(f".{host_rule.host}"):
            return host_rule
    return None


def _form_groups(
    form: platforms.UrlForm, path: str, query: dict[str, list[str]]
) -> dict[str, str] | None:
    """The named groups of the first matching form, with ``id`` resolved.

    Every named group the path pattern captured is kept so a canonical
    template can use it (an X post needs the account name as well as the id).
    """
    match = form.path.match(path)
    if match is None:
        return None
    groups = {
        key: value
        for key, value in match.groupdict().items()
        if isinstance(value, str)
    }
    value = groups.get("id")
    if value is None and form.query_id is not None:
        values = [item for item in query.get(form.query_id, []) if item]
        value = values[0] if values else None
        if value is not None:
            groups["id"] = value
    if value is None:
        return None
    if form.video_id_pattern is not None and not form.video_id_pattern.match(value):
        return None
    return groups


def _form_part(form: platforms.UrlForm, query: dict[str, list[str]]) -> int:
    """The requested part index; absent or ``p=1`` means the first part."""
    values = query.get(form.part_query) if form.part_query else None
    if not values:
        return 1
    raw = (values[0] or "").strip()
    if not raw.isdigit() or int(raw) < 1:
        raise UnsupportedUrlError(form.part_error.format(value=raw))
    return int(raw)


def _canonical_url(
    form: platforms.UrlForm,
    *,
    groups: dict[str, str],
    part: int | None,
    original: str,
) -> str:
    if form.canonical is None:
        return original
    part_suffix = f"?p={part}" if form.part_query and part and part > 1 else ""
    return form.canonical.format(part=part_suffix, **groups)


def _parse_host(
    rule: platforms.PlatformRule,
    host_rule: platforms.HostRule,
    *,
    original: str,
    path: str,
    query: dict[str, list[str]],
) -> VideoRef:
    for key in rule.reject_query:
        if key in query:
            raise UnsupportedUrlError(
                rule.reject_query_message or f"{key} links are not supported"
            )
    if host_rule.reject_path is not None and host_rule.reject_path.search(path):
        raise UnsupportedUrlError(
            host_rule.reject_path_message or "This link is not a single video"
        )
    for form in host_rule.forms:
        groups = _form_groups(form, path, query)
        if groups is None:
            continue
        part = _form_part(form, query) if form.part_query else None
        return VideoRef(
            platform=rule.platform,
            video_id=groups["id"],
            canonical_url=_canonical_url(
                form, groups=groups, part=part, original=original
            ),
            original_url=original,
            part=part,
        )
    raise UnsupportedUrlError(host_rule.unsupported_message)


def parse_video_url(url: str) -> VideoRef:
    """Return the platform reference for a supported single-video URL.

    Raises :class:`InvalidUrlError` when the value is not an absolute http(s)
    URL and :class:`UnsupportedUrlError` for every other host or non-video
    page (playlists, collections, live streams, channels). The original string
    is kept verbatim for result metadata.
    """
    original = (url or "").strip()
    try:
        parts = urlsplit(original)
    except ValueError as exc:
        raise InvalidUrlError(f"Not a valid URL: {url!r}") from exc
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise InvalidUrlError(f"Not an absolute http(s) URL: {url!r}")
    host = _hostname(parts)
    path = parts.path or "/"
    query = _query(parts)

    for rule in platforms.PLATFORM_RULES:
        host_rule = _host_rule(rule, host)
        if host_rule is None:
            continue
        return _parse_host(
            rule, host_rule, original=original, path=path, query=query
        )

    raise UnsupportedUrlError(f"Unsupported video platform: {host}")


__all__ = [
    "VideoRef",
    "parse_video_url",
]

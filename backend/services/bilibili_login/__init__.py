"""Bilibili QR login, daily refresh and the write-only credential file.

Tickets #46/#47: let the operator sign in to Bilibili from the console, store
the resulting cookie where yt-dlp reads it, and check/refresh that login once a
day. Credentials are never returned by an API, never logged and never written
to the database (ADR-0011/ADR-0012).
"""

from __future__ import annotations

from backend.services.bilibili_login.client import (
    ACTIVE_LOGIN_STATES,
    BILIBILI_LOGIN_ENV,
    BILIBILI_LOGIN_FAKE,
    BILIBILI_LOGIN_REAL,
    LOGIN_CANCELLED,
    LOGIN_CONFIRMED,
    LOGIN_FAILED,
    LOGIN_IDLE,
    LOGIN_PENDING,
    LOGIN_SCANNED,
    LOGIN_STATES,
    BilibiliApiLoginClient,
    BilibiliLoginClient,
    BilibiliLoginError,
    BilibiliLoginStartFailedError,
    BilibiliLoginUnavailableError,
    LoginPoll,
    QRCodePayload,
    bilibili_login_name,
    get_login_client,
)
from backend.services.bilibili_login.cookies import (
    COOKIE_FILENAME,
    LOGIN_UNAVAILABLE_STATES,
    cookie_file_path,
    login_expired,
    login_status,
    login_unavailable,
    write_cookie_file,
)
from backend.services.bilibili_login.fake import (
    FakeBilibiliRefreshClient,
    FakeBilibiliLoginClient,
)
from backend.services.bilibili_login.refresh import (
    BILIBILI_REFRESH_ENV,
    BILIBILI_REFRESH_FAKE,
    BILIBILI_REFRESH_REAL,
    REFRESH_ABSENT,
    REFRESH_EXPIRED,
    REFRESH_FAILED,
    REFRESH_REFRESHED,
    REFRESH_STATES,
    REFRESH_VALID,
    BilibiliRefreshClient,
    BilibiliRefreshService,
    BilibiliRefreshUnavailableError,
    RefreshOutcome,
    RefreshPoll,
    bilibili_refresh_name,
    get_refresh_client,
)
from backend.services.bilibili_login.service import BilibiliLoginService, LoginSnapshot

__all__ = [
    "ACTIVE_LOGIN_STATES",
    "BILIBILI_LOGIN_ENV",
    "BILIBILI_LOGIN_FAKE",
    "BILIBILI_LOGIN_REAL",
    "BILIBILI_REFRESH_ENV",
    "BILIBILI_REFRESH_FAKE",
    "BILIBILI_REFRESH_REAL",
    "COOKIE_FILENAME",
    "LOGIN_CANCELLED",
    "LOGIN_CONFIRMED",
    "LOGIN_FAILED",
    "LOGIN_IDLE",
    "LOGIN_PENDING",
    "LOGIN_SCANNED",
    "LOGIN_STATES",
    "LOGIN_UNAVAILABLE_STATES",
    "REFRESH_ABSENT",
    "REFRESH_EXPIRED",
    "REFRESH_FAILED",
    "REFRESH_REFRESHED",
    "REFRESH_STATES",
    "REFRESH_VALID",
    "BilibiliApiLoginClient",
    "BilibiliLoginClient",
    "BilibiliLoginError",
    "BilibiliLoginService",
    "BilibiliLoginStartFailedError",
    "BilibiliLoginUnavailableError",
    "BilibiliRefreshClient",
    "BilibiliRefreshService",
    "BilibiliRefreshUnavailableError",
    "FakeBilibiliLoginClient",
    "FakeBilibiliRefreshClient",
    "LoginPoll",
    "LoginSnapshot",
    "QRCodePayload",
    "RefreshOutcome",
    "RefreshPoll",
    "bilibili_login_name",
    "bilibili_refresh_name",
    "cookie_file_path",
    "get_login_client",
    "get_refresh_client",
    "login_expired",
    "login_status",
    "login_unavailable",
    "write_cookie_file",
]

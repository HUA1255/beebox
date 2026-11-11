import { useEffect, useRef, useState } from "react";

import {
  cancelBilibiliLogin,
  getBilibiliLoginStatus,
  getHealth,
  getYouTubeCookie,
  startBilibiliLogin,
  uploadYouTubeCookie,
} from "./api";
import { describeError } from "./errors";
import { formatBytes, formatDateTime } from "./format";
import type {
  BilibiliLoginSessionState,
  BilibiliLoginStartResponse,
  BilibiliLoginState,
  BilibiliLoginStatusResponse,
} from "./types";
import { usePolledData } from "./usePolledData";

// The health check is the single source of truth for the stored Bilibili
// credential, so the page can never disagree with /v1/health.
const HEALTH_REFRESH_MS = 5000;
const COOKIE_REFRESH_MS = 5000;
const LOGIN_POLL_MS = 1000;

const LOGIN_STATE_LABELS: Record<BilibiliLoginState, string> = {
  valid: "有效",
  expired: "已失效",
  absent: "未登录",
};

const LOGIN_STATE_CLASSES: Record<BilibiliLoginState, string> = {
  valid: "status-succeeded",
  expired: "status-failed",
  absent: "status-cancelled",
};

const SESSION_LABELS: Record<BilibiliLoginSessionState, string> = {
  idle: "未开始",
  pending: "等待扫码",
  scanned: "已扫码，等待确认",
  confirmed: "登录成功",
  failed: "登录失败",
  cancelled: "已取消",
};

function isActiveSession(status: BilibiliLoginSessionState | undefined): boolean {
  return status === "pending" || status === "scanned";
}

export function CredentialsPage() {
  const health = usePolledData(getHealth, HEALTH_REFRESH_MS);
  const youtube = usePolledData(getYouTubeCookie, COOKIE_REFRESH_MS);
  const refreshHealth = health.refresh;

  const [qr, setQr] = useState<BilibiliLoginStartResponse | null>(null);
  const [session, setSession] = useState<BilibiliLoginStatusResponse | null>(null);
  const [loginBusy, setLoginBusy] = useState(false);
  const [loginError, setLoginError] = useState<string | null>(null);

  const [youtubeFile, setYoutubeFile] = useState<File | null>(null);
  const [youtubeBusy, setYoutubeBusy] = useState(false);
  const [youtubeMessage, setYoutubeMessage] = useState<string | null>(null);
  const [youtubeError, setYoutubeError] = useState<string | null>(null);
  const youtubeInput = useRef<HTMLInputElement>(null);

  // One poll chain per login attempt. The token invalidates an in-flight
  // response when the operator cancels, restarts or leaves the page.
  const pollToken = useRef(0);
  const pollTimer = useRef<number | undefined>(undefined);

  useEffect(
    () => () => {
      pollToken.current += 1;
      if (pollTimer.current !== undefined) {
        window.clearTimeout(pollTimer.current);
      }
    },
    [],
  );

  function stopLoginPolling() {
    pollToken.current += 1;
    if (pollTimer.current !== undefined) {
      window.clearTimeout(pollTimer.current);
      pollTimer.current = undefined;
    }
  }

  function beginLoginPolling() {
    const token = ++pollToken.current;
    const tick = async () => {
      if (token !== pollToken.current) {
        return;
      }
      try {
        const snapshot = await getBilibiliLoginStatus();
        if (token !== pollToken.current) {
          return;
        }
        setSession(snapshot);
        if (snapshot.status === "confirmed") {
          stopLoginPolling();
          setQr(null);
          // The credential file now exists; health reports `valid` on the next
          // read, which keeps the page and /v1/health in lockstep.
          refreshHealth();
          return;
        }
        if (snapshot.status === "failed" || snapshot.status === "cancelled") {
          stopLoginPolling();
          setQr(null);
          return;
        }
      } catch (failure) {
        if (token !== pollToken.current) {
          return;
        }
        stopLoginPolling();
        setLoginError(describeError(failure));
        return;
      }
      if (token === pollToken.current) {
        pollTimer.current = window.setTimeout(tick, LOGIN_POLL_MS);
      }
    };
    void tick();
  }

  async function handleStartLogin() {
    setLoginBusy(true);
    setLoginError(null);
    stopLoginPolling();
    try {
      const payload = await startBilibiliLogin();
      setQr(payload);
      setSession({ status: "pending", detail: null });
      beginLoginPolling();
    } catch (failure) {
      setLoginError(describeError(failure));
      setQr(null);
    } finally {
      setLoginBusy(false);
    }
  }

  async function handleCancelLogin() {
    setLoginBusy(true);
    setLoginError(null);
    stopLoginPolling();
    try {
      const snapshot = await cancelBilibiliLogin();
      setSession(snapshot);
      setQr(null);
    } catch (failure) {
      setLoginError(describeError(failure));
    } finally {
      setLoginBusy(false);
    }
  }

  async function handleUploadCookie() {
    if (!youtubeFile) {
      return;
    }
    setYoutubeBusy(true);
    setYoutubeError(null);
    setYoutubeMessage(null);
    try {
      const status = await uploadYouTubeCookie(youtubeFile);
      setYoutubeMessage(
        `已保存 YouTube cookie：${status.cookie_count} 条，${formatBytes(status.size_bytes)}。`,
      );
      setYoutubeFile(null);
      if (youtubeInput.current) {
        youtubeInput.current.value = "";
      }
      youtube.refresh();
    } catch (failure) {
      setYoutubeError(describeError(failure));
    } finally {
      setYoutubeBusy(false);
    }
  }

  const loginState = health.data?.bilibili_login;
  const sessionActive = isActiveSession(session?.status);
  const cookieStatus = youtube.data;

  return (
    <section className="page" data-testid="credentials-page">
      <div className="page-heading">
        <div>
          <h1>平台凭证</h1>
          <p className="muted">
            凭证只写不读：页面只显示配置状态、大小与更新时间，永远不会显示 cookie
            内容。
          </p>
        </div>
        <button
          type="button"
          className="button ghost"
          data-testid="credentials-refresh"
          onClick={() => {
            health.refresh();
            youtube.refresh();
          }}
        >
          立即刷新
        </button>
      </div>

      {health.error ? (
        <p className="alert" role="alert" data-testid="credentials-health-error">
          {describeError(health.error)}
        </p>
      ) : null}

      <div className="cards">
        <article className="card" data-testid="credentials-bilibili-card">
          <h2>B 站扫码登录</h2>
          <p className="credential-status">
            <span className="muted">当前登录状态</span>
            <span
              className={`badge ${loginState ? LOGIN_STATE_CLASSES[loginState] : "status-cancelled"}`}
              data-testid="credentials-login-status"
            >
              {loginState ? LOGIN_STATE_LABELS[loginState] : "…"}
            </span>
          </p>

          {loginState === "expired" ? (
            <p className="alert" role="alert" data-testid="credentials-login-warning">
              B 站登录已失效：B 站链接任务会失败（VIDEO_LOGIN_REQUIRED），请立即重新扫码登录。
            </p>
          ) : null}

          {loginState === "absent" ? (
            <p className="alert" role="alert" data-testid="credentials-login-absent">
              还没有 B 站登录：B 站链接任务会失败（VIDEO_LOGIN_REQUIRED），请先扫码登录再提交。
            </p>
          ) : null}

          <div className="action-row">
            <button
              type="button"
              className="button primary"
              data-testid="credentials-qr-start"
              disabled={loginBusy || sessionActive}
              onClick={() => void handleStartLogin()}
            >
              开始扫码登录
            </button>
            {sessionActive ? (
              <button
                type="button"
                className="button"
                data-testid="credentials-qr-cancel"
                disabled={loginBusy}
                onClick={() => void handleCancelLogin()}
              >
                取消登录
              </button>
            ) : null}
          </div>

          {qr?.qr_image ? (
            <img
              className="qr-image"
              data-testid="credentials-qr-image"
              src={qr.qr_image}
              alt="B 站登录二维码"
            />
          ) : null}

          {qr?.qr_url ? (
            <p className="mono small muted" data-testid="credentials-qr-url">
              {qr.qr_url}
            </p>
          ) : null}

          <dl className="facts">
            <dt>本次登录</dt>
            <dd data-testid="credentials-session-status">
              {session ? SESSION_LABELS[session.status] : "未开始"}
            </dd>
            {qr ? (
              <>
                <dt>二维码有效期</dt>
                <dd className="mono" data-testid="credentials-qr-expires">
                  {qr.expires_in} 秒
                </dd>
              </>
            ) : null}
          </dl>

          {session?.detail ? (
            <p className="error-text small" data-testid="credentials-session-detail">
              {session.detail}
            </p>
          ) : null}

          {loginError ? (
            <p className="alert" role="alert" data-testid="credentials-action-error">
              {loginError}
            </p>
          ) : null}

          <p className="muted small">
            用 B 站手机客户端扫码并确认。确认后凭证直接写入服务端，页面不会显示其内容。
          </p>
        </article>

        <article className="card" data-testid="credentials-youtube-card">
          <h2>YouTube cookie</h2>
          <p className="muted small">
            YouTube 默认免登录；只有年龄限制视频需要上传浏览器导出的 Netscape
            cookies.txt。
          </p>

          <dl className="facts">
            <dt>状态</dt>
            <dd data-testid="credentials-youtube-status">
              {cookieStatus ? (cookieStatus.configured ? "已配置" : "未配置") : "…"}
            </dd>
            <dt>大小</dt>
            <dd className="mono" data-testid="credentials-youtube-size">
              {cookieStatus?.configured ? formatBytes(cookieStatus.size_bytes) : "—"}
            </dd>
            <dt>cookie 数量</dt>
            <dd className="mono" data-testid="credentials-youtube-count">
              {cookieStatus?.configured ? `${cookieStatus.cookie_count} 条` : "—"}
            </dd>
            <dt>更新时间</dt>
            <dd data-testid="credentials-youtube-updated">
              {cookieStatus?.configured ? formatDateTime(cookieStatus.updated_at) : "—"}
            </dd>
          </dl>

          <div className="field">
            <label htmlFor="credentials-youtube-file">选择 cookies.txt 文件</label>
            <input
              id="credentials-youtube-file"
              ref={youtubeInput}
              type="file"
              accept=".txt,text/plain"
              data-testid="credentials-youtube-file"
              onChange={(event) => {
                setYoutubeFile(event.target.files?.[0] ?? null);
                setYoutubeMessage(null);
                setYoutubeError(null);
              }}
            />
          </div>

          <p className="muted small" data-testid="credentials-youtube-filename">
            {youtubeFile ? youtubeFile.name : "尚未选择文件"}
          </p>

          <div className="action-row">
            <button
              type="button"
              className="button primary"
              data-testid="credentials-youtube-upload"
              disabled={!youtubeFile || youtubeBusy}
              onClick={() => void handleUploadCookie()}
            >
              上传并替换
            </button>
          </div>

          {youtubeMessage ? (
            <p className="notice" role="status" data-testid="credentials-youtube-message">
              {youtubeMessage}
            </p>
          ) : null}

          {youtubeError ? (
            <p className="alert" role="alert" data-testid="credentials-youtube-error">
              {youtubeError}
            </p>
          ) : null}

          {youtube.error ? (
            <p className="alert" role="alert" data-testid="credentials-youtube-load-error">
              {describeError(youtube.error)}
            </p>
          ) : null}

          <p className="muted small">
            上传会整体替换现有 cookie；服务端只保留文件本身，不回显、不入库、不写日志。
          </p>
        </article>
      </div>
    </section>
  );
}

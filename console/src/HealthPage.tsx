import { Link } from "react-router-dom";

import { getHealth, getQueue } from "./api";
import { describeError } from "./errors";
import { shortId } from "./format";
import { QueuePanel } from "./QueuePanel";
import { usePolledData } from "./usePolledData";

const REFRESH_MS = 4000;

export function HealthPage() {
  const health = usePolledData(getHealth, REFRESH_MS);
  const queue = usePolledData(getQueue, REFRESH_MS);

  const status = health.data;
  const queueState = queue.data ?? status?.queue ?? null;

  return (
    <section className="page" data-testid="health-page">
      <div className="page-heading">
        <div>
          <h1>健康与队列</h1>
          <p className="muted">每 {REFRESH_MS / 1000} 秒自动刷新。</p>
        </div>
        <button
          type="button"
          className="button ghost"
          onClick={() => {
            health.refresh();
            queue.refresh();
          }}
        >
          立即刷新
        </button>
      </div>

      {health.error ? (
        <p className="alert" role="alert" data-testid="health-error">
          {describeError(health.error)}
        </p>
      ) : null}

      <div className="cards">
        <article className="card">
          <h2>服务</h2>
          <dl className="facts">
            <dt>在线</dt>
            <dd data-testid="health-online">
              {status ? (status.online ? "是" : "否") : "…"}
            </dd>
            <dt>就绪</dt>
            <dd data-testid="health-ready">
              {status ? (status.ready ? "是" : "否（数据库或存储不可用）") : "…"}
            </dd>
            <dt>版本</dt>
            <dd className="mono" data-testid="health-version">
              {status?.version ?? "…"}
            </dd>
            <dt>提交</dt>
            <dd className="mono small" data-testid="health-commit">
              {status?.commit ?? "—"}
            </dd>
            <dt>视频下载器</dt>
            <dd className="mono" data-testid="health-ytdlp">
              {status?.yt_dlp_version ?? "—"}
            </dd>
          </dl>
        </article>

        <QueuePanel
          state={queueState}
          error={queue.error}
          onChanged={() => {
            health.refresh();
            queue.refresh();
          }}
        />

        <article className="card">
          <h2>GPU</h2>
          <dl className="facts">
            <dt>可用</dt>
            <dd data-testid="health-gpu-available">
              {status ? (status.gpu?.available ? "是" : "否（未检测到 CUDA 设备）") : "…"}
            </dd>
            <dt>设备</dt>
            <dd className="mono" data-testid="health-gpu-name">
              {status?.gpu?.name ?? "—"}
            </dd>
            <dt>显存</dt>
            <dd className="mono" data-testid="health-gpu-memory">
              {status?.gpu?.memory_total_mb != null
                ? `${status.gpu.memory_used_mb ?? 0} / ${status.gpu.memory_total_mb} MiB`
                : "—"}
            </dd>
          </dl>
        </article>
      </div>

      <article className="card">
        <h2>排队详情</h2>
        {queue.data && queue.data.queued.length > 0 ? (
          <ul className="queue-list" data-testid="queue-list">
            {queue.data.queued.map((entry) => (
              <li key={entry.job_id}>
                <Link className="mono" to={`/jobs/${entry.job_id}`}>
                  {shortId(entry.job_id)}
                </Link>
                <span className="muted"> · {entry.kind}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted" data-testid="queue-empty">
            队列为空。
          </p>
        )}
      </article>
    </section>
  );
}

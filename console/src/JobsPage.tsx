import { Link } from "react-router-dom";

import { describeError } from "./errors";
import { listJobs } from "./api";
import { formatDateTime, formatPercent, jobTitle, shortId, stageLabel, statusLabel } from "./format";
import type { JobResponse } from "./types";
import { usePolledData } from "./usePolledData";

const REFRESH_MS = 2000;

export function JobsPage() {
  const { data, error, loading, refresh } = usePolledData(listJobs, REFRESH_MS);
  const jobs: JobResponse[] = data?.items ?? [];

  return (
    <section className="page" data-testid="jobs-page">
      <div className="page-heading">
        <div>
          <h1>任务</h1>
          <p className="muted">
            每 {REFRESH_MS / 1000} 秒自动刷新；共 {data?.total ?? 0} 个任务。
          </p>
        </div>
        <div className="heading-actions">
          <Link className="button primary" to="/submit">
            提交任务
          </Link>
          <button type="button" className="button ghost" onClick={refresh}>
            立即刷新
          </button>
        </div>
      </div>

      {error ? (
        <p className="alert" role="alert" data-testid="jobs-error">
          {describeError(error)}
        </p>
      ) : null}

      {loading && !data ? <p className="muted">正在加载任务…</p> : null}

      {!loading && jobs.length === 0 ? (
        <p className="empty" data-testid="jobs-empty">
          暂无任务。点击“提交任务”上传文件或粘贴视频链接。
        </p>
      ) : null}

      {jobs.length > 0 ? (
        <div className="table-wrap">
          <table className="table" data-testid="jobs-table">
            <thead>
              <tr>
                <th>任务</th>
                <th>状态</th>
                <th>步骤</th>
                <th className="progress-column">进度</th>
                <th>创建时间</th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <tr key={job.id} data-testid="job-row" data-job-id={job.id}>
                  <td>
                    <Link className="row-title" data-testid="job-link" to={`/jobs/${job.id}`}>
                      {jobTitle(job.source)}
                    </Link>
                    <span className="mono muted">{shortId(job.id)}</span>
                  </td>
                  <td>
                    <span className={`badge status-${job.status}`} data-testid="job-status">
                      {statusLabel(job.status)}
                    </span>
                  </td>
                  <td data-testid="job-stage">{stageLabel(job.stage)}</td>
                  <td>
                    <div className="progress">
                      <div className="progress-track">
                        <div className="progress-fill" style={{ width: formatPercent(job.progress) }} />
                      </div>
                      <span className="mono" data-testid="job-progress">
                        {formatPercent(job.progress)}
                      </span>
                    </div>
                  </td>
                  <td className="mono">{formatDateTime(job.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}

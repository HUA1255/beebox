import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { cancelJob, deleteJob, getJob, getJobResult, retryJob } from "./api";
import { describeError, errorCodeMessage } from "./errors";
import {
  formatDateTime,
  formatDuration,
  formatPercent,
  formatSeconds,
  jobTitle,
  stageLabel,
  statusLabel,
  textSourceLabel,
} from "./format";
import type { JobResponse, JobResultResponse } from "./types";
import { usePolledData } from "./usePolledData";

const REFRESH_MS = 2000;
const MAX_PREVIEW_PARAGRAPHS = 30;

interface DetailData {
  job: JobResponse;
  result: JobResultResponse | null;
  resultError: unknown;
}

type PendingConfirmation = "cancel" | "delete" | null;

export function JobDetailPage() {
  const { jobId } = useParams<{ jobId: string }>();
  const navigate = useNavigate();

  const { data, error, loading, refresh } = usePolledData<DetailData>(async () => {
    if (!jobId) {
      throw new Error("缺少任务 id");
    }
    const job = await getJob(jobId);
    let result: JobResultResponse | null = null;
    let resultError: unknown = null;
    if (job.status === "succeeded") {
      try {
        result = await getJobResult(jobId);
      } catch (failure) {
        resultError = failure;
      }
    }
    return { job, result, resultError };
  }, REFRESH_MS);

  const [busy, setBusy] = useState(false);
  const [confirmation, setConfirmation] = useState<PendingConfirmation>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  async function run(action: "cancel" | "retry" | "delete", job: JobResponse) {
    setBusy(true);
    setActionError(null);
    setMessage(null);
    try {
      if (action === "cancel") {
        await cancelJob(job.id);
        setMessage("已取消任务。");
      } else if (action === "retry") {
        await retryJob(job.id);
        setMessage("已重新提交任务，等待处理。");
      } else {
        await deleteJob(job.id);
        navigate("/jobs", { replace: true });
        return;
      }
      setConfirmation(null);
      refresh();
    } catch (failure) {
      setActionError(describeError(failure));
    } finally {
      setBusy(false);
    }
  }

  if (loading && !data) {
    return <p className="muted">正在加载任务…</p>;
  }

  if (!data) {
    return (
      <section className="page" data-testid="job-detail-page">
        <p className="alert" role="alert" data-testid="job-detail-error">
          {describeError(error)}
        </p>
        <Link to="/jobs">返回任务列表</Link>
      </section>
    );
  }

  const { job, result, resultError } = data;
  const source = job.source;
  const previewParagraphs = result?.paragraphs.slice(0, MAX_PREVIEW_PARAGRAPHS) ?? [];

  // Actions mirror the /v1 rules: cancel queued/running, retry only a
  // retryable failure, delete only a terminal job. Disabled buttons keep the
  // reason in their title instead of hiding the action entirely.
  const cancellable = job.status === "queued" || job.status === "running";
  const retryable = job.status === "failed" && job.retryable;
  const deletable =
    job.status === "succeeded" || job.status === "failed" || job.status === "cancelled";
  const locked = busy || confirmation !== null;

  const failureMessage = errorCodeMessage(job.error_code) ?? job.error ?? "任务失败。";

  return (
    <section className="page" data-testid="job-detail-page">
      <div className="page-heading">
        <div>
          <p className="breadcrumb">
            <Link to="/jobs">任务</Link> / <span className="mono">{job.id}</span>
          </p>
          <h1>{jobTitle(source)}</h1>
        </div>
        <div className="heading-actions">
          <Link className="button" to="/submit">
            再提交一个
          </Link>
          <button type="button" className="button ghost" onClick={refresh}>
            立即刷新
          </button>
        </div>
      </div>

      {error ? (
        <p className="alert" role="alert">
          {describeError(error)}
        </p>
      ) : null}

      <article className="card" data-testid="job-actions">
        <h2>任务操作</h2>
        <div className="action-row">
          <button
            type="button"
            className="button"
            data-testid="job-action-cancel"
            disabled={!cancellable || locked}
            title={cancellable ? undefined : "只有排队中或处理中的任务可以取消"}
            onClick={() => {
              setActionError(null);
              setMessage(null);
              setConfirmation("cancel");
            }}
          >
            取消任务
          </button>
          <button
            type="button"
            className="button"
            data-testid="job-action-retry"
            disabled={!retryable || locked}
            title={retryable ? undefined : "只有可重试的失败任务可以重试"}
            onClick={() => void run("retry", job)}
          >
            重试任务
          </button>
          <button
            type="button"
            className="button danger"
            data-testid="job-action-delete"
            disabled={!deletable || locked}
            title={deletable ? undefined : "排队中或处理中的任务不能删除，请先取消"}
            onClick={() => {
              setActionError(null);
              setMessage(null);
              setConfirmation("delete");
            }}
          >
            删除任务
          </button>
        </div>
        <p className="muted small">
          取消：仅排队中或处理中；重试：仅可重试的失败；删除：仅已完成、失败或已取消。
        </p>

        {confirmation === "cancel" ? (
          <div className="confirm-bar" data-testid="job-confirm-cancel">
            <span>取消后任务会立即停止，确定取消吗？</span>
            <button
              type="button"
              className="button danger"
              data-testid="job-confirm-accept"
              disabled={busy}
              onClick={() => void run("cancel", job)}
            >
              确认取消
            </button>
            <button
              type="button"
              className="button ghost"
              data-testid="job-confirm-abort"
              disabled={busy}
              onClick={() => setConfirmation(null)}
            >
              返回
            </button>
          </div>
        ) : null}

        {confirmation === "delete" ? (
          <div className="confirm-bar" data-testid="job-confirm-delete">
            <span>删除后任务、转写结果与导出都无法恢复，确定删除吗？</span>
            <button
              type="button"
              className="button danger"
              data-testid="job-confirm-accept"
              disabled={busy}
              onClick={() => void run("delete", job)}
            >
              确认删除
            </button>
            <button
              type="button"
              className="button ghost"
              data-testid="job-confirm-abort"
              disabled={busy}
              onClick={() => setConfirmation(null)}
            >
              返回
            </button>
          </div>
        ) : null}

        {busy ? (
          <p className="muted" data-testid="job-action-busy">
            正在执行…
          </p>
        ) : null}
        {message ? (
          <p className="notice" role="status" data-testid="job-action-message">
            {message}
          </p>
        ) : null}
        {actionError ? (
          <p className="alert" role="alert" data-testid="job-action-error">
            {actionError}
          </p>
        ) : null}
      </article>

      <div className="cards">
        <article className="card">
          <h2>状态</h2>
          <dl className="facts">
            <dt>状态</dt>
            <dd>
              <span className={`badge status-${job.status}`} data-testid="job-detail-status">
                {statusLabel(job.status)}
              </span>
            </dd>
            <dt>步骤</dt>
            <dd data-testid="job-detail-stage">{stageLabel(job.stage)}</dd>
            <dt>进度</dt>
            <dd>
              <div className="progress">
                <div className="progress-track">
                  <div className="progress-fill" style={{ width: formatPercent(job.progress) }} />
                </div>
                <span className="mono" data-testid="job-detail-progress">
                  {formatPercent(job.progress)}
                </span>
              </div>
            </dd>
            <dt>创建</dt>
            <dd className="mono">{formatDateTime(job.created_at)}</dd>
            <dt>更新</dt>
            <dd className="mono">{formatDateTime(job.updated_at)}</dd>
            <dt>完成</dt>
            <dd className="mono">{formatDateTime(job.completed_at)}</dd>
          </dl>
          {job.status === "failed" ? (
            <div className="alert" data-testid="job-detail-failure">
              <strong data-testid="job-error-message">{failureMessage}</strong>
              {job.error_code ? (
                <p className="mono small" data-testid="job-error-code">
                  {job.error_code}
                </p>
              ) : null}
              <p className="muted">{job.retryable ? "该任务可以重试。" : "该任务不可重试。"}</p>
              {job.error ? (
                <details>
                  <summary className="small">技术详情（服务端原文）</summary>
                  <p className="small mono">{job.error}</p>
                </details>
              ) : null}
            </div>
          ) : null}
        </article>

        <article className="card">
          <h2>来源</h2>
          <dl className="facts">
            <dt>类型</dt>
            <dd data-testid="job-source-type">{source.type === "url" ? "视频链接" : "上传文件"}</dd>
            <dt>{source.type === "url" ? "标题" : "文件"}</dt>
            <dd data-testid="job-source-title">{source.title || source.filename || "—"}</dd>
            {source.type === "url" ? (
              <>
                <dt>作者</dt>
                <dd data-testid="job-source-author">{source.author || "—"}</dd>
                <dt>链接</dt>
                <dd data-testid="job-source-url">
                  {source.url ? (
                    <a href={source.url} target="_blank" rel="noreferrer noopener">
                      {source.url}
                    </a>
                  ) : (
                    "—"
                  )}
                </dd>
              </>
            ) : null}
            <dt>时长</dt>
            <dd className="mono" data-testid="job-source-duration">
              {formatDuration(source.duration_ms)}
            </dd>
            <dt>语言</dt>
            <dd data-testid="job-source-language">{source.language || "未指定"}</dd>
            <dt>文字来源</dt>
            <dd data-testid="job-source-text-source">{textSourceLabel(source.text_source)}</dd>
            <dt>字幕语言</dt>
            <dd data-testid="job-source-subtitle-language">{source.subtitle_language || "—"}</dd>
          </dl>
          <p className="muted mono">文件 id：{source.file_id || "—"}</p>
        </article>

        <article className="card">
          <h2>转写参数</h2>
          <dl className="facts">
            <dt>模型</dt>
            <dd className="mono">{job.options.model}</dd>
            <dt>说话人分离</dt>
            <dd data-testid="job-option-diarization">{job.options.diarization ? "开启" : "关闭"}</dd>
            <dt>说话人数</dt>
            <dd data-testid="job-option-num-speakers">{job.options.num_speakers ?? "自动"}</dd>
            <dt>字幕语言</dt>
            <dd>{job.options.subtitle_language || "—"}</dd>
            <dt>文字路径</dt>
            <dd data-testid="job-option-prefer">
              {job.options.prefer === "asr" ? "强制语音识别" : "自动选择"}
            </dd>
          </dl>
        </article>
      </div>

      <article className="card result-card">
        <h2>转写结果预览</h2>
        {result ? (
          <div data-testid="job-result-preview">
            <p className="muted">
              共 {result.paragraphs.length} 个段落、{result.segments.length} 个分段
              {result.speakers.length > 0 ? `、${result.speakers.length} 个说话人` : ""}。
            </p>
            {previewParagraphs.length > 0 ? (
              <ol className="paragraphs">
                {previewParagraphs.map((paragraph) => (
                  <li key={paragraph.index} className="paragraph" data-testid="result-paragraph">
                    <div className="paragraph-meta mono">
                      #{paragraph.index} · {formatSeconds(paragraph.start)}–{formatSeconds(paragraph.end)}
                      {paragraph.speaker ? ` · ${paragraph.speaker}` : ""}
                    </div>
                    <p data-testid="result-paragraph-text">{paragraph.text}</p>
                  </li>
                ))}
              </ol>
            ) : (
              <p data-testid="job-transcript">{result.text || "（空转写）"}</p>
            )}
            {result.paragraphs.length > previewParagraphs.length ? (
              <p className="muted">
                仅显示前 {previewParagraphs.length} 段；完整结果请通过 /v1/jobs/{job.id}/exports 获取。
              </p>
            ) : null}
          </div>
        ) : resultError ? (
          <p className="alert" role="alert" data-testid="job-result-error">
            {describeError(resultError)}
          </p>
        ) : (
          <p className="muted" data-testid="job-result-pending">
            任务完成后显示转写结果预览。
          </p>
        )}
      </article>
    </section>
  );
}

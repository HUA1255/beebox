import { useState } from "react";
import { Link } from "react-router-dom";

import { pauseQueue, resumeQueue } from "./api";
import { describeError } from "./errors";
import { shortId } from "./format";
import type { QueueState } from "./types";

interface QueuePanelProps {
  state: QueueState | null;
  error?: unknown;
  /** Called after a pause/resume succeeds so the page can re-read its data. */
  onChanged: () => void;
}

/**
 * The GPU queue card, shared by the health page and the models page. Pause and
 * resume go through the public `/v1/queue` routes; the running job is never
 * interrupted (ADR-0009), so the copy here says exactly that.
 */
export function QueuePanel({ state, error, onChanged }: QueuePanelProps) {
  const [pending, setPending] = useState<"pause" | "resume" | null>(null);
  const [actionError, setActionError] = useState<unknown>(null);

  async function run(action: "pause" | "resume") {
    setPending(action);
    setActionError(null);
    try {
      await (action === "pause" ? pauseQueue() : resumeQueue());
      onChanged();
    } catch (failure) {
      setActionError(failure);
    } finally {
      setPending(null);
    }
  }

  const paused = state?.paused ?? false;

  return (
    <article className="card" data-testid="queue-panel">
      <h2>GPU 队列</h2>
      <dl className="facts">
        <dt>状态</dt>
        <dd data-testid="queue-paused">
          {state ? (paused ? "已暂停" : "运行中") : "…"}
        </dd>
        <dt>等待中</dt>
        <dd className="mono" data-testid="queue-length">
          {state ? state.length : "…"}
        </dd>
        <dt>当前任务</dt>
        <dd data-testid="queue-current">
          {state?.current_job_id ? (
            <>
              <Link className="mono" to={`/jobs/${state.current_job_id}`}>
                {shortId(state.current_job_id)}
              </Link>
              {state.current_job_kind ? (
                <span className="muted"> · {state.current_job_kind}</span>
              ) : null}
            </>
          ) : (
            "空闲"
          )}
        </dd>
      </dl>
      <div className="actions">
        <button
          type="button"
          className="button"
          data-testid="queue-pause"
          disabled={!state || paused || pending !== null}
          onClick={() => void run("pause")}
        >
          {pending === "pause" ? "暂停中…" : "暂停队列"}
        </button>
        <button
          type="button"
          className="button"
          data-testid="queue-resume"
          disabled={!state || !paused || pending !== null}
          onClick={() => void run("resume")}
        >
          {pending === "resume" ? "恢复中…" : "恢复队列"}
        </button>
      </div>
      <p className="muted small">暂停后当前任务会跑完，新任务继续排队；恢复按提交顺序继续。</p>
      {error ? (
        <p className="alert" role="alert" data-testid="queue-error">
          {describeError(error)}
        </p>
      ) : null}
      {actionError ? (
        <p className="alert" role="alert" data-testid="queue-action-error">
          {describeError(actionError)}
        </p>
      ) : null}
    </article>
  );
}

import { useEffect, useState } from "react";

import {
  cancelModelDownload,
  deleteModel,
  downloadModel,
  getQueue,
  listModels,
  retryModelDownload,
  unloadModel,
} from "./api";
import { describeError } from "./errors";
import { formatBytes, formatPercent } from "./format";
import { QueuePanel } from "./QueuePanel";
import type { ModelDownloadStatus, ModelInfo } from "./types";
import { usePolledData } from "./usePolledData";

const IDLE_REFRESH_MS = 5000;
const ACTIVE_REFRESH_MS = 800;
const QUEUE_REFRESH_MS = 4000;

const DOWNLOAD_LABELS: Record<ModelDownloadStatus, string> = {
  idle: "空闲",
  downloading: "下载中",
  cancelling: "取消中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

function isActive(model: ModelInfo): boolean {
  return model.download.state === "downloading" || model.download.state === "cancelling";
}

function isRestartable(model: ModelInfo): boolean {
  return model.download.state === "failed" || model.download.state === "cancelled";
}

function verificationLabel(model: ModelInfo): string {
  if (!model.downloaded) {
    return "未校验";
  }
  if (model.complete) {
    return "校验通过";
  }
  const missing = model.missing_files.length;
  return missing > 0 ? `缺少 ${missing} 个文件` : "校验未通过";
}

function verificationTitle(model: ModelInfo): string | undefined {
  return model.missing_files.length > 0 ? `缺少：${model.missing_files.join("、")}` : undefined;
}

export function ModelsPage() {
  // Downloads are byte-progress driven, so poll quickly while one is running
  // and fall back to a cheap cadence when everything is idle.
  const [active, setActive] = useState(false);
  const models = usePolledData(listModels, active ? ACTIVE_REFRESH_MS : IDLE_REFRESH_MS);
  const queue = usePolledData(getQueue, QUEUE_REFRESH_MS);

  const [pending, setPending] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const items = models.data?.items ?? [];

  useEffect(() => {
    setActive(models.data?.items.some(isActive) ?? false);
  }, [models.data]);

  async function run(modelId: string, action: string, call: () => Promise<unknown>) {
    setPending(`${modelId}:${action}`);
    setActionError(null);
    try {
      await call();
    } catch (failure) {
      setActionError(`${modelId}：${describeError(failure)}`);
    } finally {
      setPending(null);
      models.refresh();
    }
  }

  const storageDown = items.some((model) => model.storage_status === "unavailable");

  return (
    <section className="page" data-testid="models-page">
      <div className="page-heading">
        <div>
          <h1>模型</h1>
          <p className="muted">
            共 {models.data?.total ?? 0} 个模型；下载中每 {ACTIVE_REFRESH_MS / 1000} 秒刷新。
          </p>
        </div>
        <button type="button" className="button ghost" onClick={() => models.refresh()}>
          立即刷新
        </button>
      </div>

      {models.error ? (
        <p className="alert" role="alert" data-testid="models-error">
          {describeError(models.error)}
        </p>
      ) : null}

      {storageDown ? (
        <p className="alert" role="alert" data-testid="models-storage-error">
          模型存储位置不可用：下载会失败，请检查 BEEBOX_MODEL_STORAGE_ROOT。
        </p>
      ) : null}

      {actionError ? (
        <p className="alert" role="alert" data-testid="models-action-error">
          {actionError}
        </p>
      ) : null}

      {models.loading && !models.data ? <p className="muted">正在加载模型…</p> : null}

      {items.length > 0 ? (
        <div className="table-wrap">
          <table className="table" data-testid="models-table">
            <thead>
              <tr>
                <th>模型</th>
                <th>状态</th>
                <th>校验</th>
                <th>占用空间</th>
                <th>下载</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((model) => {
                const rowPending = pending?.startsWith(`${model.id}:`) ?? false;
                const busy = rowPending || isActive(model);
                return (
                  <tr key={model.id} data-testid="model-row" data-model-id={model.id}>
                    <td>
                      <span className="row-title" data-testid="model-name">
                        {model.name}
                      </span>
                      <span className="mono muted" data-testid="model-id">
                        {model.id}
                      </span>
                      <span className="muted small">
                        {model.engine} · {model.source}
                        {model.repo_id ? ` · ${model.repo_id}` : ""}
                      </span>
                    </td>
                    <td>
                      <span
                        className={`badge ${model.downloaded ? "status-succeeded" : "status-cancelled"}`}
                        data-testid="model-downloaded"
                      >
                        {model.downloaded ? "已下载" : "未下载"}
                      </span>
                      <span
                        className={`badge ${model.loaded ? "status-running" : "status-cancelled"}`}
                        data-testid="model-loaded"
                      >
                        {model.loaded ? "已加载" : "未加载"}
                      </span>
                    </td>
                    <td data-testid="model-verified" title={verificationTitle(model)}>
                      {verificationLabel(model)}
                    </td>
                    <td className="mono" data-testid="model-size">
                      {formatBytes(model.size_bytes)}
                    </td>
                    <td>
                      <span className="mono" data-testid="model-download-state">
                        {DOWNLOAD_LABELS[model.download.state]}
                      </span>
                      <div className="progress">
                        <div className="progress-track">
                          <div
                            className="progress-fill"
                            style={{ width: formatPercent(model.download.progress) }}
                          />
                        </div>
                        <span className="mono" data-testid="model-progress">
                          {formatPercent(model.download.progress)}
                        </span>
                      </div>
                      {model.download.total_bytes ? (
                        <span className="muted small">
                          {formatBytes(model.download.bytes_downloaded)} /{" "}
                          {formatBytes(model.download.total_bytes)}
                          {model.download.filename ? ` · ${model.download.filename}` : ""}
                        </span>
                      ) : null}
                      {model.download.error ? (
                        <span
                          className="error-text small"
                          data-testid="model-download-error"
                          title={model.download.error_code ?? undefined}
                        >
                          {model.download.error}
                        </span>
                      ) : null}
                    </td>
                    <td>
                      <div className="actions">
                        {isActive(model) ? (
                          <button
                            type="button"
                            className="button"
                            data-testid="model-cancel"
                            disabled={rowPending || model.download.state === "cancelling"}
                            onClick={() =>
                              void run(model.id, "cancel", () => cancelModelDownload(model.id))
                            }
                          >
                            取消
                          </button>
                        ) : null}
                        {!isActive(model) && !model.complete ? (
                          isRestartable(model) ? (
                            <button
                              type="button"
                              className="button"
                              data-testid="model-retry"
                              disabled={rowPending}
                              onClick={() =>
                                void run(model.id, "retry", () => retryModelDownload(model.id))
                              }
                            >
                              重试
                            </button>
                          ) : (
                            <button
                              type="button"
                              className="button primary"
                              data-testid="model-download"
                              disabled={rowPending}
                              onClick={() =>
                                void run(model.id, "download", () => downloadModel(model.id))
                              }
                            >
                              下载
                            </button>
                          )
                        ) : null}
                        {model.loaded ? (
                          <button
                            type="button"
                            className="button"
                            data-testid="model-unload"
                            disabled={busy || rowPending}
                            onClick={() => void run(model.id, "unload", () => unloadModel(model.id))}
                          >
                            卸载
                          </button>
                        ) : null}
                        {!isActive(model) && (model.downloaded || model.size_bytes > 0) ? (
                          <button
                            type="button"
                            className="button"
                            data-testid="model-delete"
                            disabled={busy || rowPending}
                            onClick={() => void run(model.id, "delete", () => deleteModel(model.id))}
                          >
                            删除
                          </button>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}

      <QueuePanel
        state={queue.data}
        error={queue.error}
        onChanged={() => {
          queue.refresh();
          models.refresh();
        }}
      />
    </section>
  );
}

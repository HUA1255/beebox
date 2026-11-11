import type { JobStatus } from "./types";

const STATUS_LABELS: Record<JobStatus, string> = {
  queued: "排队中",
  running: "处理中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

// The public stage vocabulary comes from backend/v1/status.py. The internal
// names are kept as aliases so older API snapshots still render sensibly.
const STAGE_LABELS: Record<string, string> = {
  queued: "排队",
  intake: "接收媒体",
  model: "准备模型",
  transcribe: "转写",
  postprocess: "后处理",
  export: "导出",
  done: "完成",
  failed: "失败",
  cancelled: "已取消",
  created: "已创建",
  importing: "导入媒体",
  preprocessing: "预处理",
  waiting_model: "等待模型",
  downloading_model: "下载模型",
  transcribing: "转写中",
  postprocessing: "后处理中",
  exporting: "导出中",
  completed: "完成",
  failed_resumable: "可重试失败",
  interrupted: "已中断",
  video: "获取视频信息",
  result: "读取结果",
  job: "任务",
};

const TEXT_SOURCE_LABELS: Record<string, string> = {
  asr: "语音识别",
  platform_manual: "平台人工字幕",
  platform_auto: "平台自动字幕",
  platform_pending: "待确定",
};

export function statusLabel(status: string): string {
  return STATUS_LABELS[status as JobStatus] ?? status;
}

export function stageLabel(stage: string | null | undefined): string {
  if (!stage) {
    return "—";
  }
  return STAGE_LABELS[stage] ?? stage;
}

export function textSourceLabel(value: string | null | undefined): string {
  if (!value) {
    return "—";
  }
  return TEXT_SOURCE_LABELS[value] ?? value;
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return "—";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString("zh-CN", { hour12: false });
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) {
    return "—";
  }
  const totalSeconds = Math.max(0, Math.round(ms / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const pad = (part: number) => String(part).padStart(2, "0");
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${minutes}:${pad(seconds)}`;
}

export function formatSeconds(seconds: number): string {
  return formatDuration(Math.round(seconds * 1000));
}

export function formatPercent(progress: number | null | undefined): string {
  const value = Math.max(0, Math.min(1, progress ?? 0));
  return `${Math.round(value * 100)}%`;
}

const BYTE_UNITS = ["B", "KiB", "MiB", "GiB", "TiB"];

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || bytes <= 0) {
    return "0 B";
  }
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < BYTE_UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const digits = unit === 0 ? 0 : value >= 100 ? 0 : 1;
  return `${value.toFixed(digits)} ${BYTE_UNITS[unit]}`;
}

export function shortId(id: string): string {
  return id.length > 8 ? id.slice(0, 8) : id;
}

export function jobTitle(source: {
  title: string | null;
  filename: string | null;
  url: string | null;
}): string {
  return source.title || source.filename || source.url || "未命名任务";
}

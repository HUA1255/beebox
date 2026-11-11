// Mirrors the public /v1 contract (backend/v1/schemas.py). The console never
// imports backend code; these types are the only coupling to the API.

export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export interface QueueState {
  paused: boolean;
  length: number;
  current_job_id: string | null;
  current_job_kind: string | null;
}

export type BilibiliLoginState = "valid" | "expired" | "absent";
export type BilibiliLoginSessionState =
  | "idle"
  | "pending"
  | "scanned"
  | "confirmed"
  | "failed"
  | "cancelled";

export interface GpuState {
  available: boolean;
  name: string | null;
  memory_total_mb: number | null;
  memory_used_mb: number | null;
}

export interface HealthResponse {
  online: boolean;
  ready: boolean;
  version: string;
  commit: string | null;
  queue: QueueState;
  gpu: GpuState;
  yt_dlp_version: string | null;
  bilibili_login: BilibiliLoginState;
}

export interface BilibiliLoginStartResponse {
  status: "pending";
  qr_url: string | null;
  qr_image: string | null;
  expires_in: number;
}

export interface BilibiliLoginStatusResponse {
  status: BilibiliLoginSessionState;
  detail: string | null;
}

export interface YouTubeCookieStatus {
  configured: boolean;
  size_bytes: number;
  cookie_count: number;
  updated_at: string | null;
}

export interface QueuedJob {
  job_id: string;
  kind: string;
}

export interface QueueStatusResponse extends QueueState {
  queued: QueuedJob[];
}

export interface JobSourceInfo {
  type: string;
  file_id: string | null;
  filename: string | null;
  text_source: string;
  language: string | null;
  duration_ms: number | null;
  url: string | null;
  title: string | null;
  author: string | null;
  subtitle_language: string | null;
}

export interface JobOptionsInfo {
  language: string | null;
  diarization: boolean;
  num_speakers: number | null;
  model: string;
  subtitle_language: string | null;
  prefer: "asr" | null;
}

export interface JobResponse {
  id: string;
  kind: "transcription";
  status: JobStatus;
  stage: string;
  progress: number;
  error: string | null;
  error_code: string | null;
  retryable: boolean;
  source: JobSourceInfo;
  options: JobOptionsInfo;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface JobListResponse {
  items: JobResponse[];
  total: number;
}

export interface ResultSegment {
  id: number;
  start: number;
  end: number;
  text: string;
  speaker: string | null;
  confidence: number | null;
  paragraph: number | null;
}

export interface ResultParagraph {
  index: number;
  start: number;
  end: number;
  speaker: string | null;
  text: string;
  segment_ids: number[];
}

export interface ResultSpeaker {
  speaker: string | null;
  display_name: string | null;
  segment_count: number;
  duration_ms: number;
}

export interface JobResultResponse {
  job_id: string;
  status: "succeeded";
  text: string;
  language: string | null;
  duration_ms: number | null;
  segments: ResultSegment[];
  paragraphs: ResultParagraph[];
  speakers: ResultSpeaker[];
  source: JobSourceInfo;
}

export type ModelDownloadStatus =
  | "idle"
  | "downloading"
  | "cancelling"
  | "succeeded"
  | "failed"
  | "cancelled";

export type ModelStorageStatus = "available" | "read_only" | "unavailable";

export interface ModelDownloadState {
  state: ModelDownloadStatus;
  progress: number;
  bytes_downloaded: number;
  total_bytes: number | null;
  filename: string | null;
  error: string | null;
  error_code: string | null;
}

export interface ModelInfo {
  id: string;
  name: string;
  engine: string;
  source: string;
  repo_id: string | null;
  size_bytes: number;
  downloaded: boolean;
  complete: boolean;
  missing_files: string[];
  loaded: boolean;
  download: ModelDownloadState;
  storage_status: ModelStorageStatus;
}

export interface ModelListResponse {
  items: ModelInfo[];
  total: number;
}

export interface ApiErrorBody {
  detail: string;
  error_code: string | null;
  stage: string | null;
}

export interface FileMetadata {
  id: string;
  filename: string;
  content_type: string | null;
  size_bytes: number;
  sha256: string;
  created_at: string;
  download_url: string;
}

/** Optional transcription request fields; omitted fields use server defaults. */
export interface JobOptionsInput {
  language?: string;
  diarization?: boolean;
  num_speakers?: number;
  model?: string;
  subtitle_language?: string;
  prefer?: "asr";
}

export interface JobSourceInput {
  type: "file" | "url";
  file_id?: string;
  url?: string;
}

export interface JobCreateRequest {
  kind: "transcription";
  source: JobSourceInput;
  options?: JobOptionsInput;
}

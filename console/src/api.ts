// The only network seam in the console: every call goes to the public /v1
// API with `Authorization: Bearer <token>`. A 401 clears the stored token and
// tells the app to return to the login page with a clear message.

import type {
  ApiErrorBody,
  BilibiliLoginStartResponse,
  BilibiliLoginStatusResponse,
  FileMetadata,
  HealthResponse,
  JobCreateRequest,
  JobListResponse,
  JobResponse,
  JobResultResponse,
  ModelInfo,
  ModelListResponse,
  QueueStatusResponse,
  YouTubeCookieStatus,
} from "./types";

const TOKEN_KEY = "beebox.console.token";
export const UNAUTHORIZED_EVENT = "beebox:unauthorized";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly stage: string | null;

  constructor(status: number, message: string, code: string | null = null, stage: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.stage = stage;
  }
}

export function getToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function storeToken(token: string): void {
  sessionStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  sessionStorage.removeItem(TOKEN_KEY);
}

function parseErrorBody(body: unknown): ApiErrorBody | null {
  if (typeof body !== "object" || body === null) {
    return null;
  }
  const record = body as Record<string, unknown>;
  if (typeof record.detail !== "string") {
    return null;
  }
  return {
    detail: record.detail,
    error_code: typeof record.error_code === "string" ? record.error_code : null,
    stage: typeof record.stage === "string" ? record.stage : null,
  };
}

async function request<T>(path: string, init: RequestInit, token: string | null): Promise<T> {
  const headers = new Headers(init.headers);
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  headers.set("Accept", "application/json");

  let response: Response;
  try {
    response = await fetch(path, { ...init, headers });
  } catch (error) {
    throw new ApiError(0, `无法连接 BeeBox：${(error as Error).message}`);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  const contentType = response.headers.get("content-type") ?? "";
  const body: unknown = contentType.includes("application/json")
    ? await response.json().catch(() => null)
    : await response.text();

  if (!response.ok) {
    const parsed = parseErrorBody(body);
    const message =
      parsed?.detail ?? (typeof body === "string" && body ? body : `请求失败（HTTP ${response.status}）`);
    const error = new ApiError(response.status, message, parsed?.error_code ?? null, parsed?.stage ?? null);
    if (response.status === 401 && token !== null && token === getToken()) {
      clearToken();
      window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT, { detail: error }));
    }
    throw error;
  }

  return body as T;
}

function apiGet<T>(path: string, token: string | null = getToken()): Promise<T> {
  return request<T>(path, { method: "GET" }, token);
}

/** Upload a chosen or dropped file; the browser sets the multipart boundary. */
export async function uploadFile(file: File): Promise<FileMetadata> {
  const body = new FormData();
  body.append("file", file, file.name);
  return request<FileMetadata>("/v1/files", { method: "POST", body }, getToken());
}

export const createJob = (payload: JobCreateRequest) =>
  request<JobResponse>(
    "/v1/jobs",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
    getToken(),
  );

export const cancelJob = (id: string) =>
  request<JobResponse>(
    `/v1/jobs/${encodeURIComponent(id)}/cancel`,
    { method: "POST" },
    getToken(),
  );

export const retryJob = (id: string) =>
  request<JobResponse>(
    `/v1/jobs/${encodeURIComponent(id)}/retry`,
    { method: "POST" },
    getToken(),
  );

export const deleteJob = (id: string) =>
  request<void>(`/v1/jobs/${encodeURIComponent(id)}`, { method: "DELETE" }, getToken());

function apiPost<T>(path: string, token: string | null = getToken()): Promise<T> {
  return request<T>(path, { method: "POST" }, token);
}
/** Validate a token without storing it; a 401 is reported to the caller. */
export async function validateToken(token: string): Promise<void> {
  await apiGet<JobListResponse>("/v1/jobs?limit=1", token);
}

export const getHealth = () => apiGet<HealthResponse>("/v1/health");
export const getQueue = () => apiGet<QueueStatusResponse>("/v1/queue");
export const pauseQueue = () => apiPost<QueueStatusResponse>("/v1/queue/pause");
export const resumeQueue = () => apiPost<QueueStatusResponse>("/v1/queue/resume");
export const listJobs = () => apiGet<JobListResponse>("/v1/jobs?limit=100");
export const getJob = (id: string) => apiGet<JobResponse>(`/v1/jobs/${encodeURIComponent(id)}`);
export const getJobResult = (id: string) =>
  apiGet<JobResultResponse>(`/v1/jobs/${encodeURIComponent(id)}/result`);

// Credentials are write-only: these calls exchange the QR image and cookie
// metadata only. The upload is the one place a cookie file's bytes are sent;
// every response is metadata, so nothing here can echo a credential back.
export const getBilibiliLoginStatus = () =>
  apiGet<BilibiliLoginStatusResponse>("/v1/bilibili/login");
export const startBilibiliLogin = () =>
  apiPost<BilibiliLoginStartResponse>("/v1/bilibili/login");
export const cancelBilibiliLogin = () =>
  apiPost<BilibiliLoginStatusResponse>("/v1/bilibili/login/cancel");
export const getYouTubeCookie = () =>
  apiGet<YouTubeCookieStatus>("/v1/admin/youtube-cookie");

/** Upload a Netscape cookies.txt; the browser sets the multipart boundary. */
export async function uploadYouTubeCookie(file: File): Promise<YouTubeCookieStatus> {
  const body = new FormData();
  body.append("file", file, file.name);
  return request<YouTubeCookieStatus>(
    "/v1/admin/youtube-cookie",
    { method: "POST", body },
    getToken(),
  );
}

export const listModels = () => apiGet<ModelListResponse>("/v1/models");
export const downloadModel = (id: string) =>
  apiPost<ModelInfo>(`/v1/models/${encodeURIComponent(id)}/download`);
export const cancelModelDownload = (id: string) =>
  apiPost<ModelInfo>(`/v1/models/${encodeURIComponent(id)}/download/cancel`);
export const retryModelDownload = (id: string) =>
  apiPost<ModelInfo>(`/v1/models/${encodeURIComponent(id)}/download/retry`);
export const unloadModel = (id: string) =>
  apiPost<ModelInfo>(`/v1/models/${encodeURIComponent(id)}/unload`);
export const deleteModel = (id: string) =>
  request<void>(`/v1/models/${encodeURIComponent(id)}`, { method: "DELETE" }, getToken());

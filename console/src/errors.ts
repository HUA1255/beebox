// The console speaks Chinese. Every /v1 error carries a stable `error_code`
// (the catalog is GET /v1/errors); this local map holds the human meaning of
// each code so operators never see an English detail string or a stack trace.
// An unknown code falls back to the server detail plus the code, which still
// names the failure.

import { ApiError } from "./api";

export const ERROR_MESSAGES: Record<string, string> = {
  // Authentication and routing.
  AUTH_REQUIRED: "需要 token 才能访问 BeeBox。",
  AUTH_INVALID_TOKEN: "token 不正确，请检查后重试。",
  AUTH_QUERY_TOKEN_REJECTED: "不支持把 token 放在链接里，请用 Authorization 头。",
  AUTH_INVALID_RESOURCE_TICKET: "下载票据无效、已过期或与请求不匹配。",
  RESOURCE_TICKET_PATH_INVALID: "下载票据只能签发给 /v1 下载路径。",
  NOT_FOUND: "接口不存在，请检查 BeeBox 版本。",
  METHOD_NOT_ALLOWED: "该接口不支持这种请求方法。",
  INTERNAL_ERROR: "BeeBox 内部错误，详见服务端日志。",
  INVALID_REQUEST: "请求参数校验未通过，请检查填写内容。",

  // Uploads and files.
  FILE_NOT_FOUND: "文件不存在，或内容已被清理。",
  FILE_TOO_LARGE: "文件超过服务端允许的最大上传大小。",
  EMPTY_FILE: "文件内容为空，请重新选择。",
  FILE_IN_USE: "文件仍被排队中或处理中的任务使用，暂时不能删除。",
  UNSUPPORTED_FORMAT: "文件格式或内容不受支持，请换一个音视频文件。",
  UNSUPPORTED_MEDIA_FORMAT: "媒体格式不受支持。",
  NO_AUDIO_STREAM: "文件里没有音轨，无法转写。",

  // Job requests.
  JOB_NOT_FOUND: "任务不存在，可能已被清理。",
  JOB_CREATION_FAILED: "任务创建后无法读回，请查看服务端日志。",
  JOB_NOT_SUCCEEDED: "任务尚未成功，结果还不可用。",
  JOB_NOT_CANCELLABLE: "只有排队中或处理中的任务可以取消。",
  JOB_NOT_RETRYABLE: "该任务当前不可重试，只有可重试的失败任务才能重试。",
  JOB_ACTIVE: "任务仍在排队或处理中，请先取消再删除。",
  UNSUPPORTED_KIND: "不支持的任务类型。",
  UNSUPPORTED_SOURCE_TYPE: "不支持的任务来源类型。",
  INVALID_SOURCE: "任务来源缺少必需字段。",
  LANGUAGE_REQUIRED: "当前模型要求显式指定语言。",

  // Models.
  MODEL_NOT_FOUND: "所选模型不在模型注册表中。",
  MODEL_COMPATIBILITY_FAILED: "该模型与当前运行环境不兼容。",
  MODEL_NOT_DOWNLOADED: "模型文件尚未下载。",
  MODEL_DOWNLOAD_IN_PROGRESS: "模型正在下载中，请稍后再试。",
  MODEL_ALREADY_DOWNLOADED: "模型已经下载完成。",
  MODEL_NOT_DOWNLOADABLE: "该模型不支持下载。",
  MODEL_NOT_DOWNLOADING: "模型当前没有在下载。",
  MODEL_LOAD_FAILED: "模型加载失败。",
  MODEL_STORAGE_UNAVAILABLE: "模型存储目录不可用。",
  MODEL_STORAGE_READ_ONLY: "模型存储目录不可写。",

  // Media and pipeline.
  FFMPEG_FAILED: "ffmpeg 处理媒体失败，文件可能损坏。",
  FFPROBE_FAILED: "ffprobe 无法识别该媒体文件。",
  LOCAL_TOOL_MISSING:
    "服务端缺少 ffmpeg 或 ffprobe，无法处理音轨；请配置工具路径（BEEBOX_FFMPEG_PATH / BEEBOX_FFPROBE_PATH）后重新提交。",
  MEDIA_IMPORT_FAILED: "媒体导入失败。",
  UNSUPPORTED_URL: "链接不是受支持的单个 Bilibili 或 YouTube 视频。",
  INVALID_URL: "链接不是有效的绝对地址。",
  VIDEO_NOT_ACCESSIBLE: "视频不可用、已删除、私密或受地区限制。",
  VIDEO_LOGIN_REQUIRED:
    "该视频需要有效的平台登录：B 站请在「平台凭证」页扫码登录；YouTube 年龄限制视频请先上传 cookie，再重试任务。",
  VIDEO_SUBTITLE_UNAVAILABLE: "视频没有可用的平台字幕。",
  VIDEO_AUDIO_DOWNLOAD_FAILED: "视频音轨下载失败。",
  VIDEO_PLATFORM_CONFIG_INVALID: "视频平台配置有误。",
  VIDEO_PLATFORM_FAILED: "视频平台处理失败，原因未归类。",
  BILIBILI_LOGIN_UNAVAILABLE: "B 站登录组件不可用。",
  BILIBILI_LOGIN_START_FAILED: "B 站登录服务没有返回二维码。",
  BILIBILI_LOGIN_FAILED: "B 站登录失败，请重新生成二维码。",
  YOUTUBE_COOKIE_INVALID: "上传的文件不是有效的 Netscape cookies.txt。",
  YOUTUBE_COOKIE_TOO_LARGE: "cookie 文件超过 1 MiB 限制。",
  DOWNLOAD_NETWORK_ERROR: "视频下载因网络问题失败。",
  DOWNLOAD_TIMEOUT: "视频下载超时。",
  CHUNK_FAILED: "长音频的某个分片转写失败。",
  CHUNK_AUDIO_MISSING: "分片音频丢失且无法重新生成。",
  ALIGNMENT_FAILED: "强制对齐未能生成时间戳。",
  DIARIZATION_FAILED: "说话人分离失败。",
  SPEECH_ENGINE_INVALID: "配置的语音引擎不受支持。",
  SPEECH_ENGINE_CONFIG_INVALID: "语音引擎的配置无效。",
  SPEECH_ENGINE_FAILED: "语音引擎在转写或说话人分离时失败。",
  INVALID_TRANSCRIPTION_BACKEND: "配置的转写后端不受支持。",
  LOCAL_WORKER_FAILED: "本地转写进程失败。",
  LOCAL_WORKER_EXITED: "本地转写进程在返回结果前退出。",
  LOCAL_WORKER_PROTOCOL_ERROR: "本地转写进程返回了无效响应。",
  LOCAL_WORKER_STALLED: "本地转写进程长时间没有进度，已被终止。",
  WORKER_RUNTIME_ERROR: "任务进程出现未预期的错误。",
  TASK_CANCELLED: "任务已取消。",
  TASK_FAILED: "任务失败，没有更具体的错误码。",
  TASK_INTERRUPTED: "任务因服务重启而中断。",
};

/** The Chinese meaning for a bare ``error_code``, or ``null`` when unknown. */
export function errorCodeMessage(code: string | null | undefined): string | null {
  if (!code) {
    return null;
  }
  return ERROR_MESSAGES[code] ?? null;
}

/**
 * A single Chinese sentence for any failure. Known codes win over the server
 * detail; the code stays visible in parentheses for log searches.
 */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) {
      return error.code === "AUTH_INVALID_TOKEN"
        ? "token 不正确，请检查后重试。"
        : "登录已失效，请重新输入 token。";
    }
    const known = errorCodeMessage(error.code);
    if (known) {
      return `${known}（${error.code}）`;
    }
    if (error.code) {
      return `${error.message}（${error.code}）`;
    }
    return error.message;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "未知错误";
}

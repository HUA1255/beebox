# 所有能力共用一个任务资源与 GPU 队列

TTS 等以后的能力与转写进同一个服务，而不是各开项目：机器只有一块 16GB 显卡，必须由一个进程统一排队与加载卸载模型，否则会互相 OOM；任务、进度、文件、模型管理、鉴权也都能复用。对外统一为 `/v1/jobs`（用 `kind` 区分能力）与 `/v1/files`；状态只暴露 `queued / running / succeeded / failed / cancelled`，细节放在 `stage`、`progress`、`error_code`、`retryable`。错误统一为 `{detail, error_code, stage}`，错误码大写下划线；轮询是标准方式，SSE 可选，不做 webhook。

**Considered Options**

- 每个能力一套资源（`/v1/transcriptions`、`/v1/speeches`）：直观，但生命周期要实现多遍，调用方也要写多套轮询。
- TTS 独立成项目：依赖隔离最好，但 GPU 抢占无法协调；只在依赖无法共存时才这样做。

**Consequences**

- 改成 `/v1` 时与 home 一次性切换，不保留旧路径兼容层。
- 契约以提交进仓库的 `openapi.json` 快照为准，契约测试比对快照。
- 一个 GPU 队列、先来先跑、同一时间一个任务；字幕类链接任务不占 GPU。

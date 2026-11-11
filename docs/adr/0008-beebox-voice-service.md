# BeeBox：只提供后端接口的家用语音服务

项目从 Verbatim（asrbox 分支）改为 **BeeBox**：一个只通过 `/v1` 接口对外提供语音能力的后端服务，调用方是 Beer 的家里服务、脚本、agent 与控制台。不再带面向终端用户的工作台：字幕编辑、版本历史、AI 校对、翻译、聊天、批量任务、在线 ASR、MCP 都删除；LLM 类文本加工由调用方负责。控制台只是一个调用方，只用公开接口。

**Considered Options**

- 保留 asrbox 工作台并继续扩展：功能全，但 Beer 一个都不用，维护面大，还会误导 agent。
- 只删 UI、保留后端全部能力：接口面仍然巨大，且 LLM 能力与 home 的 LLM 管线重复。

**Consequences**

- 取代 ADR-0003（v1 范围）与 ADR-0004（复用 asrbox 前端）。
- 转写稿产出后不再修改；改稿在 Obsidian 里完成。
- 以后要 MCP，在 `/v1` 之上重写一个薄层。

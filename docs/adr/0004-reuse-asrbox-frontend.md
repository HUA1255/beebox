---
status: superseded by ADR-0008
---

# 复用 asrbox 的 React UI，不做桌面端

前端直接复用 asrbox（MIT）的 React UI，以 Web 构建产物由 Nginx 提供；不引入 Tauri 桌面壳，也不新写一套前端。asrbox 的桌面壳与 Web 共用同一套 React UI，Tauri 仅用于少量桌面能力且有 Web fallback，移除或禁用这些调用后同一套 UI 可在浏览器运行。

**Considered Options**

- 复用 asrbox React UI：中文 UI 完整，已覆盖上传、任务列表、分块进度、转写稿查看与多种导出；缺口是说话人改名 UI 与 DOCX（DOCX 已移出 v1）。
- 新写前端：可控，但会重复上传、任务、进度、校对等已有工作，不符合复用优先。
- 复用 OpenTranscribe UI：功能最全，但与 8–17 个服务的栈耦合，且为 AGPL。

**Consequences**

- 需要移除或条件禁用 Tauri 专用调用，并确认 Web fallback 覆盖全部使用路径。
- 需要补说话人改名 UI，并把导出调整为 TXT、Markdown、SRT。
- asrbox UI 依赖其后端 API 形状；Gate 2 冻结契约后，需要按契约调整现有调用。
- UI 构建产物进入 Nginx 前端容器，不进入 GPU 后端镜像。

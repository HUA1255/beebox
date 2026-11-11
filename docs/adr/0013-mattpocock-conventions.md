# 用 mattpocock 规范取代 openspec

开发规范只保留一套：`GLOSSARY.md`、`docs/adr/`、GitHub Issues（triage 标签、sub-issue、原生 blocked-by），由 `AGENTS.md` 作为唯一入口（`CLAUDE.md` 只引用它）。openspec、beads、codex 配置与上游 asrbox 的 agent 文档删除；openspec 中仍有效的约束改写为 ADR 或契约测试。两套规范并存会让 Windows 上开发的 agent 不知道听谁的，而接口兼容靠快照测试比靠 spec 文本可靠。

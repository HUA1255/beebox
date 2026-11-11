# BeeBox

BeeBox 是家里的语音 AI 服务：在 Windows 11 GPU 主机上原生运行，只通过 `/v1` 接口向调用方（Beer 的家里服务、脚本、agent、控制台）提供转写等语音能力，单用户自用。没有面向终端用户的工作台；转写稿产出后不再修改，LLM 类文本加工由调用方负责。定位与边界见 ADR-0008。

**本文件是仓库唯一的 agent 规范入口。** `CLAUDE.md` 只引用本文件。不要再引入第二套规格文本或工具专属规范；约束以 ADR、契约测试和 GitHub issue 为准（见 ADR-0013）。

## 先读什么

1. 当前 GitHub issue 的 Acceptance criteria —— 本次改动的规格。
2. `GLOSSARY.md` —— 领域术语。写 issue、测试名与提交信息时使用其中定义的词，不要使用 `_Avoid_` 列出的同义词。
3. `docs/adr/` —— 与本区域相关的决策。当前有效的是 0007–0013：
   - 0007 句子级分段与段落契约
   - 0008 BeeBox 只提供 `/v1` 接口
   - 0009 统一任务资源与 GPU 队列
   - 0010 Windows 原生部署（不用 Docker）
   - 0011 局域网 token 与媒体保留期
   - 0012 B 站扫码登录与自动续期
   - 0013 本套规范（AGENTS.md 为唯一入口）
   更早的 0002、0003、0004、0006 已被取代，读到时只看其替代关系。
4. `docs/agents/issue-tracker.md`（issue 与依赖）、`docs/agents/triage-labels.md`（标签）、`docs/agents/domain.md`（如何消费术语表与 ADR）。

ADR 与 issue 冲突时先指出冲突，不要静默覆盖。

## Issue 与 triage

Issues 在 GitHub 的 `HUA1255/beebox`，用 `gh` 操作；父子关系用 sub-issue，依赖用原生 blocked-by。完整命令见 `docs/agents/issue-tracker.md`。

triage 标签只有五个，标签字符串与角色同名：`needs-triage`、`needs-info`、`ready-for-agent`、`ready-for-human`、`wontfix`。含义见 `docs/agents/triage-labels.md`。

```powershell
gh issue view 31 --json number,title,body,labels,comments
gh issue list --state open --json number,title,labels
gh issue comment 31 --body "验证结果：..."
gh issue edit 31 --add-label "ready-for-human"
```

只在 `HUA1255/beebox` 建 issue。除非用户明确要求，不要 push、不要关闭 issue。

## 开发命令（PowerShell）

在仓库根目录执行：

```powershell
# 分支与工作树状态
git status --short --branch
git log --oneline -10

# 后端测试（先用 uv sync --frozen 按 uv.lock 准备 .venv）
uv run pytest backend/tests -q

# 单个测试文件
uv run pytest backend/tests/test_api.py -q

# 部署脚本安全检查（不需要管理员，也不会改动机器）
powershell -ExecutionPolicy Bypass -File .\deploy\windows\install.ps1 -DryRun

# 开发实例（与线上实例隔离，从同一 checkout 运行；详见下节）
powershell -ExecutionPolicy Bypass -File .\deploy\windows\dev-instance.ps1 -Action Start
```

- 依赖由 uv 锁定：根目录 `pyproject.toml` + `uv.lock`，Python 3.12。用 `uv sync --frozen` 安装，不要用系统 Python 3.11/3.13。
- 已删除的形态不要再引用：Docker / compose、npm 脚本、React 工作台、桌面端。
- 改代码前先 `git status --short --branch`，不要覆盖工作树里已有的修改。
- 不要为了让改动通过而削弱、跳过或删除测试。
- 提交用 Conventional Commits（`feat:` / `fix:` / `chore:` / `docs:` / `test:` / `refactor:`），正文写 `refs #<issue>`。

## 开发实例与线上实例

同一台 Windows 机器上，开发实例与线上实例必须隔离：独立根目录、端口、数据目录与 token；模型目录只读共用，不复制、不写入。开发与测试不得写线上数据，也不得占用线上 GPU 队列。默认测试只使用假引擎与假平台：`uv run pytest backend/tests -q`，不需要 GPU、模型或网络。

开发实例从当前 checkout 运行，端口 `17495`，默认根目录 `C:\BeeBoxDev`（数据、日志、token、模型缓存各自独立；线上模型目录 `C:\BeeBox\models` 只读共用）：

```powershell
# 先按 uv.lock 准备解释器（首次）
uv sync --frozen

# 启动 / 状态 / 停止（普通终端；不操作线上服务）
powershell -ExecutionPolicy Bypass -File .\deploy\windows\dev-instance.ps1 -Action Start
powershell -ExecutionPolicy Bypass -File .\deploy\windows\dev-instance.ps1 -Action Status
powershell -ExecutionPolicy Bypass -File .\deploy\windows\dev-instance.ps1 -Action Stop

# 隔离检查：路径守卫、token 分离、共享模型不可写；失败时退出码非 0
powershell -ExecutionPolicy Bypass -File .\deploy\windows\dev-instance.ps1 -Action VerifyIsolation

# 只打印计划（不建目录、不写文件、不启动进程）
powershell -ExecutionPolicy Bypass -File .\deploy\windows\dev-instance.ps1 -Action Start -DryRun
```

- 共享模型目录对当前用户可写时 Start 会拒绝启动；在管理员终端执行一次 `-Action ProtectModels`：原始 ACL 先备份到 `<DevRoot>\secrets\model-acl-backup.txt`，当前用户被授予只读（RX），SYSTEM/Administrators 保持全权，因此线上服务（LocalSystem）不受影响。`-Action UnprotectModels` 从备份恢复。
- 应用在 `BEEBOX_DEV_INSTANCE=1` 时拒绝任何落在 `BEEBOX_PROD_ROOT` 内的可写路径（数据、日志、上传、派生音频、模型缓存），并拒绝模型下载/删除/清理（`409 MODEL_STORAGE_READ_ONLY`），只允许读取共享模型。不要绕过或删除这些守卫。
- 需要真实模型时，先确保开发实例已启动，再运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\windows\run-real-tests.ps1 -MediaDir <真实媒体目录>
```

  脚本会暂停线上队列、等待当前任务结束、在开发实例环境运行 `backend/real_tests`，并在 `finally` 中恢复线上队列（测试失败、异常、Ctrl-C 都会恢复）；原本已暂停的队列保持暂停。机器重启或强杀导致未恢复时用 `-ResumeOnly`。不要绕过脚本直接跑真实测试，也不要删除共享模型。具体参数见 `deploy/windows/README.md`。

## Windows 安装、服务与外部工具

线上实例是 Windows 原生服务，由 WinSW 托管；首次安装与重装在管理员 PowerShell 中执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\windows\install.ps1 -ModelRoot <现有模型存储根>
```

- 默认根目录 `C:\BeeBox`，子目录 `app`、`venv`、`models`、`data`、`secrets`、`logs`、`tools`。参数与故障排查见 `deploy/windows/README.md`。
- 安装脚本按 `uv.lock` 建立 `venv`，在 `secrets\settings.env` 生成随机 Bearer token（已有则保留），并用 WinSW 注册自动启动的 `BeeBox` 服务；不要提交 `settings.env` 或 `secrets` 下的 XML。
- 模型只复用、不下载：`-ModelRoot` 指向现有模型存储根（权重在 `<ModelRoot>\models`），安装脚本不会调用任何下载接口。
- 外部工具固定在 `tools`：`tools\ffmpeg\bin\ffmpeg.exe`、`tools\ffmpeg\bin\ffprobe.exe`、`tools\deno\deno.exe`；对应 `BEEBOX_FFMPEG_PATH`、`BEEBOX_FFPROBE_PATH`、`BEEBOX_DENO_PATH` 由安装脚本写入。不要依赖 winget 的版本化路径；应用解析顺序为手动路径 → 环境变量 → 固定工具目录 → `PATH`。
- YouTube 默认免登录：`uv.lock` 锁定 `bgutil-ytdlp-pot-provider==2.0.2`（GPL-3.0，与项目许可证一致）生成 PO token，默认使用脚本模式，由 `BEEBOX_DENO_PATH` 的 Deno（2.4.3+）运行 `<root>\tools\bgutil-ytdlp-pot-provider\server`（`BEEBOX_BGUTIL_SERVER_HOME` 可覆盖）。不需要 Docker 或额外 Windows 服务；`BEEBOX_YOUTUBE_PO_TOKEN_PROVIDER=bgutil-http` 仅作为常驻 HTTP 服务的备选。
- 年龄限制视频的 YouTube cookie 通过 `POST /v1/admin/youtube-cookie` 上传，写入 `<root>\secrets\youtube-cookies.txt`（`BEEBOX_YOUTUBE_COOKIE_FILE` 可覆盖）。cookie 只写不读：接口不回显、不写数据库、不进日志；没有可用 cookie 时任务报 `VIDEO_LOGIN_REQUIRED`，上传后重试即可。
- 日志为 UTF-8 并按大小轮转：服务进程写 `BEEBOX_LOG_DIR`（默认 `<root>\logs`）下的 `beebox-server.log` / `beebox-worker.log`。开发实例未设置时写 `<BEEBOX_DATA_DIR>\logs`。
- agent 不要注册、启动、停止或卸载 Windows 服务；服务操作由维护者在提升权限的终端完成。`install.ps1 -DryRun` 只打印步骤，可以在普通终端运行。

## 入口文件说明

ZCode 读取仓库根目录的 `AGENTS.md`（工作区指令文件）。Claude Code 读取 `CLAUDE.md`，因此仓库保留一个只导入本文件的 `CLAUDE.md`。规范只改这里，两处都不重复规则。

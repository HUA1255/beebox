<div align="center">

# BeeBox

**把声音变成可靠的文字。本地运行的语音转写服务。**

录音、视频文件、B 站 / YouTube 链接，进去的是声音，出来的是带字符级时间戳、说话人和自然段落的转写稿。<br>
一块消费级显卡就能跑，模型和数据都留在你自己的机器上。

<p><a href="LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0-blue?style=flat-square" alt="license"></a> <img src="https://img.shields.io/badge/stack-FastAPI_·_PyTorch-0b84fe?style=flat-square" alt="stack"> <img src="https://img.shields.io/badge/version-1.0.1-blue?style=flat-square" alt="version"> <img src="https://img.shields.io/badge/platform-Windows_11_·_NVIDIA-555?style=flat-square" alt="platform"></p>

<p><a href="README.en.md">English</a> · <a href="#快速上手">快速上手</a> · <a href="#ai-agent-部署指南">AI Agent 部署</a> · <a href="docs/history.md">修复与决策历史</a> · <a href="CHANGELOG.md">更新日志</a></p>

</div>

---

## 为什么选 BeeBox

| | |
| --- | --- |
| 🎯 **中文准确** | 以 Qwen3-ASR-1.7B 为基座，主打中文会议、讲座、访谈，夹杂的英文术语也能识别 |
| ⏱️ **字符级时间戳** | 强制对齐给每个字标出起止时间，字幕能精确定位到原声 |
| 🗣️ **分清谁在说话** | pyannote 说话人分离，每一段只属于一个说话人 |
| 📖 **读起来像文章** | 按句子切分，再按说话人合并成自然段落；转写全程不改动识别出来的任何一个字 |
| 🔗 **链接直接转** | 粘贴 B 站 / YouTube 链接即可。有平台字幕时几秒出结果、不占显卡；没有字幕才下载音轨识别 |
| 💪 **长录音不怕断** | 长录音自动分块，每块结果即时落库。服务重启后从断点继续，排队中的任务原样保留 |
| 🔌 **一套接口搞定** | 统一的 `/v1` HTTP 接口：上传、建任务、轮询、取结果，脚本、应用、AI Agent 都能直接调用 |
| 🔒 **数据不出门** | 全部本地推理；源媒体用完即删；局域网访问必须带 token；平台凭证只写不读 |
| 🧩 **20 多个本地模型可选** | Whisper、SenseVoice、Paraformer、FireRedASR2、Granite、Voxtral 等，按语言和速度选用 |
| 🛠️ **省心的运维** | 作为 Windows 服务运行，开机自启、崩溃自动重启；一条命令按版本号升级或回滚 |

## 实测表现

数据来自 RTX 2080 Ti（22 GB 改装版）上的真实素材，测试记录都在仓库里：

- **82.7 分钟多人会议录音**：转写、字符级对齐和说话人分离全程约 13 分钟，显存峰值约 10 GB，16 GB 显卡放得下。
- **段落质量**：改造前有 240 段被固定的 84 字上限硬切开，改造后为 0；95.5% 的段落以句末标点结尾；没有一个段落混入两个说话人。
- **5 分钟三人对话**：识别、对齐、分离全部完成约 83 秒。
- **有字幕的 B 站视频**：3 秒出结果，全程不进显卡队列。
- **只读不改**：所有分段按顺序拼回去，和原始识别文本逐字一致。这条写成了断言，每次都检查。

## 适合用来做什么

- **会议与讲座记录**：长录音一次转完，按说话人分段，直接导出 Markdown 放进笔记。
- **视频笔记**：把 B 站、YouTube 上的课程和访谈变成可搜索、可引用的文字。
- **字幕制作**：SRT 一条对应一句话，时间轴精确到字。
- **给 AI Agent 加上"听"的能力**：Agent 调用 `/v1` 提交任务、取回结构化结果（全文、分段、段落、说话人、来源），再做总结、检索或问答。

BeeBox 专注把声音可靠地变成文字。校对、翻译、摘要这类加工交给下游的应用或大模型，服务本身保持小而稳。

---

## 目录

1. [快速上手](#快速上手)
2. [AI Agent 部署指南](#ai-agent-部署指南)
3. [方案选型与理由](#方案选型与理由)
4. [整体架构](#整体架构)
5. [已知限制](#已知限制)
6. [路线图](#路线图)
7. [配置、接口与开发](#配置接口与开发)

> 开发过程中问题是怎么发现、判断和修复的，见[修复与决策历史](docs/history.md)。代码和文档里的 `#编号` 指私有开发仓库的 issue，对照表也在那份文档里。

---

## 快速上手

**运行要求**：Windows 11 x64、NVIDIA GPU（兼容 CUDA，建议 16 GB 显存）、[uv](https://docs.astral.sh/uv/) 0.12+、Git for Windows、ffmpeg / ffprobe。Python 3.12 由 uv 按锁文件准备。

**生产安装**（注册为 Windows 服务，开机自启、崩溃自动重启），在仓库根目录用管理员 PowerShell 执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\windows\install.ps1 -ModelRoot D:\BeeBoxModels
Invoke-RestMethod http://127.0.0.1:17494/v1/health
```

**转写第一段录音**：

```powershell
$h = @{ Authorization = "Bearer $env:BEEBOX_API_TOKEN" }
$file = curl.exe -s -H "Authorization: Bearer $env:BEEBOX_API_TOKEN" -F "file=@meeting.m4a" http://127.0.0.1:17494/v1/files | ConvertFrom-Json
$job = Invoke-RestMethod -Method Post -Headers $h -ContentType 'application/json' `
  -Body (@{ source = @{ type = 'file'; file_id = $file.id } } | ConvertTo-Json) http://127.0.0.1:17494/v1/jobs
Invoke-RestMethod -Headers $h http://127.0.0.1:17494/v1/jobs/$($job.id)            # 轮询到 succeeded
Invoke-RestMethod -Headers $h http://127.0.0.1:17494/v1/jobs/$($job.id)/exports/md # 下载 Markdown
```

视频链接把 `source` 换成 `@{ type = 'url'; url = 'https://www.bilibili.com/video/BV...' }` 即可。也可以打开 `http://127.0.0.1:17494/console`，在控制台里上传、粘贴链接、查看进度和结果。

**开发或临时运行**：

```powershell
uv sync --frozen
$env:BEEBOX_DATA_DIR = "D:\beebox-data"
uv run python -m backend.server --host 127.0.0.1 --port 17494
```

完整的安装参数、目录结构、升级与回滚见 [deploy/windows/README.md](deploy/windows/README.md)。

---

## AI Agent 部署指南

BeeBox 的安装、升级和验证全部脚本化，每一步都有可以检查的结果，适合交给 Claude Code、Codex 等编程 Agent 来完成。

### 发给 Agent 的提示词

在目标 Windows 机器上打开 Agent，把下面这段话发给它：

```text
请在这台 Windows 11 机器上部署 BeeBox（https://github.com/HUA1255/beebox）。
先完整阅读仓库 README.md 的「AI Agent 部署指南」和 deploy/windows/README.md，再按步骤执行。
规则：
1. 需要管理员权限、会注册或修改系统服务、或要下载数 GB 内容的步骤，先说明要做什么，等我确认后再执行。
2. 每一步都用指南里的检查方法确认结果，失败时先看日志定位原因，不要跳过或绕过检查。
3. 不要打印、记录或提交 secrets\settings.env 里的 token 和任何 cookie。
4. 完成后告诉我：安装的版本、服务状态、模型状态，以及测试任务的结果。
```

### Agent 执行步骤

以下步骤写给 Agent 看。每一步都有"检查"项，满足后再进入下一步。

**1. 检查环境（只读）**

```powershell
[System.Environment]::OSVersion.Version   # Windows 11：Build 22000 及以上
nvidia-smi                                 # 能看到 NVIDIA GPU 和驱动版本
uv --version                               # 0.12 或更高
git --version
Get-PSDrive -PSProvider FileSystem         # 安装盘和模型盘建议各留 20 GB 以上
```

检查：上面每条命令都正常输出。缺 uv 或 Git 时，先告诉用户，由用户决定是否安装。

**2. 获取代码并切到最新发布版本**

```powershell
git clone https://github.com/HUA1255/beebox.git
cd beebox
$tag = git describe --tags --abbrev=0
git checkout $tag
```

检查：`git status` 干净，`$tag` 形如 `v1.0.1`。

**3. 准备外部工具和模型**

- ffmpeg / ffprobe：准备一个包含 `ffmpeg.exe` 和 `ffprobe.exe` 的目录或 zip。
- Deno 2.4.3+：准备一个包含 `deno.exe` 的目录或 zip（视频链接需要）。
- 模型根目录（例如 `D:\BeeBoxModels`）。对齐模型和说话人分离模型需要预先放进去：

| 目录 | 来源 | 必需文件 |
| --- | --- | --- |
| `<模型根>\models\qwen3-forced-aligner-0.6b` | Hugging Face `Qwen/Qwen3-ForcedAligner-0.6B-hf` | `config.json`、`processor_config.json` 或 `preprocessor_config.json`、`model.safetensors` |
| `<模型根>\models\pyannote-community-1` | Hugging Face `pyannote/speaker-diarization-community-1`（或 ModelScope 镜像） | `config.yaml` 及其引用的子模型 |

识别模型（默认 `qwen3-asr-1.7b`）不用手动准备，安装后通过接口下载（见第 6 步）。

检查：上表中的必需文件都存在。

**4. 预演安装（不改动机器）**

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\windows\install.ps1 -DryRun
```

检查：退出码为 0，输出列出了全部安装步骤，并且没有创建 `C:\BeeBox`。

**5. 正式安装（需要管理员，先征得用户确认）**

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\windows\install.ps1 `
  -ModelRoot D:\BeeBoxModels -FfmpegSource <ffmpeg 目录或 zip> -DenoSource <deno 目录或 zip> -Tag $tag
```

检查：

```powershell
Get-Service BeeBox                                   # Status 为 Running
Invoke-RestMethod http://127.0.0.1:17494/v1/health   # online、ready 为 True，version 与 $tag 一致
```

安装脚本会生成随机 token，写在 `C:\BeeBox\secrets\settings.env`。只有管理员能读这个文件。Agent 读取后只放进当前会话的环境变量，不要输出到屏幕：

```powershell
$env:BEEBOX_API_TOKEN = ((Get-Content C:\BeeBox\secrets\settings.env | Select-String '^BEEBOX_API_TOKEN=').Line -split '=', 2)[1]
```

**6. 下载识别模型并确认模型状态**

```powershell
$h = @{ Authorization = "Bearer $env:BEEBOX_API_TOKEN" }
Invoke-RestMethod -Method Post -Headers $h http://127.0.0.1:17494/v1/models/qwen3-asr-1.7b/download
Invoke-RestMethod -Headers $h http://127.0.0.1:17494/v1/models/qwen3-asr-1.7b   # 轮询到下载完成
```

检查：`GET /v1/models/qwen3-asr-1.7b` 返回的 `downloaded` 和 `complete` 都为 `True`，`missing_files` 为空。注意，`/v1/health` 的 `ready` 只表示数据库和存储可用，模型是否齐全要看 `/v1/models`。

**7. 端到端验收**

用一段 1 到 5 分钟的录音，按[快速上手](#快速上手)里的命令上传、建任务、轮询，再取回结果。

检查：任务状态变为 `succeeded`，`/v1/jobs/{id}/result` 有分段和段落，`exports/txt`、`exports/md`、`exports/srt` 都能下载。完成后用 `DELETE /v1/jobs/{id}` 删掉测试任务。

**8. 可选功能**

- **B 站登录**：在 `http://127.0.0.1:17494/console` 的"平台凭证"页扫码。扫码必须由用户本人在手机上完成。
- **YouTube 免登录**：检出 bgutil provider，步骤见 [deploy/windows/README.md](deploy/windows/README.md) 的"YouTube 免登录与年龄限制 cookie"。
- **局域网访问**：默认监听 `0.0.0.0:17494`，其他机器调用时带上同一个 Bearer token；不要对公网开放。

**9. 升级与回滚**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\deploy.ps1 -Tag <新版本> -DryRun
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\deploy.ps1 -Tag <新版本>
```

部署脚本会先暂停队列、等当前任务跑完，再切换代码、同步依赖、迁移数据库、重启服务，最后核对 `/v1/health` 报告的版本号和 commit。回滚就是用上一个 tag 再跑一遍。

### 出问题时

- 先看 `C:\BeeBox\logs\beebox-server.log` 和同目录的 WinSW 日志。
- 常见故障和处理办法见 [deploy/windows/README.md](deploy/windows/README.md) 的"故障排查"。
- 不要手动修改 `C:\BeeBox\app` 里的代码。工作区一旦不干净，下次部署会拒绝执行。

---

## 方案选型与理由

### 产品形态：只提供接口的服务

BeeBox 起步于开源项目 [asrbox](https://github.com/Goldloli/asrbox)，在它的基础上几乎完全重写：去掉了网页工作台和大模型加工功能，围绕统一的 `/v1` 任务接口重新设计。

**为什么只做接口**
- 转写是基础能力，最好的用法是被其他应用和 Agent 调用，而不是再做一个封闭的工作台。
- 校对、翻译、摘要更适合交给下游的大模型管线。服务只做一件事，才能把它做稳。
- 自带的控制台也只是"又一个调用方"，只使用公开接口。这样接口本身必须够用，不存在后门。

### ASR 基座：Qwen3-ASR-1.7B + ForcedAligner + pyannote

| 环节 | 选择 | 理由 | 备选 / 为什么没选 |
| --- | --- | --- | --- |
| 语音识别 | **Qwen3-ASR-1.7B**（Apache-2.0，fp16） | 中文会议、讲座的准确率明显更好 | Whisper large-v3：生态成熟但中文较弱；FireRedASR2：中文强，但当时工具链不完善 |
| 时间戳 | **Qwen3-ForcedAligner-0.6B** | Qwen3-ASR 本身不出精确时间戳，对齐器能给到字符级 | — |
| 说话人分离 | **pyannote community-1**（可用 ModelScope 镜像） | 中文会议实测可用，不需要 Hugging Face token | sherpa-onnx：依赖轻但未实测；MOSS-Transcribe-Diarize：联合分离，但会替换掉识别基座 |

识别、对齐、分离**分阶段**执行，不用一个端到端模型。每个阶段都能单独替换、单独失败、单独度量；代价是阶段之间的拼接要自己处理（见[已知限制](#已知限制)）。

选型有实测支撑：[Gate 1 报告](docs/spikes/0001-gate1.md)用 82.7 分钟的真实录音跑通了全链路，识别加对齐的显存峰值约 8.6 GB，pyannote 约 1.6 GB。详见 [ADR-0001](docs/adr/0001-qwen3-asr-base.md)、[ADR-0005](docs/adr/0005-pyannote-diarization.md)。

### 多模型目录：基座之外，按需选用

- **Whisper 系**：transformers / faster-whisper（CTranslate2）各五档，含 Distil-Whisper（仅英文）
- **中文优先**：SenseVoice Small、Paraformer-large、Fun-ASR-Nano、FireRedASR2-AED 1.1B
- **speech-LM 多语言**：Qwen3-ASR 0.6B / 1.7B、Granite Speech 4.1、Cohere Transcribe、ARK-ASR、Voxtral Mini
- **联合分离**：MOSS-Transcribe-Diarize 0.9B
- **NeMo（可选运行时）**：Parakeet TDT 0.6B v3、Canary 1B Flash

**为什么不只用一个模型**：没有哪个模型在所有语言、时长和速度上都最好。例如实测 Paraformer 是目录里最快的（RTF ≈ 0.1），而 Distil-Whisper 只适合英文，用在中文上会凭空生成英文。每个模型都**如实声明**自己的语言覆盖、时间戳和说话人能力、支持的设备，由调用方按需选择。

**为什么权重不进仓库**：权重动辄数 GB，许可证也各不相同（Apache-2.0、MIT、CC-BY-4.0、模型专有协议）。所以按需从 ModelScope / Hugging Face 下载，并在 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 维护署名。详见[模型指南](docs/models.md)和[模型路线图](docs/asr-models-roadmap.md)。

### 部署：Windows 原生服务

| 选择 | 理由 |
| --- | --- |
| Windows 11 原生运行，依赖由 **uv** 锁定 | 消费级 GPU 主机大多是 Windows。Docker 在 Windows 上用 GPU 要绕道 WSL，镜像有 20 多 GB，排查问题时多一层 |
| 用 **WinSW** 托管为系统服务 | 开机自启、不需要登录、崩溃后 5 秒自动重启 |
| 按 **git tag** 部署 | 线上运行的代码永远对应一个明确的版本，回滚就是换一个 tag |
| 控制台构建产物 `console/dist` 提交进仓库 | 生产机不需要安装 Node，服务只占一个端口；CI 会检查构建产物与源码一致 |

开发也可以在同一台机器上进行：**开发实例与线上实例严格隔离**，目录、端口（17495 和 17494）、数据和 token 各自独立，模型目录只读共享。详见 [ADR-0010](docs/adr/0010-windows-native-deployment.md)。

### 技术栈

| 层 | 选择 | 理由 |
| --- | --- | --- |
| Web 框架 | FastAPI + Uvicorn | 原生生成 OpenAPI，接口契约可以做成快照测试；Pydantic 统一校验 |
| 存储 | SQLite（SQLAlchemy）+ 本地数据目录 | 单机部署不需要独立的数据库进程，备份就是复制目录 |
| 推理 | PyTorch / transformers / CTranslate2 / FunASR | 覆盖目录中所有模型的官方推理路径 |
| 媒体 | ffmpeg / ffprobe | 事实标准，格式覆盖最全 |
| 视频链接 | yt-dlp + bgutil（YouTube PO token）+ bilibili-api（扫码登录） | 能拿到平台字幕和高音质音轨；版本锁定 |
| 依赖管理 | uv + `uv.lock` | 可复现、安装快，生产与开发用同一份锁文件 |
| 控制台 | React + Vite + TypeScript，Playwright 端到端测试 | 只使用公开接口的轻量单页应用 |

### 安全与数据

- 监听非回环地址时**必须配置 Bearer token 才能启动**，不会意外在局域网里裸奔；不接受放在 URL 参数里的 token，避免它出现在日志中。
- 浏览器或播放器无法携带请求头时，使用 60 秒有效、只能用一次、绑定路径的临时票据。
- **任务成功后立即删除源媒体和临时音频**，结果默认保留 30 天后自动清理。
- 下载用的平台凭证（B 站、YouTube cookie）**只写不读**：接口不回显、日志不记录、不进数据库。

详见 [ADR-0011](docs/adr/0011-lan-token-and-retention.md)、[隐私与本地数据](docs/privacy.md)。

---

## 整体架构

### 分层总览

```mermaid
flowchart TB
    subgraph Clients[调用方]
        app[你的应用 / 服务]
        script[脚本 / AI Agent]
        console[控制台 /console]
    end

    subgraph API["/v1 接口层（backend/v1）"]
        files["/v1/files"]
        jobs["/v1/jobs"]
        models["/v1/models"]
        queue["/v1/queue · /v1/health"]
    end

    subgraph Services[服务层（backend/services）]
        tasks[任务生命周期 tasks]
        video[视频链接 video_jobs]
        gpuq[GPU 队列 gpu_queue<br/>FIFO · 并发 1]
        pipeline[媒体 → 分块 → 合并 → 后处理<br/>→ 分段 → 段落 → 导出]
        mgr[模型管理 models]
        ret[保留期清理 retention]
    end

    subgraph Engine[SpeechEngine 边界]
        local[local：真实模型]
        fake[fake：测试替身]
    end

    worker[推理子进程<br/>local_task_worker<br/>心跳 + 卸载]
    backends[ASR 后端 · 对齐器 · pyannote<br/>backend/backends]
    store[(SQLite + 数据目录<br/>上传 / 派生音频 / 导出)]

    Clients -->|HTTP + Bearer token| API
    API --> Services
    tasks --> gpuq --> pipeline
    video -->|有平台字幕：不占 GPU| pipeline
    video -->|无字幕：只下音轨| gpuq
    pipeline --> Engine
    local --> worker --> backends
    Services --> store
```

### 一条转写任务怎么走

```text
POST /v1/files（流式上传）
  └─ POST /v1/jobs  → 状态 queued，进入 GPU 队列
       └─ preprocessing   ffprobe 预检 + ffmpeg 转成统一格式的 WAV
       └─ waiting_model   需要时下载 / 加载模型
       └─ transcribing    长音频按 120s 窗口、2s 重叠切成分块，逐块推理，进度落库
                          → 强制对齐得到字符级时间戳
                          → 跨块合并去重
       └─ diarize         （可选）pyannote 说话人分离，再把说话人归属到文字上
       └─ 后处理          句子级分段（句末标点 > 停顿 > 长度上限）→ 同说话人分组成段落
       └─ exporting       TXT / Markdown 按段落，SRT 按分段
  └─ succeeded：删除源媒体与临时音频，结果保留 30 天
```

视频链接任务在入队之前先决定走哪条路：平台上有可用的人工或自动字幕，就直接转换成同样的分段和段落，**完全不占 GPU**；没有可用字幕，或调用方指定 `prefer=asr`，才只下载音轨，然后走上面的识别流程。

### 关键设计

**① 所有能力共用一个任务资源和一个 GPU 队列**（[ADR-0009](docs/adr/0009-unified-job-api.md)）

- 一块 16 GB 显卡上，两个进程各自加载模型就会互相挤爆显存，所以由**一个进程统一排队、统一加载和卸载模型**。
- 队列严格先进先出、并发为 1，可以暂停：当前任务跑完后，新任务继续排队，恢复后接着跑。部署和真实模型测试之前都会先暂停队列。
- 对外只有 `/v1/jobs`（用 `kind` 区分能力）和 `/v1/files`。以后加入语音合成，只是多一个 `kind`，任务、进度、文件、鉴权、模型管理全部复用，调用方也只需要一套轮询逻辑。
- 对外状态只有五个：`queued / running / succeeded / failed / cancelled`。细节放在 `stage`、`progress`、`error_code`、`retryable` 里，错误格式统一为 `{detail, error_code, stage}`。调用方的状态机永远简单，内部阶段可以自由演进。

**② SpeechEngine：流水线与模型之间唯一的边界**

流水线从不直接导入任何模型运行时，只构造 `SpeechRequest`、消费 `TranscriptionResult`。边界后面有两个实现：`local` 接真实的识别、对齐和分离模型；`fake` 是脚本化的替身，返回预设的分段、说话人或失败。

这样一来，分块、合并、后处理、段落、导出这些真正复杂的逻辑，都能在**没有 GPU、没有模型、没有网络**的 CI 上用真代码测到。

**③ 推理放在独立子进程，并带活跃心跳**

- 模型崩溃、显存泄漏、CUDA 死锁都不会拖垮 API 进程；任务结束后子进程退出，显存一定能释放干净。
- 进程还活着不代表还在干活：推理死锁时进程依然存在。worker 把自己累计的 CPU 时间作为心跳上报，心跳停止增长、或者超过硬上限仍没有产出，才判定为停滞并终止。慢的分块不会被误杀，卡死的分块也不会一直占着队列。

**④ 分块落库，支持断点续跑**

长录音切成 120 秒、重叠 2 秒的分块，每一块的结果和状态都写进数据库。服务重启或崩溃后，任务从上一个完成的分块继续，排队中的任务按原顺序恢复。对一段 80 分钟的录音来说，出问题时最多重跑两分钟，而不是从头再来。

**⑤ 分段与段落两级文本结构**（[ADR-0007](docs/adr/0007-paragraph-contract.md)）

- **分段**大约是一句话，只属于一个说话人，是字幕和时间轴的单位（SRT 一条对应一个分段）。
- **段落**由同一说话人的若干分段组成，目标长度 10 到 120 秒，是阅读和导出的单位（TXT / Markdown 按段落输出）。

字幕需要短，阅读需要长。只有一级的话，要么一条字幕好几行，要么阅读稿碎成一句一行。

**⑥ 接口契约即快照**

仓库根目录的 `openapi.json` 是提交进仓库的接口快照，实现与快照不一致时测试直接失败。接口兼容性靠测试守护，而不是靠文档自觉；每次接口变更都会在评审 diff 里清楚显示出来。

**⑦ 轮询为主，不做 webhook**

调用方轮询 `GET /v1/jobs/{id}` 是标准用法，SSE 事件流可选。轮询足够简单可靠，而 webhook 要处理回调地址、重试和鉴权，复杂度不划算。

### 目录结构

```text
backend/
  v1/                  /v1 路由、请求/响应 schema、错误码
  services/            任务生命周期、GPU 队列、媒体与分块、合并、后处理、
                       分段/段落、导出、模型管理、视频平台、B 站登录、保留期清理
  backends/            模型注册表（registry）与本地 ASR 引擎分发（local_asr）
  database/            SQLite 模型与迁移
  vendor/fireredasr2/  FireRedASR2 最小推理集（上游 Apache-2.0）
  tests/               默认测试（假引擎、假平台，无需 GPU 与网络）
  real_tests/          真实模型 / 真实平台测试及结果记录
console/               控制台源码（React）与已提交的构建产物 dist/
deploy/windows/        安装、部署、开发实例脚本与 WinSW 模板
docs/                  ADR、模型指南、隐私、CI、路线图、spike 报告、修复历史
openapi.json           接口契约快照
```

---

## 已知限制

- **平台**：目前只支持 Windows 11 + NVIDIA GPU 作为生产环境。macOS / Linux 可以用于开发和部分模型验证。
- **队列**：单卡、单队列、并发为 1，没有优先级，长任务会挡住后面的 GPU 任务；有平台字幕的视频任务不受影响。
- **用户**：单 token，没有多用户、权限分级和配额，不适合对公网开放。
- **说话人分离**：短插话和主持人容易被拆成多个标签，说话人切换点可能切断一句话，重叠语音的归属不一定准确。
- **时间戳**：原生不输出时间戳的模型（如 Fun-ASR-Nano、Cohere Transcribe）暂时只有句段级时间轴。
- **实时**：不支持流式或实时转写，只处理文件和链接。
- **模型**：NeMo 模型（Parakeet、Canary）还没在 CUDA 真机上验证；FireRedASR2 单次输入上限 60 秒；部分 Hugging Face 模型需要额外授权或镜像源。
- **视频平台**：依赖 B 站和 YouTube 的非公开接口，平台改版可能导致失效。B 站登录大约每 180 天需要重新扫码一次（[ADR-0012](docs/adr/0012-bilibili-login-refresh.md)）；YouTube 年龄限制视频需要上传 cookie。
- **结果**：转写稿产出后不可修改；导出格式为 TXT / Markdown / SRT。

---

## 路线图

模型扩充见[模型路线图](docs/asr-models-roadmap.md)。

**近期：补齐质量短板**
- **通用强制对齐**：任何不输出时间戳的模型，转写完成后都可以再跑一次对齐，升级为字符级时间轴。一次投入，全部模型受益。
- **说话人分离后处理**：合并过短的说话片段、稳定说话人标签、标记重叠语音，在说话人切换处平滑句子边界。
- **NeMo 模型的 CUDA 真机验证**，决定 Parakeet / Canary 是否进入正式目录。

**中期：新能力，同一套骨架**
- **语音合成（TTS）**：作为 `/v1/jobs` 下的新 `kind` 接入，与转写共用文件、任务、GPU 队列和模型管理。
- **MCP 适配层**：在 `/v1` 之上加一层很薄的 MCP 适配，让 AI Agent 不写代码也能直接调用。
- **更多模型**：Granite Speech 5、Canary-1B-v2、Voxtral Small 24B 等，按"能力如实声明、许可合规、体积可控"的原则逐个评估。

**长期**
- 队列优先级或抢占（例如短任务插队）。
- 流式 / 近实时转写。
- 拆分 `services/tasks.py` 和 `backends/local_asr.py` 两个大模块，让流水线各阶段更容易单独测试和替换。

---

## 配置、接口与开发

### 环境变量

| 变量 | 说明 |
| --- | --- |
| `BEEBOX_DATA_DIR` | 数据目录：数据库、上传、派生音频、导出与缓存 |
| `BEEBOX_API_TOKEN` | Bearer token；监听非回环地址时必填，配置后除健康检查外的 `/v1` 接口都需要鉴权 |
| `BEEBOX_MODEL_STORAGE_ROOT` | 模型与框架缓存的存储根 |
| `BEEBOX_RETENTION_DAYS` | 结果保留天数，默认 30 |
| `BEEBOX_DOWNLOAD_PROXY` | 视频元数据、字幕与音轨下载使用的 HTTP(S) 代理；不配置则直连，代理地址不写入结果或日志 |

示例见 `.env.example`。服务**不会**自动读取 `.env`，启动前请把变量导出到环境中。

### 常用接口

| 接口 | 用途 |
| --- | --- |
| `GET /v1/health` | 在线、就绪、版本、GPU、队列、B 站登录状态 |
| `POST /v1/files` | 流式上传，返回文件 id 与元数据 |
| `POST /v1/jobs` | 以文件或视频链接为来源创建任务 |
| `GET /v1/jobs` · `GET /v1/jobs/{id}` | 任务列表 / 状态、阶段与进度 |
| `GET /v1/jobs/{id}/result` | 结构化结果（全文、分段、段落、说话人、来源） |
| `GET /v1/jobs/{id}/exports/{fmt}` | 导出 `txt` / `md` / `srt` |
| `GET /v1/models` | 本地模型状态；下载、取消、重试、卸载与删除 |
| `GET /v1/queue` | GPU 队列状态；`/pause`、`/resume` 控制队列 |
| `GET /v1/errors` | 全部错误码及含义、是否可重试 |

完整契约见 [`openapi.json`](openapi.json)。修改路由或模型后重新生成并提交：

```powershell
uv run python scripts/export_openapi.py
```

### 测试

```powershell
uv run pytest backend/tests -q          # 默认测试：假引擎、假平台，不需要 GPU、模型或网络
cd console; npm ci; npm run build       # 改了控制台源码后重新构建并提交 console/dist
```

真实模型测试需要自行准备合法媒体并预先下载模型，不属于默认测试。每个 PR 都在 `windows-latest` 上跑完整 CI，见 [docs/ci.md](docs/ci.md)。

### 延伸阅读

- [docs/history.md](docs/history.md)：修复与决策历史（工作方式、典型修错案例、issue 编号对照）
- [AGENTS.md](AGENTS.md)：开发规范与流程（唯一入口）
- [GLOSSARY.md](GLOSSARY.md)：领域术语
- [docs/adr/](docs/adr/)：架构决策记录（当前有效：0007–0013）
- [docs/models.md](docs/models.md)：模型指南与验证记录
- [docs/privacy.md](docs/privacy.md)：隐私与本地数据

## License

源代码采用 [GPL-3.0](LICENSE)（GPL-3.0-only），版权归 Feng 所有。项目起步于 MIT 许可的 asrbox 0.4.0，上游署名见 [NOTICE.md](NOTICE.md)。FFmpeg、模型及其他第三方组件分别受各自许可证约束；模型权重不随仓库分发。

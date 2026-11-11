# Windows 原生安装、服务托管与按 tag 部署（D1/D2）

BeeBox 在 Windows 11 GPU 主机上以原生 Windows 服务运行，由 WinSW 托管：开机自启、无需登录、进程崩溃后数秒内自动重启。`install.ps1` 负责首次安装，并把应用目录装成 git checkout；`deploy.ps1` 负责用一条命令把指定 git tag 部署到线上。

## 前置条件

- Windows 11 x64，管理员权限的 Windows PowerShell 5.1+。
- [uv](https://docs.astral.sh/uv/) 0.12.x 或更高版本在 `PATH` 中。`uv sync --frozen` 会按 `uv.lock` 下载依赖（torch 的 CUDA wheel 有数 GB，首次安装需要网络）。
- [Git for Windows](https://git-scm.com/download/win) 在 `PATH` 中。`<root>\app` 是一个 git checkout，安装与按 tag 部署都需要它。
- 本地已有模型权重（可选但推荐）。安装脚本只把 `BEEBOX_MODEL_STORAGE_ROOT` 指向模型目录，**不会下载任何模型**。
- ffmpeg / ffprobe 与 Deno（视频下载的 JS 运行时，也是 YouTube PO token 供应方的脚本运行时）。它们放在固定的工具目录，不使用 winget 带版本号的安装路径。
- YouTube 免登录需要与插件同版本的 bgutil provider checkout；见“YouTube 免登录与年龄限制 cookie”。

## 一条命令安装

在仓库根目录用管理员 PowerShell 执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\windows\install.ps1 -ModelRoot D:\beebox-models
```

默认安装到 `C:\BeeBox`，监听 `0.0.0.0:17494`，服务名 `BeeBox`。安装完成后检查：

```powershell
Get-Service BeeBox
Invoke-RestMethod http://127.0.0.1:17494/v1/health
```

`install.ps1` 会依次完成：

1. 检查 `uv`、`git`、管理员权限、安装根目录与源码目录的合法性。
2. 建立固定目录结构，把应用 `git clone` 到 `<root>\app`（保留 `.git` 与全部 tag），供之后的 `deploy.ps1` 切 tag。clone 源默认是 `-AppSource` 的 `origin` URL；生产机没有 GitHub 网络时用 `-GitRemote` 指向本地镜像，或用 `-Tag` 直接指定要装出的 tag。
3. 在 `<root>\app` 执行 `uv sync --frozen`，用 `UV_PROJECT_ENVIRONMENT` 把虚拟环境放到 `<root>\venv`。
4. 首次运行时生成 `<root>\secrets\settings.env`，其中包含随机 Bearer token；已有 token 时保留，不覆盖。随后把该目录 ACL 收紧为 SYSTEM 与 Administrators。
5. 把外部工具路径与当前 git 提交写进设置（`BEEBOX_FFMPEG_PATH`、`BEEBOX_FFPROBE_PATH`、`BEEBOX_DENO_PATH`、`BEEBOX_GIT_COMMIT`）。
6. 下载固定版本 WinSW（v2.12.0，SHA256 校验）或使用 `-WinSwSource` 指定的本地文件，生成服务 XML 并注册为自动启动的 Windows 服务。
7. 启动服务并轮询 `/v1/health`；失败时打印服务状态与最近的日志。

`<root>\app` 已存在但不是 git checkout（例如旧版安装复制出来的目录）时，安装脚本会拒绝覆盖并提示先移走该目录；这样能保证之后的按 tag 部署可用。

### DryRun

`-DryRun` 只打印每一步的计划，不建目录、不 clone、不下载、不写 token、不改服务：

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\windows\install.ps1 -DryRun
```

CI 会执行语法检查与一次 DryRun，并断言它没有创建任何文件。

## 按 git tag 部署（deploy.ps1）

已安装的主机可以一条命令升级到任意已发布 tag：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\deploy.ps1 -Tag v0.4.1
```

`deploy.ps1` 按以下顺序执行，任何一步失败都会立即停止并打印回滚提示：

1. **暂停 GPU 队列**：`POST /v1/queue/pause`，token 从 `<root>\secrets\settings.env` 读取，脚本不会打印它。若 API 不可达（服务本来就没在跑），跳过暂停并继续。
2. **等待当前任务结束**：轮询 `/v1/queue` 直到 `current_job_id` 为空，默认最多等 3600 秒（`-DrainTimeoutSeconds`）。正在运行的任务不会被中断；超时则失败且不改动文件。
3. **准备 tag**：`git fetch --tags --prune --force origin`，确认 `<root>\app` 没有未提交改动，解析 `refs/tags/<tag>` 的 commit，并从该 commit 读取 `pyproject.toml` 的版本号。tag 不存在或工作区不干净时在此失败，此时服务仍在运行。git 以非交互方式运行（`GIT_TERMINAL_PROMPT=0`、`GCM_INTERACTIVE=Never`），所以凭据必须已经缓存，或者 `origin` 指向本地镜像，否则会快速失败而不是卡在凭据提示。
4. **停止服务**：队列已排空后停止 WinSW 服务；Windows 上无法可靠替换正在被进程加载的文件。
5. **切换代码**：`git checkout --detach <tag 的 commit>`，并核对 `HEAD`。
6. **同步依赖**：在 `<root>\app` 执行 `uv sync --frozen`，`UV_PROJECT_ENVIRONMENT=<root>\venv`。
7. **数据库迁移**：用服务 venv 的 Python 执行 `init_db()`（含内置迁移），环境变量取自 `settings.env`。
8. **记录提交**：把 `BEEBOX_GIT_COMMIT` 写进 `settings.env` 与 WinSW 服务 XML，保证 `/v1/health` 报出本次部署的 commit。
9. **启动服务**：`BeeBoxService.exe start`，等待服务状态为 Running。
10. **核对健康**：轮询 `/v1/health`，要求 `online`、`ready` 为真，`commit` 等于 tag 的 commit，`version` 等于 tag 中 `pyproject.toml` 的版本号（默认最多 300 秒）。不一致即失败并打印最近的日志。
11. **恢复队列**：`POST /v1/queue/resume`。服务重启会丢失内存中的暂停状态，这里的 resume 是幂等的确认。

脚本只等待正在运行的任务（票面要求）；暂停期间新提交的任务与已在排队的任务不会开始，服务重启后会被启动时的恢复逻辑标记为 `interrupted` / `failed_resumable`。要做到完全零影响，请在 `/v1/queue` 的 `length` 为 0 时部署。

失败时 finally 分支会在服务可达时恢复队列；如果服务已停止，暂停状态随进程一起消失，服务下次启动自然是不暂停的。脚本会提示回滚命令，例如：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\deploy.ps1 -Tag v1.0.1
```

### deploy.ps1 参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `-Tag` | 必填 | 要部署的 git tag，例如 `v0.4.1` |
| `-Root` | `C:\BeeBox` | 固定安装根目录 |
| `-DrainTimeoutSeconds` | `3600` | 等待当前任务结束的最长秒数；超时失败，不中断任务 |
| `-HealthTimeoutSeconds` | `300` | 重启后等待 `/v1/health` 报出新 commit 与版本的最长秒数 |
| `-IssueNumber` | 空 | 可选。部署结果（成功或失败、tag、commit、版本、耗时）通过 `gh` 回写到该 issue，不含任何密钥 |
| `-DryRun` | 关闭 | 只打印步骤；不发 HTTP、不 fetch、不写文件、不动服务，退出码 0 |

### DryRun

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\deploy.ps1 -Tag v0.4.1 -DryRun
```

DryRun 不需要已安装好的 `<root>`，也不会要求管理员权限（CI 就是这样跑的）。它能读取现有 checkout 与 `settings.env` 时，会显示解析到的 commit、版本、端口与服务名；读不到就用默认值继续打印计划步骤。CI 断言退出码为 0、没有创建根目录，并且步骤顺序正确。

### 回写 issue

`-IssueNumber` 使用部署主机上的 `gh` CLI。要求 `gh` 已登录，且 `<root>\app` 的 `origin` 指向 GitHub 仓库（`install.ps1` 默认就从 origin clone）。回写失败只警告，不会让已经成功的部署变成失败。示例：

```powershell
.\deploy\windows\deploy.ps1 -Tag v0.4.1 -IssueNumber 54
```

## 从 Mac 通过 SSH 远程部署

Windows OpenSSH 只对局域网与 Tailscale 开放。一次性配置（管理员 PowerShell）：

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Set-Service sshd -StartupType Automatic
Start-Service sshd
# 只放行私网网卡与 Tailscale 网段（100.64.0.0/10），不要对公网开放 22 端口
Set-NetFirewallRule -Name OpenSSH-Server-In-TCP -Enabled True -Profile Private `
  -RemoteAddress LocalSubnet,100.64.0.0/10
```

确认 Windows 用户可以登录（管理员组用户通过 SSH 登录时默认拿到提升后的令牌）。在 Mac 上先试 DryRun，再正式部署：

```bash
ssh <user>@<beebox-host> \
  'powershell -NoProfile -ExecutionPolicy Bypass -File C:\Admin\beebox\deploy\windows\deploy.ps1 -Tag v0.4.1 -DryRun'

ssh <user>@<beebox-host> \
  'powershell -NoProfile -ExecutionPolicy Bypass -File C:\Admin\beebox\deploy\windows\deploy.ps1 -Tag v0.4.1 -IssueNumber 54'
```

- `C:\Admin\beebox` 是运维用的仓库 checkout（或至少是放部署脚本的目录），不要用 `<root>\app` 里即将被切换的那份作为唯一副本；虽然 PowerShell 会在切换前读完脚本，独立副本更容易排查问题。
- 路径含空格时在远端命令里用双引号包住整个 `-File` 路径；Mac 的 zsh 用单引号包住整条 SSH 命令即可。
- OpenSSH 的默认 shell 可以是 `cmd.exe` 或 PowerShell；上面的写法显式调用 `powershell -File`，不依赖默认 shell。
- Tailscale 的地址用 `tailscale ip -4` 查；在同一 tailnet 内也可以直接用 MagicDNS 主机名。

## install.ps1 参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `-Root` | `C:\BeeBox` | 固定安装根目录 |
| `-AppSource` | 本仓库根目录 | 用于校验安装、并作为默认 clone 源的仓库 checkout |
| `-GitRemote` | `-AppSource` 的 origin URL | 要 clone 到 `<Root>\app` 的 URL 或本地路径；生产机可指向本地镜像 |
| `-Tag` | 空 | clone 后检出的 tag；不填则留在远端默认分支 |
| `-ModelRoot` | `<Root>\models` | 模型存储根；权重位于 `<ModelRoot>\models`，缓存位于 `<ModelRoot>\cache` |
| `-Port` | `17494` | HTTP 端口 |
| `-BindAddress` | `0.0.0.0` | 监听地址；局域网 / Tailscale 访问需要 token |
| `-ServiceName` | `BeeBox` | Windows 服务名 |
| `-SettingsPath` | `<Root>\secrets\settings.env` | 机器本地设置文件 |
| `-WinSwSource` | 空（下载固定版本） | 本地 `WinSW-x64.exe`，用于离线安装 |
| `-FfmpegSource` | 空 | 含 `ffmpeg.exe` / `ffprobe.exe` 的目录或 zip，复制到 `<Root>\tools\ffmpeg` |
| `-DenoSource` | 空 | 含 `deno.exe` 的目录或 zip，复制到 `<Root>\tools\deno` |
| `-HealthTimeoutSeconds` | `180` | 启动后等待 `/v1/health` 的秒数 |
| `-Force` | 关闭 | 覆盖已存在的工具目录 |
| `-DryRun` | 关闭 | 只打印步骤 |

## 目录结构

```text
C:\BeeBox\
  app\        应用 git checkout（保留 .git 与全部 tag；deploy.ps1 在这里切 tag）
  venv\       uv sync --frozen 安装的 Python 3.12 虚拟环境
  models\     模型存储根：models\<model-id>\ 权重，cache\ 框架缓存
  data\       数据库、上传、派生音频、导出与缓存
  secrets\    settings.env（含 Bearer token 与 BEEBOX_GIT_COMMIT）与 WinSW 服务文件
  logs\       UTF-8 日志，按大小轮转
  tools\      ffmpeg\bin\{ffmpeg,ffprobe}.exe、deno\deno.exe、bgutil-ytdlp-pot-provider\server\
```

`BEEBOX_MODEL_STORAGE_ROOT` 指向 `-ModelRoot`。要复用已有模型目录，直接传现有存储根即可，例如：

```powershell
.\deploy\windows\install.ps1 -ModelRoot D:\BeeBoxModels
```

安装脚本不会复制、移动或下载模型。若目录不存在 `models\` 子目录，它只创建空目录并给出警告。

## 密钥

- Bearer token 只存在于 `<root>\secrets\settings.env`，由安装脚本随机生成（32 字节、base64url、43 个字符）。
- 该目录 ACL 收紧为 `SYSTEM` 与 `Administrators`；普通用户不能读取。读取 token：

```powershell
Get-Content C:\BeeBox\secrets\settings.env | Select-String BEEBOX_API_TOKEN
```

- `settings.env` 与生成的 `secrets\winsw\BeeBoxService.xml`（其中包含同一 token）都**不得提交到仓库、不得贴进日志或 issue**。
- B 站扫码登录确认后，cookie 由服务写入 `<root>\secrets\bilibili-cookies.txt`（Netscape 格式，yt-dlp 直接读取）。对接口只写不读：不回显、日志不记录；只是内部每日刷新会读取该文件续期；`/v1/health` 的 `bilibili_login` 只给出 `valid` / `expired` / `absent`。重新扫码成功会原子覆盖该文件。
- 重新运行安装会按本次参数重写布局相关键（根目录、数据、模型、日志、工具路径），但保留已有 token；要轮换 token，删除 `settings.env` 中的 `BEEBOX_API_TOKEN=` 行后重新安装（会中断调用方，需同步更新 home 客户端）。
- 重新运行安装时，如果 `<root>\app` 已是 git checkout，脚本会 fetch tag，但只有传了 `-Tag` 才切换 commit；不传就保持当前部署的代码，避免误升到默认分支。日常升级用 `deploy.ps1`。
- 用 `-SettingsPath` 把设置放到 `secrets` 之外时，脚本只收紧 `secrets` 目录的 ACL，自定义位置需要自行保护。

## 日志

- 应用通过 `backend.logging_setup` 写 UTF-8 日志：`<root>\logs\beebox-server.log`（服务进程）与 `beebox-worker.log`（本地转写 worker）。
- 默认单文件 10 MiB、保留 5 个备份，可用 `BEEBOX_LOG_MAX_BYTES` / `BEEBOX_LOG_BACKUP_COUNT` 调整。轮转文件为 `.1`、`.2`……
- WinSW 另把 stdout/stderr 写到同一个 `logs` 目录（按大小轮转），用于捕获应用启动前的错误与崩溃信息。
- 开发实例不设置 `BEEBOX_LOG_DIR` 时，日志落在 `<BEEBOX_DATA_DIR>\logs`；控制台输出保留。

## 外部工具

应用按以下顺序解析可执行文件：数据库中的手动路径 → `BEEBOX_*_PATH` 环境变量 → `<root>\tools` 固定布局 → `PATH`。winget 的版本化目录不在解析规则内。

| 工具 | 环境变量 | 工具目录布局 |
| --- | --- | --- |
| ffmpeg | `BEEBOX_FFMPEG_PATH` | `<root>\tools\ffmpeg\bin\ffmpeg.exe` |
| ffprobe | `BEEBOX_FFPROBE_PATH` | `<root>\tools\ffmpeg\bin\ffprobe.exe` |
| Deno | `BEEBOX_DENO_PATH` | `<root>\tools\deno\deno.exe` |

安装使用的应用代码来自 git checkout（或 `-AppSource` 副本），其中包含提交进仓库的 `console/dist`，服务直接从 `<root>\app\console\dist` 提供 `/console`，生产机不需要 Node；若 checkout 缺少该目录，安装会直接报错并提示在开发机重新构建后提交。

安装时没有提供工具源码也可以完成安装并启动服务；此时 `/v1/health` 会报告 ffmpeg 不可用，转写任务会在预检阶段失败。补齐工具后重新运行安装脚本（或只改 `settings.env` 中对应的路径并重启服务）即可。

## YouTube 免登录与年龄限制 cookie

YouTube 默认不需要登录。`uv.lock` 锁定 `bgutil-ytdlp-pot-provider==2.0.2` 插件（GPL-3.0，与项目许可证一致；见 `THIRD_PARTY_NOTICES.md`），PO token 由 bgutil provider 的 Deno 脚本模式生成。这种方式不需要 Docker，也不需要额外的 Windows 服务。

首次安装后，把 provider 检出到固定目录，并让 provider 与插件保持同一版本：

```powershell
git clone --single-branch --branch 2.0.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git C:\BeeBox\tools\bgutil-ytdlp-pot-provider
cd C:\BeeBox\tools\bgutil-ytdlp-pot-provider\server
C:\BeeBox\tools\deno\deno.exe install --allow-scripts=npm:canvas --frozen
```

应用默认从 `<root>\tools\bgutil-ytdlp-pot-provider\server` 读取 provider（`BEEBOX_BGUTIL_SERVER_HOME` 可覆盖），并通过 `BEEBOX_DENO_PATH` 把 Deno 交给 yt-dlp；Deno 需要 2.4.3 或更高版本。provider 目录缺失时普通 YouTube 下载仍会尝试，但「确认你不是机器人」的校验可能失败。

如果脚本模式在这台机器上不可用，可以改用常驻 HTTP provider（默认只监听 `127.0.0.1:4416`）：

```powershell
cd C:\BeeBox\tools\bgutil-ytdlp-pot-provider\server\node_modules
C:\BeeBox\tools\deno\deno.exe run --allow-env --allow-net --allow-ffi=. --allow-read=. ..\src\main.ts
```

然后在 `settings.env` 设 `BEEBOX_YOUTUBE_PO_TOKEN_PROVIDER=bgutil-http`（必要时设 `BEEBOX_BGUTIL_HTTP_BASE_URL`）。这种方式需要一个独立的 Windows 服务托管；默认的脚本模式不需要。不要把该 HTTP 端口发布到回环地址以外。

年龄限制视频需要一次性的 YouTube cookie：

```powershell
curl.exe -X POST http://127.0.0.1:17494/v1/admin/youtube-cookie `
  -H "Authorization: Bearer <token>" `
  -F "file=@C:\Users\<you>\cookies.txt"
```

- 文件必须是 Netscape 格式的 `cookies.txt`（浏览器扩展导出），最大 1 MiB；上传会覆盖旧文件。
- 存放在 `<root>\secrets\youtube-cookies.txt`（`BEEBOX_YOUTUBE_COOKIE_FILE` 可覆盖），沿用 `secrets` 目录的 ACL。
- 接口只返回 `configured`、`size_bytes`、`cookie_count`、`updated_at`；不回显内容、不写数据库、不写日志。`GET /v1/admin/youtube-cookie` 也只查状态。
- 没有可用 cookie 时年龄限制视频的任务失败并报 `VIDEO_LOGIN_REQUIRED`；上传后重新提交即可。

## 服务管理

WinSW 可执行文件与配置在 `<root>\secrets\winsw`：

```powershell
$winsw = 'C:\BeeBox\secrets\winsw\BeeBoxService.exe'
& $winsw status
& $winsw restart
& $winsw stop
& $winsw start
```

- 启动类型为 Automatic，账户为 LocalSystem，因此开机即启动、无需任何人登录。
- 进程崩溃后 WinSW 在 5 秒后重启；`resetfailure` 为 1 小时。
- 查看服务与端口：`Get-Service BeeBox`、`sc.exe query BeeBox`、`netstat -ano | findstr :17494`。
- 升级与回滚统一用 `deploy.ps1 -Tag <tag>`，不要手工改 `<root>\app` 里的代码；手工改动会让下一次部署因为工作区不干净而拒绝执行。
- 卸载：`& $winsw uninstall`（安装脚本在重装前会自动停止并移除旧服务）。

## 开发实例（D3）

开发实例与线上服务同机运行但完全隔离（ADR-0010）：独立根目录、端口 `17495`、数据目录、日志与 Bearer token；**模型目录与线上共用同一份权重，只读**，不复制。开发实例直接从仓库 checkout 运行（不复制应用代码），线上服务、线上数据和线上 GPU 队列都不会被这个脚本启动、停止或修改。

```powershell
# 在仓库根目录，普通终端即可
powershell -ExecutionPolicy Bypass -File .\deploy\windows\dev-instance.ps1 -Action Start
powershell -ExecutionPolicy Bypass -File .\deploy\windows\dev-instance.ps1 -Action Status
powershell -ExecutionPolicy Bypass -File .\deploy\windows\dev-instance.ps1 -Action Stop
```

默认布局（参数 `-DevRoot` / `-ProdRoot` / `-Port` 可改）：

```text
C:\BeeBoxDev\
  data\          开发实例数据库、上传、导出（独立）
  secrets\       settings.env，含开发实例专用 Bearer token
  logs\          dev-instance.out.log / dev-instance.err.log
  model-cache\   HF/torch/ModelScope 缓存（写这里，不写线上）
  dev-instance.pid
C:\BeeBox\models 只读共用：权重、model.json，不复制、不修改
```

- 运行前先在 checkout 里执行 `uv sync --frozen`，脚本使用 `<checkout>\.venv\Scripts\python.exe`；也可以用 `-PythonPath` 指定解释器。
- 共享模型根默认从 `<ProdRoot>\secrets\settings.env` 的 `BEEBOX_MODEL_STORAGE_ROOT` 读取（生产用 `install.ps1 -ModelRoot` 指定过自定义目录时就是它）；读不到时退回 `<ProdRoot>\models` 并给出警告，此时可用 `-ModelRoot` 显式指定。开发实例的模型缓存写在 `<DevRoot>\model-cache`，不会落到线上模型目录。
- 开发实例的环境会在 `<DevRoot>\secrets\settings.env` 中写入 `BEEBOX_DEV_INSTANCE=1`、`BEEBOX_PROD_ROOT`、独立的 `BEEBOX_DATA_DIR`、`BEEBOX_LOG_DIR`、`BEEBOX_MODEL_CACHE_ROOT` 和随机 token；已有 token 会保留，但脚本会拒绝与线上 token 相同的值。
- 启动时脚本先检查共享模型目录对当前用户不可写。若可写，先执行一次（需要管理员）：

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\windows\dev-instance.ps1 -Action ProtectModels
```

  该操作先把原始 ACL 备份到 `<DevRoot>\secrets\model-acl-backup.txt`，然后把当前用户在该模型树上的权限改为只读 `RX`；`SYSTEM` 与 `Administrators` 保持全权，线上服务（LocalSystem）不受影响。撤销时用 `-Action UnprotectModels` 从备份恢复。注意：保护之后维护者本人也不能直接改模型目录，预置新模型前先 `UnprotectModels`，完成后重新 `ProtectModels`。不要用 `deny` ACE 代替这个只读 ACL：在 Windows 上拒绝 DELETE/写入会连带阻止读取共享权重，线上推理会失败。
- 隔离检查（无参数时探测配置文件、token 与写权限；路径不存在时跳过对应探测）：

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\windows\dev-instance.ps1 -Action VerifyIsolation
```

  退出码非 0 表示隔离不成立。应用启动时还会做一次显式路径守卫：`BEEBOX_DEV_INSTANCE=1` 时数据、日志、上传、派生音频与模型缓存路径落在 `BEEBOX_PROD_ROOT` 内会直接拒绝启动；开发实例上的模型下载、删除、清理接口返回 `409 MODEL_STORAGE_READ_ONLY`，只允许读模型。
- `-DryRun` 只打印计划，不建目录、不写 token、不改 ACL、不启动进程；CI 会执行它并断言没有创建任何文件。

## 真实模型测试（D3）

默认测试只用假引擎与假视频平台，不需要 GPU、模型或网络：

```powershell
uv run pytest backend/tests -q
```

需要真实模型时使用 `run-real-tests.ps1`。它会先确认开发实例健康，然后通过公开 API 暂停**线上** GPU 队列、等待当前任务结束、在开发实例环境里运行 `backend/real_tests`，并在 `finally` 中恢复线上队列——测试失败、脚本抛异常或 Ctrl-C 都会恢复。原本就处于暂停状态的队列不会被本脚本恢复。

```powershell
# 普通终端：token 从 C:\BeeBox\secrets\settings.env 读取；读取被拒绝时用 -ProdToken 传入
powershell -ExecutionPolicy Bypass -File .\deploy\windows\run-real-tests.ps1 -MediaDir D:\samples
```

参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `-MediaDir` | 必填（`-ResumeOnly`/`-DryRun` 除外） | 真实媒体目录，导出为 `BEEBOX_REAL_MEDIA_DIR` |
| `-DevRoot` / `-ProdRoot` | `C:\BeeBoxDev` / `C:\BeeBox` | 开发与线上根目录 |
| `-ProdBaseUrl` | `http://127.0.0.1:17494` | 线上 `/v1/queue` 地址 |
| `-DevBaseUrl` | `http://127.0.0.1:17495` | 开发实例地址 |
| `-ProdToken` | 空（读 `-ProdSettingsPath`） | 线上 Bearer token；不会打印 |
| `-ModelRoot` | 读生产设置 | 只读共用的生产模型根；默认从 `BEEBOX_MODEL_STORAGE_ROOT` 读取 |
| `-TestPath` | `backend/real_tests` | pytest 目标 |
| `-DrainTimeoutSeconds` | `3600` | 等待当前线上任务的上限 |
| `-PollSeconds` | `5` | 队列轮询间隔 |
| `-ResumeOnly` | 关闭 | 只恢复线上队列，用于机器重启/强杀导致 `finally` 未执行后的手动恢复 |
| `-DryRun` | 关闭 | 只打印计划，不访问线上队列、不跑测试 |

说明：

- 真实测试通过 HTTP 访问正在运行的开发实例（`BEEBOX_BASE_URL` 默认 `http://127.0.0.1:17495`，携带开发 token），使用只读共享的线上模型。开发实例上的真实测试**不会下载或删除权重**：模型缺失直接报错，测试结束也不执行删除，避免破坏线上模型。真实删除语义由假引擎测试覆盖。
- 可用 `BEEBOX_REAL_ALIGNMENT_AUDIO`、`BEEBOX_REAL_DIARIZATION_AUDIO`、`BEEBOX_REAL_MODEL_DOWNLOAD_TIMEOUT`、`BEEBOX_REAL_MODEL_TASK_TIMEOUT` 环境变量补充测试输入（本脚本会透传当前进程中的这些值）。
- 结果写入仓库 checkout 的 `backend/real_tests/results/`。
- 脚本不会在暂停前运行测试：开发实例必须先 `-Action Start` 且 `/v1/health` 正常。若线上队列在 60 分钟内仍跑不完当前任务，脚本放弃本次测试并恢复队列。

## 故障排查

1. 查看 `<root>\logs` 下最新的 `beebox-server.log` 与 WinSW 日志。`deploy.ps1` 健康检查失败时会自动打印服务状态与最近 5 个日志文件的末尾。
2. `Invoke-RestMethod http://127.0.0.1:17494/v1/health`；若端口被占用，`Get-NetTCPConnection -LocalPort 17494`。
3. 确认 `<root>\venv\Scripts\python.exe` 与 `<root>\secrets\winsw\BeeBoxService.xml` 存在。
4. 确认 `settings.env` 中 `BEEBOX_DATA_DIR`、`BEEBOX_MODEL_STORAGE_ROOT`、`BEEBOX_LOG_DIR` 指向预期目录。
5. 若 WinSW 下载失败（网络或代理），在有网络的机器下载 v2.12.0 的 `WinSW-x64.exe`，用 `-WinSwSource <path>` 安装；脚本会在使用下载文件时校验 SHA256。
6. `deploy.ps1` 提示 tag 不存在：先在开发机 `git tag <tag>` 并 `git push origin <tag>`；生产机需要能访问 `origin`，或者用 `install.ps1 -GitRemote` 换成可达的镜像。
7. `deploy.ps1` 提示应用目录有未提交改动：在 `<root>\app` 执行 `git status` 查看，提交/丢弃后再部署；不要用 `git checkout -f` 掩盖问题。
8. 健康检查一直报旧 commit：确认 `BeeBoxService.xml` 里有 `BEEBOX_GIT_COMMIT`，并确认服务确实重启了（`sc.exe query BeeBox`）；服务进程环境变量在启动时读取。
9. 部署失败后服务停在 Stopped：修复原因后用 `deploy.ps1 -Tag <上一个 tag>` 重跑；脚本会重新同步依赖、迁移并启动服务。

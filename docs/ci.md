# 持续集成

本仓库的 CI 定义在 `.github/workflows/ci.yml`，跑四个 job：`backend tests (windows-latest)`、`console build (windows-latest)`、`console smoke (windows-latest)` 与 `deploy scripts (windows-latest)`。每个 PR 都会触发，工作流名称是 `Backend CI`。把它设为默认分支的必过检查需要在 GitHub 仓库设置里完成，见文末。

## 工作流做什么

### backend tests

| 步骤 | 命令 | 说明 |
| --- | --- | --- |
| 安装 uv 与 Python | `astral-sh/setup-uv` | uv 固定为 `0.12.3`，Python 固定为 `3.12` |
| 校验锁文件 | `uv lock --check` | `pyproject.toml` 与 `uv.lock` 不一致时直接失败 |
| 安装依赖 | `uv sync --frozen` | 只按 `uv.lock` 安装，不做任何重新解析；包含默认的 `dev` 依赖组 |
| 清理下载缓存 | `uv cache clean` | CUDA wheel 有数 GB，测试前释放 runner 磁盘 |
| 跑测试 | `uv run pytest backend/tests -q --no-header -p no:cacheprovider` | 单元测试与契约测试，工作目录为仓库根 |

### deploy scripts

| 步骤 | 命令 | 说明 |
| --- | --- | --- |
| PowerShell 语法检查 | `[System.Management.Automation.Language.Parser]::ParseFile` | 解析 `deploy/windows/` 下每个 `.ps1` 与 `.psm1`，有语法错误即失败 |
| 服务配置渲染检查 | 导入 `BeeBox.Common.psm1` 并调用 `Write-BeeBoxServiceXml` | 用样例值生成 XML，断言自动启动、崩溃重启、`resetfailure`、日志目录与关键环境变量 |
| DryRun 检查（install） | `install.ps1 -Root <临时目录> -AppSource . -DryRun` | 断言退出码为 0、目标目录未被创建、输出包含安装步骤。不 clone、不下载、不注册服务、不需要管理员 |
| DryRun 检查（deploy） | `deploy.ps1 -Tag v0.0.0-dry-run -Root <临时目录> -DryRun` | 断言退出码为 0、目标目录未被创建，输出按顺序包含暂停队列、等待任务、准备/切换 tag、`uv sync --frozen`、数据库迁移、停止/启动服务、健康检查、恢复队列。不发 HTTP、不 fetch、不动服务 |
| 开发实例隔离与恢复检查 | `test-dev-scripts.ps1` | 纯路径守卫用例；再用 loopback 上的假 `/v1/queue` 服务断言：body 抛异常时仍恢复队列、操作员已暂停的队列不被改动。不接触线上端口或真实模型 |
| 开发实例 DryRun | `dev-instance.ps1 -Action Start -DevRoot <临时目录> -ProdRoot <临时目录> -DryRun` | 断言退出码为 0、没有创建目录、输出包含端口 17495 与 `BEEBOX_DEV_INSTANCE=1` |
| 真实模型脚本 DryRun | `run-real-tests.ps1 -DevRoot <临时目录> -ProdRoot <临时目录> -DryRun` | 断言退出码为 0、没有创建目录、输出包含 pause/resume 计划；不访问线上队列 |

部署脚本 job 使用 Windows PowerShell 5.1（`shell: powershell`），与目标机器默认解释器一致；脚本同时保持 PowerShell 7 兼容。

### console build

控制台是 `console/` 下的 Vite + React + TypeScript 单页应用，只调用公开 `/v1` 接口。这个 job 只装 Node 依赖并构建，构建产物 `console/dist` 作为 artifact 交给冒烟 job。

| 步骤 | 命令 | 说明 |
| --- | --- | --- |
| 安装 Node | `actions/setup-node@v4` | Node 固定为 `24`，npm 缓存以 `console/package-lock.json` 为 key |
| 安装依赖 | `npm ci`（`console/`） | 只按 `package-lock.json` 安装 |
| 类型检查与构建 | `npm run build`（`console/`） | 先 `tsc --noEmit`，再由 Vite 输出到 `console/dist` |
| 上传产物 | `actions/upload-artifact@v4` | artifact 名称 `console-dist` |

### console smoke

冒烟测试用 Playwright 驱动一个真实 BeeBox 进程，但后端装配的是假语音引擎、假视频平台与假 B 站登录客户端：不需要模型文件、GPU、真实账号或网络。`scripts/run_console_smoke.py` 负责设置临时数据目录、Bearer token、假引擎/假平台/假登录、本地模型源与 `console/dist`，Playwright 的 `webServer` 负责启动与停止它。

| 步骤 | 命令 | 说明 |
| --- | --- | --- |
| 下载构建产物 | `actions/download-artifact@v4` | 解压到 `console/dist`，与 build job 使用同一份构建 |
| 安装后端依赖 | `uv lock --check` / `uv sync --frozen` | 与 backend tests 相同的锁文件流程 |
| 安装 ffmpeg | `choco install ffmpeg` | 假引擎之前的真实解码步骤需要 ffmpeg |
| 安装浏览器 | `npx playwright install chromium` | 只装 Chromium |
| 跑冒烟测试 | `npx playwright test`（`console/`） | 覆盖登录（错误 token 提示）、任务列表与详情（含轮询更新、来源元数据、转写预览）、提交任务（上传文件、粘贴链接与可选转写参数）、取消、重试、删除与错误码的中文提示、健康与队列页、模型页（状态/校验/空间、下载进度、取消/重试/删除/卸载）、队列暂停/恢复，以及凭证页（B 站二维码显示与轮询、取消、重新扫码、登录状态与 `/v1/health` 一致、失效提示、YouTube cookie 上传与仅元数据显示） |

冒烟测试里的任务都通过控制台界面提交，走真实的 `POST /v1/files` 与 `POST /v1/jobs`：上传用合成 WAV，链接用假平台登记的 Bilibili 视频。取消用假引擎的 `delay_seconds` 留出可取消窗口；重试用假平台里 `login_required` 的视频，任务以可重试的 `VIDEO_LOGIN_REQUIRED` 失败，点击重试后重新排队；删除只在任务到达终态后出现。错误提示断言的是 `error_code` 的中文含义，不会出现英文堆栈。

`scripts/run_console_smoke.py` 在临时数据目录里写入一个本地模型源 `model-source/whisper-base/`，并把它设为 `BEEBOX_MODEL_SOURCE_DIR`（见 #41）：模型页的下载因此是精确字节的本地复制，不访问网络。为了让复制过程在冒烟测试里可观察，启动脚本还给 `BEEBOX_MODEL_SOURCE_COPY_DELAY_MS` 设置每 1 MiB 的延时，让进度更新与取消窗口稳定出现；该变量在正常运行时不存在，本地复制没有任何延时。

凭证页同样不碰网络：启动脚本设置 `BEEBOX_BILIBILI_LOGIN=fake` 与 `BEEBOX_BILIBILI_LOGIN_FAKE`（`pending → scanned → confirmed` 的脚本状态和生成的 PNG 二维码），用 `BEEBOX_BILIBILI_REFRESH_FAKE={"outcome":"expired"}` 让 #47 的启动刷新保持失效状态，并在临时 secrets 目录预置一份过期的 `bilibili-cookies.txt`。因此冒烟测试先看到 `expired` 警告，取消后凭证不变，重新扫码确认后 `/v1/health` 与页面都变为 `valid`；测试同时把预置的与假登录的 cookie 值当作哨兵，断言它们不出现在页面文本、API 响应或浏览器 console 中。YouTube 侧上传一份 Netscape cookies.txt，页面只显示配置状态、大小、cookie 数量与更新时间。

冒烟 job 通过 `BEEBOX_PYTHON` 指向 `uv sync` 生成的 `.venv\Scripts\python.exe`。测试失败时会上传 `console/playwright-report` 与 `console/test-results`，便于查看 trace。默认端口 `17497`，可用 `BEEBOX_CONSOLE_SMOKE_PORT` 覆盖。

`BEEBOX_DATA_DIR` 指向 runner 的临时目录，测试不会往仓库里写数据。真实模型测试在 `backend/real_tests/`，需要模型文件与网络，不在默认 CI 中运行；它们只能通过 `deploy/windows/run-real-tests.ps1` 在开发实例上运行（该脚本先暂停线上队列并在异常时恢复）。

CI 安装的是 Windows 的 CUDA 12.6 版 torch/torchaudio，与生产环境同一份锁；安装 CUDA wheel 不需要 GPU，测试也不会做推理，因此 `windows-latest` 可以跑。

## 依赖与锁

- Python 唯一的依赖入口是根目录的 `pyproject.toml`，唯一的锁是 `uv.lock`。运行时依赖在 `[project].dependencies`，测试依赖在 `[dependency-groups].dev`。
- 控制台唯一的依赖入口是 `console/package.json`，唯一的锁是 `console/package-lock.json`，CI 固定用 `npm ci`。改控制台依赖后要同时提交这两个文件。
- Windows 的 `torch` / `torchaudio` 通过 `[tool.uv.sources]` 的平台标记走 PyTorch 的 `cu126` 索引；其他平台仍从 PyPI 解析。所有平台共用同一份 `uv.lock`。
- `requires-python` 是 `>=3.12,<3.13`，与本地验证过的运行时一致。系统 Python 3.11/3.13 不要用。

修改依赖后必须同时提交 `pyproject.toml` 与 `uv.lock`：

```powershell
uv lock
uv sync
```

## 本地复现

Windows PowerShell，在仓库根目录：

```powershell
uv lock --check
uv sync --frozen
$env:BEEBOX_DATA_DIR = "$env:TEMP\beebox-data"
uv run pytest backend/tests -q
```

Git Bash：

```bash
uv lock --check
uv sync --frozen
BEEBOX_DATA_DIR="$TEMP/beebox-data" uv run pytest backend/tests -q --no-header -p no:cacheprovider
```

控制台构建与冒烟测试（本地需要 Node 24 与 Playwright Chromium）：

```bash
cd console
npm ci
npm run build
npx playwright install chromium

# 指向测试用的 Python（本机系统 3.11 不要用）
BEEBOX_PYTHON=/path/to/venv/Scripts/python.exe npx playwright test
```

`npx playwright test` 会通过 `webServer` 启动 `scripts/run_console_smoke.py --port 17497`；后端数据目录是系统临时目录，退出后可以直接删除。冒烟测试不访问真实模型或网络，也不会碰 `data/`。

部署脚本检查（不需要管理员，也不会改动机器）：

```powershell
# 语法检查：任何语法错误都会打印文件与行号
$files = Get-ChildItem deploy/windows -Recurse -File -Include *.ps1,*.psm1
foreach ($file in $files) {
  $tokens = $null; $errors = $null
  [void][System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$errors)
  if ($errors.Count) { $errors | ForEach-Object { Write-Error "$($file.Name):$($_.Extent.StartLineNumber): $($_.Message)" } }
}

# DryRun：只打印步骤
$root = Join-Path $env:TEMP 'beebox-dryrun'
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\install.ps1 -Root $root -AppSource . -DryRun
Test-Path $root   # 必须为 False

# 部署脚本 DryRun：只打印步骤，不需要已安装的根目录
$deployRoot = Join-Path $env:TEMP 'beebox-deploy-dryrun'
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\deploy.ps1 -Tag v0.0.0-dry-run -Root $deployRoot -DryRun
Test-Path $deployRoot   # 必须为 False
```

首次同步会从 PyTorch 索引下载数 GB 的 CUDA wheel，需要网络；之后 `uv sync --frozen` 会复用 `uv` 缓存。如果本机需要代理，先设置 `HTTPS_PROXY` / `HTTP_PROXY`，`uv` 会读取这两个变量。

真实模型测试（需要预下载模型，不属于默认测试）：先启动开发实例（`deploy/windows/dev-instance.ps1 -Action Start`），再通过编排脚本运行；它会暂停并恢复线上队列，异常退出时也会恢复：

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\windows\run-real-tests.ps1 -MediaDir <真实媒体目录>
```

不要直接对线上数据目录或共享模型目录执行 `BEEBOX_RUN_REAL_MODELS=1 pytest backend/real_tests`。

## 设为必过检查

工作流提交后，在 GitHub 仓库的 **Settings → Branches → Branch protection rules** 中为默认分支（以及集成分支，如使用）启用 *Require status checks to pass before merging*，勾选 `Backend CI / backend tests (windows-latest)`、`Backend CI / console build (windows-latest)` 与 `Backend CI / console smoke (windows-latest)`。这是仓库设置，无法由工作流文件本身开启。

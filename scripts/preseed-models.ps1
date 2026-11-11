<#
.SYNOPSIS
    预置 Gate 2 所需的本地模型到 ./models（BeeBox 布局）。

.DESCRIPTION
    从 Gate 1 spike 目录复制以下模型：
      - Qwen3-ASR-1.7B-hf              -> models/qwen3-asr-1.7b
      - Qwen3-ForcedAligner-0.6B-hf    -> models/qwen3-forced-aligner-0.6b
      - pyannote-community-1           -> models/pyannote-community-1

    同时写入 BeeBox 需要的 model.json 标记和根目录 preseed.json 清单。
    不会启动容器，也不会修改 ./data。

.PARAMETER SourceDir
    Gate 1 模型目录，默认 ..\..\_spike_qwen3asr_20261031\models。

.PARAMETER TargetDir
    目标目录，默认仓库根目录下的 models。

.PARAMETER Force
    覆盖已存在的模型目录与标记。

.PARAMETER DryRun
    只打印计划，不复制文件。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\preseed-models.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\preseed-models.ps1 -Force

.NOTES
    需要 Windows 自带的 robocopy。脚本可在 Windows PowerShell 5.1+ 运行。
#>

[CmdletBinding()]
param(
    [string]$SourceDir,
    [string]$TargetDir,
    [switch]$Force,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

if (-not $PSScriptRoot) {
    $PSScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
}
if (-not $SourceDir) {
    $SourceDir = Join-Path $PSScriptRoot '..\..\_spike_qwen3asr_20261031\models'
}
if (-not $TargetDir) {
    $TargetDir = Join-Path $PSScriptRoot '..\models'
}

function Write-Step {
    param([string]$Message)
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Message)
    Write-Host "    $Message" -ForegroundColor Green
}

function Write-Skip {
    param([string]$Message)
    Write-Host "    $Message" -ForegroundColor Yellow
}

function Get-DirectorySizeBytes {
    param([string]$Path)
    $sum = (Get-ChildItem -LiteralPath $Path -Recurse -File -ErrorAction SilentlyContinue |
        Measure-Object -Property Length -Sum).Sum
    if ($null -eq $sum) { return 0 }
    return [double]$sum
}

function Write-JsonNoBom {
    param(
        [string]$Path,
        [object]$Value
    )
    $json = $Value | ConvertTo-Json -Depth 6
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($Path, $json + [Environment]::NewLine, $utf8)
}

function Assert-ModelFiles {
    param(
        [string]$Path,
        [string[]]$RequiredFiles
    )
    foreach ($relative in $RequiredFiles) {
        $candidate = Join-Path $Path $relative
        if (-not (Test-Path -LiteralPath $candidate)) {
            throw "模型目录缺少必要文件: $candidate"
        }
    }
}

function Copy-Model {
    param(
        [string]$Name,
        [string]$SourceName,
        [string]$RepoId,
        [string]$Engine,
        [string]$ContainerPath,
        [string[]]$RequiredFiles
    )

    $source = Join-Path $SourceDir $SourceName
    $destination = Join-Path (Join-Path $TargetDir 'models') $Name
    $markerPath = Join-Path $destination 'model.json'

    Write-Step "$Name"
    if (-not (Test-Path -LiteralPath $source)) {
        throw "找不到源模型目录: $source"
    }
    Assert-ModelFiles -Path $source -RequiredFiles $RequiredFiles

    $targetExists = Test-Path -LiteralPath $destination
    $markerExists = Test-Path -LiteralPath $markerPath
    if ($targetExists -and $markerExists -and -not $Force) {
        Write-Skip "已存在且已有 marker，跳过（用 -Force 覆盖）: $destination"
        return
    }

    if ($DryRun) {
        Write-Host "    [DryRun] robocopy `"$source`" `"$destination`" /E /XD .cache" -ForegroundColor DarkGray
        return
    }

    New-Item -ItemType Directory -Force -Path $destination | Out-Null
    $robocopyArgs = @(
        $source,
        $destination,
        '/E',
        '/XD', '.cache',
        '/NFL', '/NDL', '/NJH', '/NJS', '/NP'
    )
    if ($Force) { $robocopyArgs += '/IS' }

    & robocopy @robocopyArgs | Out-Null
    if ($LASTEXITCODE -ge 8) {
        throw "robocopy 失败（exit $LASTEXITCODE）: $source -> $destination"
    }

    $sizeBytes = Get-DirectorySizeBytes -Path $destination
    $now = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
    $marker = [ordered]@{
        model_name      = $Name
        repo_id         = $RepoId
        source          = 'modelscope'
        engine          = $Engine
        snapshot_path   = $ContainerPath
        installed_at    = $now
        last_verified_at = $now
        size_on_disk_mb = [math]::Round($sizeBytes / 1MB, 6)
        preseeded_by    = 'scripts/preseed-models.ps1'
    }
    Write-JsonNoBom -Path $markerPath -Value $marker
    Write-Ok ("{0:N2} GB -> {1}" -f ($sizeBytes / 1GB), $destination)
}

$SourceDir = [IO.Path]::GetFullPath($SourceDir)
$TargetDir = [IO.Path]::GetFullPath($TargetDir)

Write-Step '检查路径'
if (-not (Test-Path -LiteralPath $SourceDir)) {
    throw "源目录不存在: $SourceDir。请先运行 Gate 1 spike，或用模型页首次下载。"
}
Write-Ok "source: $SourceDir"
Write-Ok "target: $TargetDir"

$models = @(
    @{
        Name          = 'qwen3-asr-1.7b'
        SourceName    = 'Qwen3-ASR-1.7B-hf'
        RepoId        = 'Qwen/Qwen3-ASR-1.7B-hf'
        Engine        = 'transformers_speech_lm'
        ContainerPath = '/models-root/models/qwen3-asr-1.7b'
        RequiredFiles = @('config.json', 'model.safetensors')
    },
    @{
        Name          = 'qwen3-forced-aligner-0.6b'
        SourceName    = 'Qwen3-ForcedAligner-0.6B-hf'
        RepoId        = 'Qwen/Qwen3-ForcedAligner-0.6B-hf'
        Engine        = 'transformers_token_classification'
        ContainerPath = '/models-root/models/qwen3-forced-aligner-0.6b'
        RequiredFiles = @('config.json', 'model.safetensors')
    },
    @{
        Name          = 'pyannote-community-1'
        SourceName    = 'pyannote-community-1'
        RepoId        = 'pyannote/speaker-diarization-community-1'
        Engine        = 'pyannote.audio.pipelines.SpeakerDiarization'
        ContainerPath = '/models-root/models/pyannote-community-1'
        RequiredFiles = @('config.yaml', 'segmentation/pytorch_model.bin', 'embedding/pytorch_model.bin', 'plda/plda.npz')
    }
)

foreach ($model in $models) {
    Copy-Model `
        -Name $model.Name `
        -SourceName $model.SourceName `
        -RepoId $model.RepoId `
        -Engine $model.Engine `
        -ContainerPath $model.ContainerPath `
        -RequiredFiles $model.RequiredFiles
}

if (-not $DryRun) {
    $manifest = [ordered]@{
        version    = 1
        created_at = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
        source_dir = $SourceDir
        target_dir = $TargetDir
        models     = @($models | ForEach-Object {
            [ordered]@{
                model_name = $_.Name
                repo_id    = $_.RepoId
                path       = (Join-Path (Join-Path $TargetDir 'models') $_.Name)
            }
        })
    }
    Write-JsonNoBom -Path (Join-Path $TargetDir 'preseed.json') -Value $manifest
    Write-Step '完成'
    Write-Ok "模型已写入: $(Join-Path $TargetDir 'models')"
    Write-Ok "清单: $(Join-Path $TargetDir 'preseed.json')"
    Write-Host ""
    Write-Host "下一步：" -ForegroundColor Cyan
    Write-Host "  1. 设置 `$env:BEEBOX_MODEL_STORAGE_ROOT 指向该模型目录的父目录"
    Write-Host "  2. 启动服务：python -m backend.server --host 127.0.0.1 --port 17494"
    Write-Host "  3. 请求 http://127.0.0.1:17494/health 与 /models/status，确认模型就绪"
    Write-Host "  4. 提交一段真实录音做端到端验收"
}

#Requires -Version 5.1
<#
.SYNOPSIS
    Run the opt-in real-model acceptance suite against the dev instance while
    the production GPU queue is paused, and always resume it afterwards.

.DESCRIPTION
    The default test suite uses fake engines and a fake video platform and
    never needs a GPU. This script is for the occasional real-model run
    (ADR-0010): it pauses the production queue through the public
    /v1/queue/pause API, waits until the job that is already running finishes,
    runs backend/real_tests over HTTP against the running dev instance (its
    URL, its token, the production models directory read-only), and resumes
    the production queue in a finally block -- also when the tests fail or the
    script is interrupted.

    A queue that an operator had already paused is left paused: the script only
    undoes the pause it issued itself. The queue length is not drained; queued
    submissions stay queued and continue after the resume.

    Model weights are never downloaded, deleted or modified by this run: the
    dev instance shares production models read-only and the real suite asserts
    that precondition.

.PARAMETER DevRoot
    Dev-owned root created by dev-instance.ps1. Default C:\BeeBoxDev.

.PARAMETER ProdRoot
    Production install root. Default C:\BeeBox.

.PARAMETER ProdBaseUrl
    Production base URL used for /v1/queue. Default http://127.0.0.1:17494.

.PARAMETER DevBaseUrl
    Dev instance base URL checked before the production queue is paused.
    Default http://127.0.0.1:17495.

.PARAMETER Checkout
    Code checkout used for pytest. Default the repository containing this script.

.PARAMETER PythonPath
    Interpreter for pytest. Default <Checkout>\.venv\Scripts\python.exe.

.PARAMETER ModelRoot
    Production model storage root shared read-only. Default read from the
    production settings file, falling back to <ProdRoot>\models.

.PARAMETER MediaDir
    Directory with real media files. Required unless -ResumeOnly or -DryRun;
    exported as BEEBOX_REAL_MEDIA_DIR.

.PARAMETER ProdToken
    Production Bearer token. When omitted it is read from
    <ProdRoot>\secrets\settings.env, which usually requires an elevated prompt
    because the installer restricts that directory to SYSTEM/Administrators.
    The token is never printed.

.PARAMETER ProdSettingsPath
    Override the production settings file path. Default
    <ProdRoot>\secrets\settings.env.

.PARAMETER TestPath
    Pytest target. Default backend/real_tests.

.PARAMETER DrainTimeoutSeconds
    How long to wait for the running production job. Default 3600.

.PARAMETER PollSeconds
    Queue polling interval. Default 5.

.PARAMETER HealthTimeoutSeconds
    How long to wait for the dev /v1/health check. Default 30.

.PARAMETER ResumeOnly
    Only resume the production queue. Use this after an abnormal termination
    (blue screen, taskkill) where the finally block could not run.

.PARAMETER DryRun
    Print the plan without calling the production queue or running tests.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\deploy\windows\run-real-tests.ps1 -MediaDir D:\samples

.EXAMPLE
    # Elevated prompt, because the production settings file is protected
    powershell -ExecutionPolicy Bypass -File .\deploy\windows\run-real-tests.ps1 -MediaDir D:\samples

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\deploy\windows\run-real-tests.ps1 -ResumeOnly

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\deploy\windows\run-real-tests.ps1 -MediaDir D:\samples -DryRun
#>
[CmdletBinding()]
param(
    [string]$DevRoot = 'C:\BeeBoxDev',
    [string]$ProdRoot = 'C:\BeeBox',
    [string]$ProdBaseUrl = 'http://127.0.0.1:17494',
    [string]$DevBaseUrl = 'http://127.0.0.1:17495',
    [string]$Checkout,
    [string]$PythonPath,
    [string]$ModelRoot,
    [string]$MediaDir,
    [string]$ProdToken,
    [string]$ProdSettingsPath,
    [string]$TestPath = 'backend/real_tests',
    [int]$DrainTimeoutSeconds = 3600,
    [int]$PollSeconds = 5,
    [int]$HealthTimeoutSeconds = 30,
    [switch]$ResumeOnly,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'BeeBox.DevInstance.psm1') -Force

if (-not $Checkout) {
    $Checkout = Resolve-FullPath (Join-Path $PSScriptRoot '..\..')
}
$layout = Get-BeeBoxDevLayout -DevRoot $DevRoot -ProdRoot $ProdRoot -Checkout $Checkout -PythonPath $PythonPath -ModelRoot $ModelRoot
if (-not $ProdSettingsPath) {
    $ProdSettingsPath = $layout.ProdSettingsPath
}

function Resolve-ProductionToken {
    if ($ProdToken) {
        return $ProdToken
    }
    $token = $null
    try {
        $token = Get-BeeBoxEnvValue -Path $ProdSettingsPath -Name 'BEEBOX_API_TOKEN'
    }
    catch {
        $token = $null
    }
    if ([string]::IsNullOrWhiteSpace($token)) {
        throw "No production Bearer token: pass -ProdToken or run this script from an elevated prompt so it can read '$ProdSettingsPath' (the installer restricts that directory to SYSTEM/Administrators)."
    }
    return $token
}

Write-Step 'BeeBox real-model run'
Write-Info "Checkout:   $($layout.Checkout)"
Write-Info "ProdRoot:   $($layout.ProdRoot)"
Write-Info "DevRoot:    $($layout.DevRoot)"
Write-Info "Production: $($ProdBaseUrl.TrimEnd('/'))/v1/queue"
Write-Info "Dev:        $($DevBaseUrl.TrimEnd('/'))/v1/health"
Write-Info "Models:     $($layout.ModelRoot) (shared read-only)"
if ($DryRun) {
    Write-Warn 'DRY RUN: the production queue will not be contacted and no test will run.'
}

Assert-BeeBoxDevIsolation -DevRoot $layout.DevRoot -ProdRoot $layout.ProdRoot -DataDir $layout.DataDir -ModelRoot $layout.ModelRoot -ProdModelRoot $layout.ProdModelsDir -ModelCacheRoot $layout.ModelCacheRoot -Port $layout.Port -ProdPort (Get-BeeBoxProductionPort)
Write-Ok 'Dev and production paths do not overlap.'

if ($ResumeOnly) {
    $tokenForResume = Resolve-ProductionToken
    if ($DryRun) {
        Write-DryRun "POST $($ProdBaseUrl.TrimEnd('/'))/v1/queue/resume with the production token and the idempotent resume route."
        return
    }
    Write-Step 'Resuming the production queue'
    $status = Invoke-BeeBoxQueueRequest -BaseUrl $ProdBaseUrl -Token $tokenForResume -Action 'resume'
    if ([bool]$status.paused) {
        throw 'The production queue still reports paused after /v1/queue/resume.'
    }
    Write-Ok 'Production queue resumed.'
    return
}

if (-not $DryRun) {
    if ([string]::IsNullOrWhiteSpace($MediaDir)) {
        throw '-MediaDir is required for a real-model run; it must contain real media files.'
    }
    $resolvedMedia = Resolve-FullPath $MediaDir
    if (-not (Test-Path -LiteralPath $resolvedMedia -PathType Container)) {
        throw "Media directory '$resolvedMedia' does not exist."
    }
}

if ($DryRun) {
    Write-DryRun "Read the production Bearer token from -ProdToken or '$ProdSettingsPath'."
    Write-DryRun "GET $($DevBaseUrl.TrimEnd('/'))/v1/health and fail before pausing if the dev instance is not running."
    Write-DryRun "Read '$($layout.SettingsPath)' for the dev data directory, dev token and read-only model root."
    Write-DryRun "GET $($ProdBaseUrl.TrimEnd('/'))/v1/queue; if it is not paused, POST /v1/queue/pause (the operator's pause state is preserved)."
    Write-DryRun "Poll GET /v1/queue until current_job_id is null (timeout ${DrainTimeoutSeconds}s, interval ${PollSeconds}s)."
    Write-DryRun "Run '$($layout.PythonPath)' -m pytest $TestPath -q --no-header -p no:cacheprovider from '$($layout.Checkout)' with BEEBOX_RUN_REAL_MODELS=1, BEEBOX_DEV_INSTANCE=1, BEEBOX_REAL_MEDIA_DIR=<media>, the dev data directory and the dev token."
    Write-DryRun 'Finally: POST /v1/queue/resume if (and only if) this script paused the queue. This also runs when pytest fails or the script is interrupted.'
    Write-DryRun "Manual recovery after a hard kill: run this script with -ResumeOnly. No token is printed."
    return
}

Write-Step 'Checking the dev instance'
try {
    $health = Wait-BeeBoxDevHealth -Url "$($DevBaseUrl.TrimEnd('/'))/v1/health" -TimeoutSeconds $HealthTimeoutSeconds
}
catch {
    throw "The dev instance is not healthy at $($DevBaseUrl.TrimEnd('/'))/v1/health. Start it with dev-instance.ps1 -Action Start first. Last error: $($_.Exception.Message)"
}
Write-Ok "Dev health: $($health | ConvertTo-Json -Compress -Depth 6)"

$devToken = Get-BeeBoxDevToken -SettingsPath $layout.SettingsPath
try {
    $devQueue = Invoke-BeeBoxQueueRequest -BaseUrl $DevBaseUrl -Token $devToken -Action 'status'
}
catch {
    throw "The dev instance rejected its own token from '$($layout.SettingsPath)'. Recreate it with dev-instance.ps1 -Action Start. Last error: $($_.Exception.Message)"
}
Write-Ok "Dev queue reachable (paused=$($devQueue.paused), length=$($devQueue.length)); the production queue is untouched so far."

$productionToken = Resolve-ProductionToken

Write-Step 'Checking the production queue API'
$initialQueue = Invoke-BeeBoxQueueRequest -BaseUrl $ProdBaseUrl -Token $productionToken -Action 'status'
Write-Ok "Production queue: paused=$($initialQueue.paused), running=$($initialQueue.current_job_id), queued=$($initialQueue.length)."

$environment = New-BeeBoxRealTestEnvironment -Layout $layout -MediaDir $resolvedMedia -DevToken $devToken
Write-Info "Real tests: $TestPath"
Write-Info "Dev data:   $($layout.DataDir)"
Write-Info "Media:      $resolvedMedia"

$body = {
    param($context)
    Invoke-BeeBoxRealModelSuite -Layout $context.Layout -TestPath $context.TestPath -Environment $context.Environment
}
$context = [pscustomobject]@{
    Layout      = $layout
    TestPath    = $TestPath
    Environment = $environment
}

Write-Step 'Pausing the production queue'
try {
    Invoke-BeeBoxQueueExclusive -BaseUrl $ProdBaseUrl -Token $productionToken -Body $body -BodyArgument $context -DrainTimeoutSeconds $DrainTimeoutSeconds -PollSeconds $PollSeconds
}
catch {
    Write-Warn "The real-model run did not finish cleanly: $($_.Exception.Message)"
    Write-Warn 'Check whether the production queue is resumed (GET /v1/queue). If a hard kill prevented the finally block, run this script with -ResumeOnly.'
    throw
}

Write-Step 'Real-model run finished'
Write-Ok 'The production queue was resumed. Results are under backend/real_tests/results in the checkout.'

#Requires -Version 5.1
<#
.SYNOPSIS
    Hermetic smoke test for the dev-instance scripts (ticket #55).

.DESCRIPTION
    Proves, without a GPU, real models or the production service:

    * the path guards reject dev layouts that overlap production;
    * Invoke-BeeBoxQueueExclusive pauses the queue, waits for the running job,
      resumes in a finally block when the body throws, and leaves an
      operator-paused queue untouched.

    A fake /v1/queue server runs on loopback through TcpListener; nothing on
    port 17494 (or any real BeeBox process) is contacted. Run from the
    repository root:

        powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\test-dev-scripts.ps1

    Exit code 0 means every check passed.
#>
[CmdletBinding()]
param(
    [string]$WorkDirectory,
    [int]$ReadyTimeoutSeconds = 30
)

$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'BeeBox.DevInstance.psm1') -Force

if (-not $WorkDirectory) {
    $WorkDirectory = Join-Path ([System.IO.Path]::GetTempPath()) ('beebox-dev-scripts-' + [Guid]::NewGuid().ToString('N'))
}
New-Item -ItemType Directory -Force -Path $WorkDirectory | Out-Null

$script:Failures = New-Object System.Collections.Generic.List[string]

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if ($Condition) {
        Write-Ok $Message
    }
    else {
        $script:Failures.Add($Message)
        Write-Warn "FAILED: $Message"
    }
}

function Assert-HelperThrows {
    param([scriptblock]$Action, [string]$Message)
    try {
        & $Action
        Assert-True -Condition $false -Message $Message
    }
    catch {
        Assert-True -Condition $true -Message $Message
    }
}

# ---------------------------------------------------------------------------
# Path guards
# ---------------------------------------------------------------------------
Write-Step 'Path guards'
$prod = Join-Path $WorkDirectory 'BeeBox'
$dev = Join-Path $WorkDirectory 'BeeBoxDev'
$valid = @{
    DevRoot        = $dev
    ProdRoot       = $prod
    DataDir        = (Join-Path $dev 'data')
    ModelRoot      = (Join-Path $prod 'models')
    ModelCacheRoot = (Join-Path $dev 'model-cache')
    Port           = 17495
    ProdPort       = 17494
}
Assert-BeeBoxDevIsolation @valid
Assert-True -Condition $true -Message 'A dev root outside production with shared models is accepted.'

$customModels = Join-Path $prod 'weights'
$customCase = @{
    DevRoot        = $dev
    ProdRoot       = $prod
    DataDir        = (Join-Path $dev 'data')
    ModelRoot      = $customModels
    # The production model directory is <ProdRoot>\models here, so a dev model
    # root pointing at <ProdRoot>\weights must be refused. Passing the dev
    # model root as ProdModelRoot would compare the path with itself and
    # accept any production subdirectory.
    ProdModelRoot  = (Join-Path $prod 'models')
    ModelCacheRoot = (Join-Path $dev 'model-cache')
    Port           = 17495
    ProdPort       = 17494
}
Assert-HelperThrows -Action { Assert-BeeBoxDevIsolation @customCase } -Message 'Rejected: dev model root is inside production but is not the configured production model directory.'

# A production whose settings point at a custom model root is legitimate: the
# guard compares the dev model root with the actual configured directory.
$configuredCase = @{
    DevRoot        = $dev
    ProdRoot       = $prod
    DataDir        = (Join-Path $dev 'data')
    ModelRoot      = $customModels
    ProdModelRoot  = $customModels
    ModelCacheRoot = (Join-Path $dev 'model-cache')
    Port           = 17495
    ProdPort       = 17494
}
Assert-BeeBoxDevIsolation @configuredCase
Assert-True -Condition $true -Message 'The configured production model directory is accepted read-only.'

$overlapCases = @(
    @{
        Name = 'dev data inside production'
        Args = @{ DevRoot = $dev; ProdRoot = $prod; DataDir = (Join-Path $prod 'data'); ModelRoot = (Join-Path $prod 'models'); ModelCacheRoot = (Join-Path $dev 'model-cache'); Port = 17495; ProdPort = 17494 }
    },
    @{
        Name = 'dev root inside production'
        Args = @{ DevRoot = (Join-Path $prod 'dev'); ProdRoot = $prod; DataDir = (Join-Path $prod 'dev\data'); ModelRoot = (Join-Path $prod 'models'); ModelCacheRoot = (Join-Path $prod 'dev\cache'); Port = 17495; ProdPort = 17494 }
    },
    @{
        Name = 'model root inside production data'
        Args = @{ DevRoot = $dev; ProdRoot = $prod; DataDir = (Join-Path $dev 'data'); ModelRoot = (Join-Path $prod 'data\models'); ModelCacheRoot = (Join-Path $dev 'model-cache'); Port = 17495; ProdPort = 17494 }
    },
    @{
        Name = 'model root inside production secrets'
        Args = @{ DevRoot = $dev; ProdRoot = $prod; DataDir = (Join-Path $dev 'data'); ModelRoot = (Join-Path $prod 'secrets'); ModelCacheRoot = (Join-Path $dev 'model-cache'); Port = 17495; ProdPort = 17494 }
    },
    @{
        Name = 'model root inside production but not the configured one'
        Args = @{ DevRoot = $dev; ProdRoot = $prod; DataDir = (Join-Path $dev 'data'); ModelRoot = (Join-Path $prod 'weights'); ProdModelRoot = (Join-Path $prod 'models'); ModelCacheRoot = (Join-Path $dev 'model-cache'); Port = 17495; ProdPort = 17494 }
    },
    @{
        Name = 'model cache inside production'
        Args = @{ DevRoot = $dev; ProdRoot = $prod; DataDir = (Join-Path $dev 'data'); ModelRoot = (Join-Path $prod 'models'); ModelCacheRoot = (Join-Path $prod 'models\cache'); Port = 17495; ProdPort = 17494 }
    },
    @{
        Name = 'dev port equals production port'
        Args = @{ DevRoot = $dev; ProdRoot = $prod; DataDir = (Join-Path $dev 'data'); ModelRoot = (Join-Path $prod 'models'); ModelCacheRoot = (Join-Path $dev 'model-cache'); Port = 17494; ProdPort = 17494 }
    }
)
foreach ($case in $overlapCases) {
    $parameters = $case.Args
    $name = [string]$case.Name
    Assert-HelperThrows -Action { Assert-BeeBoxDevIsolation @parameters } -Message "Rejected: $name."
}
Assert-True -Condition (-not (Test-BeeBoxPathWithin -Path 'C:\BeeBoxDev' -Parent 'C:\BeeBox')) -Message 'C:\BeeBoxDev is a sibling of C:\BeeBox, not a child.'

# ---------------------------------------------------------------------------
# Fake /v1/queue server (loopback, TcpListener, no URL ACL needed)
# ---------------------------------------------------------------------------
$serverScriptPath = Join-Path $WorkDirectory 'fake-queue-server.ps1'
$serverSource = @'
param(
    [int]$Port,
    [int]$InitialPaused,
    [string]$LogPath,
    [string]$ReadyPath
)

$listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $Port)
$listener.Start()
Set-Content -LiteralPath $ReadyPath -Value 'ready' -Encoding ASCII
$paused = ($InitialPaused -ne 0)
$polls = 0
$current = 'job-1'
if ($InitialPaused -ne 0) {
    $current = $null
}
try {
    while ($true) {
        $client = $listener.AcceptTcpClient()
        try {
            $stream = $client.GetStream()
            $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::ASCII)
            $requestLine = $reader.ReadLine()
            while ($true) {
                $line = $reader.ReadLine()
                if ($null -eq $line -or $line -eq '') {
                    break
                }
            }
            if ([string]::IsNullOrWhiteSpace($requestLine)) {
                continue
            }
            $parts = $requestLine -split ' '
            $method = $parts[0]
            $path = $parts[1]
            Add-Content -LiteralPath $LogPath -Value "$method $path"

            if ($method -eq 'GET' -and $path -eq '/v1/queue') {
                $polls = $polls + 1
                if ($paused -and $polls -ge 2) {
                    $current = $null
                }
            }
            elseif ($method -eq 'POST' -and $path -eq '/v1/queue/pause') {
                $paused = $true
            }
            elseif ($method -eq 'POST' -and $path -eq '/v1/queue/resume') {
                $paused = $false
                $current = $null
            }

            $pausedValue = 'false'
            if ($paused) {
                $pausedValue = 'true'
            }
            $currentValue = 'null'
            $kindValue = 'null'
            if ($null -ne $current) {
                $currentValue = '"' + $current + '"'
                $kindValue = '"transcription"'
            }
            $json = '{"paused":' + $pausedValue + ',"length":0,"current_job_id":' + $currentValue + ',"current_job_kind":' + $kindValue + ',"queued":[]}'
            $body = [System.Text.Encoding]::UTF8.GetBytes($json)
            $header = "HTTP/1.1 200 OK`r`nContent-Type: application/json`r`nContent-Length: $($body.Length)`r`nConnection: close`r`n`r`n"
            $headerBytes = [System.Text.Encoding]::ASCII.GetBytes($header)
            $stream.Write($headerBytes, 0, $headerBytes.Length)
            $stream.Write($body, 0, $body.Length)
            $stream.Flush()
        }
        finally {
            $client.Close()
        }
    }
}
finally {
    $listener.Stop()
}
'@
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($serverScriptPath, $serverSource, $utf8NoBom)

function Start-FakeQueueServer {
    param(
        [int]$Port,
        [bool]$InitialPaused,
        [string]$LogPath,
        [string]$ReadyPath
    )
    $arguments = @(
        '-NoProfile',
        '-ExecutionPolicy', 'Bypass',
        '-File', $serverScriptPath,
        '-Port', [string]$Port,
        '-InitialPaused', $(if ($InitialPaused) { 1 } else { 0 }),
        '-LogPath', $LogPath,
        '-ReadyPath', $ReadyPath
    )
    $errorLog = Join-Path (Split-Path -Parent $ReadyPath) 'fake-queue.err.log'
    $outputLog = Join-Path (Split-Path -Parent $ReadyPath) 'fake-queue.out.log'
    $process = Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments -PassThru -WindowStyle Hidden -RedirectStandardError $errorLog -RedirectStandardOutput $outputLog
    $deadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path -LiteralPath $ReadyPath -PathType Leaf) {
            return $process
        }
        if ($process.HasExited) {
            $details = ''
            if (Test-Path -LiteralPath $errorLog) {
                $details = (Get-Content -LiteralPath $errorLog -Raw)
            }
            throw "The fake queue server exited with code $($process.ExitCode): $details"
        }
        Start-Sleep -Milliseconds 200
    }
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    throw "The fake queue server was not ready within $ReadyTimeoutSeconds seconds."
}

function Stop-FakeQueueServer {
    param($Process)
    if ($null -eq $Process) {
        return
    }
    Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
    $Process.WaitForExit(5000) | Out-Null
}

function New-FakeQueuePort {
    return (Get-Random -Minimum 20000 -Maximum 40000)
}

# ---------------------------------------------------------------------------
# Resume after a failing body
# ---------------------------------------------------------------------------
Write-Step 'Queue pause/resume around a failing body'
$port = New-FakeQueuePort
$callsLog = Join-Path $WorkDirectory 'calls-failure.log'
$ready = Join-Path $WorkDirectory 'ready-failure.txt'
$server = $null
try {
    $server = Start-FakeQueueServer -Port $port -InitialPaused $false -LogPath $callsLog -ReadyPath $ready
    $threw = $false
    $message = ''
    try {
        Invoke-BeeBoxQueueExclusive -BaseUrl "http://127.0.0.1:$port" -Token 'test-token' -PollSeconds 1 -DrainTimeoutSeconds 30 -Body { param($ctx) throw 'simulated real-model failure' }
    }
    catch {
        $threw = $true
        $message = $_.Exception.Message
    }
    Assert-True -Condition $threw -Message 'The body failure surfaced to the caller.'
    Assert-True -Condition ($message -match 'simulated real-model failure') -Message 'The caller sees the original body error, not a queue error.'
    $calls = @(Get-Content -LiteralPath $callsLog -ErrorAction SilentlyContinue)
    Assert-True -Condition (@($calls | Where-Object { $_ -eq 'POST /v1/queue/pause' }).Count -eq 1) -Message 'The production queue was paused exactly once.'
    Assert-True -Condition (@($calls | Where-Object { $_ -eq 'POST /v1/queue/resume' }).Count -eq 1) -Message 'The production queue was resumed exactly once after the failure.'
}
finally {
    Stop-FakeQueueServer -Process $server
}

# ---------------------------------------------------------------------------
# An operator-paused queue is left paused
# ---------------------------------------------------------------------------
Write-Step 'An operator pause is preserved'
$port = New-FakeQueuePort
$callsLog = Join-Path $WorkDirectory 'calls-already-paused.log'
$ready = Join-Path $WorkDirectory 'ready-already-paused.txt'
$marker = Join-Path $WorkDirectory 'body-ran.txt'
$server = $null
try {
    $server = Start-FakeQueueServer -Port $port -InitialPaused $true -LogPath $callsLog -ReadyPath $ready
    Invoke-BeeBoxQueueExclusive -BaseUrl "http://127.0.0.1:$port" -Token 'test-token' -PollSeconds 1 -DrainTimeoutSeconds 30 -Body { param($ctx) Set-Content -LiteralPath $ctx -Value 'ran' -Encoding ASCII } -BodyArgument $marker
    Assert-True -Condition (Test-Path -LiteralPath $marker -PathType Leaf) -Message 'The body ran while the queue was already paused.'
    $calls = @(Get-Content -LiteralPath $callsLog -ErrorAction SilentlyContinue)
    Assert-True -Condition (@($calls | Where-Object { $_ -match '^POST /v1/queue/(pause|resume)$' }).Count -eq 0) -Message 'The script neither paused nor resumed a queue an operator had paused.'
}
finally {
    Stop-FakeQueueServer -Process $server
}

# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
if ($script:Failures.Count -gt 0) {
    Write-Warn "Dev script checks failed: $($script:Failures.Count)"
    foreach ($failure in $script:Failures) {
        Write-Warn "  - $failure"
    }
    exit 1
}
Write-Ok 'All dev-instance script checks passed.'

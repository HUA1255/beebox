#Requires -Version 5.1
<#
.SYNOPSIS
    Hermetic checks for verify-url-job.ps1 (ticket #12).

.DESCRIPTION
    Proves, without a GPU, real models, network or the production service:

    * -DryRun prints the plan and sends no request;
    * a successful link job is submitted with the Bearer token read from
      <root>\secrets\settings.env and polled to a terminal state;
    * -Prefer asr is carried in the request body;
    * the summary has the job status, error_code, text_source and counts, and
      never the token or the transcript text;
    * failed, cancelled, rejected, not-ready and token-less runs exit non-zero
      and keep error_code.

    A fake /v1 server runs on loopback through TcpListener; nothing on port
    17494 (or any real BeeBox process) is contacted. The verifier itself is
    invoked as a child process so its exit code is observable. Run from the
    repository root:

        powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\test-verify-url-job.ps1

    Exit code 0 means every check passed.
#>
[CmdletBinding()]
param(
    [string]$WorkDirectory,
    [int]$ReadyTimeoutSeconds = 30
)

$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'BeeBox.Common.psm1') -Force

if (-not $WorkDirectory) {
    $WorkDirectory = Join-Path ([System.IO.Path]::GetTempPath()) ('beebox-verify-url-' + [Guid]::NewGuid().ToString('N'))
}
New-Item -ItemType Directory -Force -Path $WorkDirectory | Out-Null

$VerifierPath = Join-Path $PSScriptRoot 'verify-url-job.ps1'
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

function New-TestRoot {
    <#
    .SYNOPSIS
        Create a fake install root whose settings file holds a test token.
    #>
    param([string]$Name = 'BeeBox')
    $root = Join-Path $WorkDirectory $Name
    $secrets = Join-Path $root 'secrets'
    New-Item -ItemType Directory -Force -Path $secrets | Out-Null
    $settings = "# test settings`nBEEBOX_API_TOKEN=test-token`n"
    [System.IO.File]::WriteAllText((Join-Path $secrets 'settings.env'), $settings, (New-Object System.Text.UTF8Encoding($false)))
    return $root
}

function Invoke-Verifier {
    <#
    .SYNOPSIS
        Run verify-url-job.ps1 as a child process and capture its exit code.
    #>
    param([string[]]$Arguments)
    $command = @(
        '-NoProfile',
        '-ExecutionPolicy', 'Bypass',
        '-File', $VerifierPath
    ) + $Arguments
    # A child that writes stderr (a terminating error) would otherwise raise
    # NativeCommandError under this script's Stop preference; the helper keeps
    # the text and the exit code without aborting the checks.
    $capture = Invoke-BeeBoxNativeCapture -FilePath 'powershell.exe' -Arguments $command
    return [pscustomobject]@{
        ExitCode = $capture.ExitCode
        Output   = $capture.Text
    }
}

# Nine BMP CJK characters plus one non-BMP character, as char codes: the test
# file stays ASCII so Windows PowerShell 5.1 reads it identically with or
# without a BOM. The non-BMP character proves the count is code points, not
# UTF-16 units.
$script:Transcript = (-join @(
    [char]0x4F60, [char]0x597D, [char]0xFF0C, [char]0x4E16, [char]0x754C,
    [char]0x3002, [char]0x518D, [char]0x89C1, [char]0x3002
)) + [char]::ConvertFromUtf32(0x1F600)
$transcriptPath = Join-Path $WorkDirectory 'transcript.txt'
[System.IO.File]::WriteAllText($transcriptPath, $script:Transcript, (New-Object System.Text.UTF8Encoding($false)))

# ---------------------------------------------------------------------------
# Fake /v1 server (loopback, TcpListener, no URL ACL needed)
# ---------------------------------------------------------------------------
$serverScriptPath = Join-Path $WorkDirectory 'fake-v1-jobs-server.ps1'
$serverSource = @'
param(
    [int]$Port,
    [string]$Scenario,
    [string]$RequestLog,
    [string]$BodyLog,
    [string]$AuthLog,
    [string]$ReadyPath,
    [string]$TranscriptPath
)

$listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $Port)
$listener.Start()
[System.IO.File]::WriteAllText($ReadyPath, 'ready', [System.Text.Encoding]::ASCII)
$jobPolls = 0
$transcript = ''
if ($TranscriptPath) {
    # ReadAllText returns a plain string; Get-Content -Raw would carry PS
    # provider properties and ConvertTo-Json would serialise those as an object.
    $transcript = [System.IO.File]::ReadAllText($TranscriptPath, [System.Text.Encoding]::UTF8)
}

function Send-Json {
    param($Stream, [int]$StatusCode, [string]$Json)
    $body = [System.Text.Encoding]::UTF8.GetBytes($Json)
    $reason = 'OK'
    if ($StatusCode -eq 201) { $reason = 'Created' }
    if ($StatusCode -eq 400) { $reason = 'Bad Request' }
    if ($StatusCode -eq 404) { $reason = 'Not Found' }
    $header = "HTTP/1.1 $StatusCode $reason`r`nContent-Type: application/json; charset=utf-8`r`nContent-Length: $($body.Length)`r`nConnection: close`r`n`r`n"
    $headerBytes = [System.Text.Encoding]::ASCII.GetBytes($header)
    $Stream.Write($headerBytes, 0, $headerBytes.Length)
    $Stream.Write($body, 0, $body.Length)
    $Stream.Flush()
}

try {
    while ($true) {
        $client = $listener.AcceptTcpClient()
        try {
            $stream = $client.GetStream()
            $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8)
            $requestLine = $reader.ReadLine()
            $contentLength = 0
            $authorization = ''
            $expectContinue = $false
            while ($true) {
                $line = $reader.ReadLine()
                if ($null -eq $line -or $line -eq '') { break }
                if ($line -match '^(?i)Content-Length:\s*(\d+)\s*$') { $contentLength = [int]$Matches[1] }
                if ($line -match '^(?i)Authorization:\s*(.+)$') { $authorization = $Matches[1].Trim() }
                if ($line -match '^(?i)Expect:\s*100-continue') { $expectContinue = $true }
            }
            if ($expectContinue) {
                $continue = [System.Text.Encoding]::ASCII.GetBytes("HTTP/1.1 100 Continue`r`n`r`n")
                $stream.Write($continue, 0, $continue.Length)
                $stream.Flush()
            }
            $body = ''
            if ($contentLength -gt 0) {
                # Request bodies in these checks are ASCII, so one char per
                # Content-Length byte; the StreamReader already owns the buffer.
                $chars = New-Object char[] $contentLength
                $read = 0
                while ($read -lt $contentLength) {
                    $count = $reader.Read($chars, $read, $contentLength - $read)
                    if ($count -le 0) { break }
                    $read += $count
                }
                if ($read -gt 0) { $body = -join $chars[0..($read - 1)] }
            }

            if ([string]::IsNullOrWhiteSpace($requestLine)) { continue }
            $parts = $requestLine -split ' '
            $method = $parts[0]
            $path = $parts[1]
            Add-Content -LiteralPath $RequestLog -Value "$method $path"

            if ($method -eq 'POST' -and $path -eq '/v1/jobs') {
                if ($body) {
                    [System.IO.File]::WriteAllText($BodyLog, $body, (New-Object System.Text.UTF8Encoding($false)))
                }
                if ($authorization) {
                    [System.IO.File]::WriteAllText($AuthLog, $authorization, [System.Text.Encoding]::ASCII)
                }
            }

            if ($method -eq 'GET' -and $path -eq '/v1/health') {
                $readyValue = 'true'
                if ($Scenario -eq 'unready') { $readyValue = 'false' }
                $json = '{"online":true,"ready":' + $readyValue + ',"version":"9.9.9","commit":"abcdef1234567890","queue":{"paused":false,"length":0},"gpu":{"available":false},"bilibili_login":"valid"}'
                Send-Json -Stream $stream -StatusCode 200 -Json $json
            }
            elseif ($method -eq 'POST' -and $path -eq '/v1/jobs') {
                if ($Scenario -eq 'reject') {
                    # Echo the submitted URL the way a malformed-link detail can,
                    # so the checks prove the verifier redacts it.
                    $submittedUrl = ''
                    try {
                        $submittedUrl = [string](($body | ConvertFrom-Json).source.url)
                    }
                    catch {
                        $submittedUrl = ''
                    }
                    $json = (@{
                        detail     = "The link is not a supported single Bilibili or YouTube video: $submittedUrl"
                        error_code = 'UNSUPPORTED_URL'
                        stage      = 'job'
                    } | ConvertTo-Json -Compress)
                    Send-Json -Stream $stream -StatusCode 400 -Json $json
                }
                else {
                    $json = '{"id":"job-1","kind":"transcription","status":"queued","stage":"queued","progress":0.0,"error":null,"error_code":null,"retryable":false,"source":{"type":"url","url":"https://example.invalid/video","text_source":"platform_pending"},"options":{"language":null,"diarization":true,"num_speakers":null,"model":"test-model","subtitle_language":null,"prefer":"asr"},"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z","completed_at":null}'
                    Send-Json -Stream $stream -StatusCode 201 -Json $json
                }
            }
            elseif ($method -eq 'GET' -and $path -eq '/v1/jobs/job-1') {
                $jobPolls = $jobPolls + 1
                if ($Scenario -eq 'failure' -and $jobPolls -ge 2) {
                    $json = '{"id":"job-1","status":"failed","stage":"failed","progress":1.0,"error":"A valid platform login is required.","error_code":"VIDEO_LOGIN_REQUIRED","retryable":false,"source":{"type":"url","text_source":"asr"}}'
                }
                elseif ($Scenario -eq 'cancelled' -and $jobPolls -ge 2) {
                    $json = '{"id":"job-1","status":"cancelled","stage":"cancelled","progress":1.0,"error":"The task was cancelled.","error_code":"TASK_CANCELLED","retryable":false,"source":{"type":"url","text_source":"asr"}}'
                }
                elseif ($jobPolls -ge 2) {
                    $json = '{"id":"job-1","status":"succeeded","stage":"completed","progress":1.0,"error":null,"error_code":null,"retryable":false,"source":{"type":"url","text_source":"asr"}}'
                }
                else {
                    $json = '{"id":"job-1","status":"running","stage":"transcribing","progress":0.4,"error":null,"error_code":null,"retryable":false,"source":{"type":"url","text_source":"asr"}}'
                }
                Send-Json -Stream $stream -StatusCode 200 -Json $json
            }
            elseif ($method -eq 'GET' -and $path -eq '/v1/jobs/job-1/result') {
                $paragraphs = '{"index":1,"start":0.0,"end":1.0,"speaker":null,"text":"x","segment_ids":[1]},{"index":2,"start":1.0,"end":2.0,"speaker":null,"text":"y","segment_ids":[2,3]}'
                $segments = '{"id":1,"start":0.0,"end":1.0,"text":"x"},{"id":2,"start":1.0,"end":2.0,"text":"y"},{"id":3,"start":2.0,"end":3.0,"text":"z"}'
                $textJson = ConvertTo-Json -InputObject $transcript -Compress
                $json = '{"job_id":"job-1","status":"succeeded","text":' + $textJson + ',"language":"zh","duration_ms":3000,"segments":[' + $segments + '],"paragraphs":[' + $paragraphs + '],"speakers":[],"source":{"type":"url","text_source":"asr"}}'
                Send-Json -Stream $stream -StatusCode 200 -Json $json
            }
            else {
                Send-Json -Stream $stream -StatusCode 404 -Json '{"detail":"No route matches this path.","error_code":"NOT_FOUND","stage":"routing"}'
            }
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

function Start-FakeJobServer {
    param(
        [int]$Port,
        [string]$Scenario,
        [string]$RequestLog,
        [string]$BodyLog,
        [string]$AuthLog,
        [string]$ReadyPath,
        [string]$TranscriptPath
    )
    $arguments = @(
        '-NoProfile',
        '-ExecutionPolicy', 'Bypass',
        '-File', $serverScriptPath,
        '-Port', [string]$Port,
        '-Scenario', $Scenario,
        '-RequestLog', $RequestLog,
        '-BodyLog', $BodyLog,
        '-AuthLog', $AuthLog,
        '-ReadyPath', $ReadyPath,
        '-TranscriptPath', $TranscriptPath
    )
    $errorLog = Join-Path (Split-Path -Parent $ReadyPath) 'fake-v1.err.log'
    $outputLog = Join-Path (Split-Path -Parent $ReadyPath) 'fake-v1.out.log'
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
            throw "The fake job server exited with code $($process.ExitCode): $details"
        }
        Start-Sleep -Milliseconds 200
    }
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    throw "The fake job server was not ready within $ReadyTimeoutSeconds seconds."
}

function Stop-FakeJobServer {
    param($Process)
    if ($null -eq $Process) {
        return
    }
    Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
    $Process.WaitForExit(5000) | Out-Null
}

function Get-TextFile {
    param([string]$Path)
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8)
    }
    return ''
}

# ---------------------------------------------------------------------------
# DryRun
# ---------------------------------------------------------------------------
Write-Step 'DryRun sends nothing'
$dryRunRoot = New-TestRoot -Name 'BeeBoxDryRun'
$unusedPort = Get-Random -Minimum 20000 -Maximum 40000
$dryRun = Invoke-Verifier -Arguments @(
    '-Url', 'https://example.invalid/video',
    '-Root', $dryRunRoot,
    '-BaseUrl', "http://127.0.0.1:$unusedPort",
    '-DryRun'
)
Assert-True -Condition ($dryRun.ExitCode -eq 0) -Message "DryRun exits 0 (exit code $($dryRun.ExitCode))."
Assert-True -Condition ($dryRun.Output -match '/v1/jobs') -Message 'DryRun names the /v1/jobs request it would send.'
Assert-True -Condition ($dryRun.Output -match 'No request') -Message 'DryRun states that no request was sent.'
Assert-True -Condition (-not ($dryRun.Output -match [regex]::Escape('test-token'))) -Message 'DryRun output does not contain the token.'

# ---------------------------------------------------------------------------
# A successful job is submitted with -Prefer asr and summarised
# ---------------------------------------------------------------------------
Write-Step 'A successful job is submitted and summarised'
$successRoot = New-TestRoot -Name 'BeeBoxSuccess'
$port = Get-Random -Minimum 20000 -Maximum 40000
$callsLog = Join-Path $WorkDirectory 'calls-success.log'
$bodyLog = Join-Path $WorkDirectory 'body-success.json'
$authLog = Join-Path $WorkDirectory 'auth-success.log'
$ready = Join-Path $WorkDirectory 'ready-success.txt'
$server = $null
try {
    $server = Start-FakeJobServer -Port $port -Scenario 'success' -RequestLog $callsLog -BodyLog $bodyLog -AuthLog $authLog -ReadyPath $ready -TranscriptPath $transcriptPath
    $run = Invoke-Verifier -Arguments @(
        '-Url', 'https://example.invalid/video',
        '-Root', $successRoot,
        '-BaseUrl', "http://127.0.0.1:$port",
        '-Prefer', 'asr',
        '-TimeoutMinutes', '1',
        '-PollSeconds', '1'
    )
    Assert-True -Condition ($run.ExitCode -eq 0) -Message "A succeeded job exits 0 (exit code $($run.ExitCode))."
    Assert-True -Condition ($run.Output -match 'Job status:\s+succeeded') -Message 'The summary reports the succeeded job status.'
    Assert-True -Condition ($run.Output -match 'Error code:\s+\(none\)') -Message 'The summary reports no error_code for a succeeded job.'
    Assert-True -Condition ($run.Output -match 'Text source:\s+asr') -Message 'The summary reports the asr text source.'
    Assert-True -Condition ($run.Output -match 'Paragraphs:\s+2') -Message 'The summary counts the result paragraphs.'
    Assert-True -Condition ($run.Output -match 'Characters:\s+10') -Message 'The summary counts transcript characters as Unicode code points, not UTF-16 units.'
    Assert-True -Condition ($run.Output -match 'Segments:\s+3') -Message 'The summary counts the result segments.'
    Assert-True -Condition (-not ($run.Output -match [regex]::Escape('test-token'))) -Message 'The output does not contain the Bearer token.'
    Assert-True -Condition (-not ($run.Output -match [regex]::Escape($script:Transcript))) -Message 'The output does not contain the transcript text.'

    $body = Get-TextFile -Path $bodyLog
    Assert-True -Condition ($body -match '"type"\s*:\s*"url"') -Message 'The request body uses a url source.'
    Assert-True -Condition ($body -match '"prefer"\s*:\s*"asr"') -Message 'The request body carries -Prefer asr.'
    Assert-True -Condition ($body -match 'example\.invalid') -Message 'The request body carries the submitted URL.'
    Assert-True -Condition ((Get-TextFile -Path $authLog) -eq 'Bearer test-token') -Message 'The request carries the Bearer token from settings.env.'

    $calls = @(Get-Content -LiteralPath $callsLog -ErrorAction SilentlyContinue)
    Assert-True -Condition (@($calls | Where-Object { $_ -eq 'GET /v1/health' }).Count -eq 1) -Message 'The instance health was checked once.'
    Assert-True -Condition (@($calls | Where-Object { $_ -eq 'POST /v1/jobs' }).Count -eq 1) -Message 'Exactly one job was submitted.'
    Assert-True -Condition (@($calls | Where-Object { $_ -eq 'GET /v1/jobs/job-1/result' }).Count -eq 1) -Message 'The succeeded job result was fetched once.'
}
finally {
    Stop-FakeJobServer -Process $server
}

# ---------------------------------------------------------------------------
# Without -Prefer the request carries no text-path override
# ---------------------------------------------------------------------------
Write-Step 'Without -Prefer the request leaves the text path to the instance'
$defaultRoot = New-TestRoot -Name 'BeeBoxDefault'
$port = Get-Random -Minimum 20000 -Maximum 40000
$callsLog = Join-Path $WorkDirectory 'calls-default.log'
$bodyLog = Join-Path $WorkDirectory 'body-default.json'
$authLog = Join-Path $WorkDirectory 'auth-default.log'
$ready = Join-Path $WorkDirectory 'ready-default.txt'
$server = $null
try {
    $server = Start-FakeJobServer -Port $port -Scenario 'success' -RequestLog $callsLog -BodyLog $bodyLog -AuthLog $authLog -ReadyPath $ready -TranscriptPath $transcriptPath
    $run = Invoke-Verifier -Arguments @(
        '-Url', 'https://example.invalid/video',
        '-Root', $defaultRoot,
        '-BaseUrl', "http://127.0.0.1:$port",
        '-TimeoutMinutes', '1',
        '-PollSeconds', '1'
    )
    Assert-True -Condition ($run.ExitCode -eq 0) -Message 'A job without -Prefer exits 0.'
    $body = Get-TextFile -Path $bodyLog
    Assert-True -Condition (-not ($body -match '"prefer"')) -Message 'The request body has no prefer key when -Prefer is omitted.'
}
finally {
    Stop-FakeJobServer -Process $server
}

# ---------------------------------------------------------------------------
# A failed job keeps its error_code and does not fetch a result
# ---------------------------------------------------------------------------
Write-Step 'A failed job keeps its error_code'
$failureRoot = New-TestRoot -Name 'BeeBoxFailure'
$port = Get-Random -Minimum 20000 -Maximum 40000
$callsLog = Join-Path $WorkDirectory 'calls-failure.log'
$bodyLog = Join-Path $WorkDirectory 'body-failure.json'
$authLog = Join-Path $WorkDirectory 'auth-failure.log'
$ready = Join-Path $WorkDirectory 'ready-failure.txt'
$server = $null
try {
    $server = Start-FakeJobServer -Port $port -Scenario 'failure' -RequestLog $callsLog -BodyLog $bodyLog -AuthLog $authLog -ReadyPath $ready -TranscriptPath $transcriptPath
    $run = Invoke-Verifier -Arguments @(
        '-Url', 'https://example.invalid/video',
        '-Root', $failureRoot,
        '-BaseUrl', "http://127.0.0.1:$port",
        '-TimeoutMinutes', '1',
        '-PollSeconds', '1'
    )
    Assert-True -Condition ($run.ExitCode -eq 1) -Message "A failed job exits 1 (exit code $($run.ExitCode))."
    Assert-True -Condition ($run.Output -match 'Job status:\s+failed') -Message 'The summary reports the failed job status.'
    Assert-True -Condition ($run.Output -match 'Error code:\s+VIDEO_LOGIN_REQUIRED') -Message 'The summary keeps the job error_code.'
    Assert-True -Condition ($run.Output -match 'Text source:\s+asr') -Message 'The summary keeps the text source of a failed job.'
    Assert-True -Condition ($run.Output -match 'Detail:.*platform login is required') -Message 'The summary includes the safe failure detail.'
    Assert-True -Condition (-not ($run.Output -match [regex]::Escape('test-token'))) -Message 'The failed-run output does not contain the Bearer token.'
    $calls = @(Get-Content -LiteralPath $callsLog -ErrorAction SilentlyContinue)
    Assert-True -Condition (@($calls | Where-Object { $_ -match '^GET /v1/jobs/job-1/result$' }).Count -eq 0) -Message 'A failed job does not request a result.'
}
finally {
    Stop-FakeJobServer -Process $server
}

# ---------------------------------------------------------------------------
# A rejected submission keeps the API error_code
# ---------------------------------------------------------------------------
Write-Step 'A rejected submission keeps the API error_code'
$rejectRoot = New-TestRoot -Name 'BeeBoxReject'
$port = Get-Random -Minimum 20000 -Maximum 40000
$callsLog = Join-Path $WorkDirectory 'calls-reject.log'
$bodyLog = Join-Path $WorkDirectory 'body-reject.json'
$authLog = Join-Path $WorkDirectory 'auth-reject.log'
$ready = Join-Path $WorkDirectory 'ready-reject.txt'
$server = $null
try {
    $server = Start-FakeJobServer -Port $port -Scenario 'reject' -RequestLog $callsLog -BodyLog $bodyLog -AuthLog $authLog -ReadyPath $ready -TranscriptPath $transcriptPath
    $run = Invoke-Verifier -Arguments @(
        '-Url', 'https://example.invalid/playlist',
        '-Root', $rejectRoot,
        '-BaseUrl', "http://127.0.0.1:$port",
        '-TimeoutMinutes', '1',
        '-PollSeconds', '1'
    )
    Assert-True -Condition ($run.ExitCode -eq 1) -Message "A rejected submission exits 1 (exit code $($run.ExitCode))."
    Assert-True -Condition ($run.Output -match 'Job status:\s+rejected') -Message 'The summary reports the rejected submission.'
    Assert-True -Condition ($run.Output -match 'Error code:\s+UNSUPPORTED_URL') -Message 'The summary keeps the HTTP error_code.'
    Assert-True -Condition ($run.Output -match 'HTTP 400') -Message 'The summary names the HTTP status of a rejection.'
    Assert-True -Condition (-not ($run.Output -match [regex]::Escape('https://example.invalid/playlist'))) -Message 'The rejection detail does not echo the submitted URL.'
    Assert-True -Condition ($run.Output -match '<url>') -Message 'The rejection detail redacts the submitted URL.'
    Assert-True -Condition (-not ($run.Output -match [regex]::Escape('test-token'))) -Message 'The rejected-run output does not contain the Bearer token.'
    $calls = @(Get-Content -LiteralPath $callsLog -ErrorAction SilentlyContinue)
    Assert-True -Condition (@($calls | Where-Object { $_ -match '^GET /v1/jobs/' }).Count -eq 0) -Message 'A rejected submission is not polled.'
}
finally {
    Stop-FakeJobServer -Process $server
}

# ---------------------------------------------------------------------------
# A cancelled job exits non-zero and keeps its error_code
# ---------------------------------------------------------------------------
Write-Step 'A cancelled job keeps its error_code'
$cancelRoot = New-TestRoot -Name 'BeeBoxCancelled'
$port = Get-Random -Minimum 20000 -Maximum 40000
$callsLog = Join-Path $WorkDirectory 'calls-cancelled.log'
$bodyLog = Join-Path $WorkDirectory 'body-cancelled.json'
$authLog = Join-Path $WorkDirectory 'auth-cancelled.log'
$ready = Join-Path $WorkDirectory 'ready-cancelled.txt'
$server = $null
try {
    $server = Start-FakeJobServer -Port $port -Scenario 'cancelled' -RequestLog $callsLog -BodyLog $bodyLog -AuthLog $authLog -ReadyPath $ready -TranscriptPath $transcriptPath
    $run = Invoke-Verifier -Arguments @(
        '-Url', 'https://example.invalid/video',
        '-Root', $cancelRoot,
        '-BaseUrl', "http://127.0.0.1:$port",
        '-TimeoutMinutes', '1',
        '-PollSeconds', '1'
    )
    Assert-True -Condition ($run.ExitCode -eq 1) -Message 'A cancelled job exits 1.'
    Assert-True -Condition ($run.Output -match 'Job status:\s+cancelled') -Message 'The summary reports the cancelled job status.'
    Assert-True -Condition ($run.Output -match 'Error code:\s+TASK_CANCELLED') -Message 'The summary keeps the cancelled job error_code.'
    Assert-True -Condition (-not ($run.Output -match [regex]::Escape('test-token'))) -Message 'The cancelled-run output does not contain the Bearer token.'
    $calls = @(Get-Content -LiteralPath $callsLog -ErrorAction SilentlyContinue)
    Assert-True -Condition (@($calls | Where-Object { $_ -match '^GET /v1/jobs/job-1/result$' }).Count -eq 0) -Message 'A cancelled job does not request a result.'
}
finally {
    Stop-FakeJobServer -Process $server
}

# ---------------------------------------------------------------------------
# A not-ready instance fails the preflight and submits nothing
# ---------------------------------------------------------------------------
Write-Step 'A not-ready instance is rejected before submission'
$unreadyRoot = New-TestRoot -Name 'BeeBoxUnready'
$port = Get-Random -Minimum 20000 -Maximum 40000
$callsLog = Join-Path $WorkDirectory 'calls-unready.log'
$bodyLog = Join-Path $WorkDirectory 'body-unready.json'
$authLog = Join-Path $WorkDirectory 'auth-unready.log'
$ready = Join-Path $WorkDirectory 'ready-unready.txt'
$server = $null
try {
    $server = Start-FakeJobServer -Port $port -Scenario 'unready' -RequestLog $callsLog -BodyLog $bodyLog -AuthLog $authLog -ReadyPath $ready -TranscriptPath $transcriptPath
    $run = Invoke-Verifier -Arguments @(
        '-Url', 'https://example.invalid/video',
        '-Root', $unreadyRoot,
        '-BaseUrl', "http://127.0.0.1:$port",
        '-TimeoutMinutes', '1',
        '-PollSeconds', '1'
    )
    Assert-True -Condition ($run.ExitCode -eq 1) -Message 'A not-ready instance exits 1.'
    Assert-True -Condition ($run.Output -match 'ready=false') -Message 'The output names the failed readiness check.'
    $calls = @(Get-Content -LiteralPath $callsLog -ErrorAction SilentlyContinue)
    Assert-True -Condition (@($calls | Where-Object { $_ -eq 'POST /v1/jobs' }).Count -eq 0) -Message 'A not-ready instance receives no job submission.'
}
finally {
    Stop-FakeJobServer -Process $server
}

Write-Step 'A missing token fails before contacting the API'
$noTokenRoot = Join-Path $WorkDirectory 'BeeBoxNoToken'
New-Item -ItemType Directory -Force -Path (Join-Path $noTokenRoot 'secrets') | Out-Null
$unusedPort = Get-Random -Minimum 20000 -Maximum 40000
$noToken = Invoke-Verifier -Arguments @(
    '-Url', 'https://example.invalid/video',
    '-Root', $noTokenRoot,
    '-BaseUrl', "http://127.0.0.1:$unusedPort",
    '-TimeoutMinutes', '1',
    '-PollSeconds', '1'
)
Assert-True -Condition ($noToken.ExitCode -eq 1) -Message "A missing token exits 1 (exit code $($noToken.ExitCode))."
Assert-True -Condition ($noToken.Output -match 'No usable Bearer token') -Message 'A missing token explains how to run the script.'
Assert-True -Condition ($noToken.Output -match 'elevated') -Message 'A missing token points at the elevated prompt.'
Assert-True -Condition (-not ($noToken.Output -match 'Checking the instance')) -Message 'A missing token fails before the health check.'

# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
if ($script:Failures.Count -gt 0) {
    Write-Warn "verify-url-job checks failed: $($script:Failures.Count)"
    foreach ($failure in $script:Failures) {
        Write-Warn "  - $failure"
    }
    exit 1
}
Write-Ok 'All verify-url-job checks passed.'

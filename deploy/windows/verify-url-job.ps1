#Requires -Version 5.1
<#
.SYNOPSIS
    Submit one real video-link job to a deployed BeeBox instance and report its
    outcome; the post-deployment acceptance for link, audio and ASR changes
    (ticket #12).

.DESCRIPTION
    A URL job is only proven fixed against a deployed instance with a real
    link. The production Bearer token lives in <root>\secrets\settings.env,
    which the installer restricts to SYSTEM/Administrators, so this script must
    run from an elevated prompt. It reads the token there, submits the job
    through POST /v1/jobs, polls GET /v1/jobs/{id} to a terminal state and
    prints only the status, error_code, text_source and summary counts.

    The token and the transcript text are never printed, and the submitted URL
    is not echoed: the summary carries only the status, error_code, text_source
    and summary counts. The job itself stays in the instance so a failure can
    be inspected; nothing is deleted.

    This is a maintenance tool. It submits a real job and occupies the GPU, so
    it is not run by CI; the hermetic checks in test-verify-url-job.ps1 use a
    fake loopback /v1 server and never submit a real job.

.PARAMETER Url
    Bilibili or YouTube single-video URL. Other hosts, playlists, collections
    and live streams are rejected by the API before a job is created.

.PARAMETER Prefer
    Pass asr to force the audio/speech-recognition path instead of using a
    platform subtitle track. Omit it to let the instance choose (a usable
    platform subtitle is preferred).

.PARAMETER TimeoutMinutes
    How long to wait for a terminal state. Default 25.

.PARAMETER Root
    Production install root. Default C:\BeeBox.

.PARAMETER BaseUrl
    API base URL. Default http://127.0.0.1:17494; pass the listen port used at
    install time when it is not the default.

.PARAMETER PollSeconds
    Status polling interval. Default 5.

.PARAMETER DryRun
    Print the request plan and exit 0 without contacting the API.

.EXAMPLE
    # Elevated prompt, because the production settings file is protected
    powershell -ExecutionPolicy Bypass -File .\deploy\windows\verify-url-job.ps1 `
      -Url 'https://www.bilibili.com/video/BV...'

.EXAMPLE
    # Force the audio/ASR path for a video that also has platform subtitles
    powershell -ExecutionPolicy Bypass -File .\deploy\windows\verify-url-job.ps1 `
      -Url 'https://www.youtube.com/watch?v=...' -Prefer asr

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\deploy\windows\verify-url-job.ps1 `
      -Url 'https://www.bilibili.com/video/BV...' -DryRun
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Url,
    [ValidateSet('asr')][string]$Prefer,
    [ValidateRange(1, 600)][int]$TimeoutMinutes = 25,
    [string]$Root = 'C:\BeeBox',
    [string]$BaseUrl = 'http://127.0.0.1:17494',
    [ValidateRange(1, 60)][int]$PollSeconds = 5,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'BeeBox.Common.psm1') -Force

$Root = Resolve-FullPath $Root
$SettingsPath = Join-Path $Root 'secrets\settings.env'
$BaseUrl = $BaseUrl.TrimEnd('/')

function Get-SettingsToken {
    <#
    .SYNOPSIS
        Read BEEBOX_API_TOKEN without printing the file or the value.
    #>
    param([Parameter(Mandatory = $true)][string]$Path)
    $values = Read-BeeBoxEnvFile -Path $Path
    if ($values.Contains('BEEBOX_API_TOKEN')) {
        return [string]$values['BEEBOX_API_TOKEN']
    }
    return ''
}

function Get-JsonValue {
    param(
        $Object,
        [Parameter(Mandatory = $true)][string]$Name,
        $Default = $null
    )
    if ($null -eq $Object) {
        return $Default
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $Default
    }
    return $property.Value
}

function Read-Utf8Stream {
    param($Stream)
    if ($null -eq $Stream) {
        return $null
    }
    if ($Stream.CanSeek) {
        $Stream.Position = 0
    }
    $reader = New-Object System.IO.StreamReader($Stream, [System.Text.Encoding]::UTF8)
    try {
        return $reader.ReadToEnd()
    }
    finally {
        $reader.Dispose()
    }
}

function Read-JsonResponse {
    <#
    .SYNOPSIS
        Decode a response as UTF-8 and parse it, without trusting Content-Type.
    .DESCRIPTION
        Windows PowerShell 5.1 decodes a response without a charset as
        ISO-8859-1, which mangles the transcript text and makes the character
        count wrong. Reading the raw stream as UTF-8 keeps the count correct.
    #>
    param([Parameter(Mandatory = $true)]$Response)
    $text = $null
    try {
        $text = Read-Utf8Stream -Stream $Response.RawContentStream
    }
    catch {
        $text = $null
    }
    if ([string]::IsNullOrWhiteSpace($text)) {
        $text = [string]$Response.Content
    }
    if ([string]::IsNullOrWhiteSpace($text)) {
        return $null
    }
    return ($text | ConvertFrom-Json)
}

function Invoke-JsonApi {
    <#
    .SYNOPSIS
        One /v1 request; returns the parsed JSON body.
    #>
    param(
        [Parameter(Mandatory = $true)][ValidateSet('GET', 'POST')][string]$Method,
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$Token,
        [object]$Body,
        [int]$TimeoutSeconds = 30
    )
    $headers = @{}
    if (-not [string]::IsNullOrWhiteSpace($Token)) {
        $headers['Authorization'] = "Bearer $Token"
    }
    $parameters = @{
        Uri             = $BaseUrl + $Path
        Method          = $Method
        Headers         = $headers
        UseBasicParsing = $true
        TimeoutSec      = $TimeoutSeconds
        ErrorAction     = 'Stop'
    }
    if ($null -ne $Body) {
        $json = $Body | ConvertTo-Json -Depth 8 -Compress
        $parameters['Body'] = [System.Text.Encoding]::UTF8.GetBytes($json)
        $parameters['ContentType'] = 'application/json; charset=utf-8'
    }
    $response = Invoke-WebRequest @parameters
    return Read-JsonResponse -Response $response
}

function Get-HttpFailure {
    <#
    .SYNOPSIS
        The v1 error payload and HTTP status behind an Invoke-WebRequest error.
    #>
    param([Parameter(Mandatory = $true)]$ErrorRecord)
    $statusCode = $null
    $response = $ErrorRecord.Exception.Response
    if ($null -ne $response) {
        try {
            $statusCode = [int]$response.StatusCode
        }
        catch {
            $statusCode = $null
        }
    }
    $text = $null
    if ($null -ne $ErrorRecord.ErrorDetails -and -not [string]::IsNullOrWhiteSpace([string]$ErrorRecord.ErrorDetails.Message)) {
        $text = [string]$ErrorRecord.ErrorDetails.Message
    }
    if ([string]::IsNullOrWhiteSpace($text) -and $null -ne $response) {
        try {
            $text = Read-Utf8Stream -Stream $response.GetResponseStream()
        }
        catch {
            $text = $null
        }
    }
    $payload = $null
    if (-not [string]::IsNullOrWhiteSpace($text)) {
        try {
            $payload = $text | ConvertFrom-Json
        }
        catch {
            $payload = $null
        }
    }
    return [pscustomobject]@{
        StatusCode = $statusCode
        ErrorCode  = [string](Get-JsonValue -Object $payload -Name 'error_code' -Default '')
        Detail     = [string](Get-JsonValue -Object $payload -Name 'detail' -Default '')
        Stage      = [string](Get-JsonValue -Object $payload -Name 'stage' -Default '')
        Message    = [string]$ErrorRecord.Exception.Message
    }
}

function Get-TextSource {
    param($State)
    $source = Get-JsonValue -Object $State -Name 'source' -Default $null
    return [string](Get-JsonValue -Object $source -Name 'text_source' -Default '')
}

function Get-JsonArray {
    param(
        $Object,
        [Parameter(Mandatory = $true)][string]$Name
    )
    $value = Get-JsonValue -Object $Object -Name $Name -Default @()
    if ($null -eq $value) {
        return @()
    }
    return @($value)
}

function Measure-Characters {
    <#
    .SYNOPSIS
        Count Unicode code points; a UTF-16 surrogate pair counts as one.
    #>
    param([string]$Text)
    if ([string]::IsNullOrEmpty($Text)) {
        return 0
    }
    $count = 0
    $index = 0
    while ($index -lt $Text.Length) {
        $count = $count + 1
        if ([char]::IsHighSurrogate($Text[$index]) -and ($index + 1) -lt $Text.Length -and [char]::IsLowSurrogate($Text[$index + 1])) {
            $index = $index + 2
        }
        else {
            $index = $index + 1
        }
    }
    return $count
}

function Hide-Url {
    <#
    .SYNOPSIS
        Keep the submitted URL out of an API detail the operator may paste.
    #>
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text) -or [string]::IsNullOrWhiteSpace($Url)) {
        return $Text
    }
    return $Text.Replace($Url, '<url>')
}

function Write-JobSummary {
    param(
        [Parameter(Mandatory = $true)][string]$Status,
        [string]$ErrorCode,
        [string]$TextSource,
        [string]$Paragraphs = 'n/a',
        [string]$Characters = 'n/a',
        [string]$Segments = 'n/a',
        [string]$Detail
    )
    if ([string]::IsNullOrWhiteSpace($ErrorCode)) {
        $ErrorCode = '(none)'
    }
    if ([string]::IsNullOrWhiteSpace($TextSource)) {
        $TextSource = 'unknown'
    }
    $Detail = Hide-Url -Text $Detail
    Write-Host ''
    Write-Step 'Job result'
    Write-Info ("Job status:   {0}" -f $Status)
    Write-Info ("Error code:   {0}" -f $ErrorCode)
    Write-Info ("Text source:  {0}" -f $TextSource)
    Write-Info ("Paragraphs:   {0}" -f $Paragraphs)
    Write-Info ("Characters:   {0}" -f $Characters)
    Write-Info ("Segments:     {0}" -f $Segments)
    if (-not [string]::IsNullOrWhiteSpace($Detail)) {
        Write-Info ("Detail:       {0}" -f $Detail)
    }
}

$Token = Get-SettingsToken -Path $SettingsPath

Write-Step 'BeeBox video-link job acceptance'
Write-Info "Root:     $Root"
Write-Info "Settings: $SettingsPath"
Write-Info "API:      $BaseUrl"
if ($Prefer) {
    Write-Info "Text:     forced ASR (-Prefer $Prefer)"
}
else {
    Write-Info 'Text:     platform subtitle when usable, otherwise ASR'
}
if ($DryRun) {
    if ([string]::IsNullOrWhiteSpace($Token)) {
        Write-Warn "No BEEBOX_API_TOKEN could be read from '$SettingsPath'. The installer restricts it to SYSTEM/Administrators; run this script from an elevated prompt."
    }
    Write-Warn 'DRY RUN: no request is sent and no job is created.'
    Write-DryRun "GET $BaseUrl/v1/health and fail before submitting when the instance is not online and ready."
    if ($Prefer) {
        Write-DryRun "POST $BaseUrl/v1/jobs with {`"kind`":`"transcription`",`"source`":{`"type`":`"url`",`"url`":<url>},`"options`":{`"prefer`":`"$Prefer`"}} and the Bearer token from settings.env."
    }
    else {
        Write-DryRun "POST $BaseUrl/v1/jobs with {`"kind`":`"transcription`",`"source`":{`"type`":`"url`",`"url`":<url>}} and the Bearer token from settings.env."
    }
    Write-DryRun "Poll GET $BaseUrl/v1/jobs/<id> every ${PollSeconds}s until succeeded, failed or cancelled (timeout ${TimeoutMinutes} min)."
    Write-DryRun 'On success, GET /v1/jobs/<id>/result and print only the status, error_code, text_source and summary counts.'
    Write-DryRun "Exit code 0 only for succeeded; failed, cancelled, rejected and timeout exit 1. No request was sent."
    return
}

if ([string]::IsNullOrWhiteSpace($Token)) {
    Write-Warn "No usable Bearer token. Run this script from an elevated prompt so it can read '$SettingsPath', or check that the production settings file contains BEEBOX_API_TOKEN."
    exit 1
}

try {
    Write-Step 'Checking the instance'
    $health = Invoke-JsonApi -Method Get -Path '/v1/health' -TimeoutSeconds 15
    if ($null -eq $health) {
        throw "BeeBox returned an empty /v1/health response at $BaseUrl."
    }
    if (-not [bool](Get-JsonValue -Object $health -Name 'online' -Default $false)) {
        throw "BeeBox reports online=false at $BaseUrl; check the service and logs."
    }
    if (-not [bool](Get-JsonValue -Object $health -Name 'ready' -Default $false)) {
        throw "BeeBox reports ready=false at $BaseUrl; the database, storage or service setup needs attention."
    }
    $version = [string](Get-JsonValue -Object $health -Name 'version' -Default '')
    $commit = [string](Get-JsonValue -Object $health -Name 'commit' -Default '')
    if ($version) {
        $healthLine = "Instance online and ready (version $version"
        if ($commit) {
            $healthLine = $healthLine + ", commit $commit"
        }
        Write-Ok ($healthLine + ').')
    }
    else {
        Write-Ok 'Instance online and ready.'
    }

    Write-Step 'Submitting the job'
    $options = @{}
    if ($Prefer) {
        $options['prefer'] = $Prefer
    }
    $request = @{
        kind    = 'transcription'
        source  = @{ type = 'url'; url = $Url }
        options = $options
    }
    try {
        $job = Invoke-JsonApi -Method Post -Path '/v1/jobs' -Token $Token -Body $request -TimeoutSeconds 60
    }
    catch {
        $failure = Get-HttpFailure -ErrorRecord $_
        $detail = $failure.Detail
        if ($null -ne $failure.StatusCode) {
            $prefix = "The API rejected the job with HTTP $($failure.StatusCode)."
            if ($detail) {
                $detail = "$prefix $detail"
            }
            else {
                $detail = $prefix
            }
        }
        if ([string]::IsNullOrWhiteSpace($failure.ErrorCode)) {
            if ($detail) {
                $detail = "$detail $($failure.Message)"
            }
            else {
                $detail = $failure.Message
            }
        }
        Write-JobSummary -Status 'rejected' -ErrorCode $failure.ErrorCode -TextSource 'n/a' -Detail $detail
        exit 1
    }

    $jobId = [string](Get-JsonValue -Object $job -Name 'id' -Default '')
    if ([string]::IsNullOrWhiteSpace($jobId)) {
        throw 'The API accepted the request but returned no job id.'
    }
    Write-Ok "Job submitted; polling every ${PollSeconds}s (timeout ${TimeoutMinutes} min)."

    Write-Step 'Waiting for the job'
    $state = $job
    $deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    $lastLabel = ''
    $failedPolls = 0
    while ($true) {
        $status = [string](Get-JsonValue -Object $state -Name 'status' -Default '')
        $stage = [string](Get-JsonValue -Object $state -Name 'stage' -Default '')
        $progress = 0
        $progressValue = Get-JsonValue -Object $state -Name 'progress' -Default 0
        if ($null -ne $progressValue) {
            $progress = [int]([double]$progressValue * 100)
        }
        $label = "$status/$stage ${progress}%"
        if ($label -ne $lastLabel) {
            Write-Info ("  {0}" -f $label)
            $lastLabel = $label
        }
        if (@('succeeded', 'failed', 'cancelled') -contains $status) {
            break
        }
        if ((Get-Date) -ge $deadline) {
            Write-JobSummary -Status "timeout ($status)" -ErrorCode '(none)' -TextSource (Get-TextSource -State $state) `
                -Detail "The job did not reach succeeded, failed or cancelled within $TimeoutMinutes minutes. It may still be running or queued."
            exit 1
        }
        Start-Sleep -Seconds $PollSeconds
        try {
            $state = Invoke-JsonApi -Method Get -Path "/v1/jobs/$([uri]::EscapeDataString($jobId))" -Token $Token
            $failedPolls = 0
        }
        catch {
            $failure = Get-HttpFailure -ErrorRecord $_
            $failedPolls = $failedPolls + 1
            if ($failure.StatusCode -eq 401) {
                Write-JobSummary -Status 'error' -ErrorCode $failure.ErrorCode -TextSource 'unknown' `
                    -Detail 'The Bearer token was rejected while polling. It may have been rotated; read settings.env again from an elevated prompt.'
                exit 1
            }
            if ($failure.StatusCode -eq 404) {
                Write-JobSummary -Status 'error' -ErrorCode $failure.ErrorCode -TextSource 'unknown' `
                    -Detail 'The job disappeared while polling.'
                exit 1
            }
            if ($failedPolls -ge 3) {
                Write-JobSummary -Status 'error' -ErrorCode $failure.ErrorCode -TextSource 'unknown' `
                    -Detail "The API was unreachable for three consecutive polls: $($failure.Message)"
                exit 1
            }
            Write-Warn "Polling failed ($failedPolls/3); retrying: $($failure.Message)"
        }
    }

    $textSource = Get-TextSource -State $state
    $errorCode = [string](Get-JsonValue -Object $state -Name 'error_code' -Default '')
    $errorMessage = [string](Get-JsonValue -Object $state -Name 'error' -Default '')

    if ($status -eq 'succeeded') {
        Write-Step 'Reading the result summary'
        $result = Invoke-JsonApi -Method Get -Path "/v1/jobs/$([uri]::EscapeDataString($jobId))/result" -Token $Token -TimeoutSeconds 120
        $paragraphs = Get-JsonArray -Object $result -Name 'paragraphs'
        $segments = Get-JsonArray -Object $result -Name 'segments'
        $text = [string](Get-JsonValue -Object $result -Name 'text' -Default '')
        if ([string]::IsNullOrWhiteSpace($textSource)) {
            $textSource = Get-TextSource -State $result
        }
        Write-JobSummary -Status 'succeeded' -ErrorCode '(none)' -TextSource $textSource `
            -Paragraphs $paragraphs.Count -Characters (Measure-Characters -Text $text) -Segments $segments.Count
        exit 0
    }

    Write-JobSummary -Status $status -ErrorCode $errorCode -TextSource $textSource -Detail $errorMessage
    exit 1
}
catch {
    Write-Warn $_.Exception.Message
    exit 1
}

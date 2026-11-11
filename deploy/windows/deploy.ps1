#Requires -Version 5.1
<#
.SYNOPSIS
    Deploy a git tag of BeeBox to the local Windows service.

.DESCRIPTION
    One command upgrades an existing install (see install.ps1) to a git tag:

      1. pause the GPU queue through POST /v1/queue/pause,
      2. wait until the running job finishes (bounded by -DrainTimeoutSeconds),
      3. fetch tags and verify that <root>\app is a clean git checkout,
      4. stop the WinSW service (files cannot be replaced under a live process),
      5. check out the tag,
      6. run uv sync --frozen into the service venv,
      7. run the database migration (init_db),
      8. record BEEBOX_GIT_COMMIT in settings.env and the WinSW service XML,
      9. start the service and poll /v1/health until the reported commit equals
         the tag's commit and the version matches the tag's pyproject.toml,
     10. resume the GPU queue.

    On failure the script fails loudly with a rollback hint and resumes the GPU
    queue whenever the API is reachable (also when an earlier step failed and
    the service is still running the previous build).

    The Bearer token is read from <root>\secrets\settings.env and is never
    printed. The script never downloads models.

.PARAMETER Tag
    Git tag to deploy, for example v0.4.1.

.PARAMETER Root
    Fixed install root. Default C:\BeeBox.

.PARAMETER DrainTimeoutSeconds
    How long to wait for the current job to finish before failing. Default
    3600 (one hour). The job is never interrupted.

.PARAMETER HealthTimeoutSeconds
    How long to wait for /v1/health to report the new commit and version after
    the restart. Default 300.

.PARAMETER IssueNumber
    Optional GitHub issue number that receives a deployment summary through the
    gh CLI. The comment never contains the token or any other secret.

.PARAMETER DryRun
    Print every planned step and change nothing: no HTTP calls, no git fetch,
    no file writes, no service control. Exits 0. Works without an installed
    layout, which makes it safe for CI.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\deploy\windows\deploy.ps1 -Tag v0.4.1

.EXAMPLE
    .\deploy\windows\deploy.ps1 -Tag v0.4.1 -Root D:\BeeBox -IssueNumber 54

.EXAMPLE
    .\deploy\windows\deploy.ps1 -Tag v0.4.1 -DryRun
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Tag,
    [string]$Root = 'C:\BeeBox',
    [int]$DrainTimeoutSeconds = 3600,
    [int]$HealthTimeoutSeconds = 300,
    [int]$IssueNumber = 0,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

# Deployments run unattended (often over SSH), so git must fail fast instead of
# waiting for a credential prompt. Credentials must already be cached, or the
# checkout must fetch from a local mirror.
if (-not $env:GIT_TERMINAL_PROMPT) {
    $env:GIT_TERMINAL_PROMPT = '0'
}
if (-not $env:GCM_INTERACTIVE) {
    $env:GCM_INTERACTIVE = 'Never'
}

Import-Module (Join-Path $PSScriptRoot 'BeeBox.Common.psm1') -Force

$IsDryRun = [bool]$DryRun

function Invoke-GitCapture {
    param(
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    return Invoke-BeeBoxNativeCapture -FilePath 'git' `
        -Arguments (@('-C', $WorkingDirectory) + $Arguments)
}

function Resolve-TagCommit {
    param(
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$TagName
    )
    $result = Invoke-GitCapture -WorkingDirectory $WorkingDirectory `
        -Arguments @('rev-parse', '--verify', '--quiet', "refs/tags/$TagName^{commit}")
    if ($result.ExitCode -ne 0 -or -not $result.Text) {
        return $null
    }
    return $result.Text
}

function Update-GitTags {
    param([Parameter(Mandatory = $true)][string]$WorkingDirectory)

    $remotes = @(& git -C $WorkingDirectory remote 2>$null)
    if ($remotes -notcontains 'origin') {
        Write-Warn "The checkout at $WorkingDirectory has no 'origin' remote; using the tags already present."
        return $false
    }
    $fetch = Invoke-GitCapture -WorkingDirectory $WorkingDirectory `
        -Arguments @('fetch', '--tags', '--prune', '--force', 'origin')
    if ($fetch.ExitCode -ne 0) {
        Write-Warn "git fetch --tags failed: $($fetch.Text)"
        Write-Warn 'Continuing with the tags already present in the checkout.'
        return $false
    }
    Write-Ok 'Tags fetched from origin.'
    return $true
}

function Get-VersionAtCommit {
    param(
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$Commit
    )
    $show = Invoke-GitCapture -WorkingDirectory $WorkingDirectory `
        -Arguments @('show', "${Commit}:pyproject.toml")
    if ($show.ExitCode -ne 0) {
        return $null
    }
    $match = [regex]::Match($show.Text, '(?m)^\s*version\s*=\s*"([^"]+)"')
    if (-not $match.Success) {
        return $null
    }
    return $match.Groups[1].Value
}

function Get-CheckoutVersion {
    param([Parameter(Mandatory = $true)][string]$AppDirectory)
    $path = Join-Path $AppDirectory 'pyproject.toml'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        return $null
    }
    $match = [regex]::Match((Get-Content -LiteralPath $path -Raw -Encoding UTF8), '(?m)^\s*version\s*=\s*"([^"]+)"')
    if (-not $match.Success) {
        return $null
    }
    return $match.Groups[1].Value
}

function Read-ServiceDefinition {
    param([Parameter(Mandatory = $true)][string]$Path)

    $definition = [pscustomobject]@{
        Name      = 'BeeBox'
        Host      = '127.0.0.1'
        Port      = 17494
        Arguments = ''
    }
    if (-not (Test-BeeBoxPathQuiet -Path $Path -PathType Leaf)) {
        return $definition
    }
    try {
        [xml]$document = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
        $serviceNode = $document.DocumentElement
        if ($null -eq $serviceNode) {
            return $definition
        }
        $id = [string]$serviceNode.id
        if ($id) {
            $definition.Name = $id
        }
        $definition.Arguments = [string]$serviceNode.arguments
        $portMatch = [regex]::Match($definition.Arguments, '--port\s+(\d+)')
        if ($portMatch.Success) {
            $definition.Port = [int]$portMatch.Groups[1].Value
        }
        $hostMatch = [regex]::Match($definition.Arguments, '--host\s+"?([^\s"]+)"?')
        if ($hostMatch.Success) {
            $definition.Host = $hostMatch.Groups[1].Value
        }
    }
    catch {
        Write-Warn "Could not read the WinSW service definition at ${Path}: $($_.Exception.Message)"
    }
    return $definition
}

function Get-ApiBaseUrl {
    param(
        [Parameter(Mandatory = $true)][string]$ListenHost,
        [Parameter(Mandatory = $true)][int]$ListenPort
    )
    $hostName = $ListenHost
    if ($hostName -eq '0.0.0.0' -or $hostName -eq '::' -or $hostName -eq '*') {
        $hostName = '127.0.0.1'
    }
    if ($hostName.Contains(':')) {
        $hostName = "[$hostName]"
    }
    return "http://${hostName}:$ListenPort"
}

function Invoke-BeeBoxApi {
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [string]$Token,
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)][string]$Path,
        [int]$TimeoutSeconds = 15
    )
    $headers = @{}
    if ($Token) {
        $headers['Authorization'] = "Bearer $Token"
    }
    $uri = $BaseUrl.TrimEnd('/') + $Path
    return Invoke-WebRequest -Uri $uri -Method $Method -Headers $headers `
        -UseBasicParsing -TimeoutSec $TimeoutSeconds -ErrorAction Stop
}

function Try-GetHealth {
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [string]$Token,
        [int]$TimeoutSeconds = 5
    )
    try {
        $response = Invoke-BeeBoxApi -BaseUrl $BaseUrl -Token $Token -Method 'GET' `
            -Path '/v1/health' -TimeoutSeconds $TimeoutSeconds
        if ($response.StatusCode -eq 200) {
            return ($response.Content | ConvertFrom-Json)
        }
    }
    catch {
        return $null
    }
    return $null
}

function Wait-QueueDrained {
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [string]$Token,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $nextReport = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-BeeBoxApi -BaseUrl $BaseUrl -Token $Token -Method 'GET' -Path '/v1/queue'
            $state = $response.Content | ConvertFrom-Json
        }
        catch {
            throw "Lost contact with the API while waiting for the current job: $($_.Exception.Message)"
        }
        if (-not $state.current_job_id) {
            return $state
        }
        if ((Get-Date) -ge $nextReport) {
            $remaining = [int]($deadline - (Get-Date)).TotalSeconds
            Write-Info "Job $($state.current_job_id) ($($state.current_job_kind)) is still running; $remaining seconds left before this deployment gives up."
            $nextReport = (Get-Date).AddSeconds(30)
        }
        Start-Sleep -Seconds 5
    }
    throw "The current job did not finish within $TimeoutSeconds seconds. No files were changed; the failure handler resumes the queue."
}

function Stop-BeeBoxService {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$WinSwExecutable
    )
    $service = Get-InstalledService -Name $Name
    if ($null -eq $service) {
        throw "The Windows service '$Name' is not installed. Run deploy/windows/install.ps1 first."
    }
    if ($service.Status.ToString() -eq 'Stopped') {
        Write-Ok "Service '$Name' is already stopped."
        return
    }
    Write-Info "Stopping service '$Name'..."
    & $WinSwExecutable stop | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "WinSW stop returned exit code $LASTEXITCODE; falling back to sc.exe stop."
        & sc.exe stop $Name | Out-Null
    }
    if (-not (Wait-ServiceState -Name $Name -State 'Stopped' -TimeoutSeconds 60)) {
        throw "Service '$Name' did not stop within 60 seconds; refusing to replace application files under a live process."
    }
    Write-Ok "Service '$Name' stopped."
}

function Start-BeeBoxService {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$WinSwExecutable
    )
    & $WinSwExecutable start | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "WinSW could not start the '$Name' service (exit code $LASTEXITCODE)."
    }
    if (-not (Wait-ServiceState -Name $Name -State 'Running' -TimeoutSeconds 90)) {
        throw "Service '$Name' did not report Running within 90 seconds."
    }
    Write-Ok "Service '$Name' is running."
}

function Set-ServiceCommitEnv {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Commit
    )
    [xml]$document = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    $serviceNode = $document.DocumentElement
    if ($null -eq $serviceNode) {
        throw "The WinSW service definition at $Path has no service element."
    }
    $envNode = $null
    foreach ($node in @($serviceNode.env)) {
        if ($null -ne $node -and $node.name -eq 'BEEBOX_GIT_COMMIT') {
            $envNode = $node
            break
        }
    }
    if ($null -eq $envNode) {
        $envNode = $document.CreateElement('env')
        [void]$envNode.SetAttribute('name', 'BEEBOX_GIT_COMMIT')
        [void]$serviceNode.AppendChild($envNode)
    }
    [void]$envNode.SetAttribute('value', $Commit)

    $writerSettings = New-Object System.Xml.XmlWriterSettings
    $writerSettings.Encoding = New-Object System.Text.UTF8Encoding($false)
    $writerSettings.Indent = $true
    $writer = [System.Xml.XmlWriter]::Create($Path, $writerSettings)
    try {
        $document.Save($writer)
    }
    finally {
        $writer.Dispose()
    }
}

function Wait-DeploymentHealth {
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [string]$Token,
        [Parameter(Mandatory = $true)][string]$ExpectedCommit,
        [string]$ExpectedVersion,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastState = 'no response yet'
    while ((Get-Date) -lt $deadline) {
        $health = $null
        try {
            $response = Invoke-BeeBoxApi -BaseUrl $BaseUrl -Token $Token -Method 'GET' `
                -Path '/v1/health' -TimeoutSeconds 5
            if ($response.StatusCode -eq 200) {
                $health = $response.Content | ConvertFrom-Json
            }
        }
        catch {
            $lastState = $_.Exception.Message
        }
        if ($null -ne $health) {
            $problems = New-Object System.Collections.Generic.List[string]
            if (-not [bool]$health.online) {
                $problems.Add('online=false')
            }
            if (-not [bool]$health.ready) {
                $problems.Add('ready=false')
            }
            if ([string]$health.commit -ne $ExpectedCommit) {
                $problems.Add("commit=$($health.commit) (expected $ExpectedCommit)")
            }
            if (-not [string]$health.version) {
                $problems.Add('version is empty')
            }
            elseif ($ExpectedVersion -and [string]$health.version -ne $ExpectedVersion) {
                $problems.Add("version=$($health.version) (expected $ExpectedVersion)")
            }
            if ($problems.Count -eq 0) {
                return $health
            }
            $lastState = $problems -join '; '
        }
        Start-Sleep -Seconds 2
    }
    throw "Service health did not converge within $TimeoutSeconds seconds. Last state: $lastState"
}

function Resume-GpuQueue {
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [string]$Token
    )
    try {
        $response = Invoke-BeeBoxApi -BaseUrl $BaseUrl -Token $Token -Method 'POST' `
            -Path '/v1/queue/resume'
        $state = $response.Content | ConvertFrom-Json
        Write-Ok "GPU queue resumed (paused=$($state.paused), waiting=$($state.length))."
        return $true
    }
    catch {
        Write-Warn "Could not resume the GPU queue: $($_.Exception.Message)"
        return $false
    }
}

function Show-BeeBoxFailureContext {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$LogDirectory
    )
    & sc.exe query $Name
    if (-not (Test-Path -LiteralPath $LogDirectory -PathType Container)) {
        return
    }
    Get-ChildItem -LiteralPath $LogDirectory -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 5 |
        ForEach-Object {
            Write-Warn "Log file: $($_.FullName)"
            Get-Content -LiteralPath $_.FullName -Tail 20 -ErrorAction SilentlyContinue |
                ForEach-Object { Write-Info "  $_" }
        }
}

function New-DeploymentComment {
    param(
        [Parameter(Mandatory = $true)][string]$TagName,
        [string]$Commit,
        [string]$Version,
        [string]$Previous,
        [Parameter(Mandatory = $true)][datetime]$StartedAt,
        [Parameter(Mandatory = $true)][datetime]$FinishedAt,
        [bool]$Succeeded,
        [string]$ErrorMessage,
        [string]$HealthSummary
    )
    $duration = [int]($FinishedAt - $StartedAt).TotalSeconds
    $result = 'failed'
    if ($Succeeded) {
        $result = 'succeeded'
    }
    $values = @(
        "BeeBox deployment via ``deploy.ps1``: $result",
        '',
        "- Host: ``$env:COMPUTERNAME``",
        "- Tag: ``$TagName``",
        "- Commit: ``$Commit``",
        "- Version: ``$Version``",
        "- Previous HEAD: ``$Previous``",
        "- Started: $($StartedAt.ToString('yyyy-MM-dd HH:mm:ss'))",
        "- Finished: $($FinishedAt.ToString('yyyy-MM-dd HH:mm:ss')) ($duration seconds)"
    )
    if ($Succeeded) {
        $values += "- Health: $HealthSummary"
    }
    else {
        $message = $ErrorMessage
        if ($message.Length -gt 2000) {
            $message = $message.Substring(0, 2000) + '...'
        }
        $values += ''
        $values += 'Error:'
        $values += '```'
        $values += $message
        $values += '```'
    }
    return ($values -join [Environment]::NewLine)
}

function Write-IssueComment {
    param(
        [Parameter(Mandatory = $true)][int]$Number,
        [Parameter(Mandatory = $true)][string]$Body,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )
    $gh = Get-Command gh -ErrorAction SilentlyContinue
    if ($null -eq $gh) {
        Write-Warn 'gh was not found on PATH; skipping the issue writeback.'
        return
    }
    $tempFile = [System.IO.Path]::GetTempFileName()
    $pushed = $false
    try {
        [System.IO.File]::WriteAllText($tempFile, $Body, (New-Object System.Text.UTF8Encoding($false)))
        if (Test-Path -LiteralPath $WorkingDirectory -PathType Container) {
            Push-Location -LiteralPath $WorkingDirectory
            $pushed = $true
        }
        $output = Invoke-BeeBoxNativeCapture -FilePath $gh.Source `
            -Arguments @('issue', 'comment', [string]$Number, '--body-file', $tempFile)
        if ($output.ExitCode -ne 0) {
            Write-Warn "gh issue comment #$Number failed: $($output.Text)"
        }
        else {
            Write-Ok "Deployment outcome written to issue #$Number."
        }
    }
    catch {
        Write-Warn "Could not write the deployment outcome to issue #${Number}: $($_.Exception.Message)"
    }
    finally {
        if ($pushed) {
            Pop-Location
        }
        Remove-Item -LiteralPath $tempFile -Force -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# Resolve the layout and validate the request
# ---------------------------------------------------------------------------

$Root = Resolve-FullPath $Root
$AppDirectory = Join-Path $Root 'app'
$VenvDirectory = Join-Path $Root 'venv'
$SecretsDirectory = Join-Path $Root 'secrets'
$SettingsPath = Join-Path $SecretsDirectory 'settings.env'
$ServiceDirectory = Join-Path $SecretsDirectory 'winsw'
$ServiceXmlPath = Join-Path $ServiceDirectory 'BeeBoxService.xml'
$WinSwExecutable = Join-Path $ServiceDirectory 'BeeBoxService.exe'
$PythonPath = Join-Path $VenvDirectory 'Scripts\python.exe'
$LogDirectory = Join-Path $Root 'logs'

$serviceDefinition = Read-ServiceDefinition -Path $ServiceXmlPath
$ServiceName = $serviceDefinition.Name
$ApiBaseUrl = Get-ApiBaseUrl -ListenHost $serviceDefinition.Host -ListenPort $serviceDefinition.Port
$HealthUrl = $ApiBaseUrl.TrimEnd('/') + '/v1/health'

$settingsValues = [ordered]@{}
if (Test-BeeBoxPathQuiet -Path $SettingsPath -PathType Leaf) {
    $settingsValues = Read-BeeBoxEnvFile -Path $SettingsPath
}
$ApiToken = ''
if ($settingsValues.Contains('BEEBOX_API_TOKEN')) {
    $ApiToken = [string]$settingsValues['BEEBOX_API_TOKEN']
}

$startedAt = Get-Date
$queuePaused = $false
$failure = $null
$targetCommit = $null
$targetVersion = $null
$previousCommit = ''
$previousLabel = 'unknown'
$previousTag = ''
$healthSummary = ''
$serviceStopped = $false
$filesChanged = $false

Write-Step 'BeeBox deployment'
Write-Info "Root:        $Root"
Write-Info "Application: $AppDirectory"
Write-Info "Tag:         $Tag"
Write-Info "Venv:        $VenvDirectory"
Write-Info "Settings:    $SettingsPath"
Write-Info "Service:     $ServiceName"
Write-Info "API:         $ApiBaseUrl"
if ($IsDryRun) {
    Write-Warn 'DRY RUN: no HTTP calls, no git fetch, no file writes and no service control.'
}

try {
    Write-Step 'Validating the deployment request'
    if ($DrainTimeoutSeconds -lt 1) {
        throw '-DrainTimeoutSeconds must be at least 1.'
    }
    if ($HealthTimeoutSeconds -lt 1) {
        throw '-HealthTimeoutSeconds must be at least 1.'
    }
    if ($IssueNumber -lt 0) {
        throw '-IssueNumber must not be negative.'
    }
    $gitCommand = Get-Command git -ErrorAction SilentlyContinue
    if ($null -eq $gitCommand) {
        throw 'git is required on the deployment host (Windows OpenSSH sessions include it on PATH after Git for Windows is installed).'
    }
    $refCheck = Invoke-BeeBoxNativeCapture -FilePath 'git' `
        -Arguments @('check-ref-format', "refs/tags/$Tag")
    if ($refCheck.ExitCode -ne 0) {
        throw "Tag '$Tag' is not a valid git tag name."
    }
    Write-Ok "Tag name '$Tag' is valid."

    $missing = New-Object System.Collections.Generic.List[string]
    if (-not (Test-Path -LiteralPath (Join-Path $AppDirectory '.git'))) {
        $missing.Add("a git checkout at $AppDirectory")
    }
    if (-not (Test-BeeBoxPathQuiet -Path $SettingsPath -PathType Leaf)) {
        $missing.Add("settings file $SettingsPath")
    }
    if (-not (Test-BeeBoxPathQuiet -Path $ServiceXmlPath -PathType Leaf)) {
        $missing.Add("WinSW service definition $ServiceXmlPath")
    }
    if (-not (Test-BeeBoxPathQuiet -Path $WinSwExecutable -PathType Leaf)) {
        $missing.Add("WinSW executable $WinSwExecutable")
    }
    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        $missing.Add("service Python $PythonPath")
    }
    if ($missing.Count -gt 0) {
        $message = "The install layout at $Root is incomplete: missing $($missing -join '; '). Run deploy/windows/install.ps1 first."
        if ($IsDryRun) {
            Write-Warn $message
        }
        else {
            throw $message
        }
    }
    else {
        Write-Ok "Install layout at $Root is complete."
    }

    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -eq $uvCommand) {
        if ($IsDryRun) {
            Write-Warn 'uv was not found on PATH; a real deployment requires uv 0.12.x or newer.'
        }
        else {
            throw 'uv is required. Install it from https://docs.astral.sh/uv/ and rerun this script.'
        }
    }
    else {
        Write-Ok "uv: $($uvCommand.Source)"
    }

    if ($IsDryRun) {
        Write-Info "Administrator: $(Test-IsAdministrator)"
        if (-not $ApiToken) {
            Write-Warn "No BEEBOX_API_TOKEN was found in $SettingsPath; API calls would be sent without a Bearer token."
        }
    }
    else {
        if (-not (Test-IsAdministrator)) {
            throw 'This deployment must run from an elevated Windows PowerShell prompt (Run as administrator) so it can stop and start the BeeBox service.'
        }
        Write-Ok 'Running elevated.'
        if (-not $ApiToken) {
            Write-Warn "No BEEBOX_API_TOKEN was found in $SettingsPath; API calls will be sent without a Bearer token."
        }
    }

    # --- 1. pause the GPU queue -------------------------------------------
    Write-Step 'Pausing the GPU queue'
    if ($IsDryRun) {
        Write-DryRun "POST $ApiBaseUrl/v1/queue/pause with the Bearer token from $SettingsPath (only when the service is reachable)."
    }
    else {
        $healthBefore = Try-GetHealth -BaseUrl $ApiBaseUrl -Token $ApiToken
        if ($null -eq $healthBefore) {
            $service = Get-InstalledService -Name $ServiceName
            $statusText = 'not installed'
            if ($null -ne $service) {
                $statusText = $service.Status.ToString()
            }
            if ($null -ne $service -and $statusText -ne 'Stopped') {
                throw "The '$ServiceName' service is $statusText but its API at $ApiBaseUrl is not reachable. Refusing to deploy because stopping it could interrupt a running job. Check the token, logs and service state, or stop the service explicitly before running the deployment again."
            }
            Write-Warn "The API at $ApiBaseUrl is not reachable and the service is $statusText. No running job can be interrupted; continuing."
        }
        else {
            try {
                $response = Invoke-BeeBoxApi -BaseUrl $ApiBaseUrl -Token $ApiToken -Method 'POST' -Path '/v1/queue/pause'
                $queueState = $response.Content | ConvertFrom-Json
            }
            catch {
                throw "Could not pause the GPU queue: $($_.Exception.Message)"
            }
            if (-not [bool]$queueState.paused) {
                throw 'The API did not report the queue as paused after POST /v1/queue/pause.'
            }
            $queuePaused = $true
            Write-Ok "GPU queue paused (waiting jobs: $($queueState.length); current job: $($queueState.current_job_id))."
        }
    }

    # --- 2. wait for the running job --------------------------------------
    Write-Step 'Waiting for the current job to finish'
    if ($IsDryRun) {
        Write-DryRun "Poll GET $ApiBaseUrl/v1/queue until current_job_id is empty, bounded to $DrainTimeoutSeconds seconds; the job is never interrupted."
    }
    elseif ($queuePaused) {
        $drainedState = Wait-QueueDrained -BaseUrl $ApiBaseUrl -Token $ApiToken -TimeoutSeconds $DrainTimeoutSeconds
        Write-Ok "No job is running; the queue holds $($drainedState.length) waiting job(s)."
    }
    else {
        Write-Info 'The queue was not paused because the API was unreachable; there is no job to drain.'
    }

    # --- 3. prepare the tag -----------------------------------------------
    Write-Step "Preparing tag '$Tag'"
    $localTagCommit = $null
    if (Test-Path -LiteralPath (Join-Path $AppDirectory '.git')) {
        $localTagCommit = Resolve-TagCommit -WorkingDirectory $AppDirectory -TagName $Tag
    }
    if ($IsDryRun) {
        if ($localTagCommit) {
            $targetCommit = $localTagCommit
            $targetVersion = Get-VersionAtCommit -WorkingDirectory $AppDirectory -Commit $localTagCommit
            Write-DryRun "Tag '$Tag' already resolves to $localTagCommit; a real run fetches tags, requires a clean worktree in $AppDirectory and then checks out the tag."
        }
        else {
            Write-DryRun "git fetch --tags --prune --force origin (or reuse local tags), resolve refs/tags/$Tag, require a clean worktree in $AppDirectory and check out the tag."
        }
    }
    else {
        $status = Invoke-GitCapture -WorkingDirectory $AppDirectory -Arguments @('status', '--porcelain', '--untracked-files=no')
        if ($status.ExitCode -ne 0) {
            throw "Could not read the git status of $AppDirectory`: $($status.Text)"
        }
        if ($status.Text) {
            throw "The application checkout at $AppDirectory has uncommitted changes:`n$($status.Text)`nCommit, stash or discard them, then rerun the deployment."
        }
        Write-Ok 'Application checkout is clean.'

        [void](Update-GitTags -WorkingDirectory $AppDirectory)
        $targetCommit = Resolve-TagCommit -WorkingDirectory $AppDirectory -TagName $Tag
        if (-not $targetCommit) {
            throw "Tag '$Tag' was not found in $AppDirectory, not even after git fetch --tags. Publish the tag first (git tag $Tag; git push origin $Tag)."
        }
        $targetVersion = Get-VersionAtCommit -WorkingDirectory $AppDirectory -Commit $targetCommit
        $previousResult = Invoke-GitCapture -WorkingDirectory $AppDirectory -Arguments @('rev-parse', 'HEAD')
        if ($previousResult.ExitCode -eq 0) {
            $previousCommit = $previousResult.Text
            $describe = Invoke-GitCapture -WorkingDirectory $AppDirectory -Arguments @('describe', '--tags', '--always', 'HEAD')
            if ($describe.ExitCode -eq 0 -and $describe.Text) {
                $previousLabel = "$previousCommit ($($describe.Text))"
            }
            else {
                $previousLabel = $previousCommit
            }
            $exactTag = Invoke-GitCapture -WorkingDirectory $AppDirectory -Arguments @('describe', '--tags', '--exact-match', 'HEAD')
            if ($exactTag.ExitCode -eq 0 -and $exactTag.Text) {
                $previousTag = $exactTag.Text
            }
        }
        if ($previousCommit -eq $targetCommit) {
            Write-Warn "The checkout is already at tag '$Tag'; the deployment still reinstalls dependencies, migrates and restarts the service."
        }
        Write-Ok "Tag '$Tag' resolves to $targetCommit."
    }

    # --- 4. stop the service ----------------------------------------------
    Write-Step "Stopping the '$ServiceName' service"
    if ($IsDryRun) {
        Write-DryRun "& $WinSwExecutable stop and wait up to 60 s for status Stopped (files cannot be replaced under a live process)."
    }
    else {
        Stop-BeeBoxService -Name $ServiceName -WinSwExecutable $WinSwExecutable
        $serviceStopped = $true
    }

    # --- 5. check out the tag ---------------------------------------------
    Write-Step "Checking out tag '$Tag'"
    if ($IsDryRun) {
        $checkoutTarget = $localTagCommit
        if (-not $checkoutTarget) {
            $checkoutTarget = "refs/tags/$Tag"
        }
        Write-DryRun "git checkout --detach --quiet $checkoutTarget in $AppDirectory."
    }
    else {
        $checkout = Invoke-GitCapture -WorkingDirectory $AppDirectory `
            -Arguments @('checkout', '--detach', '--quiet', $targetCommit)
        if ($checkout.ExitCode -ne 0) {
            throw "git checkout of tag '$Tag' ($targetCommit) failed: $($checkout.Text)"
        }
        $headResult = Invoke-GitCapture -WorkingDirectory $AppDirectory -Arguments @('rev-parse', 'HEAD')
        if ($headResult.ExitCode -ne 0 -or $headResult.Text -ne $targetCommit) {
            throw "After checkout, HEAD is '$($headResult.Text)' instead of '$targetCommit'."
        }
        $checkoutVersion = Get-CheckoutVersion -AppDirectory $AppDirectory
        if ($checkoutVersion) {
            $targetVersion = $checkoutVersion
        }
        $filesChanged = $true
        Write-Ok "Checked out tag '$Tag' at $targetCommit (version $targetVersion)."
    }

    # --- 6. synchronise the locked environment ----------------------------
    Write-Step 'Synchronising the locked Python environment'
    if ($IsDryRun) {
        Write-DryRun "uv sync --frozen in $AppDirectory with UV_PROJECT_ENVIRONMENT=$VenvDirectory."
    }
    else {
        Invoke-NativeCommand -FilePath $uvCommand.Source `
            -Arguments @('sync', '--frozen') `
            -WorkingDirectory $AppDirectory `
            -Environment @{ UV_PROJECT_ENVIRONMENT = $VenvDirectory }
        if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
            throw "uv finished but $PythonPath does not exist."
        }
        Write-Ok 'Locked environment synchronised.'
    }

    # --- 7. database migration --------------------------------------------
    Write-Step 'Running database migrations'
    if ($IsDryRun) {
        Write-DryRun "& $PythonPath -c ""from backend.database import init_db; init_db()"" in $AppDirectory with the settings from $SettingsPath."
    }
    else {
        $migrationEnvironment = @{
            PYTHONUTF8       = '1'
            PYTHONIOENCODING = 'utf-8'
            BEEBOX_GIT_COMMIT = $targetCommit
        }
        foreach ($key in $settingsValues.Keys) {
            $migrationEnvironment[$key] = [string]$settingsValues[$key]
        }
        Invoke-NativeCommand -FilePath $PythonPath `
            -Arguments @('-c', 'from backend.database import init_db; init_db()') `
            -WorkingDirectory $AppDirectory `
            -Environment $migrationEnvironment
        Write-Ok 'Database migrations applied.'
    }

    # --- 8. record the deployed commit ------------------------------------
    Write-Step "Recording commit $targetCommit"
    if ($IsDryRun) {
        Write-DryRun "Set BEEBOX_GIT_COMMIT=$targetCommit in $SettingsPath and in $ServiceXmlPath."
    }
    else {
        $settingsValues['BEEBOX_GIT_COMMIT'] = $targetCommit
        Write-BeeBoxEnvFile -Path $SettingsPath -Values $settingsValues `
            -Header 'BeeBox machine settings. Written by deploy/windows/install.ps1 and updated by deploy/windows/deploy.ps1. Do not commit this file.'
        Set-ServiceCommitEnv -Path $ServiceXmlPath -Commit $targetCommit
        Write-Ok 'Deployed commit recorded in settings.env and the WinSW service XML.'
    }

    # --- 9. start the service ---------------------------------------------
    Write-Step "Starting the '$ServiceName' service"
    if ($IsDryRun) {
        Write-DryRun "& $WinSwExecutable start and wait up to 90 s for status Running."
    }
    else {
        Start-BeeBoxService -Name $ServiceName -WinSwExecutable $WinSwExecutable
    }

    # --- 10. verify health -------------------------------------------------
    Write-Step 'Checking service health'
    if ($IsDryRun) {
        if ($targetVersion) {
            Write-DryRun "Poll $HealthUrl until online and ready are true, commit equals $targetCommit and version equals $targetVersion (timeout ${HealthTimeoutSeconds}s)."
        }
        else {
            Write-DryRun "Poll $HealthUrl until online and ready are true, commit equals the tag's commit and version is non-empty (timeout ${HealthTimeoutSeconds}s)."
        }
    }
    else {
        try {
            $health = Wait-DeploymentHealth -BaseUrl $ApiBaseUrl -Token $ApiToken `
                -ExpectedCommit $targetCommit -ExpectedVersion $targetVersion `
                -TimeoutSeconds $HealthTimeoutSeconds
        }
        catch {
            Write-Warn $_.Exception.Message
            Show-BeeBoxFailureContext -Name $ServiceName -LogDirectory $LogDirectory
            throw
        }
        $healthSummary = "online=$($health.online), ready=$($health.ready), version=$($health.version), commit=$($health.commit)"
        Write-Ok "Health check passed: $healthSummary"
    }

    # --- 11. resume the queue ---------------------------------------------
    Write-Step 'Resuming the GPU queue'
    if ($IsDryRun) {
        Write-DryRun "POST $ApiBaseUrl/v1/queue/resume with the Bearer token from $SettingsPath."
    }
    elseif ($queuePaused) {
        if (Resume-GpuQueue -BaseUrl $ApiBaseUrl -Token $ApiToken) {
            $queuePaused = $false
        }
    }
    else {
        Write-Info 'The queue was not paused by this deployment.'
    }
}
catch {
    $failure = $_
    Write-Step 'Deployment failed'
    Write-Warn $_.Exception.Message
    if ($filesChanged) {
        if ($previousTag) {
            Write-Info "Rollback: rerun this script with -Tag $previousTag (previous HEAD was $previousLabel)."
        }
        elseif ($previousCommit) {
            Write-Info "Previous HEAD was $previousLabel. It was not exactly a tag; restore it with: git -C `"$AppDirectory`" checkout $previousCommit"
        }
        else {
            Write-Info 'No previous HEAD was recorded; the application checkout may be at a partial state.'
        }
    }
    else {
        Write-Info 'No application files were changed by this run.'
    }
    if ($serviceStopped) {
        Write-Info "The service is stopped. Inspect $LogDirectory, fix the cause, then deploy a known tag."
    }
    else {
        Write-Info 'The service was not stopped by this run.'
    }
    Write-Info 'The GPU queue is resumed automatically whenever the API is reachable.'
}
finally {
    if (-not $IsDryRun -and $queuePaused) {
        $reachable = $null -ne (Try-GetHealth -BaseUrl $ApiBaseUrl -Token $ApiToken)
        if (-not $reachable) {
            Write-Warn 'The service is not reachable; the in-memory queue pause was lost with the process. It will start unpaused once the service is back.'
        }
        else {
            $null = Resume-GpuQueue -BaseUrl $ApiBaseUrl -Token $ApiToken
        }
    }
    if ($IssueNumber -gt 0) {
        $finishedAt = Get-Date
        $succeeded = ($null -eq $failure)
        $errorMessage = ''
        if (-not $succeeded) {
            $errorMessage = $failure.Exception.Message
        }
        $body = New-DeploymentComment -TagName $Tag -Commit $targetCommit -Version $targetVersion `
            -Previous $previousLabel -StartedAt $startedAt -FinishedAt $finishedAt `
            -Succeeded $succeeded -ErrorMessage $errorMessage -HealthSummary $healthSummary
        if ($IsDryRun) {
            Write-DryRun "gh issue comment $IssueNumber --body <deployment summary with tag, commit, version, duration and result; no secrets>."
        }
        else {
            $commentDirectory = $AppDirectory
            if (-not (Test-Path -LiteralPath $commentDirectory -PathType Container)) {
                $commentDirectory = Resolve-FullPath (Join-Path $PSScriptRoot '..\..')
            }
            Write-IssueComment -Number $IssueNumber -Body $body -WorkingDirectory $commentDirectory
        }
    }
}

if ($null -ne $failure) {
    throw $failure
}

if ($IsDryRun) {
    Write-Step 'Dry run summary'
    if ($targetCommit) {
        Write-Info "Would deploy tag '$Tag' ($targetCommit, version $targetVersion) to $Root."
    }
    else {
        Write-Info "Would deploy tag '$Tag' to $Root (the commit is resolved after the tag is fetched)."
    }
    Write-Info 'No HTTP calls, no git fetch, no file writes and no service control were performed.'
    Write-Ok 'Dry run finished: nothing was changed.'
}
else {
    Write-Step 'Deployment summary'
    Write-Info "Tag:       $Tag"
    Write-Info "Commit:    $targetCommit"
    Write-Info "Version:   $targetVersion"
    Write-Info "Previous:  $previousLabel"
    Write-Info "Service:   $ServiceName"
    Write-Info "Health:    $HealthUrl ($healthSummary)"
    Write-Info "Rollback:  rerun .\deploy.ps1 -Tag <previous tag> (or inspect $LogDirectory)"
    Write-Ok "Tag '$Tag' is deployed and healthy."
}

#Requires -Version 5.1
<#
.SYNOPSIS
    First-time install of BeeBox as a Windows service.

.DESCRIPTION
    Creates the fixed-root layout below -Root (default C:\BeeBox), clones the
    application as a git checkout into <root>\app (so deploy.ps1 can switch
    tags), installs the locked Python environment with uv into <root>\venv,
    writes <root>\secrets\settings.env (including a random Bearer token, only if
    no token exists yet), registers WinSW as the BeeBox Windows service with
    automatic boot start and crash restart, starts it and polls /v1/health.

    The installer never downloads models. Point -ModelRoot at an existing model
    storage root to reuse weights; the default is <root>\models.

    WinSW is downloaded from the pinned release and verified by SHA256 unless
    -WinSwSource points at a local WinSW-x64.exe. ffmpeg/ffprobe and Deno can be
    provisioned from local archives with -FfmpegSource / -DenoSource; otherwise
    place them in <root>\tools as documented in deploy/windows/README.md.

.PARAMETER Root
    Fixed install root. Default C:\BeeBox.

.PARAMETER AppSource
    Repository checkout used to validate the install and, unless -GitRemote is
    given, as the default clone source. Default the repository containing this
    script.

.PARAMETER GitRemote
    Git URL or local path cloned into <Root>\app. Default the origin URL of
    -AppSource (which makes later tag fetches work), falling back to -AppSource
    itself. Use a local mirror or a GitHub URL on the production host.

.PARAMETER Tag
    Optional tag to check out after cloning. Without it the clone stays on the
    remote's default branch. Later upgrades use deploy.ps1.

.PARAMETER ModelRoot
    Model storage root (weights end up in <ModelRoot>\models). Default <Root>\models.

.PARAMETER Port
    HTTP port. Default 17494.

.PARAMETER BindAddress
    Listen address passed to backend.server. Default 0.0.0.0 (LAN/Tailscale plus
    loopback; the generated Bearer token protects the API).

.PARAMETER ServiceName
    Windows service name. Default BeeBox.

.PARAMETER SettingsPath
    Machine settings file. Default <Root>\secrets\settings.env.

.PARAMETER WinSwSource
    Local WinSW-x64.exe (v2.12.0) to use instead of downloading it.

.PARAMETER FfmpegSource
    Directory or .zip containing ffmpeg.exe and ffprobe.exe, copied to <Root>\tools\ffmpeg.

.PARAMETER DenoSource
    Directory or .zip containing deno.exe, copied to <Root>\tools\deno.

.PARAMETER HealthTimeoutSeconds
    How long to wait for /v1/health after starting the service. Default 180.

.PARAMETER Force
    Replace tool directories from -FfmpegSource / -DenoSource when they exist.

.PARAMETER DryRun
    Print every step without creating directories, copying files, writing the
    token, downloading WinSW or touching the service. Exits 0.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\deploy\windows\install.ps1

.EXAMPLE
    .\deploy\windows\install.ps1 -Root D:\BeeBox -ModelRoot E:\beebox-models -Port 17494

.EXAMPLE
    .\deploy\windows\install.ps1 -GitRemote https://github.com/HUA1255/beebox.git -Tag v1.0.1

.EXAMPLE
    .\deploy\windows\install.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [string]$Root = 'C:\BeeBox',
    [string]$AppSource,
    [string]$GitRemote,
    [string]$Tag,
    [string]$ModelRoot,
    [int]$Port = 17494,
    [string]$BindAddress = '0.0.0.0',
    [string]$ServiceName = 'BeeBox',
    [string]$SettingsPath,
    [string]$WinSwSource,
    [string]$FfmpegSource,
    [string]$DenoSource,
    [int]$HealthTimeoutSeconds = 180,
    [switch]$Force,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'BeeBox.Common.psm1') -Force

$WinSwVersion = '2.12.0'
$WinSwDownloadUrl = 'https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe'
$WinSwSha256 = '05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da'
$IsDryRun = [bool]$DryRun

function Assert-AppSource {
    param([string]$Path)
    foreach ($required in @('pyproject.toml', 'uv.lock', 'backend\server.py')) {
        if (-not (Test-Path -LiteralPath (Join-Path $Path $required) -PathType Leaf)) {
            throw "AppSource '$Path' does not look like a BeeBox checkout (missing $required)."
        }
    }
}

function Assert-SafeRoot {
    param([string]$InstallRoot, [string]$Source)
    $trimmed = $InstallRoot.TrimEnd('\')
    $driveRoot = [System.IO.Path]::GetPathRoot($InstallRoot).TrimEnd('\')
    if ($trimmed -eq $driveRoot) {
        throw "Refusing to install into the drive root '$InstallRoot'. Choose a dedicated directory."
    }
    if ($Source -eq $InstallRoot -or $Source.StartsWith($InstallRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "AppSource '$Source' must not live inside the install root '$InstallRoot'; otherwise copying the app recurses into itself."
    }
    if ($InstallRoot.StartsWith($Source + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "The install root '$InstallRoot' must not live inside AppSource '$Source'."
    }
}

function Resolve-GitRemote {
    param([string]$Source, [string]$Explicit)
    if ($Explicit) {
        return $Explicit
    }
    $origin = & git -C $Source config --get remote.origin.url 2>$null
    if ($LASTEXITCODE -eq 0 -and $origin) {
        return (@($origin)[0]).Trim()
    }
    Write-Warn "No origin remote was found in $Source; using the local path as the clone source."
    return $Source
}

function Install-AppCheckout {
    param(
        [string]$Remote,
        [string]$Destination,
        [string]$TagName,
        [switch]$DryRun
    )

    if (Test-Path -LiteralPath $Destination -PathType Container) {
        if (Test-Path -LiteralPath (Join-Path $Destination '.git')) {
            if ($DryRun) {
                Write-DryRun "Fetch tags in the existing checkout $Destination."
                if ($TagName) {
                    Write-DryRun "git -C `"$Destination`" checkout --detach `"refs/tags/$TagName`"."
                }
                return
            }
            $fetch = Invoke-BeeBoxNativeCapture -FilePath 'git' `
                -Arguments @('-C', $Destination, 'fetch', '--tags', '--prune', '--force', 'origin')
            if ($fetch.ExitCode -ne 0) {
                Write-Warn "git fetch failed in the existing checkout: $($fetch.Text). Keeping the current commit."
            }
            if ($TagName) {
                & git -C $Destination checkout --detach --quiet "refs/tags/$TagName" | Out-Null
                if ($LASTEXITCODE -ne 0) {
                    throw "Tag '$TagName' was not found in the existing checkout at $Destination."
                }
            }
            return
        }
        $entries = @(Get-ChildItem -LiteralPath $Destination -Force -ErrorAction SilentlyContinue)
        if ($entries.Count -gt 0) {
            throw "App directory '$Destination' exists but is not a git checkout. Move it out of the way (for example to '$Destination.old') and rerun; deployments need a checkout to switch tags."
        }
    }

    if ($DryRun) {
        Write-DryRun "git clone `"$Remote`" `"$Destination`" (a full checkout so deploy.ps1 can switch tags)."
        if ($TagName) {
            Write-DryRun "git -C `"$Destination`" checkout --detach `"refs/tags/$TagName`"."
        }
        return
    }

    & git clone --quiet $Remote $Destination
    if ($LASTEXITCODE -ne 0) {
        throw "git clone from '$Remote' failed with exit code $LASTEXITCODE."
    }
    if ($TagName) {
        & git -C $Destination checkout --detach --quiet "refs/tags/$TagName" | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Tag '$TagName' was not found in '$Remote'."
        }
    }
}

function Copy-ToolSource {
    param([string]$Source, [string]$Destination)
    if (-not (Test-Path -LiteralPath $Source)) {
        throw "Tool source '$Source' does not exist."
    }
    if (Test-Path -LiteralPath $Destination) {
        if (-not $Force) {
            Write-Warn "Tools already exist in $Destination; pass -Force to replace them."
            return
        }
        Remove-Item -LiteralPath $Destination -Recurse -Force
    }
    if (Test-Path -LiteralPath $Source -PathType Container) {
        New-Item -ItemType Directory -Force -Path $Destination | Out-Null
        Copy-Item -Path (Join-Path $Source '*') -Destination $Destination -Recurse -Force
        return
    }
    if ([System.IO.Path]::GetExtension($Source) -ieq '.zip') {
        New-Item -ItemType Directory -Force -Path $Destination | Out-Null
        Expand-Archive -LiteralPath $Source -DestinationPath $Destination -Force
        return
    }
    throw "Tool source '$Source' must be a directory or a .zip archive."
}

function Find-ToolExecutable {
    param([string]$Directory, [string]$Name)
    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) {
        return $null
    }
    $match = Get-ChildItem -LiteralPath $Directory -Recurse -File -Filter "$Name.exe" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $match) {
        return $null
    }
    return $match.FullName
}

function Resolve-ToolPaths {
    param([string]$ToolsDirectory, [string]$FfmpegSourcePath, [string]$DenoSourcePath)

    $ffmpegDirectory = Join-Path $ToolsDirectory 'ffmpeg'
    $denoDirectory = Join-Path $ToolsDirectory 'deno'

    if ($FfmpegSourcePath) {
        if ($IsDryRun) {
            Write-DryRun "Copy ffmpeg/ffprobe from $FfmpegSourcePath to $ffmpegDirectory."
        }
        elseif ((Test-Path -LiteralPath (Join-Path $ffmpegDirectory 'bin\ffmpeg.exe')) -and -not $Force) {
            Write-Info "ffmpeg already present in $ffmpegDirectory; keeping it (pass -Force to replace)."
        }
        else {
            Copy-ToolSource -Source $FfmpegSourcePath -Destination $ffmpegDirectory
        }
    }
    if ($DenoSourcePath) {
        if ($IsDryRun) {
            Write-DryRun "Copy Deno from $DenoSourcePath to $denoDirectory."
        }
        elseif ((Test-Path -LiteralPath (Join-Path $denoDirectory 'deno.exe')) -and -not $Force) {
            Write-Info "Deno already present in $denoDirectory; keeping it (pass -Force to replace)."
        }
        else {
            Copy-ToolSource -Source $DenoSourcePath -Destination $denoDirectory
        }
    }

    $ffmpeg = Find-ToolExecutable -Directory $ffmpegDirectory -Name 'ffmpeg'
    $ffprobe = Find-ToolExecutable -Directory $ffmpegDirectory -Name 'ffprobe'
    $deno = Find-ToolExecutable -Directory $denoDirectory -Name 'deno'

    if ($null -eq $ffmpeg) {
        $ffmpeg = Join-Path $ffmpegDirectory 'bin\ffmpeg.exe'
        Write-Warn "ffmpeg.exe was not found below $ffmpegDirectory. The service starts, but transcription stays unavailable until ffmpeg is installed there (no winget path is used)."
    }
    if ($null -eq $ffprobe) {
        $ffprobe = Join-Path $ffmpegDirectory 'bin\ffprobe.exe'
        Write-Warn "ffprobe.exe was not found below $ffmpegDirectory."
    }
    if ($null -eq $deno) {
        $deno = Join-Path $denoDirectory 'deno.exe'
        Write-Warn "deno.exe was not found below $denoDirectory. Video downloads that need a JavaScript runtime will fail until it is installed there."
    }

    return @{
        Ffmpeg = $ffmpeg
        Ffprobe = $ffprobe
        Deno = $deno
    }
}

function Get-WinSwExecutable {
    param([string]$ServiceDirectory, [string]$LocalSource)
    $target = Join-Path $ServiceDirectory 'BeeBoxService.exe'

    if ($LocalSource) {
        $resolved = Resolve-FullPath $LocalSource
        if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
            throw "WinSwSource '$resolved' does not exist."
        }
        if ($IsDryRun) {
            Write-DryRun "Copy WinSW from $resolved to $target."
            return $target
        }
        Copy-Item -LiteralPath $resolved -Destination $target -Force
        return $target
    }

    if ((Test-Path -LiteralPath $target -PathType Leaf) -and
        ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -eq $WinSwSha256)) {
        Write-Ok "WinSW $WinSwVersion already present (SHA256 verified)."
        return $target
    }

    if ($IsDryRun) {
        Write-DryRun "Download WinSW $WinSwVersion from $WinSwDownloadUrl and verify SHA256 $WinSwSha256."
        return $target
    }

    Write-Step "Downloading WinSW $WinSwVersion"
    $download = Join-Path ([System.IO.Path]::GetTempPath()) ("beebox-winsw-" + [Guid]::NewGuid().ToString('N') + ".exe")
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $WinSwDownloadUrl -OutFile $download -UseBasicParsing
        $hash = (Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash
        if ($hash -ne $WinSwSha256) {
            throw "WinSW download SHA256 mismatch: expected $WinSwSha256, got $hash. Refusing to install."
        }
        New-Item -ItemType Directory -Force -Path $ServiceDirectory | Out-Null
        Move-Item -LiteralPath $download -Destination $target -Force
    }
    finally {
        Remove-Item -LiteralPath $download -Force -ErrorAction SilentlyContinue
    }
    Write-Ok "WinSW $WinSwVersion stored at $target."
    return $target
}

function Install-WindowsService {
    param([string]$WinSwExecutable, [string]$Name)

    $existing = Get-InstalledService -Name $Name
    if ($null -ne $existing) {
        Write-Step "Replacing the existing '$Name' service"
        if ($existing.Status.ToString() -ne 'Stopped') {
            & $WinSwExecutable stop | Out-Null
            if (-not (Wait-ServiceState -Name $Name -State 'Stopped' -TimeoutSeconds 45)) {
                Write-Warn "Service '$Name' did not stop within 45 seconds; continuing with uninstall."
            }
        }
        & $WinSwExecutable uninstall | Out-Null
        if ($LASTEXITCODE -ne 0) {
            & sc.exe delete $Name | Out-Null
        }
        if (-not (Wait-ServiceState -Name $Name -State 'Missing' -TimeoutSeconds 30)) {
            throw "Could not remove the existing '$Name' service."
        }
    }

    Write-Step "Registering the '$Name' Windows service"
    & $WinSwExecutable install | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "WinSW could not install the '$Name' service (exit code $LASTEXITCODE)."
    }
    # Automatic start at boot without an interactive login; LocalSystem account.
    & sc.exe config $Name 'start=' 'auto' | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "sc.exe could not force start=auto; WinSW already configured Automatic start in the XML."
    }

    Write-Step "Starting the '$Name' service"
    & $WinSwExecutable start | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "WinSW could not start the '$Name' service (exit code $LASTEXITCODE)."
    }
}

function Wait-BeeBoxHealth {
    param([string]$Url, [string]$Token, [int]$TimeoutSeconds)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastError = 'no response yet'
    while ((Get-Date) -lt $deadline) {
        try {
            $headers = @{ Authorization = "Bearer $Token" }
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -Headers $headers -TimeoutSec 5
            if ($response.StatusCode -eq 200) {
                return ($response.Content | ConvertFrom-Json)
            }
            $lastError = "HTTP $($response.StatusCode)"
        }
        catch {
            $lastError = $_.Exception.Message
        }
        Start-Sleep -Seconds 2
    }
    throw "Health check failed for $Url within $TimeoutSeconds seconds. Last error: $lastError"
}

function Show-FailureContext {
    param([string]$Name, [string]$LogDirectory)
    & sc.exe query $Name
    if (Test-Path -LiteralPath $LogDirectory -PathType Container) {
        Get-ChildItem -LiteralPath $LogDirectory -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 5 |
            ForEach-Object {
                Write-Warn "Log file: $($_.FullName)"
                Get-Content -LiteralPath $_.FullName -Tail 20 -ErrorAction SilentlyContinue |
                    ForEach-Object { Write-Info "  $_" }
            }
    }
}

# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

$Root = Resolve-FullPath $Root
if (-not $AppSource) {
    $AppSource = Resolve-FullPath (Join-Path $PSScriptRoot '..\..')
}
else {
    $AppSource = Resolve-FullPath $AppSource
}
if (-not $ModelRoot) {
    $ModelRoot = Join-Path $Root 'models'
}
$ModelRoot = Resolve-FullPath $ModelRoot
if (-not $SettingsPath) {
    $SettingsPath = Join-Path $Root 'secrets\settings.env'
}
$SettingsPath = Resolve-FullPath $SettingsPath
$GitRemote = Resolve-GitRemote -Source $AppSource -Explicit $GitRemote

$AppDirectory = Join-Path $Root 'app'
$ConsoleDist = Join-Path $AppDirectory 'console\dist'
$VenvDirectory = Join-Path $Root 'venv'
$DataDirectory = Join-Path $Root 'data'
$SecretsDirectory = Join-Path $Root 'secrets'
$LogDirectory = Join-Path $Root 'logs'
$ToolsDirectory = Join-Path $Root 'tools'
$ServiceDirectory = Join-Path $SecretsDirectory 'winsw'
$ServiceXmlPath = Join-Path $ServiceDirectory 'BeeBoxService.xml'
$PythonPath = Join-Path $VenvDirectory 'Scripts\python.exe'
$appCommit = ''

Write-Step 'BeeBox Windows installer'
Write-Info "Root:        $Root"
Write-Info "AppSource:   $AppSource"
Write-Info "GitRemote:   $GitRemote"
if ($Tag) {
    Write-Info "Tag:         $Tag"
}
Write-Info "ModelRoot:   $ModelRoot"
Write-Info "Data:        $DataDirectory"
Write-Info "Logs:        $LogDirectory"
Write-Info "Tools:       $ToolsDirectory"
Write-Info "Settings:    $SettingsPath"
Write-Info "Service:     $ServiceName (port $Port, bind $BindAddress)"
if ($IsDryRun) {
    Write-Warn 'DRY RUN: no directories, files, downloads or services will be changed.'
}

Assert-AppSource -Path $AppSource
Assert-SafeRoot -InstallRoot $Root -Source $AppSource

Write-Step 'Checking prerequisites'
$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
if ($null -eq $uvCommand) {
    if ($IsDryRun) {
        Write-Warn 'uv was not found on PATH. A real install requires uv 0.12.x or newer.'
    }
    else {
        throw 'uv is required. Install it from https://docs.astral.sh/uv/ and rerun this script.'
    }
}
else {
    Write-Ok "uv: $($uvCommand.Source)"
}
$gitCommand = Get-Command git -ErrorAction SilentlyContinue
if ($null -eq $gitCommand) {
    if ($IsDryRun) {
        Write-Warn 'git was not found on PATH. A real install requires Git for Windows so tag deployment can check out tags.'
    }
    else {
        throw 'git is required. Install Git for Windows (https://git-scm.com/download/win) and rerun this script.'
    }
}
else {
    Write-Ok "git: $($gitCommand.Source)"
}
if ($IsDryRun) {
    Write-Info "Administrator: $(Test-IsAdministrator)"
}
else {
    Assert-Administrator
    Write-Ok 'Running elevated.'
}

$modelWeights = Join-Path $ModelRoot 'models'
if (Test-Path -LiteralPath $modelWeights -PathType Container) {
    $modelCount = @(Get-ChildItem -LiteralPath $modelWeights -Directory -ErrorAction SilentlyContinue).Count
    Write-Ok "Reusing existing model storage root '$ModelRoot' ($modelCount model directories). Nothing is downloaded."
}
else {
    Write-Warn "Model storage root '$ModelRoot' has no models directory yet; the installer will create it empty and never download weights."
}

Write-Step 'Creating the fixed-root layout'
$directories = @($Root, $AppDirectory, $VenvDirectory, $ModelRoot, $DataDirectory, $SecretsDirectory, $LogDirectory, $ToolsDirectory)
foreach ($directory in $directories) {
    if ($IsDryRun) {
        Write-DryRun "Create directory $directory"
    }
    else {
        New-Item -ItemType Directory -Force -Path $directory | Out-Null
    }
}

Write-Step 'Stopping a previously installed service'
if ($IsDryRun) {
    Write-DryRun "Stop the existing '$ServiceName' service before replacing application files."
}
else {
    $existingService = Get-InstalledService -Name $ServiceName
    if ($null -eq $existingService) {
        Write-Info "No existing '$ServiceName' service."
    }
    elseif ($existingService.Status.ToString() -eq 'Stopped') {
        Write-Ok "Existing '$ServiceName' service is already stopped."
    }
    else {
        & sc.exe stop $ServiceName | Out-Null
        if (-not (Wait-ServiceState -Name $ServiceName -State 'Stopped' -TimeoutSeconds 60)) {
            throw "The existing '$ServiceName' service did not stop within 60 seconds; refusing to replace locked files."
        }
        Write-Ok "Existing '$ServiceName' service stopped."
    }
}

Write-Step 'Installing the application checkout'
if ($IsDryRun) {
    Install-AppCheckout -Remote $GitRemote -Destination $AppDirectory -TagName $Tag -DryRun
    if (-not $Tag) {
        $sourceCommit = & git -C $AppSource rev-parse HEAD 2>$null
        if ($LASTEXITCODE -eq 0 -and $sourceCommit) {
            $appCommit = (@($sourceCommit)[0]).Trim()
        }
    }
    Write-DryRun "The generated settings record BEEBOX_GIT_COMMIT for the checked-out commit."
    Write-DryRun "Serve the committed console build from $ConsoleDist (no Node is required on this host)."
}
else {
    Install-AppCheckout -Remote $GitRemote -Destination $AppDirectory -TagName $Tag
    $appCommit = (& git -C $AppDirectory rev-parse HEAD 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $appCommit) {
        throw "Could not resolve the application commit in $AppDirectory after cloning."
    }
    $appCommit = (@($appCommit)[0]).Trim()
    Write-Ok "Application checkout at $AppDirectory (commit $appCommit)."
    if (-not (Test-Path -LiteralPath (Join-Path $ConsoleDist 'index.html') -PathType Leaf)) {
        throw "The deployed checkout has no committed console build at '$ConsoleDist'. Rebuild and commit console/dist on a development machine; Node is not required on this host."
    }
    Write-Ok "Console build found at $ConsoleDist."
}

Write-Step 'Installing the locked Python environment'
if ($IsDryRun) {
    Write-DryRun "uv sync --frozen in $AppDirectory with UV_PROJECT_ENVIRONMENT=$VenvDirectory."
}
else {
    Invoke-NativeCommand -FilePath $uvCommand.Source `
        -Arguments @('sync', '--frozen') `
        -WorkingDirectory $AppDirectory `
        -Environment @{ UV_PROJECT_ENVIRONMENT = $VenvDirectory }
    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        throw "uv finished but $PythonPath was not created."
    }
    Write-Ok "Locked environment installed in $VenvDirectory."
}

Write-Step 'Preparing external tools'
$toolPaths = Resolve-ToolPaths -ToolsDirectory $ToolsDirectory -FfmpegSourcePath $FfmpegSource -DenoSourcePath $DenoSource

Write-Step 'Preparing machine settings and the Bearer token'
$existingSettings = Read-BeeBoxEnvFile -Path $SettingsPath
$settings = [ordered]@{}
foreach ($key in $existingSettings.Keys) {
    $settings[$key] = $existingSettings[$key]
}
$managed = [ordered]@{
    'BEEBOX_ROOT'                  = $Root
    'BEEBOX_DATA_DIR'              = $DataDirectory
    'BEEBOX_MODEL_STORAGE_ROOT'    = $ModelRoot
    'BEEBOX_LOG_DIR'               = $LogDirectory
    'BEEBOX_LOG_MAX_BYTES'         = '10485760'
    'BEEBOX_LOG_BACKUP_COUNT'      = '5'
    'BEEBOX_TOOLS_DIR'             = $ToolsDirectory
    'BEEBOX_FFMPEG_PATH'           = $toolPaths.Ffmpeg
    'BEEBOX_FFPROBE_PATH'          = $toolPaths.Ffprobe
    'BEEBOX_DENO_PATH'             = $toolPaths.Deno
    'BEEBOX_GIT_COMMIT'            = $appCommit
    'BEEBOX_CONSOLE_DIR'           = $ConsoleDist
}
foreach ($key in $managed.Keys) {
    $settings[$key] = $managed[$key]
}
$tokenWasPresent = $settings.Contains('BEEBOX_API_TOKEN') -and -not [string]::IsNullOrWhiteSpace([string]$settings['BEEBOX_API_TOKEN'])
if (-not $tokenWasPresent) {
    $settings['BEEBOX_API_TOKEN'] = New-BeeBoxToken
}
if ($IsDryRun) {
    if ($tokenWasPresent) {
        Write-DryRun "Preserve the existing Bearer token in $SettingsPath."
    }
    else {
        Write-DryRun "Generate a random Bearer token and write $SettingsPath (never committed)."
    }
    Write-DryRun "Restrict ACL on $SecretsDirectory to SYSTEM and Administrators."
}
else {
    if (-not (Test-Path -LiteralPath $SecretsDirectory -PathType Container)) {
        New-Item -ItemType Directory -Force -Path $SecretsDirectory | Out-Null
    }
    Write-BeeBoxEnvFile -Path $SettingsPath -Values $settings
    Set-PrivateDirectoryAcl -Path $SecretsDirectory
    if ($tokenWasPresent) {
        Write-Ok "Kept the existing Bearer token in $SettingsPath."
    }
    else {
        Write-Ok "Generated a new Bearer token in $SettingsPath."
    }
}

Write-Step 'Configuring the WinSW service'
$winSwExecutable = Get-WinSwExecutable -ServiceDirectory $ServiceDirectory -LocalSource $WinSwSource
$serverArguments = "-m backend.server --host $BindAddress --port $Port"
if ($IsDryRun) {
    Write-DryRun "Write WinSW XML $ServiceXmlPath (LocalSystem, startmode Automatic, restart after 5 s, resetfailure 1 hour, logs in $LogDirectory)."
    Write-DryRun "Register service '$ServiceName' pointing at $PythonPath and start it."
}
else {
    Write-BeeBoxServiceXml -TemplatePath (Join-Path $PSScriptRoot 'BeeBoxService.xml.template') `
        -Destination $ServiceXmlPath `
        -SettingsValues $settings `
        -PythonPath $PythonPath `
        -Arguments $serverArguments `
        -WorkingDirectory $AppDirectory `
        -LogDirectory $LogDirectory `
        -ServiceId $ServiceName `
        -DisplayName $ServiceName
    Set-PrivateDirectoryAcl -Path $ServiceDirectory
    Install-WindowsService -WinSwExecutable $winSwExecutable -Name $ServiceName
}

$healthHost = $BindAddress
if ($BindAddress -eq '0.0.0.0' -or $BindAddress -eq '::' -or $BindAddress -eq '*') {
    $healthHost = '127.0.0.1'
}
$healthUrl = "http://${healthHost}:$Port/v1/health"

Write-Step 'Checking service health'
if ($IsDryRun) {
    Write-DryRun "Poll $healthUrl until it reports 200 (timeout ${HealthTimeoutSeconds}s)."
}
else {
    try {
        $health = Wait-BeeBoxHealth -Url $healthUrl -Token $settings['BEEBOX_API_TOKEN'] -TimeoutSeconds $HealthTimeoutSeconds
    }
    catch {
        Write-Warn $_.Exception.Message
        Show-FailureContext -Name $ServiceName -LogDirectory $LogDirectory
        throw
    }
    Write-Ok "Health check passed: $($health | ConvertTo-Json -Compress -Depth 5)"
}

Write-Step 'Installation summary'
Write-Info "Root:      $Root"
if ($appCommit) {
    Write-Info "App:       $AppDirectory (git checkout, commit $appCommit)"
}
else {
    Write-Info "App:       $AppDirectory (git checkout; commit assigned during a real install)"
}
Write-Info "Venv:      $VenvDirectory"
Write-Info "Models:    $ModelRoot (weights in $modelWeights; reused, never downloaded)"
Write-Info "Data:      $DataDirectory"
Write-Info "Logs:      $LogDirectory (UTF-8, size-rotated)"
Write-Info "Tools:     $ToolsDirectory"
Write-Info "Settings:  $SettingsPath (contains the Bearer token; SYSTEM/Administrators only)"
Write-Info "Service:   $ServiceName"
Write-Info "Health:    $healthUrl"
Write-Info "Manage:    $winSwExecutable {status|start|stop|restart|uninstall}"
Write-Info "Deploy:    .\deploy\windows\deploy.ps1 -Tag <tag> -Root $Root"
Write-Info "Token:     read it from $SettingsPath in an elevated shell; it is never printed by this script."
if ($IsDryRun) {
    Write-Ok 'Dry run finished: nothing was changed.'
}
else {
    Write-Ok 'BeeBox is installed and running as a Windows service.'
}

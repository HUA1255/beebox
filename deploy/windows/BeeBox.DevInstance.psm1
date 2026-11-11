# Shared helpers for the BeeBox development instance (ticket #55 / ADR-0010).
# Windows PowerShell 5.1 and PowerShell 7 compatible: no ternary operator,
# no null-coalescing operator, no Get-Content -AsHashtable.
#
# The dev instance runs from the same checkout as the production application,
# on port 17495, with its own root (data, logs, token) while the production
# model directory is referenced read-only. Everything here is either a pure
# path calculation or an explicit, named side effect; no production service is
# started, stopped or reconfigured.

Set-StrictMode -Version 2.0

Import-Module (Join-Path $PSScriptRoot 'BeeBox.Common.psm1') -Force

$script:DevInstancePort = 17495
$script:ProductionPort = 17494

function Get-BeeBoxDevInstancePort {
    return $script:DevInstancePort
}

function Get-BeeBoxProductionPort {
    return $script:ProductionPort
}

function Get-BeeBoxDevLayout {
    <#
    .SYNOPSIS
        Resolve every path the dev instance uses. Pure path arithmetic.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$DevRoot,
        [Parameter(Mandatory = $true)][string]$ProdRoot,
        [Parameter(Mandatory = $true)][string]$Checkout,
        [string]$PythonPath,
        [string]$ModelRoot,
        [int]$Port = 17495
    )

    $resolvedDev = Resolve-FullPath $DevRoot
    $resolvedProd = Resolve-FullPath $ProdRoot
    $resolvedCheckout = Resolve-FullPath $Checkout
    if (-not $PythonPath) {
        $PythonPath = Join-Path $resolvedCheckout '.venv\Scripts\python.exe'
    }
    $prodSettingsPath = Join-Path $resolvedProd 'secrets\settings.env'
    # Production may store weights outside <ProdRoot> (install.ps1 -ModelRoot).
    # This is the authoritative production model directory; the isolation guard
    # compares the dev model root against it, never against itself.
    $prodModelsDir = Join-Path $resolvedProd 'models'
    $configured = $null
    try {
        if (Test-BeeBoxPathQuiet -Path $prodSettingsPath -PathType Leaf) {
            $configured = Get-BeeBoxEnvValue -Path $prodSettingsPath -Name 'BEEBOX_MODEL_STORAGE_ROOT'
        }
    }
    catch {
        $configured = $null
    }
    if (-not [string]::IsNullOrWhiteSpace($configured)) {
        $prodModelsDir = $configured
        Write-Info "Production model root from settings: $prodModelsDir"
    }
    elseif (-not $ModelRoot) {
        if (Test-BeeBoxPathQuiet -Path $prodSettingsPath -PathType Leaf) {
            Write-Warn "Could not read BEEBOX_MODEL_STORAGE_ROOT from '$prodSettingsPath' (elevated prompt required); assuming '$prodModelsDir'. When production uses a custom model root, run from an elevated prompt so the isolation guard can compare against it."
        }
    }
    if (-not $ModelRoot) {
        $ModelRoot = $prodModelsDir
    }

    $layout = [ordered]@{
        DevRoot          = $resolvedDev
        ProdRoot         = $resolvedProd
        Checkout         = $resolvedCheckout
        PythonPath       = (Resolve-FullPath $PythonPath)
        Port             = $Port
        Url              = "http://127.0.0.1:$Port"
        DataDir          = (Join-Path $resolvedDev 'data')
        SecretsDir       = (Join-Path $resolvedDev 'secrets')
        SettingsPath     = (Join-Path $resolvedDev 'secrets\settings.env')
        LogDir           = (Join-Path $resolvedDev 'logs')
        ModelCacheRoot   = (Join-Path $resolvedDev 'model-cache')
        ModelRoot        = (Resolve-FullPath $ModelRoot)
        ModelAclBackup   = (Join-Path $resolvedDev 'secrets\model-acl-backup.txt')
        ProdDataDir      = (Join-Path $resolvedProd 'data')
        ProdModelsDir    = (Resolve-FullPath $prodModelsDir)
        ProdSettingsPath = $prodSettingsPath
        ToolsDir         = (Join-Path $resolvedProd 'tools')
        PidFile          = (Join-Path $resolvedDev 'dev-instance.pid')
        ServerOutLog     = (Join-Path $resolvedDev 'logs\dev-instance.out.log')
        ServerErrorLog   = (Join-Path $resolvedDev 'logs\dev-instance.err.log')
    }
    return $layout
}

function Test-BeeBoxPathWithin {
    <#
    .SYNOPSIS
        True when $Path equals $Parent or lives below it (case-insensitive).
    .DESCRIPTION
        Uses a separator boundary so C:\BeeBoxDev is not treated as living
        inside C:\BeeBox.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Parent
    )
    $resolvedPath = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
    $resolvedParent = [System.IO.Path]::GetFullPath($Parent).TrimEnd('\')
    if ($resolvedPath.Equals($resolvedParent, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    return $resolvedPath.StartsWith($resolvedParent + '\', [System.StringComparison]::OrdinalIgnoreCase)
}

function Assert-BeeBoxDevIsolation {
    <#
    .SYNOPSIS
        Refuse a dev layout that overlaps production (ADR-0010).
    .DESCRIPTION
        Only the production models directory may be referenced from inside the
        production root, and only read-only. Data, logs, caches and the dev
        root itself must stay outside production; the two instances must use
        different ports. Pure path checks: nothing on disk is created.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$DevRoot,
        [Parameter(Mandatory = $true)][string]$ProdRoot,
        [Parameter(Mandatory = $true)][string]$DataDir,
        [string]$ModelRoot,
        [string]$ProdModelRoot,
        [string]$ModelCacheRoot,
        [int]$Port = 17495,
        [int]$ProdPort = 17494
    )

    $dev = Resolve-FullPath $DevRoot
    $prod = Resolve-FullPath $ProdRoot
    $prodData = Join-Path $prod 'data'
    $prodModels = Join-Path $prod 'models'
    if ($ProdModelRoot) {
        $prodModels = Resolve-FullPath $ProdModelRoot
    }
    $devData = Resolve-FullPath $DataDir

    if (Test-BeeBoxPathWithin -Path $dev -Parent $prod) {
        throw "The dev root '$dev' must not live inside the production root '$prod'."
    }
    if (Test-BeeBoxPathWithin -Path $prod -Parent $dev) {
        throw "The production root '$prod' must not live inside the dev root '$dev'."
    }
    if (Test-BeeBoxPathWithin -Path $devData -Parent $prod) {
        throw "The dev data directory '$devData' must not live inside the production root '$prod' (production data is '$prodData')."
    }
    if (-not (Test-BeeBoxPathWithin -Path $devData -Parent $dev)) {
        throw "The dev data directory '$devData' must live below the dev root '$dev' so a single directory holds every dev write."
    }
    if ($ModelRoot) {
        $resolvedModel = Resolve-FullPath $ModelRoot
        $sharedModels = Resolve-FullPath $prodModels
        foreach ($forbidden in @($prodData, (Join-Path $prod 'secrets'))) {
            if (Test-BeeBoxPathWithin -Path $resolvedModel -Parent $forbidden) {
                throw "The dev model root '$resolvedModel' must not live inside production data or secrets ('$forbidden')."
            }
        }
        $modelIsShared = $resolvedModel.Equals($sharedModels, [System.StringComparison]::OrdinalIgnoreCase)
        if ((Test-BeeBoxPathWithin -Path $resolvedModel -Parent $prod) -and -not $modelIsShared) {
            throw "The dev model root '$resolvedModel' must be '$sharedModels' (read-only shared) or outside the production root '$prod'."
        }
    }
    if ($ModelCacheRoot) {
        $resolvedCache = Resolve-FullPath $ModelCacheRoot
        if (Test-BeeBoxPathWithin -Path $resolvedCache -Parent $prod) {
            throw "The dev model cache '$resolvedCache' must stay outside the production root '$prod'; framework caches are written at runtime."
        }
    }
    if ($Port -eq $ProdPort) {
        throw "The dev port $Port must differ from the production port $ProdPort."
    }
    if ($Port -lt 1024 -or $Port -gt 65535) {
        throw "The dev port $Port must be between 1024 and 65535."
    }
}

function Test-BeeBoxFileReadable {
    param([Parameter(Mandatory = $true)][string]$Path)
    try {
        $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        $stream.Dispose()
        return $true
    }
    catch {
        return $false
    }
}

function Get-BeeBoxModelSampleFile {
    <#
    .SYNOPSIS
        Return one file below the model root for an access probe, or $null.
    #>
    param([Parameter(Mandatory = $true)][string]$ModelRoot)
    $modelsDirectory = Join-Path $ModelRoot 'models'
    if (Test-Path -LiteralPath $modelsDirectory -PathType Container) {
        $firstModel = Get-ChildItem -LiteralPath $modelsDirectory -Directory -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $firstModel) {
            $sample = Get-ChildItem -LiteralPath $firstModel.FullName -File -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($null -ne $sample) {
                return $sample
            }
        }
    }
    if (Test-Path -LiteralPath $ModelRoot -PathType Container) {
        return (Get-ChildItem -LiteralPath $ModelRoot -File -ErrorAction SilentlyContinue | Select-Object -First 1)
    }
    return $null
}

function Assert-BeeBoxSharedModelsReadOnly {
    <#
    .SYNOPSIS
        Fail when the current user can still write the shared production models.
    .DESCRIPTION
        The dev instance reads the production weights in place. A writable
        model root is a real risk (a download or delete call could corrupt
        production), so starting the dev instance without the read-only ACL is
        refused. Run dev-instance.ps1 -Action ProtectModels once, elevated.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$ModelRoot)

    if (-not (Test-Path -LiteralPath $ModelRoot -PathType Container)) {
        Write-Warn "Shared model directory '$ModelRoot' does not exist yet; the read-only probe was skipped. The dev instance cannot transcribe until models are provisioned there."
        return
    }
    if (Test-PathWritable -Path $ModelRoot) {
        throw "The dev instance can write the shared production model directory '$ModelRoot'. Run 'deploy\windows\dev-instance.ps1 -Action ProtectModels' once from an elevated prompt, then retry."
    }
    $sample = Get-BeeBoxModelSampleFile -ModelRoot $ModelRoot
    if ($null -ne $sample) {
        if (-not (Test-BeeBoxFileReadable -Path $sample.FullName)) {
            throw "The dev instance cannot read '$($sample.FullName)' even though the model root is not writable. Restore the model ACL with dev-instance.ps1 -Action UnprotectModels, then -Action ProtectModels."
        }
        Write-Ok "Shared model directory '$ModelRoot' is read-only and readable (probe: $($sample.Name))."
        return
    }
    Write-Ok "Shared model directory '$ModelRoot' is read-only for the current user."
}

function Get-BeeBoxDevSettings {
    <#
    .SYNOPSIS
        Build the dev settings file contents, preserving unrelated keys.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$Layout)

    $existing = Read-BeeBoxEnvFile -Path $Layout.SettingsPath
    $settings = [ordered]@{}
    foreach ($key in $existing.Keys) {
        $settings[$key] = $existing[$key]
    }

    $settings['BEEBOX_ROOT'] = $Layout.DevRoot
    $settings['BEEBOX_DEV_INSTANCE'] = '1'
    $settings['BEEBOX_PROD_ROOT'] = $Layout.ProdRoot
    $settings['BEEBOX_DATA_DIR'] = $Layout.DataDir
    $settings['BEEBOX_LOG_DIR'] = $Layout.LogDir
    $settings['BEEBOX_MODEL_STORAGE_ROOT'] = $Layout.ModelRoot
    $settings['BEEBOX_MODEL_CACHE_ROOT'] = $Layout.ModelCacheRoot
    $settings['BEEBOX_TOOLS_DIR'] = $Layout.ToolsDir
    $settings['BEEBOX_FFMPEG_PATH'] = (Join-Path $Layout.ToolsDir 'ffmpeg\bin\ffmpeg.exe')
    $settings['BEEBOX_FFPROBE_PATH'] = (Join-Path $Layout.ToolsDir 'ffmpeg\bin\ffprobe.exe')
    $settings['BEEBOX_DENO_PATH'] = (Join-Path $Layout.ToolsDir 'deno\deno.exe')

    $tokenWasPresent = $settings.Contains('BEEBOX_API_TOKEN') -and -not [string]::IsNullOrWhiteSpace([string]$settings['BEEBOX_API_TOKEN'])
    if (-not $tokenWasPresent) {
        $settings['BEEBOX_API_TOKEN'] = New-BeeBoxToken
    }

    return [pscustomobject]@{
        Values         = $settings
        TokenWasPresent = $tokenWasPresent
    }
}

function Get-BeeBoxEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name
    )
    $values = Read-BeeBoxEnvFile -Path $Path
    if ($values.Contains($Name)) {
        return [string]$values[$Name]
    }
    return $null
}

function Get-BeeBoxDevToken {
    param([Parameter(Mandatory = $true)][string]$SettingsPath)
    $token = Get-BeeBoxEnvValue -Path $SettingsPath -Name 'BEEBOX_API_TOKEN'
    if ([string]::IsNullOrWhiteSpace($token)) {
        throw "No BEEBOX_API_TOKEN in '$SettingsPath'. Start the dev instance with dev-instance.ps1 -Action Start first."
    }
    return $token
}

function Assert-BeeBoxDevTokenDistinct {
    <#
    .SYNOPSIS
        The dev token must never equal the production token.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$DevSettingsPath,
        [Parameter(Mandatory = $true)][string]$ProdSettingsPath
    )

    $devToken = Get-BeeBoxEnvValue -Path $DevSettingsPath -Name 'BEEBOX_API_TOKEN'
    $prodToken = Get-BeeBoxEnvValue -Path $ProdSettingsPath -Name 'BEEBOX_API_TOKEN'
    if ([string]::IsNullOrWhiteSpace($prodToken)) {
        Write-Warn "Production settings '$ProdSettingsPath' are unreadable or have no token; token uniqueness was not verified. Read it from an elevated prompt once to confirm."
        return
    }
    if (-not [string]::IsNullOrWhiteSpace($devToken) -and $devToken -eq $prodToken) {
        throw 'The dev instance uses the production Bearer token. Delete BEEBOX_API_TOKEN from the dev settings file and restart the dev instance.'
    }
    Write-Ok 'The dev and production Bearer tokens are different.'
}

function Set-BeeBoxDevSecretsAcl {
    <#
    .SYNOPSIS
        Restrict the dev secrets directory to the current user, SYSTEM and Administrators.
    .DESCRIPTION
        icacls does not apply (OI)(CI) rights to plain files; with
        /inheritance:r that would leave every file with an empty DACL. A
        second pass grants file-level rights explicitly.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $userSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & icacls.exe $Path /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' ('*' + $userSid + ':(OI)(CI)F') '/T' '/C' | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to restrict permissions on '$Path' (icacls exit code $LASTEXITCODE)."
    }
    foreach ($file in @(Get-ChildItem -LiteralPath $Path -Recurse -File -ErrorAction SilentlyContinue)) {
        & icacls.exe $file.FullName /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' ('*' + $userSid + ':F') '/C' | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to grant access to '$($file.FullName)' (icacls exit code $LASTEXITCODE)."
        }
    }
}

function Set-BeeBoxSharedModelsReadOnly {
    <#
    .SYNOPSIS
        Make the shared production model tree read-only for the current user.
    .DESCRIPTION
        Replaces inheritance with explicit grants: SYSTEM and Administrators
        keep full control (production runs as LocalSystem) and the current user
        is granted RX only, so reads work but downloads, overwrites and deletes
        are denied. The original ACL is saved first and -Restore puts it back.

        Deny ACEs are deliberately not used: on Windows a denied DELETE or
        generic write also blocks opening the file for reading, which would
        break production inference from the shared tree.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$BackupPath,
        [switch]$Restore,
        [switch]$DryRun
    )

    $userSid = '*' + [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value

    if ($Restore) {
        $restoreArguments = @($Path, '/restore', $BackupPath, '/C')
        if ($DryRun) {
            Write-DryRun "icacls $($restoreArguments -join ' ')"
            return
        }
        if (-not (Test-IsAdministrator)) {
            throw 'Restoring the model ACL requires an elevated PowerShell prompt (Run as administrator).'
        }
        if (-not (Test-Path -LiteralPath $BackupPath -PathType Leaf)) {
            throw "No ACL backup at '$BackupPath'; restore the production model permissions manually with icacls."
        }
        & icacls.exe @restoreArguments | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "icacls /restore failed on '$Path' (exit code $LASTEXITCODE)."
        }
        Remove-Item -LiteralPath $BackupPath -Force -ErrorAction SilentlyContinue
        Write-Ok "Restored the original ACL of '$Path' from '$BackupPath'."
        return
    }

    $saveArguments = @($Path, '/save', $BackupPath, '/T', '/C')
    $directoryArguments = @(
        $Path,
        '/inheritance:r',
        '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F', ($userSid + ':(OI)(CI)RX'),
        '/T',
        '/C'
    )

    if ($DryRun) {
        Write-DryRun "icacls $($saveArguments -join ' ')  (only when no backup exists)"
        Write-DryRun "icacls $($directoryArguments -join ' ')"
        Write-DryRun "icacls <each file below $Path> /grant:r *S-1-5-18:F *S-1-5-32-544:F ${userSid}:RX /C  (icacls cannot apply (OI)(CI) rights to files)"
        return
    }
    if (-not (Test-IsAdministrator)) {
        throw 'Changing ACLs on the production model directory requires an elevated PowerShell prompt (Run as administrator).'
    }
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "The production model directory '$Path' does not exist."
    }

    if (-not (Test-Path -LiteralPath $BackupPath -PathType Leaf)) {
        $backupDirectory = Split-Path -Parent $BackupPath
        New-Item -ItemType Directory -Force -Path $backupDirectory | Out-Null
        & icacls.exe @saveArguments | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Could not save the current ACL of '$Path' to '$BackupPath' (exit code $LASTEXITCODE). No change was made."
        }
        Write-Ok "Saved the original ACL to '$BackupPath'."
    }
    else {
        Write-Info "Reusing the existing ACL backup '$BackupPath'."
    }

    & icacls.exe @directoryArguments | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "icacls failed on '$Path' (exit code $LASTEXITCODE)."
    }
    foreach ($file in @(Get-ChildItem -LiteralPath $Path -Recurse -File -ErrorAction SilentlyContinue)) {
        & icacls.exe $file.FullName /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' ($userSid + ':RX') '/C' | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "icacls failed on '$($file.FullName)' (exit code $LASTEXITCODE)."
        }
    }

    if (Test-PathWritable -Path $Path) {
        throw "The production model directory '$Path' is still writable after the ACL change."
    }
    try {
        Get-ChildItem -LiteralPath $Path -ErrorAction Stop | Out-Null
    }
    catch {
        throw "The production model directory '$Path' is no longer readable: $($_.Exception.Message)"
    }
    $sample = Get-BeeBoxModelSampleFile -ModelRoot $Path
    if (($null -ne $sample) -and (-not (Test-BeeBoxFileReadable -Path $sample.FullName))) {
        throw "The production model file '$($sample.FullName)' is no longer readable after the ACL change. Restore it with -Action UnprotectModels."
    }
    Write-Ok "The current user can read but not modify '$Path' (SYSTEM and Administrators keep full control)."
}

function Wait-BeeBoxDevHealth {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [int]$TimeoutSeconds = 120,
        [string]$Token
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastError = 'no response yet'
    while ((Get-Date) -lt $deadline) {
        try {
            $headers = @{}
            if ($Token) {
                $headers['Authorization'] = "Bearer $Token"
            }
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -Headers $headers -TimeoutSec 5
            if ($response.StatusCode -eq 200) {
                return ($response.Content | ConvertFrom-Json)
            }
            $lastError = "HTTP $($response.StatusCode)"
        }
        catch {
            $lastError = $_.Exception.Message
        }
        Start-Sleep -Milliseconds 1000
    }
    throw "Health check failed for $Url within $TimeoutSeconds seconds. Last error: $lastError"
}

function Get-BeeBoxDevProcessRecord {
    param([Parameter(Mandatory = $true)][string]$PidFile)
    if (-not (Test-Path -LiteralPath $PidFile -PathType Leaf)) {
        return $null
    }
    try {
        return (Get-Content -LiteralPath $PidFile -Raw -Encoding UTF8 | ConvertFrom-Json)
    }
    catch {
        return $null
    }
}

function Test-BeeBoxDevProcessCommand {
    <#
    .SYNOPSIS
        True when the PID really is this dev instance's backend.server process.
    #>
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [Parameter(Mandatory = $true)][int]$Port
    )
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return $false
    }
    $commandLine = [string]$process.CommandLine
    if ($commandLine -notmatch 'backend\.server') {
        return $false
    }
    if ($commandLine -notmatch ('--port\s+' + [regex]::Escape([string]$Port))) {
        return $false
    }
    return $true
}

function Get-BeeBoxListeningProcessIds {
    param([Parameter(Mandatory = $true)][int]$Port)
    try {
        $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    }
    catch {
        return @()
    }
    if ($null -eq $connections) {
        return @()
    }
    return @($connections | Select-Object -ExpandProperty OwningProcess -Unique)
}

function Show-BeeBoxDevLogTail {
    param([Parameter(Mandatory = $true)]$Layout, [int]$Lines = 20)
    foreach ($file in @($Layout.ServerOutLog, $Layout.ServerErrorLog)) {
        if (Test-Path -LiteralPath $file -PathType Leaf) {
            Write-Warn "Log: $file"
            Get-Content -LiteralPath $file -Tail $Lines -ErrorAction SilentlyContinue |
                ForEach-Object { Write-Info "  $_" }
        }
    }
}

function Start-BeeBoxDevInstance {
    <#
    .SYNOPSIS
        Start the dev instance from the current checkout on port 17495.
    .DESCRIPTION
        Creates only dev-owned directories, writes the dev settings file and
        token, and launches `python -m backend.server` with the dev environment.
        The production service is never started, stopped or reconfigured.
        -DryRun prints the plan without touching the machine.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Layout,
        [int]$HealthTimeoutSeconds = 120,
        [switch]$DryRun
    )

    Assert-BeeBoxDevIsolation -DevRoot $Layout.DevRoot -ProdRoot $Layout.ProdRoot -DataDir $Layout.DataDir -ModelRoot $Layout.ModelRoot -ProdModelRoot $Layout.ProdModelsDir -ModelCacheRoot $Layout.ModelCacheRoot -Port $Layout.Port -ProdPort (Get-BeeBoxProductionPort)

    $settingsInfo = Get-BeeBoxDevSettings -Layout $Layout
    $settings = $settingsInfo.Values

    if ($DryRun) {
        Write-DryRun "Verify the shared model directory '$($Layout.ModelRoot)' is not writable by the current user (run -Action ProtectModels from an elevated prompt if it is)."
    }
    else {
        Assert-BeeBoxSharedModelsReadOnly -ModelRoot $Layout.ModelRoot
    }

    $existing = Get-BeeBoxDevProcessRecord -PidFile $Layout.PidFile
    if ($null -ne $existing) {
        $runningId = [int]$existing.pid
        $process = Get-Process -Id $runningId -ErrorAction SilentlyContinue
        if (($null -ne $process) -and (Test-BeeBoxDevProcessCommand -ProcessId $runningId -Port $Layout.Port)) {
            Write-Ok "The dev instance is already running as PID $runningId at $($Layout.Url)."
            return
        }
        if ($DryRun) {
            Write-DryRun "Remove the stale dev PID file $($Layout.PidFile)."
        }
        else {
            Remove-Item -LiteralPath $Layout.PidFile -Force -ErrorAction SilentlyContinue
            Write-Info "Removed the stale dev PID file (PID $runningId is not the dev server)."
        }
    }

    $listeners = @(Get-BeeBoxListeningProcessIds -Port $Layout.Port)
    if ($listeners.Count -gt 0) {
        throw "Port $($Layout.Port) is already in use by PID $($listeners -join ', '). Stop that process before starting the dev instance."
    }

    if (-not $DryRun -and -not (Test-Path -LiteralPath $Layout.PythonPath -PathType Leaf)) {
        throw "Python environment '$($Layout.PythonPath)' was not found. Run 'uv sync --frozen' in '$($Layout.Checkout)' or pass -PythonPath."
    }
    if ($DryRun -and -not (Test-Path -LiteralPath $Layout.PythonPath -PathType Leaf)) {
        Write-Warn "Python environment '$($Layout.PythonPath)' does not exist yet; a real start requires 'uv sync --frozen' in '$($Layout.Checkout)'."
    }

    if ($DryRun) {
        foreach ($directory in @($Layout.DevRoot, $Layout.DataDir, $Layout.SecretsDir, $Layout.LogDir, $Layout.ModelCacheRoot)) {
            Write-DryRun "Create directory $directory"
        }
        if ($settingsInfo.TokenWasPresent) {
            Write-DryRun "Keep the existing dev Bearer token in $($Layout.SettingsPath)."
        }
        else {
            Write-DryRun "Generate a random dev-only Bearer token in $($Layout.SettingsPath)."
        }
        Write-DryRun "Restrict the ACL on $($Layout.SecretsDir) to the current user, SYSTEM and Administrators."
        Write-DryRun "Verify the dev token differs from the production token in $($Layout.ProdSettingsPath)."
        Write-DryRun "Start '$($Layout.PythonPath)' -m backend.server --host 127.0.0.1 --port $($Layout.Port) from '$($Layout.Checkout)'."
        Write-DryRun "Dev environment: BEEBOX_DEV_INSTANCE=1, BEEBOX_DATA_DIR=$($Layout.DataDir), BEEBOX_MODEL_STORAGE_ROOT=$($Layout.ModelRoot) (read-only), BEEBOX_MODEL_CACHE_ROOT=$($Layout.ModelCacheRoot), BEEBOX_LOG_DIR=$($Layout.LogDir)."
        Write-DryRun "Redirect stdout/stderr to $($Layout.ServerOutLog) / $($Layout.ServerErrorLog) and write $($Layout.PidFile)."
        Write-DryRun "Poll $($Layout.Url)/v1/health until it reports 200 (timeout ${HealthTimeoutSeconds}s), then check /v1/queue with the dev token."
        Write-DryRun 'No directory, file, ACL or process was changed.'
        return
    }

    Write-Step 'Preparing the dev root'
    foreach ($directory in @($Layout.DevRoot, $Layout.DataDir, $Layout.SecretsDir, $Layout.LogDir, $Layout.ModelCacheRoot)) {
        New-Item -ItemType Directory -Force -Path $directory | Out-Null
    }
    Write-Info "Dev data:   $($Layout.DataDir)"
    Write-Info "Dev logs:   $($Layout.LogDir)"
    Write-Info "Dev cache:  $($Layout.ModelCacheRoot)"
    Write-Info "Models:     $($Layout.ModelRoot) (shared read-only)"

    Write-Step 'Writing the dev settings'
    Write-BeeBoxEnvFile -Path $Layout.SettingsPath -Values $settings -Header 'BeeBox development instance settings. Generated by deploy/windows/dev-instance.ps1. Do not commit this file.'
    Set-BeeBoxDevSecretsAcl -Path $Layout.SecretsDir
    if ($settingsInfo.TokenWasPresent) {
        Write-Ok "Kept the existing dev Bearer token in $($Layout.SettingsPath)."
    }
    else {
        Write-Ok "Generated a new dev-only Bearer token in $($Layout.SettingsPath)."
    }
    Assert-BeeBoxDevTokenDistinct -DevSettingsPath $Layout.SettingsPath -ProdSettingsPath $Layout.ProdSettingsPath

    Write-Step "Starting the dev instance on port $($Layout.Port)"
    $previous = @{}
    foreach ($key in $settings.Keys) {
        $previous[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
        [Environment]::SetEnvironmentVariable($key, [string]$settings[$key], 'Process')
    }
    try {
        $arguments = @('-m', 'backend.server', '--host', '127.0.0.1', '--port', [string]$Layout.Port)
        $process = Start-Process -FilePath $Layout.PythonPath -ArgumentList $arguments -WorkingDirectory $Layout.Checkout -RedirectStandardOutput $Layout.ServerOutLog -RedirectStandardError $Layout.ServerErrorLog -PassThru -WindowStyle Hidden
    }
    finally {
        foreach ($key in $settings.Keys) {
            [Environment]::SetEnvironmentVariable($key, $previous[$key], 'Process')
        }
    }

    $pidPayload = [ordered]@{
        pid        = $process.Id
        port       = $Layout.Port
        url        = $Layout.Url
        checkout   = $Layout.Checkout
        python     = $Layout.PythonPath
        started_at = (Get-Date).ToUniversalTime().ToString('o')
    }
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Layout.PidFile, ($pidPayload | ConvertTo-Json -Compress), $utf8NoBom)
    Write-Info "Process ID: $($process.Id) (recorded in $($Layout.PidFile))"

    Write-Step 'Checking dev health'
    try {
        $health = Wait-BeeBoxDevHealth -Url "$($Layout.Url)/v1/health" -TimeoutSeconds $HealthTimeoutSeconds
        Write-Ok "Health: $($health | ConvertTo-Json -Compress -Depth 6)"
        $queue = Invoke-BeeBoxQueueRequest -BaseUrl $Layout.Url -Token $settings['BEEBOX_API_TOKEN'] -Action 'status'
        Write-Ok "The dev token is accepted (queue paused=$($queue.paused), length=$($queue.length))."
    }
    catch {
        Write-Warn "The dev instance did not become healthy: $($_.Exception.Message)"
        Show-BeeBoxDevLogTail -Layout $Layout
        Stop-BeeBoxDevInstance -Layout $Layout | Out-Null
        throw
    }

    Write-Step 'Dev instance summary'
    Write-Info "URL:       $($Layout.Url)"
    Write-Info "Root:      $($Layout.DevRoot)"
    Write-Info "Data:      $($Layout.DataDir)"
    Write-Info "Logs:      $($Layout.LogDir)"
    Write-Info "Settings:  $($Layout.SettingsPath) (dev token; not the production token)"
    Write-Info "Models:    $($Layout.ModelRoot) (read-only; weights are never copied)"
    Write-Info "Stop:      deploy\windows\dev-instance.ps1 -Action Stop"
    Write-Info 'The production service, its data and its queue were not touched.'
}

function Stop-BeeBoxDevInstance {
    <#
    .SYNOPSIS
        Stop only the recorded dev process; never touches the production service.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$Layout)

    $record = Get-BeeBoxDevProcessRecord -PidFile $Layout.PidFile
    if ($null -eq $record) {
        Write-Info "No dev instance PID file at $($Layout.PidFile); nothing to stop."
        return $false
    }
    $processId = [int]$record.pid
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        Remove-Item -LiteralPath $Layout.PidFile -Force -ErrorAction SilentlyContinue
        Write-Info "Removed the stale dev PID file (process $processId is gone)."
        return $false
    }
    if (-not (Test-BeeBoxDevProcessCommand -ProcessId $processId -Port $Layout.Port)) {
        throw "Refusing to stop PID ${processId}: its command line is not the BeeBox dev server on port $($Layout.Port). Delete '$($Layout.PidFile)' manually after checking the process."
    }
    Write-Step "Stopping the dev instance (PID $processId)"
    & taskkill.exe /PID $processId /T /F | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "taskkill failed for PID $processId (exit code $LASTEXITCODE)."
    }
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        if ($null -eq (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
            break
        }
        Start-Sleep -Milliseconds 500
    }
    Remove-Item -LiteralPath $Layout.PidFile -Force -ErrorAction SilentlyContinue
    Write-Ok 'Dev instance stopped. The production service and its queue were not touched.'
    return $true
}

function Invoke-BeeBoxQueueRequest {
    <#
    .SYNOPSIS
        GET /v1/queue, POST /v1/queue/pause or POST /v1/queue/resume.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [Parameter(Mandatory = $true)][string]$Token,
        [ValidateSet('status', 'pause', 'resume')][string]$Action = 'status',
        [int]$TimeoutSeconds = 15
    )
    $headers = @{ Authorization = "Bearer $Token" }
    $base = $BaseUrl.TrimEnd('/')
    if ($Action -eq 'status') {
        return Invoke-RestMethod -Uri "$base/v1/queue" -Method Get -Headers $headers -TimeoutSec $TimeoutSeconds
    }
    return Invoke-RestMethod -Uri "$base/v1/queue/$Action" -Method Post -Headers $headers -TimeoutSec $TimeoutSeconds
}

function Wait-BeeBoxQueueDrain {
    <#
    .SYNOPSIS
        Wait until the paused production queue has no running job.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [Parameter(Mandatory = $true)][string]$Token,
        [int]$TimeoutSeconds = 3600,
        [int]$PollSeconds = 5
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $queue = Invoke-BeeBoxQueueRequest -BaseUrl $BaseUrl -Token $Token -Action 'status'
        if ($null -eq $queue.current_job_id) {
            Write-Ok 'The production queue has finished its current job.'
            return
        }
        Write-Info "Waiting for the production job $($queue.current_job_id) to finish ($([int]$queue.length) queued behind it)..."
        Start-Sleep -Seconds $PollSeconds
    }
    throw "The production queue still had a running job after $TimeoutSeconds seconds; the real-model run was not started."
}

function Invoke-BeeBoxQueueExclusive {
    <#
    .SYNOPSIS
        Run a script block while the production GPU queue is paused.
    .DESCRIPTION
        Pauses the production queue through /v1/queue/pause, waits for the
        current job to finish, runs the body and always resumes -- including
        when the body throws. A queue that was already paused by an operator is
        left paused: this function only undoes its own pause.

        -BodyArgument is passed to the script block as its only parameter so
        the caller does not depend on dynamic scoping.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [Parameter(Mandatory = $true)][string]$Token,
        [Parameter(Mandatory = $true)][scriptblock]$Body,
        [object]$BodyArgument = $null,
        [int]$DrainTimeoutSeconds = 3600,
        [int]$PollSeconds = 5
    )

    $initial = Invoke-BeeBoxQueueRequest -BaseUrl $BaseUrl -Token $Token -Action 'status'
    $pausedByUs = $false
    if ([bool]$initial.paused) {
        Write-Warn 'The production queue was already paused by an operator; it will stay paused when this run ends.'
    }
    else {
        $paused = Invoke-BeeBoxQueueRequest -BaseUrl $BaseUrl -Token $Token -Action 'pause'
        if (-not [bool]$paused.paused) {
            throw 'The production queue did not report itself as paused after /v1/queue/pause.'
        }
        $pausedByUs = $true
        Write-Ok 'Production queue paused; the running job is allowed to finish and new submissions stay queued.'
    }

    $bodyError = $null
    $resumeError = $null
    try {
        if ($pausedByUs) {
            Wait-BeeBoxQueueDrain -BaseUrl $BaseUrl -Token $Token -TimeoutSeconds $DrainTimeoutSeconds -PollSeconds $PollSeconds
        }
        & $Body $BodyArgument
    }
    catch {
        $bodyError = $_
    }
    finally {
        if ($pausedByUs) {
            try {
                $resumed = Invoke-BeeBoxQueueRequest -BaseUrl $BaseUrl -Token $Token -Action 'resume'
                if ([bool]$resumed.paused) {
                    throw 'The queue still reports paused after /v1/queue/resume.'
                }
                Write-Ok 'Production queue resumed.'
            }
            catch {
                $resumeError = $_
                Write-Warn "FAILED to resume the production queue: $($_.Exception.Message). Resume it manually: POST $($BaseUrl.TrimEnd('/'))/v1/queue/resume (Bearer token required)."
            }
        }
    }

    if ($null -ne $resumeError) {
        throw "The production queue resume failed: $($resumeError.Exception.Message). Resume it manually before starting other work."
    }
    if ($null -ne $bodyError) {
        throw $bodyError
    }
}

function Get-BeeBoxDevStatus {
    <#
    .SYNOPSIS
        Read-only status of the dev instance, the production service and both ports.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$Layout)

    $record = Get-BeeBoxDevProcessRecord -PidFile $Layout.PidFile
    $running = $false
    $processId = $null
    if ($null -ne $record) {
        $processId = [int]$record.pid
        if ($null -ne (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
            $running = $true
        }
    }

    Write-Step 'BeeBox dev instance status'
    Write-Info "Dev root:   $($Layout.DevRoot)"
    Write-Info "Checkout:   $($Layout.Checkout)"
    Write-Info "Data:       $($Layout.DataDir)"
    Write-Info "Logs:       $($Layout.LogDir)"
    Write-Info "Models:     $($Layout.ModelRoot) (shared read-only with production)"
    Write-Info "Cache:      $($Layout.ModelCacheRoot)"
    Write-Info "Port:       $($Layout.Port)"
    Write-Info "URL:        $($Layout.Url)"
    if ($running) {
        Write-Ok "Running as PID $processId."
    }
    elseif ($null -ne $record) {
        Write-Warn "PID $processId from '$($Layout.PidFile)' is not running (stale PID file)."
    }
    else {
        Write-Info 'Not running (no PID file).'
    }

    if ($running) {
        try {
            $health = Wait-BeeBoxDevHealth -Url "$($Layout.Url)/v1/health" -TimeoutSeconds 5
            Write-Ok "Health: $($health | ConvertTo-Json -Compress -Depth 6)"
        }
        catch {
            Write-Warn "Health check failed: $($_.Exception.Message)"
        }
        $devToken = $null
        try {
            $devToken = Get-BeeBoxDevToken -SettingsPath $Layout.SettingsPath
        }
        catch {
            Write-Warn $_.Exception.Message
        }
        if ($devToken) {
            try {
                $queue = Invoke-BeeBoxQueueRequest -BaseUrl $Layout.Url -Token $devToken -Action 'status'
                Write-Ok "Dev queue: paused=$($queue.paused) length=$($queue.length) current=$($queue.current_job_id)"
            }
            catch {
                Write-Warn "The dev queue status failed: $($_.Exception.Message)"
            }
        }
    }

    $devListeners = @(Get-BeeBoxListeningProcessIds -Port $Layout.Port)
    $prodListeners = @(Get-BeeBoxListeningProcessIds -Port (Get-BeeBoxProductionPort))
    Write-Info "Port $($Layout.Port) listeners: $(if ($devListeners.Count -eq 0) { 'none' } else { $devListeners -join ', ' })"
    Write-Info "Port $(Get-BeeBoxProductionPort) listeners: $(if ($prodListeners.Count -eq 0) { 'none' } else { $prodListeners -join ', ' })"

    $prodService = Get-Service -Name 'BeeBox' -ErrorAction SilentlyContinue
    if ($null -eq $prodService) {
        Write-Info 'Production Windows service BeeBox: not installed.'
    }
    else {
        Write-Info "Production Windows service BeeBox: $($prodService.Status)."
    }
    if (Test-Path -LiteralPath $Layout.SettingsPath -PathType Leaf) {
        Write-Info "Settings:   $($Layout.SettingsPath) (token present: $([bool](Get-BeeBoxEnvValue -Path $Layout.SettingsPath -Name 'BEEBOX_API_TOKEN')))"
    }
    else {
        Write-Info 'Settings:   not created yet.'
    }
}

function Invoke-BeeBoxDevVerifyIsolation {
    <#
    .SYNOPSIS
        Prove the dev layout cannot write production; throws on any violation.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Layout,
        [switch]$SkipModelProbe
    )

    $failures = New-Object System.Collections.Generic.List[string]
    $notes = New-Object System.Collections.Generic.List[string]
    $skippedProbes = 0

    Write-Step 'Path guards'
    try {
        Assert-BeeBoxDevIsolation -DevRoot $Layout.DevRoot -ProdRoot $Layout.ProdRoot -DataDir $Layout.DataDir -ModelRoot $Layout.ModelRoot -ProdModelRoot $Layout.ProdModelsDir -ModelCacheRoot $Layout.ModelCacheRoot -Port $Layout.Port -ProdPort (Get-BeeBoxProductionPort)
        Write-Ok "The dev root and its writable paths are outside '$($Layout.ProdRoot)'."
    }
    catch {
        $failures.Add($_.Exception.Message)
        Write-Warn $_.Exception.Message
    }

    Write-Step 'Bearer token separation'
    if (Test-Path -LiteralPath $Layout.SettingsPath -PathType Leaf) {
        try {
            Assert-BeeBoxDevTokenDistinct -DevSettingsPath $Layout.SettingsPath -ProdSettingsPath $Layout.ProdSettingsPath
        }
        catch {
            $failures.Add($_.Exception.Message)
            Write-Warn $_.Exception.Message
        }
    }
    else {
        $notes.Add("No dev settings file at '$($Layout.SettingsPath)' yet; start the dev instance to generate its token.")
        Write-Info $notes[$notes.Count - 1]
    }

    Write-Step 'Writable dev data directory'
    if (Test-Path -LiteralPath $Layout.DataDir -PathType Container) {
        if (Test-PathWritable -Path $Layout.DataDir) {
            Write-Ok "The dev instance writes only below '$($Layout.DataDir)'."
        }
        else {
            $failures.Add("The dev data directory '$($Layout.DataDir)' is not writable.")
            Write-Warn $failures[$failures.Count - 1]
        }
    }
    else {
        $skippedProbes++
        Write-Info "The dev data directory '$($Layout.DataDir)' does not exist yet; the write probe was skipped."
    }

    Write-Step 'Read-only shared production models'
    if ($SkipModelProbe) {
        $skippedProbes++
        Write-Info 'The model write probe was skipped (-SkipModelProbe).'
    }
    elseif (Test-Path -LiteralPath $Layout.ModelRoot -PathType Container) {
        if (Test-PathWritable -Path $Layout.ModelRoot) {
            $message = "The current user can write the production model directory '$($Layout.ModelRoot)'. Run 'dev-instance.ps1 -Action ProtectModels' from an elevated prompt."
            $failures.Add($message)
            Write-Warn $message
        }
        else {
            $sample = Get-BeeBoxModelSampleFile -ModelRoot $Layout.ModelRoot
            if (($null -ne $sample) -and (-not (Test-BeeBoxFileReadable -Path $sample.FullName))) {
                $message = "The current user cannot read '$($sample.FullName)' below the production model directory '$($Layout.ModelRoot)'. Restore the ACL with -Action UnprotectModels and apply -Action ProtectModels again."
                $failures.Add($message)
                Write-Warn $message
            }
            else {
                Write-Ok "The production model directory '$($Layout.ModelRoot)' is read-only and readable for the current user."
            }
        }
    }
    else {
        $skippedProbes++
        Write-Info "The production model directory '$($Layout.ModelRoot)' does not exist; the write probe was skipped."
    }

    Write-Step 'Port separation'
    if ($Layout.Port -eq (Get-BeeBoxProductionPort)) {
        $failures.Add("The dev port equals the production port $(Get-BeeBoxProductionPort).")
        Write-Warn $failures[$failures.Count - 1]
    }
    else {
        Write-Ok "Dev port $($Layout.Port) differs from production port $(Get-BeeBoxProductionPort)."
    }

    if ($failures.Count -gt 0) {
        throw ("Dev instance isolation is NOT satisfied:`n  - " + ($failures -join [Environment]::NewLine + '  - '))
    }
    Write-Step 'Isolation verified'
    if ($skippedProbes -gt 0) {
        Write-Ok "The path guards and token/port separation hold. $skippedProbes write probe(s) were skipped because the directory does not exist yet; rerun after provisioning production and the dev data directory."
    }
    else {
        Write-Ok 'The dev instance cannot write production data and cannot modify the shared production models.'
    }
}

function New-BeeBoxRealTestEnvironment {
    <#
    .SYNOPSIS
        Build the environment for backend/real_tests from the dev settings.
    .DESCRIPTION
        The real-model suite runs in the dev instance's environment: its data
        directory, its token and the read-only shared production models. The
        suite is therefore identical to what the running dev instance uses, but
        it never writes production data or models.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Layout,
        [Parameter(Mandatory = $true)][string]$MediaDir,
        [string]$DevToken
    )

    $values = Read-BeeBoxEnvFile -Path $Layout.SettingsPath
    foreach ($required in @('BEEBOX_DATA_DIR', 'BEEBOX_LOG_DIR', 'BEEBOX_MODEL_STORAGE_ROOT', 'BEEBOX_MODEL_CACHE_ROOT', 'BEEBOX_API_TOKEN')) {
        $present = $values.Contains($required) -and -not [string]::IsNullOrWhiteSpace([string]$values[$required])
        if (-not $present) {
            throw "The dev settings file '$($Layout.SettingsPath)' is missing $required. Start the dev instance with dev-instance.ps1 -Action Start first."
        }
    }

    $environment = @{}
    foreach ($key in $values.Keys) {
        $environment[$key] = [string]$values[$key]
    }
    if (-not (Resolve-FullPath $environment['BEEBOX_DATA_DIR']).Equals((Resolve-FullPath $Layout.DataDir), [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "The dev settings data directory '$($environment['BEEBOX_DATA_DIR'])' does not match the isolated dev layout '$($Layout.DataDir)'. Re-run dev-instance.ps1 -Action Start."
    }
    if (-not (Resolve-FullPath $environment['BEEBOX_MODEL_STORAGE_ROOT']).Equals((Resolve-FullPath $Layout.ModelRoot), [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "The dev settings model root '$($environment['BEEBOX_MODEL_STORAGE_ROOT'])' is not the shared read-only production models directory '$($Layout.ModelRoot)'. Re-run dev-instance.ps1 -Action Start."
    }
    $environment['BEEBOX_DEV_INSTANCE'] = '1'
    $environment['BEEBOX_PROD_ROOT'] = $Layout.ProdRoot
    $environment['BEEBOX_RUN_REAL_MODELS'] = '1'
    $environment['BEEBOX_REAL_MEDIA_DIR'] = (Resolve-FullPath $MediaDir)
    $environment['BEEBOX_BASE_URL'] = $Layout.Url
    $environment['BEEBOX_REAL_TEST_RESULTS_TIMESTAMP'] = (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss')
    if ($DevToken) {
        $environment['BEEBOX_API_TOKEN'] = $DevToken
    }
    foreach ($passthrough in @('BEEBOX_REAL_ALIGNMENT_AUDIO', 'BEEBOX_REAL_DIARIZATION_AUDIO', 'BEEBOX_REAL_MODEL_DOWNLOAD_TIMEOUT', 'BEEBOX_REAL_MODEL_TASK_TIMEOUT')) {
        $value = [Environment]::GetEnvironmentVariable($passthrough, 'Process')
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            $environment[$passthrough] = $value
        }
    }
    return $environment
}

function Invoke-BeeBoxRealModelSuite {
    <#
    .SYNOPSIS
        Run the opt-in real-model suite in the dev instance environment.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Layout,
        [string]$TestPath = 'backend/real_tests',
        [Parameter(Mandatory = $true)][hashtable]$Environment
    )
    Invoke-NativeCommand -FilePath $Layout.PythonPath `
        -Arguments @('-m', 'pytest', $TestPath, '-q', '--no-header', '-p', 'no:cacheprovider') `
        -WorkingDirectory $Layout.Checkout `
        -Environment $Environment
}

Export-ModuleMember -Function *

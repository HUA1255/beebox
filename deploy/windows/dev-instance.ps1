#Requires -Version 5.1
<#
.SYNOPSIS
    Manage the BeeBox development instance, isolated from production (ADR-0010).

.DESCRIPTION
    The dev instance runs from this checkout (no application copy) on port
    17495, with its own root directory, data directory, logs and Bearer token.
    It reads the production model directory read-only and writes framework
    caches to its own directory. The production Windows service, its data and
    its GPU queue are never started, stopped or modified by this script.

    Actions:

      Start            Create the dev layout, write the dev token and launch
                       `python -m backend.server` in the background.
      Stop             Stop only the recorded dev process tree.
      Status           Read-only: PID, health, dev queue, ports, prod service.
      VerifyIsolation  Prove the path guards, token separation and read-only
                       shared models; exits non-zero on any violation.
      ProtectModels    (elevated) Grant the current user RX only on the shared
                       production model tree (SYSTEM/Administrators keep full
                       control); the original ACL is saved first.
      UnprotectModels  (elevated) Restore the saved original model ACL.

    Defaults: dev root C:\BeeBoxDev, production root C:\BeeBox, port 17495.
    Model weights are shared in place: <ProdRoot>\models, never copied.

.PARAMETER Action
    Start, Stop, Status, VerifyIsolation, ProtectModels or UnprotectModels.

.PARAMETER DevRoot
    Dev-owned root. Default C:\BeeBoxDev.

.PARAMETER ProdRoot
    Production install root. Default C:\BeeBox.

.PARAMETER Checkout
    Code checkout the dev instance runs from. Default the repository that
    contains this script.

.PARAMETER PythonPath
    Interpreter for the dev instance. Default <Checkout>\.venv\Scripts\python.exe.

.PARAMETER ModelRoot
    Production model storage root shared read-only. Default
    BEEBOX_MODEL_STORAGE_ROOT from <ProdRoot>\secrets\settings.env, falling
    back to <ProdRoot>\models.

.PARAMETER Port
    Dev HTTP port. Default 17495.

.PARAMETER HealthTimeoutSeconds
    How long Start waits for /health. Default 120.

.PARAMETER DryRun
    Print the plan without creating directories, writing files, changing ACLs
    or starting a process. Exits 0.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\deploy\windows\dev-instance.ps1 -Action Start

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\deploy\windows\dev-instance.ps1 -Action Status

.EXAMPLE
    # Elevated, once: make the shared production models read-only for this user
    powershell -ExecutionPolicy Bypass -File .\deploy\windows\dev-instance.ps1 -Action ProtectModels

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\deploy\windows\dev-instance.ps1 -Action Start -DryRun
#>
[CmdletBinding()]
param(
    [ValidateSet('Start', 'Stop', 'Status', 'VerifyIsolation', 'ProtectModels', 'UnprotectModels')]
    [string]$Action = 'Status',
    [string]$DevRoot = 'C:\BeeBoxDev',
    [string]$ProdRoot = 'C:\BeeBox',
    [string]$Checkout,
    [string]$PythonPath,
    [string]$ModelRoot,
    [int]$Port = 17495,
    [int]$HealthTimeoutSeconds = 120,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'BeeBox.DevInstance.psm1') -Force

if (-not $Checkout) {
    $Checkout = Resolve-FullPath (Join-Path $PSScriptRoot '..\..')
}
$layout = Get-BeeBoxDevLayout -DevRoot $DevRoot -ProdRoot $ProdRoot -Checkout $Checkout -PythonPath $PythonPath -ModelRoot $ModelRoot -Port $Port

# Every action, including ProtectModels (which creates the ACL backup below the
# dev root), refuses a layout that overlaps production (ADR-0010).
Assert-BeeBoxDevIsolation -DevRoot $layout.DevRoot -ProdRoot $layout.ProdRoot -DataDir $layout.DataDir -ModelRoot $layout.ModelRoot -ProdModelRoot $layout.ProdModelsDir -ModelCacheRoot $layout.ModelCacheRoot -Port $layout.Port -ProdPort (Get-BeeBoxProductionPort)

Write-Step "BeeBox dev instance ($Action)"
Write-Info "DevRoot:  $($layout.DevRoot)"
Write-Info "ProdRoot: $($layout.ProdRoot)"
Write-Info "Checkout: $($layout.Checkout)"
Write-Info "Python:   $($layout.PythonPath)"
Write-Info "Port:     $($layout.Port)"
if ($DryRun) {
    Write-Warn 'DRY RUN: no directories, files, ACLs or processes will be changed.'
}

switch ($Action) {
    'Start' {
        Start-BeeBoxDevInstance -Layout $layout -HealthTimeoutSeconds $HealthTimeoutSeconds -DryRun:$DryRun
    }
    'Stop' {
        if ($DryRun) {
            Write-DryRun "Stop the process recorded in $($layout.PidFile) after verifying it is 'backend.server --port $($layout.Port)'; the production service is not touched."
        }
        else {
            Stop-BeeBoxDevInstance -Layout $layout | Out-Null
        }
    }
    'Status' {
        Get-BeeBoxDevStatus -Layout $layout
    }
    'VerifyIsolation' {
        Invoke-BeeBoxDevVerifyIsolation -Layout $layout -SkipModelProbe:$DryRun
        if ($DryRun) {
            Write-DryRun 'The model write probe was skipped; run without -DryRun to prove the ACL.'
        }
    }
    'ProtectModels' {
        Set-BeeBoxSharedModelsReadOnly -Path $layout.ModelRoot -BackupPath $layout.ModelAclBackup -DryRun:$DryRun
    }
    'UnprotectModels' {
        Set-BeeBoxSharedModelsReadOnly -Path $layout.ModelRoot -BackupPath $layout.ModelAclBackup -Restore -DryRun:$DryRun
    }
}

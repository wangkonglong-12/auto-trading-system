[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ManifestPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "TaskDeployment.Common.ps1")

try {
    $manifest = Read-LaunchManifest -ManifestPath $ManifestPath
    $watchdogArguments = @(
        "--config",
        $manifest.config_path,
        "--",
        $manifest.runtime.executable
    ) + @($manifest.runtime.arguments)
    $exitCode = Invoke-LoggedProcess `
        -Executable $manifest.watchdog.executable `
        -Arguments $watchdogArguments `
        -LogRoot $manifest.log_root `
        -Component "watchdog"
    exit $exitCode
}
catch {
    Write-Error "Watchdog launcher failed: $($_.Exception.Message)"
    exit 70
}

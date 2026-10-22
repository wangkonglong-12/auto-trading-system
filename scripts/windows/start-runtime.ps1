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
    $exitCode = Invoke-LoggedProcess `
        -Executable $manifest.runtime.executable `
        -Arguments @($manifest.runtime.arguments) `
        -LogRoot $manifest.log_root `
        -Component "trade-runtime"
    exit $exitCode
}
catch {
    Write-Error "Runtime launcher failed: $($_.Exception.Message)"
    exit 70
}

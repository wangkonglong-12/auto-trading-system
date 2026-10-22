[CmdletBinding()]
param(
    [string]$Python = ".\.venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$outputPath = Join-Path $projectRoot "config\v1\runtime-config.schema.json"

Push-Location $projectRoot
try {
    & $Python -m auto_trading_system.config.schema --output $outputPath
    if ($LASTEXITCODE -ne 0) {
        throw "Configuration Schema export failed with exit code ${LASTEXITCODE}."
    }
}
finally {
    Pop-Location
}

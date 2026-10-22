[CmdletBinding()]
param(
    [string]$Python = ".\.venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot

Push-Location $projectRoot
try {
    $resolvedPython = (Resolve-Path -LiteralPath $Python).Path
    $testPaths = @(
        "tests/unit/test_qmt_order_preflight.py",
        "tests/unit/test_qmt_order_preflight_cli.py",
        "tests/unit/test_qmt_readonly_probe.py",
        "tests/unit/test_architecture_layout.py",
        "tests/unit/test_config_loader.py",
        "tests/unit/test_config_models.py",
        "tests/unit/test_credentials.py",
        "tests/unit/test_version.py"
    )
    & $resolvedPython -m pytest --no-cov @testPaths -q
    if ($LASTEXITCODE -ne 0) {
        throw "QMT order preflight verification failed with exit code ${LASTEXITCODE}"
    }
    & $resolvedPython -m auto_trading_system.apps.qmt_order_preflight --version
    if ($LASTEXITCODE -ne 0) {
        throw "QMT order preflight version check failed with exit code ${LASTEXITCODE}"
    }
    Write-Output "QMT order preflight verified"
}
finally {
    Pop-Location
}

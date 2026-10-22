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
        "tests/unit/test_qmt_snapshot_probe.py",
        "tests/unit/test_cicc_qmt_snapshot_adapter.py",
        "tests/unit/test_qmt_snapshot_probe_cli.py",
        "tests/unit/test_qmt_readonly_probe.py",
        "tests/unit/test_cicc_qmt_adapter.py",
        "tests/unit/test_qmt_readonly_probe_cli.py",
        "tests/unit/test_architecture_layout.py",
        "tests/unit/test_config_loader.py",
        "tests/unit/test_credentials.py",
        "tests/unit/test_version.py"
    )
    & $resolvedPython -m pytest --no-cov @testPaths -q
    if ($LASTEXITCODE -ne 0) {
        throw "QMT snapshot probe verification failed with exit code ${LASTEXITCODE}"
    }
    & $resolvedPython -m auto_trading_system.apps.qmt_snapshot_probe --version
    if ($LASTEXITCODE -ne 0) {
        throw "QMT snapshot probe version check failed with exit code ${LASTEXITCODE}"
    }
    Write-Output "QMT snapshot probe verified"
}
finally {
    Pop-Location
}

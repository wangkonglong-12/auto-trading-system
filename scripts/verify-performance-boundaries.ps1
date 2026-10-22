[CmdletBinding()]
param(
    [string]$Python = ".venv-dev\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonPath = if ([System.IO.Path]::IsPathRooted($Python)) {
    (Resolve-Path -LiteralPath $Python).Path
} else {
    (Resolve-Path -LiteralPath (Join-Path $projectRoot $Python)).Path
}

Push-Location $projectRoot
try {
    $measurement = & $pythonPath -m auto_trading_system.testing.performance `
        --symbols 15 `
        --events-per-symbol 2000 `
        --minimum-throughput 5000 `
        --maximum-p95-microseconds 5000 `
        --maximum-peak-memory-mib 64
    if ($LASTEXITCODE -ne 0) {
        throw "Market ingress performance boundary failed."
    }
    $parsed = $measurement | ConvertFrom-Json
    if (-not $parsed.passed -or $parsed.events -ne 30000 -or $parsed.overflows -ne 0) {
        throw "Market ingress measurement did not satisfy the fixed acceptance contract."
    }

    & $pythonPath -m pytest --no-cov -q `
        tests/integration/test_performance_boundaries.py `
        tests/unit/test_decision_loop.py `
        tests/unit/test_mock_market.py `
        tests/unit/test_outbox_worker.py
    if ($LASTEXITCODE -ne 0) {
        throw "Performance isolation tests failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}

[ordered]@{
    verified = $true
    market_ingress = $parsed
    outbox_isolation = $true
    test_files = 4
} | ConvertTo-Json -Compress -Depth 6

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

$testFiles = @(
    "tests/unit/test_mock_broker.py",
    "tests/integration/test_mock_broker_anomaly_matrix.py",
    "tests/unit/test_order_intent_gate.py",
    "tests/integration/test_order_intent_gate_sqlite.py"
)

Push-Location $projectRoot
try {
    & $pythonPath -m pytest --no-cov -q @testFiles
    if ($LASTEXITCODE -ne 0) {
        throw "Mock Broker anomaly matrix failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}

[ordered]@{
    verified = $true
    scenarios = @(
        "accept",
        "reject",
        "partial_fill",
        "cancel",
        "timeout",
        "duplicate",
        "out_of_order"
    )
    test_files = $testFiles.Count
} | ConvertTo-Json -Compress

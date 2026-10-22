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
    "tests/integration/test_fault_injection_acceptance.py",
    "tests/unit/test_broker_query_validation.py",
    "tests/unit/test_event_batch_transport.py",
    "tests/unit/test_outbox_worker.py",
    "tests/unit/test_runtime_state_machine.py",
    "tests/unit/test_runtime_faults.py",
    "tests/integration/test_runtime_fault_matrix.py",
    "tests/integration/test_watchdog_process_recovery.py",
    "tests/unit/test_hard_safety.py"
)

Push-Location $projectRoot
try {
    & $pythonPath -m pytest --no-cov -q @testFiles
    if ($LASTEXITCODE -ne 0) {
        throw "Fault-injection matrix failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}

[ordered]@{
    verified = $true
    faults = @(
        "central_network_outage",
        "qmt_disconnect",
        "query_none",
        "runtime_crash",
        "disk_full",
        "clock_skew"
    )
    test_files = $testFiles.Count
} | ConvertTo-Json -Compress

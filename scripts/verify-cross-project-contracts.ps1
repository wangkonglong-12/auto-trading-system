[CmdletBinding()]
param(
    [string]$QuantProjectRoot = "",
    [string]$WindowsPython = ".\.venv\Scripts\python.exe",
    [string]$QuantPython = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot

function Resolve-ProjectFile {
    param(
        [Parameter(Mandatory = $true)][string]$BasePath,
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $path = if ([IO.Path]::IsPathRooted($Candidate)) {
        $Candidate
    }
    else {
        Join-Path $BasePath $Candidate
    }
    try {
        return (Resolve-Path -LiteralPath $path -ErrorAction Stop).Path
    }
    catch {
        throw "${Label} was not found: ${path}"
    }
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$Label
    )
    Push-Location $WorkingDirectory
    try {
        & $Executable @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "${Label} failed with exit code ${LASTEXITCODE}"
        }
    }
    finally {
        Pop-Location
    }
}

if (-not $QuantProjectRoot) {
    $QuantProjectRoot = Join-Path (Split-Path -Parent $projectRoot) "ai-stock-assistant"
}
$quantRoot = (Resolve-Path -LiteralPath $QuantProjectRoot -ErrorAction Stop).Path
$quantBackend = Resolve-ProjectFile -BasePath $quantRoot -Candidate "backend" -Label "Quant backend"
$windowsPythonPath = Resolve-ProjectFile -BasePath $projectRoot -Candidate $WindowsPython -Label "Windows Python"
if (-not $QuantPython) {
    $QuantPython = ".\.venv\Scripts\python.exe"
}
$quantPythonPath = Resolve-ProjectFile -BasePath $quantBackend -Candidate $QuantPython -Label "Quant Python"

$windowsManifest = Resolve-ProjectFile `
    -BasePath $projectRoot `
    -Candidate "contracts\v1\manifest.sha256.json" `
    -Label "Windows contract manifest"
$quantManifest = Resolve-ProjectFile `
    -BasePath $quantBackend `
    -Candidate "contracts\auto_trading\v1\manifest.sha256.json" `
    -Label "Quant contract manifest"
$windowsManifestHash = (Get-FileHash -LiteralPath $windowsManifest -Algorithm SHA256).Hash
$quantManifestHash = (Get-FileHash -LiteralPath $quantManifest -Algorithm SHA256).Hash
if ($windowsManifestHash -cne $quantManifestHash) {
    throw "Contract manifests differ between the Windows and quant projects"
}

$windowsTests = @(
    "tests/contract/test_contract_manifest.py",
    "tests/contract/test_trade_plan_schema.py",
    "tests/contract/test_domain_event_schema.py",
    "tests/contract/test_node_message_schemas.py",
    "tests/contract/test_plan_ack_contract.py",
    "tests/contract/test_hmac_vectors.py",
    "tests/unit/test_protocol_compat.py",
    "tests/unit/test_event_batch_transport.py",
    "tests/unit/test_mock_central_api.py"
)
$quantTests = @(
    "tests/auto_trading/test_auto_trade_cross_project_contracts.py",
    "tests/auto_trading/test_auto_trade_plan_ack_contract.py",
    "tests/auto_trading/test_auto_trade_plan_api.py",
    "tests/auto_trading/test_auto_trade_event_api.py",
    "tests/auto_trading/test_auto_trade_ledger_api.py",
    "tests/auto_trading/test_auto_trade_runtime_api.py"
)

Invoke-Checked `
    -Executable $windowsPythonPath `
    -Arguments (@("-m", "pytest", "-q", "-o", "addopts=") + $windowsTests) `
    -WorkingDirectory $projectRoot `
    -Label "Windows contract tests"
Invoke-Checked `
    -Executable $quantPythonPath `
    -Arguments (@("-m", "pytest", "-q") + $quantTests) `
    -WorkingDirectory $quantBackend `
    -Label "Quant contract tests"

$manifest = Get-Content -LiteralPath $windowsManifest -Raw | ConvertFrom-Json
[ordered]@{
    verified = $true
    schema_version = $manifest.schema_version
    contract_files = @($manifest.files).Count
    manifest_sha256 = $windowsManifestHash.ToLowerInvariant()
    windows_test_files = $windowsTests.Count
    quant_test_files = $quantTests.Count
} | ConvertTo-Json -Compress

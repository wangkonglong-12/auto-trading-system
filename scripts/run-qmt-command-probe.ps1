[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$EnvironmentRoot,
    [Parameter(Mandatory=$true)][string]$Config,
    [Parameter(Mandatory=$true)][string]$Approval,
    [Parameter(Mandatory=$true)][ValidatePattern('^[A-Fa-f0-9]{64}$')][string]$ApprovalSha256,
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{16}$')][string]$HostFingerprint,
    [Parameter(Mandatory=$true)][string]$Output,
    [switch]$Execute
)

$ErrorActionPreference='Stop'
if(-not $Execute) { throw 'Explicit -Execute is required for the approved single live order' }
$projectRoot=Split-Path -Parent $PSScriptRoot
$environment=(Resolve-Path -LiteralPath $EnvironmentRoot).Path
$python=Join-Path $environment 'env\Scripts\python.exe'
$sdk=Join-Path $environment 'sdk'
$configPath=(Resolve-Path -LiteralPath $Config).Path
$approvalPath=(Resolve-Path -LiteralPath $Approval).Path
$outputPath=[System.IO.Path]::GetFullPath($Output)

if(-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Prepared QMT Python is missing' }
if(-not (Test-Path -LiteralPath (Join-Path $sdk 'xtquant\xttrader.py') -PathType Leaf)) {
    throw 'Prepared XtQuant SDK is missing'
}
if(Test-Path -LiteralPath $outputPath) { throw 'Output already exists; use a new JSON path' }
if([System.IO.Path]::GetExtension($outputPath) -ine '.json') { throw 'Output must be a JSON file' }
$outputParent=Split-Path -Parent $outputPath
if(-not (Test-Path -LiteralPath $outputParent -PathType Container)) {
    throw 'Output parent directory is missing'
}

$oldPythonPath=$env:PYTHONPATH
try {
    # Bind this one child process to the reviewed project source and broker SDK.
    $env:PYTHONPATH=(Join-Path $projectRoot 'src')+';'+$sdk
    & $python -m auto_trading_system.apps.qmt_command_probe `
        --config $configPath `
        --approval $approvalPath `
        --approval-sha256 $ApprovalSha256.ToLowerInvariant() `
        --host-fingerprint $HostFingerprint `
        --output $outputPath `
        --execute
    $result=$LASTEXITCODE
} finally {
    $env:PYTHONPATH=$oldPythonPath
}
if($result -ne 0) { throw "QMT command probe returned $result; inspect the new result file if present" }

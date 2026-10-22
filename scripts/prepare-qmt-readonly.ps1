[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Destination
)
# 官方券商兼容包固定版本/摘要；全新环境独立于终端、全局Python和MOCK发行包。
$ErrorActionPreference='Stop'
$qmtProject=Split-Path -Parent $PSScriptRoot
$qmtPython=(Resolve-Path -LiteralPath $Python).Path
$qmtDestination=[System.IO.Path]::GetFullPath($Destination)
if(Test-Path -LiteralPath $qmtDestination) {throw 'Choose a fresh environment destination'}
$qmtParent=Split-Path -Parent $qmtDestination
if(-not (Test-Path -LiteralPath $qmtParent -PathType Container)) {throw 'Destination parent must exist'}
if((Resolve-Path -LiteralPath $qmtParent).Path -ne $qmtParent) {throw 'Direct absolute destination required'}
& $qmtPython -c "import sys,struct; assert sys.version_info[:2]==(3,11) and struct.calcsize('P')==8"
if($LASTEXITCODE -ne 0){throw 'Python 3.11 x64 required'}
New-Item -ItemType Directory -Path $qmtDestination | Out-Null
$qmtArchive=Join-Path $qmtDestination 'xtquant_0825b_2023-09-20.rar'
$qmtUrl='https://dict.thinktrader.net/packages/xtquant_0825b_2023-09-20.rar'
$qmtExpected='F096D36E0682D1BABEE90148A0124ED2FB98280CBC9FFAEE2631A547D9824205'
Invoke-WebRequest -Uri $qmtUrl -OutFile $qmtArchive -TimeoutSec 120
if((Get-FileHash -LiteralPath $qmtArchive -Algorithm SHA256).Hash -ne $qmtExpected) {
    throw 'SDK archive digest differs; preserve evidence and review the upstream change'
}
$qmtMembers=@(tar -tf $qmtArchive)
if($LASTEXITCODE -ne 0 -or @($qmtMembers | Where-Object {
    $_ -match '(^[/\\]|^[A-Za-z]:|(^|[/\\])\.\.([/\\]|$))'
}).Count -gt 0) {throw 'Archive validation failed'}
$qmtSdk=Join-Path $qmtDestination 'sdk'
New-Item -ItemType Directory -Path $qmtSdk | Out-Null
tar -xf $qmtArchive -C $qmtSdk
if($LASTEXITCODE -ne 0){throw 'SDK extraction failed'}
& $qmtPython -m venv (Join-Path $qmtDestination 'env')
if($LASTEXITCODE -ne 0){throw 'Isolated environment creation failed'}
$qmtEnvPython=Join-Path $qmtDestination 'env/Scripts/python.exe'
& $qmtEnvPython -m pip install --require-hashes -r (Join-Path $qmtProject 'requirements/runtime.lock')
if($LASTEXITCODE -ne 0){throw 'Runtime dependency installation failed'}
$qmtOldPath=$env:PYTHONPATH
try {
    $env:PYTHONPATH=$qmtSdk
    & $qmtEnvPython -c "import xtquant.xttrader,xtquant.xtdata; print('QMT SDK import verified')"
    if($LASTEXITCODE -ne 0){throw 'SDK import failed'}
} finally {$env:PYTHONPATH=$qmtOldPath}
$qmtManifest=[ordered]@{schema='qmt-sdk-environment/v1';version='230825b';source=$qmtUrl;
    archive_sha256=$qmtExpected;python='3.11 x64';real_trading_enabled=$false}
$qmtManifestPath=Join-Path $qmtDestination 'sdk-manifest.json'
$qmtStream=[System.IO.File]::Open($qmtManifestPath,[System.IO.FileMode]::CreateNew)
try {
    $qmtBytes=[System.Text.Encoding]::UTF8.GetBytes(($qmtManifest | ConvertTo-Json))
    $qmtStream.Write($qmtBytes,0,$qmtBytes.Length)
    $qmtStream.Flush($true)
} finally {$qmtStream.Dispose()}
Write-Output 'Read-only SDK environment prepared; existing terminals and configurations preserved'

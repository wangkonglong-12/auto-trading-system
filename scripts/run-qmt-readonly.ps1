[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$EnvironmentRoot,
    [Parameter(Mandatory=$true)][string]$QmtRoot,
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [Parameter(Mandatory=$true)][ValidateSet('ACTIVE','CLOSED')][string]$Scenario,
    [string[]]$Symbol=@('600000.SH'),
    [ValidateRange(1,30)][int]$Seconds=5,
    [ValidateRange(1,2147483645)][int]$SessionId=6101901,
    [ValidatePattern('^[a-f0-9]{16}$')][string]$AccountFingerprint,
    [switch]$MarketOnly,
    [switch]$Facts
)
$ErrorActionPreference='Stop'
$qmtProject=Split-Path -Parent $PSScriptRoot
$qmtEnvironment=(Resolve-Path -LiteralPath $EnvironmentRoot).Path
$qmtInstall=(Resolve-Path -LiteralPath $QmtRoot).Path
$qmtPython=Join-Path $qmtEnvironment 'env/Scripts/python.exe'
$qmtSdk=Join-Path $qmtEnvironment 'sdk'
if(-not (Test-Path -LiteralPath $qmtPython -PathType Leaf)) {throw 'Prepared Python environment missing'}
if(-not (Test-Path -LiteralPath (Join-Path $qmtSdk 'xtquant/xttrader.py') -PathType Leaf)) {
    throw 'Prepared SDK missing'
}
$qmtArgs=@('-m','auto_trading_system.apps.qmt_onboarding','--userdata-mini',
    (Join-Path $qmtInstall 'userdata_mini'),'--output',[System.IO.Path]::GetFullPath($OutputDirectory),
    '--scenario',$Scenario,'--seconds',"$Seconds",'--session-id',"$SessionId")
foreach($qmtCode in $Symbol){$qmtArgs+=@('--symbol',$qmtCode)}
if($AccountFingerprint){$qmtArgs+=@('--account',$AccountFingerprint)}
if($MarketOnly){$qmtArgs+='--market-only'}
if($Facts){$qmtArgs+='--facts'}
$qmtOldPath=$env:PYTHONPATH
try {
    # 仅为本次有界子进程设置接口位置，外部PYTHONPATH不能引入其他版本。
    $env:PYTHONPATH=(Join-Path $qmtProject 'src')+';'+$qmtSdk
    & $qmtPython @qmtArgs
    $qmtResult=$LASTEXITCODE
} finally {$env:PYTHONPATH=$qmtOldPath}
if($qmtResult -ne 0){throw "QMT read-only check returned $qmtResult; see execution.json"}

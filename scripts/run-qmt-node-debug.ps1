[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$EnvironmentRoot,
    [Parameter(Mandatory=$true)][string]$QmtRoot,
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{16}$')][string]$AccountFingerprint,
    [ValidateRange(1,2147483645)][int]$SessionBase=6102301,
    [switch]$Trading,
    [switch]$SelfTest,
    [switch]$NoBrowser
)

$ErrorActionPreference='Stop'
$projectRoot=Split-Path -Parent $PSScriptRoot
$environment=(Resolve-Path -LiteralPath $EnvironmentRoot).Path
$qmt=(Resolve-Path -LiteralPath $QmtRoot).Path
$python=Join-Path $environment 'env\Scripts\python.exe'
$sdk=Join-Path $environment 'sdk'
$output=[System.IO.Path]::GetFullPath($OutputDirectory)

if(-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Prepared QMT Python is missing' }
if(-not (Test-Path -LiteralPath (Join-Path $sdk 'xtquant\xttrader.py') -PathType Leaf)) {
    throw 'Prepared XtQuant SDK is missing'
}
if(Test-Path -LiteralPath $output) { throw 'Output directory already exists; choose a new directory' }

$arguments=@(
    '-m','auto_trading_system.apps.qmt_debug',
    '--environment-root',$environment,
    '--qmt-root',$qmt,
    '--directory',$output,
    '--account-fingerprint',$AccountFingerprint,
    '--session-base',"$SessionBase"
)
if($Trading) { $arguments+='--trading' }
if($SelfTest) { $arguments+='--self-test' }
if($NoBrowser) { $arguments+='--no-browser' }

$oldPythonPath=$env:PYTHONPATH
$oldOperatorPassword=$env:ATS_QMT_OPERATOR_PASSWORD
$plainPassword=$null
$passwordPointer=[IntPtr]::Zero
try {
    $env:PYTHONPATH=(Join-Path $projectRoot 'src')+';'+$sdk
    if($Trading) {
        $securePassword=Read-Host '设置本次QMT调试页面的操作员口令（至少14个字符）' -AsSecureString
        $passwordPointer=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
        $plainPassword=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPointer)
        $env:ATS_QMT_OPERATOR_PASSWORD=$plainPassword
    } else {
        Remove-Item Env:ATS_QMT_OPERATOR_PASSWORD -ErrorAction SilentlyContinue
    }
    & $python @arguments
    $result=$LASTEXITCODE
} finally {
    if($passwordPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
    }
    $plainPassword=$null
    $env:PYTHONPATH=$oldPythonPath
    if($null -eq $oldOperatorPassword) {
        Remove-Item Env:ATS_QMT_OPERATOR_PASSWORD -ErrorAction SilentlyContinue
    } else {
        $env:ATS_QMT_OPERATOR_PASSWORD=$oldOperatorPassword
    }
}
if($result -ne 0) { throw "QMT node debug returned $result" }

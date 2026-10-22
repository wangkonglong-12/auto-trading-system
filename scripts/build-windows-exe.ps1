param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$FrontendAssets,
    [Parameter(Mandatory=$true)][string]$Destination
)
$ErrorActionPreference='Stop'
$exeProject = Split-Path -Parent $PSScriptRoot
$exePython = (Resolve-Path -LiteralPath $Python).Path
$exeAssets = (Resolve-Path -LiteralPath $FrontendAssets).Path
$exeDestination = [System.IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $exeDestination) { throw 'Choose a new release destination; existing artifacts are preserved' }
if (-not (Test-Path -LiteralPath (Join-Path $exeAssets 'node.html') -PathType Leaf)) { throw 'Frontend assets are incomplete' }
& $exePython -c "import sys,PyInstaller; assert sys.version_info[:2]==(3,11); assert PyInstaller.__version__=='6.22.3'"
if ($LASTEXITCODE -ne 0) { throw 'Unsupported build toolchain' }
$exeWork = Join-Path $exeProject ('.codex_tmp/exe-build-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $exeWork | Out-Null
$exeOldAssets=$env:ATS_EXE_ASSETS
try {
    $env:ATS_EXE_ASSETS=$exeAssets
    Push-Location $exeProject
    try {
        & $exePython -m PyInstaller --distpath $exeDestination --workpath $exeWork (Join-Path $exeProject 'packaging/auto-trading-desktop.spec')
        if ($LASTEXITCODE -ne 0) { throw 'EXE build failed; build evidence retained' }
    } finally { Pop-Location }
} finally { $env:ATS_EXE_ASSETS=$exeOldAssets }
Get-Item -LiteralPath (Join-Path $exeDestination 'AutoTradingSystem-0.1.0-dev56.exe') | Select-Object FullName,Length

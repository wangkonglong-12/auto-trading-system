[CmdletBinding()]
param(
    [string]$Python = ".\.venv\Scripts\python.exe",
    [string]$Pnpm = "pnpm",
    [string]$NodeBin = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$originalPath = $env:PATH

function Assert-SupportedToolchain {
    $nodeOutput = (& node --version 2>&1) -join "`n"
    if ($LASTEXITCODE -ne 0) {
        throw "Node.js is not available: ${nodeOutput}"
    }
    try {
        $nodeVersion = [version]($nodeOutput.Trim().TrimStart("v"))
    }
    catch {
        throw "Cannot parse Node.js version: ${nodeOutput}"
    }
    if ($nodeVersion -lt [version]"20.19.0") {
        throw "Node.js 20.19.0 or newer is required; found ${nodeVersion}"
    }

    $pythonOutput = (& $Python -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>&1) -join "`n"
    if ($LASTEXITCODE -ne 0) {
        throw "Python is not available at '${Python}': ${pythonOutput}"
    }
    try {
        $pythonVersion = [version]$pythonOutput.Trim()
    }
    catch {
        throw "Cannot parse Python version: ${pythonOutput}"
    }
    if ($pythonVersion -lt [version]"3.11.0" -or $pythonVersion -ge [version]"3.13.0") {
        throw "Python 3.11 or 3.12 is required; found ${pythonVersion}"
    }
}

function Invoke-NativeQualityCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [Parameter(Mandatory = $true)]
        [string]$FailureLabel
    )

    & $Executable @Arguments
    $nativeExitCode = $LASTEXITCODE
    if ($nativeExitCode -ne 0) {
        throw "${FailureLabel} failed with exit code ${nativeExitCode}"
    }
}

Push-Location $projectRoot
try {
    if ($NodeBin) {
        $resolvedNodeBin = (Resolve-Path -LiteralPath $NodeBin).Path
        if (-not (Test-Path -LiteralPath (Join-Path $resolvedNodeBin "node.exe") -PathType Leaf)) {
            throw "NodeBin must contain node.exe: ${resolvedNodeBin}"
        }
        $env:PATH = "${resolvedNodeBin};${originalPath}"
    }
    Assert-SupportedToolchain

    & (Join-Path $projectRoot "scripts\windows\verify-scheduled-task-scripts.ps1")

    $frontendRoot = Join-Path $projectRoot "frontend\operator-console"
    Invoke-NativeQualityCommand `
        -Executable $Pnpm `
        -Arguments @("--dir", $frontendRoot, "install", "--frozen-lockfile") `
        -FailureLabel "Frontend dependency installation"
    Invoke-NativeQualityCommand `
        -Executable $Pnpm `
        -Arguments @("--dir", $frontendRoot, "run", "check") `
        -FailureLabel "Frontend quality gate"

    $qualityCommands = @(
        @("-m", "auto_trading_system.security.repository_policy"),
        @("-m", "compileall", "-q", "src", "tests"),
        @("-m", "ruff", "check", "."),
        @("-m", "ruff", "format", "--check", "src", "tests"),
        @("-m", "mypy", "src", "tests"),
        @("-m", "pytest"),
        @("-m", "build")
    )

    foreach ($commandArguments in $qualityCommands) {
        Invoke-NativeQualityCommand `
            -Executable $Python `
            -Arguments $commandArguments `
            -FailureLabel "Python quality command: $($commandArguments -join ' ')"
    }
}
finally {
    $env:PATH = $originalPath
    Pop-Location
}

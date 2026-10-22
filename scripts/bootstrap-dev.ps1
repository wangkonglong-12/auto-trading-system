[CmdletBinding()]
param(
    [string]$PythonCommand = "py",
    [string]$PythonVersion = "3.11"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPath = Join-Path $projectRoot ".venv"

Push-Location $projectRoot
try {
    if ($PythonCommand -eq "py") {
        & py "-$PythonVersion" -m venv $venvPath
    }
    else {
        & $PythonCommand -m venv $venvPath
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create the Python $PythonVersion virtual environment."
    }

    $python = Join-Path $venvPath "Scripts\python.exe"
    $installCommands = @(
        @("-m", "pip", "install", "--upgrade", "pip"),
        @("-m", "pip", "install", "--require-hashes", "-r", "requirements\dev.lock"),
        @("-m", "pip", "install", "--no-deps", "-e", ".")
    )

    foreach ($commandArguments in $installCommands) {
        & $python @commandArguments
        if ($LASTEXITCODE -ne 0) {
            throw "Bootstrap command failed with exit code ${LASTEXITCODE}: python $commandArguments"
        }
    }
}
finally {
    Pop-Location
}

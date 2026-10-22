Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Assert-ExactProperties {
    param(
        [Parameter(Mandatory = $true)]
        [psobject]$Value,
        [Parameter(Mandatory = $true)]
        [string[]]$Expected,
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    $actual = @($Value.PSObject.Properties.Name | Sort-Object)
    $required = @($Expected | Sort-Object)
    $difference = @(Compare-Object -ReferenceObject $required -DifferenceObject $actual)
    if ($difference.Count -ne 0) {
        throw "$Name fields do not match manifest version 1.0"
    }
}

function Resolve-ExistingAbsoluteFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    if (-not [IO.Path]::IsPathRooted($Path) -or $Path.IndexOf([char]0) -ge 0) {
        throw "$Name must be an absolute file path"
    }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Name does not exist"
    }
    return (Resolve-Path -LiteralPath $Path).ProviderPath
}

function Resolve-AbsoluteDirectoryPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    if (-not [IO.Path]::IsPathRooted($Path) -or $Path.IndexOf([char]0) -ge 0) {
        throw "$Name must be an absolute directory path"
    }
    return [IO.Path]::GetFullPath($Path)
}

function Assert-SafeProcessArguments {
    param(
        [Parameter(Mandatory = $true)]
        [object[]]$Arguments
    )

    foreach ($argumentValue in $Arguments) {
        if ($argumentValue -isnot [string]) {
            throw "runtime arguments must all be strings"
        }
        $argument = [string]$argumentValue
        if ($argument.Length -gt 2048 -or $argument.IndexOf([char]0) -ge 0 -or
            $argument.Contains("`r") -or $argument.Contains("`n")) {
            throw "runtime argument contains an unsafe character or exceeds 2048 characters"
        }
        if ($argument -match '(?i)(password|secret|access[-_]?token|api[-_]?key)') {
            throw "runtime secrets must use configured credential references"
        }
    }
}

function Get-FileSha256 {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $stream = [IO.File]::OpenRead($Path)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = $algorithm.ComputeHash($stream)
        return ([BitConverter]::ToString($digest) -replace '-', '').ToLowerInvariant()
    }
    finally {
        $algorithm.Dispose()
        $stream.Dispose()
    }
}

function Read-LaunchManifest {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ManifestPath
    )

    $resolvedManifest = Resolve-ExistingAbsoluteFile -Path $ManifestPath -Name "manifest"
    $manifestInfo = Get-Item -LiteralPath $resolvedManifest
    if ($manifestInfo.Length -lt 2 -or $manifestInfo.Length -gt 65536) {
        throw "manifest size is invalid"
    }
    try {
        $manifest = [IO.File]::ReadAllText($resolvedManifest, [Text.Encoding]::UTF8) |
            ConvertFrom-Json
    }
    catch {
        throw "manifest is not valid UTF-8 JSON"
    }
    if ($null -eq $manifest) {
        throw "manifest root must be an object"
    }
    Assert-ExactProperties -Value $manifest -Expected @(
        "manifest_version", "config_path", "log_root", "runtime", "watchdog"
    ) -Name "manifest"
    if ($manifest.manifest_version -ne "1.0") {
        throw "manifest version is unsupported"
    }
    Assert-ExactProperties -Value $manifest.runtime -Expected @(
        "executable", "sha256", "arguments"
    ) -Name "runtime"
    Assert-ExactProperties -Value $manifest.watchdog -Expected @(
        "executable", "sha256"
    ) -Name "watchdog"

    $manifest.config_path = Resolve-ExistingAbsoluteFile `
        -Path ([string]$manifest.config_path) -Name "Runtime config"
    $manifest.runtime.executable = Resolve-ExistingAbsoluteFile `
        -Path ([string]$manifest.runtime.executable) -Name "Runtime executable"
    $manifest.watchdog.executable = Resolve-ExistingAbsoluteFile `
        -Path ([string]$manifest.watchdog.executable) -Name "Watchdog executable"
    $manifest.log_root = Resolve-AbsoluteDirectoryPath `
        -Path ([string]$manifest.log_root) -Name "log root"

    $runtimeArguments = @($manifest.runtime.arguments)
    Assert-SafeProcessArguments -Arguments $runtimeArguments
    $manifest.runtime.arguments = $runtimeArguments

    foreach ($component in @($manifest.runtime, $manifest.watchdog)) {
        $expectedHash = [string]$component.sha256
        if ($expectedHash -notmatch '^[a-f0-9]{64}$') {
            throw "component SHA-256 is invalid"
        }
        $actualHash = Get-FileSha256 -Path $component.executable
        if (-not $actualHash.Equals($expectedHash, [StringComparison]::OrdinalIgnoreCase)) {
            throw "component SHA-256 verification failed"
        }
    }
    return $manifest
}

function Invoke-LoggedProcess {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [Parameter(Mandatory = $true)]
        [string]$LogRoot,
        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[a-z][a-z0-9-]{2,31}$')]
        [string]$Component
    )

    New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
    $stdoutPath = Join-Path $LogRoot "$Component-$stamp.stdout.log"
    $stderrPath = Join-Path $LogRoot "$Component-$stamp.stderr.log"
    try {
        & $Executable @Arguments 1>> $stdoutPath 2>> $stderrPath
        $processExitCode = $LASTEXITCODE
        if ($null -eq $processExitCode) {
            return 70
        }
        return [int]$processExitCode
    }
    catch {
        $failure = "launch failed: $($_.Exception.GetType().Name)"
        Add-Content -LiteralPath $stderrPath -Value $failure -Encoding UTF8
        return 70
    }
}

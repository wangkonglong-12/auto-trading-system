[CmdletBinding()]
param(
    [string]$Python = "",
    [string]$QmtRoot = "",
    [string]$UserdataMini = "",
    [string]$OutputPath = "",
    [switch]$RequireReady
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0
$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8WithoutBom
$OutputEncoding = $utf8WithoutBom

$scriptRevision = "1.0.0"
$maximumDirectoriesPerRoot = 300
$maximumArtifactsPerRoot = 32
$maximumFingerprintFiles = 128

function Get-Sha256Text {
    param([Parameter(Mandatory = $true)][string]$Value)

    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
        $digest = $sha256.ComputeHash($bytes)
        return ([System.BitConverter]::ToString($digest)).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha256.Dispose()
    }
}

function Get-FileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = $null
    $sha256 = $null
    try {
        $sharing = [System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete
        $stream = [System.IO.File]::Open(
            $Path,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            $sharing
        )
        $sha256 = [System.Security.Cryptography.SHA256]::Create()
        $digest = $sha256.ComputeHash($stream)
        return ([System.BitConverter]::ToString($digest)).Replace("-", "").ToLowerInvariant()
    }
    catch {
        return $null
    }
    finally {
        if ($null -ne $sha256) { $sha256.Dispose() }
        if ($null -ne $stream) { $stream.Dispose() }
    }
}

function Convert-ToEvidencePath {
    param([AllowNull()][string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        return $null
    }

    $result = $Path
    $replacements = @(
        @($env:USERPROFILE, "%USERPROFILE%"),
        @($env:ProgramData, "%ProgramData%"),
        @($env:ProgramFiles, "%ProgramFiles%"),
        @(${env:ProgramFiles(x86)}, "%ProgramFiles(x86)%"),
        @($env:SystemRoot, "%SystemRoot%")
    )
    foreach ($replacement in $replacements) {
        $source = [string]$replacement[0]
        if (-not [string]::IsNullOrWhiteSpace($source) -and
            $result.StartsWith($source, [System.StringComparison]::OrdinalIgnoreCase)) {
            $result = ([string]$replacement[1]) + $result.Substring($source.Length)
            break
        }
    }
    if ($result -match "^[A-Za-z]:\\Users\\[^\\]+") {
        $result = [System.Text.RegularExpressions.Regex]::Replace(
            $result,
            "^(?<prefix>[A-Za-z]:\\Users\\)[^\\]+",
            '${prefix}%USERNAME%',
            [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
        )
    }
    if (-not [string]::IsNullOrWhiteSpace($env:USERNAME)) {
        $result = [System.Text.RegularExpressions.Regex]::Replace(
            $result,
            [System.Text.RegularExpressions.Regex]::Escape($env:USERNAME),
            "%USERNAME%",
            [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
        )
    }
    return $result
}

function Resolve-AbsoluteInputPath {
    param(
        [Parameter(Mandatory = $true)][string]$Value,
        [Parameter(Mandatory = $true)][string]$ParameterName
    )

    if (-not [System.IO.Path]::IsPathRooted($Value)) {
        throw "${ParameterName} must be an absolute path: ${Value}"
    }
    return [System.IO.Path]::GetFullPath($Value)
}

function Resolve-PythonExecutable {
    param([Parameter(Mandatory = $true)][string]$Value)

    if ([System.IO.Path]::IsPathRooted($Value)) {
        $resolved = [System.IO.Path]::GetFullPath($Value)
        if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
            throw "Python executable does not exist: ${resolved}"
        }
        return $resolved
    }

    $command = Get-Command -Name $Value -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $command) {
        throw "Python command was not found: ${Value}"
    }
    return $command.Source
}

function Add-UniqueCandidate {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][System.Collections.ArrayList]$Items,
        [Parameter(Mandatory = $true)][hashtable]$Seen,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Source
    )

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $key = $fullPath.ToLowerInvariant()
    if (-not $Seen.ContainsKey($key)) {
        $Seen[$key] = $true
        [void]$Items.Add([pscustomobject]@{
            path = $fullPath
            source = $Source
        })
    }
}

function Get-PythonCandidates {
    $items = New-Object System.Collections.ArrayList
    $seen = @{}

    if (-not [string]::IsNullOrWhiteSpace($Python)) {
        Add-UniqueCandidate -Items $items -Seen $seen `
            -Path (Resolve-PythonExecutable -Value $Python) -Source "explicit"
    }

    $launcher = Get-Command -Name "py.exe" -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $launcher) {
        $launcherOutput = @(& $launcher.Source -0p 2>$null)
        foreach ($line in $launcherOutput) {
            $text = [string]$line
            if ($text -match "([A-Za-z]:\\.*python(?:\.exe)?)\s*$") {
                $candidate = $matches[1].Trim()
                if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                    Add-UniqueCandidate -Items $items -Seen $seen -Path $candidate -Source "py-launcher"
                }
            }
        }
    }

    foreach ($commandName in @("python.exe", "python3.exe")) {
        $commands = @(Get-Command -Name $commandName -CommandType Application -ErrorAction SilentlyContinue)
        foreach ($command in $commands) {
            if (Test-Path -LiteralPath $command.Source -PathType Leaf) {
                Add-UniqueCandidate -Items $items -Seen $seen -Path $command.Source -Source "path"
            }
        }
    }

    return @($items)
}

function Get-PythonFact {
    param([Parameter(Mandatory = $true)][pscustomobject]$Candidate)

    $probe = @'
import importlib.metadata, importlib.util, json, platform, struct, sys; spec = importlib.util.find_spec('xtquant'); versions = [item.version for item in importlib.metadata.distributions() if (item.metadata.get('Name') or '').casefold() == 'xtquant']; print(json.dumps({'version': platform.python_version(), 'implementation': platform.python_implementation(), 'architecture_bits': struct.calcsize('P') * 8, 'executable': sys.executable, 'xtquant': {'available': spec is not None, 'origin': spec.origin if spec is not None else None, 'package_paths': list(spec.submodule_search_locations or []) if spec is not None else [], 'distribution_version': versions[0] if versions else None}}, ensure_ascii=False))
'@
    $probe = $probe.Trim()

    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = @(& $Candidate.path -c $probe 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $outputText = ($output | ForEach-Object { [string]$_ }) -join "`n"
    if ($exitCode -ne 0) {
        return [pscustomobject]@{
            source = $Candidate.source
            path = Convert-ToEvidencePath -Path $Candidate.path
            sha256 = Get-FileSha256 -Path $Candidate.path
            available = $false
            probe_error = "PYTHON_PROBE_FAILED"
        }
    }

    try {
        $details = $outputText | ConvertFrom-Json
    }
    catch {
        return [pscustomobject]@{
            source = $Candidate.source
            path = Convert-ToEvidencePath -Path $Candidate.path
            sha256 = Get-FileSha256 -Path $Candidate.path
            available = $false
            probe_error = "PYTHON_PROBE_INVALID_JSON"
        }
    }

    $packagePaths = @(
        $details.xtquant.package_paths |
            ForEach-Object { Convert-ToEvidencePath -Path ([string]$_) }
    )
    return [pscustomobject]@{
        source = $Candidate.source
        path = Convert-ToEvidencePath -Path $Candidate.path
        sha256 = Get-FileSha256 -Path $Candidate.path
        available = $true
        version = [string]$details.version
        implementation = [string]$details.implementation
        architecture_bits = [int]$details.architecture_bits
        xtquant = [pscustomobject]@{
            available = [bool]$details.xtquant.available
            distribution_version = $details.xtquant.distribution_version
            origin = Convert-ToEvidencePath -Path $details.xtquant.origin
            package_paths = $packagePaths
        }
    }
}

function Get-QmtRootCandidates {
    $items = New-Object System.Collections.ArrayList
    $seen = @{}

    if (-not [string]::IsNullOrWhiteSpace($QmtRoot)) {
        Add-UniqueCandidate -Items $items -Seen $seen `
            -Path (Resolve-AbsoluteInputPath -Value $QmtRoot -ParameterName "QmtRoot") `
            -Source "explicit"
    }

    $registryPaths = @(
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*"
    )
    foreach ($registryPath in $registryPaths) {
        $entries = @(Get-ItemProperty -Path $registryPath -ErrorAction SilentlyContinue)
        foreach ($entry in $entries) {
            $displayNameProperty = $entry.PSObject.Properties["DisplayName"]
            $installLocationProperty = $entry.PSObject.Properties["InstallLocation"]
            $displayName = if ($null -ne $displayNameProperty) {
                [string]$displayNameProperty.Value
            }
            else {
                ""
            }
            if ($displayName -match "QMT|XtQuant|MiniQMT") {
                $location = if ($null -ne $installLocationProperty) {
                    [string]$installLocationProperty.Value
                }
                else {
                    ""
                }
                if (-not [string]::IsNullOrWhiteSpace($location) -and
                    [System.IO.Path]::IsPathRooted($location)) {
                    Add-UniqueCandidate -Items $items -Seen $seen -Path $location -Source "uninstall-registry"
                }
            }
        }
    }

    $processes = @(Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessName -match "QMT|XtQuant|MiniQMT" })
    foreach ($process in $processes) {
        try {
            $processPath = $process.Path
            if (-not [string]::IsNullOrWhiteSpace($processPath)) {
                Add-UniqueCandidate -Items $items -Seen $seen `
                    -Path ([System.IO.Path]::GetDirectoryName($processPath)) -Source "running-process"
            }
        }
        catch {
            continue
        }
    }

    $drives = @(Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue)
    foreach ($drive in $drives) {
        if (-not (Test-Path -LiteralPath $drive.Root -PathType Container)) {
            continue
        }
        $directories = @(Get-ChildItem -LiteralPath $drive.Root -Directory -ErrorAction SilentlyContinue)
        foreach ($directory in $directories) {
            if ($directory.Name -match "QMT|XtQuant|MiniQMT") {
                Add-UniqueCandidate -Items $items -Seen $seen `
                    -Path $directory.FullName -Source "drive-root"
            }
        }
    }

    return @($items)
}

function Get-DirectoryFingerprint {
    param([Parameter(Mandatory = $true)][string]$Root)

    $files = New-Object System.Collections.ArrayList
    $queue = New-Object System.Collections.Queue
    $queue.Enqueue([pscustomobject]@{ path = $Root; depth = 0 })
    $visitedDirectories = 0
    while ($queue.Count -gt 0 -and $visitedDirectories -lt 80 -and
        $files.Count -lt $maximumFingerprintFiles) {
        $current = $queue.Dequeue()
        $visitedDirectories += 1
        $children = @(Get-ChildItem -LiteralPath $current.path -ErrorAction SilentlyContinue |
            Sort-Object -Property FullName)
        foreach ($child in $children) {
            if ($child.PSIsContainer) {
                if ($current.depth -lt 3 -and
                    (($child.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq 0)) {
                    $queue.Enqueue([pscustomobject]@{
                        path = $child.FullName
                        depth = $current.depth + 1
                    })
                }
                continue
            }
            if ($child.Extension -match "^\.(py|pyd|dll|json)$") {
                [void]$files.Add($child)
                if ($files.Count -ge $maximumFingerprintFiles) {
                    break
                }
            }
        }
    }

    $manifestLines = @(
        $files |
            ForEach-Object {
                $relative = $_.FullName.Substring($Root.Length).TrimStart("\")
                "${relative}|$($_.Length)|$(Get-FileSha256 -Path $_.FullName)"
            }
    )
    return [pscustomobject]@{
        sampled_file_count = $manifestLines.Count
        sample_truncated = ($files.Count -ge $maximumFingerprintFiles -or $queue.Count -gt 0)
        sha256 = if ($manifestLines.Count -gt 0) {
            Get-Sha256Text -Value ($manifestLines -join "`n")
        }
        else {
            $null
        }
    }
}

function Get-QmtRootFact {
    param([Parameter(Mandatory = $true)][pscustomobject]$Candidate)

    $exists = Test-Path -LiteralPath $Candidate.path -PathType Container
    if (-not $exists) {
        return [pscustomobject]@{
            source = $Candidate.source
            path = Convert-ToEvidencePath -Path $Candidate.path
            exists = $false
            artifacts = @()
            xtquant_directories = @()
        }
    }

    $artifacts = New-Object System.Collections.ArrayList
    $xtquantDirectories = New-Object System.Collections.ArrayList
    $queue = New-Object System.Collections.Queue
    $queue.Enqueue([pscustomobject]@{ path = $Candidate.path; depth = 0 })
    $visitedDirectories = 0

    while ($queue.Count -gt 0 -and $visitedDirectories -lt $maximumDirectoriesPerRoot) {
        $current = $queue.Dequeue()
        $visitedDirectories += 1
        $children = @(Get-ChildItem -LiteralPath $current.path -ErrorAction SilentlyContinue |
            Sort-Object -Property FullName)
        foreach ($child in $children) {
            if ($child.PSIsContainer) {
                if ($child.Name -ieq "xtquant") {
                    $fingerprint = Get-DirectoryFingerprint -Root $child.FullName
                    [void]$xtquantDirectories.Add([pscustomobject]@{
                        path = Convert-ToEvidencePath -Path $child.FullName
                        sampled_file_count = $fingerprint.sampled_file_count
                        sample_truncated = $fingerprint.sample_truncated
                        sha256 = $fingerprint.sha256
                    })
                    continue
                }
                if ($current.depth -lt 6 -and
                    $child.Name -notmatch "^(userdata_mini|logs?|cache|data|history|download)$" -and
                    (($child.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq 0)) {
                    $queue.Enqueue([pscustomobject]@{
                        path = $child.FullName
                        depth = $current.depth + 1
                    })
                }
                continue
            }

            if ($artifacts.Count -lt $maximumArtifactsPerRoot -and
                $child.Extension -match "^\.(exe|dll|pyd)$" -and
                $child.Name -match "QMT|XtQuant|MiniQMT") {
                $version = $null
                try {
                    $version = $child.VersionInfo.FileVersion
                }
                catch {
                    $version = $null
                }
                [void]$artifacts.Add([pscustomobject]@{
                    path = Convert-ToEvidencePath -Path $child.FullName
                    file_version = $version
                    size_bytes = [long]$child.Length
                    sha256 = Get-FileSha256 -Path $child.FullName
                })
            }
        }
    }

    return [pscustomobject]@{
        source = $Candidate.source
        path = Convert-ToEvidencePath -Path $Candidate.path
        exists = $true
        scan_truncated = ($queue.Count -gt 0)
        visited_directory_count = $visitedDirectories
        artifacts = @($artifacts)
        xtquant_directories = @($xtquantDirectories)
    }
}

function Get-UserdataCandidates {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$QmtCandidates
    )

    $items = New-Object System.Collections.ArrayList
    $seen = @{}
    if (-not [string]::IsNullOrWhiteSpace($UserdataMini)) {
        Add-UniqueCandidate -Items $items -Seen $seen `
            -Path (Resolve-AbsoluteInputPath -Value $UserdataMini -ParameterName "UserdataMini") `
            -Source "explicit"
    }
    foreach ($candidate in $QmtCandidates) {
        Add-UniqueCandidate -Items $items -Seen $seen `
            -Path (Join-Path $candidate.path "userdata_mini") -Source "qmt-root"
    }
    return @($items)
}

function Get-UserdataFact {
    param([Parameter(Mandatory = $true)][pscustomobject]$Candidate)

    $exists = Test-Path -LiteralPath $Candidate.path -PathType Container
    $queuePath = Join-Path $Candidate.path "up_queue_xtquant"
    return [pscustomobject]@{
        source = $Candidate.source
        path = Convert-ToEvidencePath -Path $Candidate.path
        exists = $exists
        function_trading_queue_exists = ($exists -and (Test-Path -LiteralPath $queuePath))
    }
}

if (-not [string]::IsNullOrWhiteSpace($QmtRoot)) {
    $QmtRoot = Resolve-AbsoluteInputPath -Value $QmtRoot -ParameterName "QmtRoot"
}
if (-not [string]::IsNullOrWhiteSpace($UserdataMini)) {
    $UserdataMini = Resolve-AbsoluteInputPath -Value $UserdataMini -ParameterName "UserdataMini"
}
if (-not [string]::IsNullOrWhiteSpace($OutputPath)) {
    $OutputPath = Resolve-AbsoluteInputPath -Value $OutputPath -ParameterName "OutputPath"
}

$pythonCandidates = @(Get-PythonCandidates)
$pythonFacts = @($pythonCandidates | ForEach-Object { Get-PythonFact -Candidate $_ })
$qmtCandidates = @(Get-QmtRootCandidates)
$qmtFacts = @($qmtCandidates | ForEach-Object { Get-QmtRootFact -Candidate $_ })
$userdataCandidates = @(Get-UserdataCandidates -QmtCandidates $qmtCandidates)
$userdataFacts = @($userdataCandidates | ForEach-Object { Get-UserdataFact -Candidate $_ })

$pythonReady = @($pythonFacts | Where-Object {
    $_.available -and $_.version -match "^3\.11\." -and $_.architecture_bits -eq 64
}).Count -gt 0
$qmtReady = @($qmtFacts | Where-Object {
    $_.exists -and ($_.artifacts.Count -gt 0 -or $_.xtquant_directories.Count -gt 0)
}).Count -gt 0
$xtquantReady = @($pythonFacts | Where-Object {
    $_.available -and $_.xtquant.available
}).Count -gt 0 -or @($qmtFacts | Where-Object {
    $_.xtquant_directories.Count -gt 0
}).Count -gt 0
$userdataReady = @($userdataFacts | Where-Object { $_.exists }).Count -gt 0
$functionTradingQueueReady = @($userdataFacts | Where-Object {
    $_.function_trading_queue_exists
}).Count -gt 0

$blockers = New-Object System.Collections.ArrayList
if (-not $pythonReady) { [void]$blockers.Add("PYTHON_3_11_X64_NOT_FOUND") }
if (-not $qmtReady) { [void]$blockers.Add("QMT_INSTALLATION_NOT_FOUND") }
if (-not $xtquantReady) { [void]$blockers.Add("XTQUANT_NOT_FOUND") }
if (-not $userdataReady) { [void]$blockers.Add("USERDATA_MINI_NOT_FOUND") }
if (-not $functionTradingQueueReady) {
    [void]$blockers.Add("FUNCTION_TRADING_QUEUE_NOT_FOUND")
}

$operatingSystem = $null
try {
    $operatingSystem = Get-CimInstance Win32_OperatingSystem
}
catch {
    $operatingSystem = $null
}
$hostSource = if ([string]::IsNullOrWhiteSpace($env:COMPUTERNAME)) {
    "unknown-host"
}
else {
    $env:COMPUTERNAME
}

$inventory = [ordered]@{
    schema = "qmt-environment-inventory/v1"
    script_revision = $scriptRevision
    collection_mode = "READ_ONLY"
    collected_at = (Get-Date).ToUniversalTime().ToString("o")
    host_fingerprint = (Get-Sha256Text -Value $hostSource).Substring(0, 16)
    os = [ordered]@{
        caption = if ($null -ne $operatingSystem) {
            [string]$operatingSystem.Caption
        }
        else {
            [System.Environment]::OSVersion.VersionString
        }
        version = if ($null -ne $operatingSystem) {
            [string]$operatingSystem.Version
        }
        else {
            [System.Environment]::OSVersion.Version.ToString()
        }
        architecture = $env:PROCESSOR_ARCHITECTURE
    }
    python = $pythonFacts
    qmt_installations = $qmtFacts
    userdata_mini = $userdataFacts
    checks = [ordered]@{
        python_3_11_x64_found = $pythonReady
        qmt_installation_found = $qmtReady
        xtquant_found = $xtquantReady
        userdata_mini_found = $userdataReady
        function_trading_queue_found = $functionTradingQueueReady
    }
    technical_ready = ($blockers.Count -eq 0)
    blockers = @($blockers)
    manual_evidence_required = @(
        "QMT client version, installer SHA-256 and rollback package",
        "XtQuant release source and package SHA-256",
        "masked broker account and account type",
        "function trading permission approval",
        "market subscription permission approval",
        "program trading compliance approval"
    )
    privacy = [ordered]@{
        raw_computer_name_omitted = $true
        broker_account_not_read = $true
        userdata_contents_not_enumerated = $true
        user_profile_paths_tokenized = $true
    }
}

$json = $inventory | ConvertTo-Json -Depth 10
if (-not [string]::IsNullOrWhiteSpace($OutputPath)) {
    $resolvedOutputPath = Resolve-AbsoluteInputPath -Value $OutputPath -ParameterName "OutputPath"
    $outputDirectory = [System.IO.Path]::GetDirectoryName($resolvedOutputPath)
    if (-not (Test-Path -LiteralPath $outputDirectory -PathType Container)) {
        throw "OutputPath parent directory does not exist: ${outputDirectory}"
    }

    $temporaryPath = "${resolvedOutputPath}.tmp.$([guid]::NewGuid().ToString('N'))"
    $backupPath = $null
    try {
        [System.IO.File]::WriteAllText($temporaryPath, "${json}`r`n", $utf8WithoutBom)
        if (Test-Path -LiteralPath $resolvedOutputPath -PathType Leaf) {
            $backupPath = "${resolvedOutputPath}.backup.$([guid]::NewGuid().ToString('N'))"
            [System.IO.File]::Replace($temporaryPath, $resolvedOutputPath, $backupPath)
        }
        else {
            [System.IO.File]::Move($temporaryPath, $resolvedOutputPath)
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
        if ($null -ne $backupPath -and (Test-Path -LiteralPath $backupPath -PathType Leaf)) {
            Remove-Item -LiteralPath $backupPath -Force
        }
    }
}

$json
if ($RequireReady -and -not $inventory.technical_ready) {
    exit 2
}

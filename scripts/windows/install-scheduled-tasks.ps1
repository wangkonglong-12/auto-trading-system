[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [Parameter(Mandatory = $true)]
    [string]$ServiceUser,
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [Parameter(Mandatory = $true)]
    [string]$RuntimeExecutable,
    [Parameter(Mandatory = $true)]
    [string]$WatchdogExecutable,
    [Parameter(Mandatory = $true)]
    [string[]]$RuntimeArguments,
    [string]$ManifestPath = "$env:ProgramData\AutoTrading\config\scheduled-task-launch.json",
    [string]$LogRoot = "$env:ProgramData\AutoTrading\logs\process",
    [switch]$Replace,
    [switch]$PlanOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "TaskDeployment.Common.ps1")

$runtimeTaskName = "AutoTrading-Runtime"
$watchdogTaskName = "AutoTrading-Watchdog"
$taskPath = "\"

if ([string]::IsNullOrWhiteSpace($ServiceUser) -or $ServiceUser.Length -gt 256 -or
    $ServiceUser.Contains('"') -or $ServiceUser.Contains("`r") -or
    $ServiceUser.Contains("`n")) {
    throw "ServiceUser is invalid"
}
Assert-SafeProcessArguments -Arguments @($RuntimeArguments)
$resolvedConfig = Resolve-ExistingAbsoluteFile -Path $ConfigPath -Name "Runtime config"
$resolvedRuntime = Resolve-ExistingAbsoluteFile `
    -Path $RuntimeExecutable -Name "Runtime executable"
$resolvedWatchdog = Resolve-ExistingAbsoluteFile `
    -Path $WatchdogExecutable -Name "Watchdog executable"
$resolvedManifest = [IO.Path]::GetFullPath($ManifestPath)
$resolvedLogRoot = Resolve-AbsoluteDirectoryPath -Path $LogRoot -Name "log root"
$runtimeScript = Resolve-ExistingAbsoluteFile `
    -Path (Join-Path $PSScriptRoot "start-runtime.ps1") -Name "Runtime launch script"
$watchdogScript = Resolve-ExistingAbsoluteFile `
    -Path (Join-Path $PSScriptRoot "start-watchdog.ps1") -Name "Watchdog launch script"
$powerShellExecutable = Resolve-ExistingAbsoluteFile `
    -Path (Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe") `
    -Name "Windows PowerShell"

$manifest = [ordered]@{
    manifest_version = "1.0"
    config_path = $resolvedConfig
    log_root = $resolvedLogRoot
    runtime = [ordered]@{
        executable = $resolvedRuntime
        sha256 = Get-FileSha256 -Path $resolvedRuntime
        arguments = @($RuntimeArguments)
    }
    watchdog = [ordered]@{
        executable = $resolvedWatchdog
        sha256 = Get-FileSha256 -Path $resolvedWatchdog
    }
}

function ConvertTo-TaskArgument {
    param([Parameter(Mandatory = $true)][string]$Value)
    if ($Value.Contains('"') -or $Value.Contains("`r") -or $Value.Contains("`n")) {
        throw "scheduled task argument contains an unsafe character"
    }
    return '"' + $Value + '"'
}

function New-WrapperArguments {
    param([Parameter(Mandatory = $true)][string]$ScriptPath)
    return @(
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "RemoteSigned",
        "-File",
        (ConvertTo-TaskArgument -Value $ScriptPath),
        "-ManifestPath",
        (ConvertTo-TaskArgument -Value $resolvedManifest)
    ) -join " "
}

$plan = [ordered]@{
    plan_version = "1.0"
    service_user = $ServiceUser
    manifest_path = $resolvedManifest
    runtime_task = [ordered]@{
        name = $runtimeTaskName
        task_path = $taskPath
        trigger = [ordered]@{ kind = "AT_LOGON"; user = $ServiceUser; delay = "PT0S" }
        action = [ordered]@{
            executable = $powerShellExecutable
            arguments = (New-WrapperArguments -ScriptPath $runtimeScript)
        }
        settings = [ordered]@{
            multiple_instances = "IGNORE_NEW"
            execution_time_limit = "PT0S"
            task_restart_count = 0
        }
    }
    watchdog_task = [ordered]@{
        name = $watchdogTaskName
        task_path = $taskPath
        trigger = [ordered]@{ kind = "AT_LOGON"; user = $ServiceUser; delay = "PT30S" }
        action = [ordered]@{
            executable = $powerShellExecutable
            arguments = (New-WrapperArguments -ScriptPath $watchdogScript)
        }
        settings = [ordered]@{
            multiple_instances = "IGNORE_NEW"
            execution_time_limit = "PT0S"
            task_restart_count = 3
            task_restart_interval = "PT1M"
        }
    }
    launch_manifest = $manifest
}

if ($PlanOnly) {
    $plan | ConvertTo-Json -Depth 8
    return
}

$existingTasks = @{}
foreach ($taskName in @($runtimeTaskName, $watchdogTaskName)) {
    $existing = Get-ScheduledTask -TaskName $taskName -TaskPath $taskPath `
        -ErrorAction SilentlyContinue
    if ($null -ne $existing) {
        if (-not $Replace) {
            throw "scheduled task $taskName already exists; use -Replace for an audited replacement"
        }
        $existingTasks[$taskName] = Export-ScheduledTask -TaskName $taskName -TaskPath $taskPath
    }
}

if (-not $PSCmdlet.ShouldProcess(
    "$runtimeTaskName and $watchdogTaskName for $ServiceUser",
    "write launch manifest and register Windows scheduled tasks"
)) {
    return
}

$manifestDirectory = Split-Path -Parent $resolvedManifest
New-Item -ItemType Directory -Path $manifestDirectory -Force | Out-Null
New-Item -ItemType Directory -Path $resolvedLogRoot -Force | Out-Null
$previousManifest = if (Test-Path -LiteralPath $resolvedManifest -PathType Leaf) {
    [IO.File]::ReadAllBytes($resolvedManifest)
} else {
    $null
}
$temporaryManifest = Join-Path $manifestDirectory (
    ".scheduled-task-launch.$PID.$([guid]::NewGuid().ToString('N')).tmp"
)
$registered = [Collections.Generic.List[string]]::new()

try {
    $manifestJson = $manifest | ConvertTo-Json -Depth 6
    [IO.File]::WriteAllText($temporaryManifest, $manifestJson, [Text.UTF8Encoding]::new($false))
    if (Test-Path -LiteralPath $resolvedManifest -PathType Leaf) {
        [IO.File]::Replace($temporaryManifest, $resolvedManifest, $null)
    } else {
        Move-Item -LiteralPath $temporaryManifest -Destination $resolvedManifest
    }

    $runtimeAction = New-ScheduledTaskAction -Execute $powerShellExecutable `
        -Argument $plan.runtime_task.action.arguments
    $watchdogAction = New-ScheduledTaskAction -Execute $powerShellExecutable `
        -Argument $plan.watchdog_task.action.arguments
    $runtimeTrigger = New-ScheduledTaskTrigger -AtLogOn -User $ServiceUser
    $watchdogTrigger = New-ScheduledTaskTrigger -AtLogOn -User $ServiceUser
    $watchdogTrigger.Delay = "PT30S"
    $principal = New-ScheduledTaskPrincipal -UserId $ServiceUser `
        -LogonType Interactive -RunLevel Highest
    $runtimeSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $watchdogSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -RestartCount 3 -RestartInterval ([TimeSpan]::FromMinutes(1))

    Register-ScheduledTask -TaskName $runtimeTaskName -TaskPath $taskPath `
        -Action $runtimeAction -Trigger $runtimeTrigger -Principal $principal `
        -Settings $runtimeSettings -Description "Long-running AutoTrading Runtime" -Force |
        Out-Null
    $registered.Add($runtimeTaskName)
    Register-ScheduledTask -TaskName $watchdogTaskName -TaskPath $taskPath `
        -Action $watchdogAction -Trigger $watchdogTrigger -Principal $principal `
        -Settings $watchdogSettings -Description "Independent AutoTrading Runtime watchdog" -Force |
        Out-Null
    $registered.Add($watchdogTaskName)
}
catch {
    foreach ($taskName in $registered) {
        Unregister-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Confirm:$false `
            -ErrorAction SilentlyContinue
    }
    foreach ($taskName in $existingTasks.Keys) {
        Register-ScheduledTask -TaskName $taskName -TaskPath $taskPath `
            -Xml $existingTasks[$taskName] -Force | Out-Null
    }
    if ($null -eq $previousManifest) {
        Remove-Item -LiteralPath $resolvedManifest -Force -ErrorAction SilentlyContinue
    } else {
        [IO.File]::WriteAllBytes($resolvedManifest, $previousManifest)
    }
    throw
}
finally {
    Remove-Item -LiteralPath $temporaryManifest -Force -ErrorAction SilentlyContinue
}

$plan | ConvertTo-Json -Depth 8

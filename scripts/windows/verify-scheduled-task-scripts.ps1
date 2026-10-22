[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$verificationRoot = Join-Path ([IO.Path]::GetTempPath()) (
    "auto-trading-ops002-$PID-$([guid]::NewGuid().ToString('N'))"
)
$resolvedVerificationRoot = [IO.Path]::GetFullPath($verificationRoot)
if (-not $resolvedVerificationRoot.StartsWith(
    [IO.Path]::GetFullPath([IO.Path]::GetTempPath()),
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw "verification path escaped the temporary directory"
}

try {
    New-Item -ItemType Directory -Path $resolvedVerificationRoot | Out-Null
    $configPath = Join-Path $resolvedVerificationRoot "runtime.json"
    [IO.File]::WriteAllText($configPath, "{}", [Text.UTF8Encoding]::new($false))
    $manifestPath = Join-Path $resolvedVerificationRoot "scheduled-task-launch.json"
    $logRoot = Join-Path $resolvedVerificationRoot "logs"
    $powerShellExecutable = Join-Path `
        $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $installer = Join-Path $PSScriptRoot "install-scheduled-tasks.ps1"
    $runtimeLauncher = Join-Path $PSScriptRoot "start-runtime.ps1"

    $planJson = & $installer `
        -ServiceUser ".\AutoTradingSvc" `
        -ConfigPath $configPath `
        -RuntimeExecutable $powerShellExecutable `
        -WatchdogExecutable $powerShellExecutable `
        -RuntimeArguments @("-NoProfile", "-Command", "exit 0") `
        -ManifestPath $manifestPath `
        -LogRoot $logRoot `
        -PlanOnly
    $plan = $planJson | ConvertFrom-Json
    if ($plan.runtime_task.name -ne "AutoTrading-Runtime" -or
        $plan.runtime_task.trigger.delay -ne "PT0S" -or
        $plan.watchdog_task.name -ne "AutoTrading-Watchdog" -or
        $plan.watchdog_task.trigger.delay -ne "PT30S" -or
        $plan.runtime_task.settings.task_restart_count -ne 0 -or
        $plan.watchdog_task.settings.task_restart_count -ne 3) {
        throw "scheduled task plan does not match the deployment contract"
    }
    if (Test-Path -LiteralPath $manifestPath) {
        throw "PlanOnly unexpectedly wrote a launch manifest"
    }

    $uninstaller = Join-Path $PSScriptRoot "uninstall-scheduled-tasks.ps1"
    $uninstallPlan = (& $uninstaller `
        -EvidenceDirectory (Join-Path $resolvedVerificationRoot "evidence") `
        -PlanOnly) | ConvertFrom-Json
    if ($uninstallPlan.task_names.Count -ne 2 -or
        $uninstallPlan.retained -notcontains "trading data") {
        throw "scheduled task uninstall plan does not preserve operational data"
    }

    $secretArgumentRejected = $false
    try {
        & $installer `
            -ServiceUser ".\AutoTradingSvc" `
            -ConfigPath $configPath `
            -RuntimeExecutable $powerShellExecutable `
            -WatchdogExecutable $powerShellExecutable `
            -RuntimeArguments @("--secret", "unsafe") `
            -ManifestPath $manifestPath `
            -LogRoot $logRoot `
            -PlanOnly | Out-Null
    }
    catch {
        $secretArgumentRejected = $true
    }
    if (-not $secretArgumentRejected) {
        throw "scheduled task installer accepted a secret-bearing command argument"
    }

    $manifestJson = $plan.launch_manifest | ConvertTo-Json -Depth 6
    [IO.File]::WriteAllText(
        $manifestPath,
        $manifestJson,
        [Text.UTF8Encoding]::new($false)
    )
    $launcherStdout = Join-Path $resolvedVerificationRoot "launcher.stdout.log"
    $launcherStderr = Join-Path $resolvedVerificationRoot "launcher.stderr.log"
    $launcherProcess = Start-Process -FilePath $powerShellExecutable `
        -ArgumentList @(
            "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "RemoteSigned",
            "-File", $runtimeLauncher, "-ManifestPath", $manifestPath
        ) `
        -RedirectStandardOutput $launcherStdout -RedirectStandardError $launcherStderr `
        -WindowStyle Hidden -Wait -PassThru
    if ($launcherProcess.ExitCode -ne 0) {
        $failureOutput = [IO.File]::ReadAllText($launcherStderr).Trim()
        throw "Runtime wrapper smoke test failed with exit code $($launcherProcess.ExitCode): $failureOutput"
    }
    if (@(Get-ChildItem -LiteralPath $logRoot -Filter "trade-runtime-*.stdout.log").Count -ne 1 -or
        @(Get-ChildItem -LiteralPath $logRoot -Filter "trade-runtime-*.stderr.log").Count -ne 1) {
        throw "Runtime wrapper did not create bounded per-launch log files"
    }

    $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
    $manifest.runtime.sha256 = "0" * 64
    [IO.File]::WriteAllText(
        $manifestPath,
        ($manifest | ConvertTo-Json -Depth 6),
        [Text.UTF8Encoding]::new($false)
    )
    $tamperedProcess = Start-Process -FilePath $powerShellExecutable `
        -ArgumentList @(
            "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "RemoteSigned",
            "-File", $runtimeLauncher, "-ManifestPath", $manifestPath
        ) `
        -RedirectStandardOutput (Join-Path $resolvedVerificationRoot "tampered.stdout.log") `
        -RedirectStandardError (Join-Path $resolvedVerificationRoot "tampered.stderr.log") `
        -WindowStyle Hidden -Wait -PassThru
    if ($tamperedProcess.ExitCode -eq 0) {
        throw "Runtime wrapper accepted a tampered executable hash"
    }

    Write-Output "Windows scheduled task scripts verified"
}
finally {
    if (Test-Path -LiteralPath $resolvedVerificationRoot) {
        Remove-Item -LiteralPath $resolvedVerificationRoot -Recurse -Force
    }
}

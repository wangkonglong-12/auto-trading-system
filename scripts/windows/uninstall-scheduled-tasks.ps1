[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [string]$EvidenceDirectory = "$env:ProgramData\AutoTrading\evidence\scheduled-tasks",
    [switch]$PlanOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$taskPath = "\"
$taskNames = @("AutoTrading-Runtime", "AutoTrading-Watchdog")
$plan = [ordered]@{
    plan_version = "1.0"
    action = "UNREGISTER_TASKS"
    task_path = $taskPath
    task_names = $taskNames
    evidence_directory = [IO.Path]::GetFullPath($EvidenceDirectory)
    retained = @("launch manifest", "process logs", "trading data", "credentials")
}
if ($PlanOnly) {
    $plan | ConvertTo-Json -Depth 4
    return
}

if (-not $PSCmdlet.ShouldProcess(
    ($taskNames -join ", "),
    "export task XML evidence and unregister Windows scheduled tasks"
)) {
    return
}

$resolvedEvidence = [IO.Path]::GetFullPath($EvidenceDirectory)
New-Item -ItemType Directory -Path $resolvedEvidence -Force | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
foreach ($taskName in $taskNames) {
    $task = Get-ScheduledTask -TaskName $taskName -TaskPath $taskPath `
        -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        continue
    }
    $xml = Export-ScheduledTask -TaskName $taskName -TaskPath $taskPath
    $destination = Join-Path $resolvedEvidence "$taskName-$stamp.xml"
    [IO.File]::WriteAllText($destination, $xml, [Text.UTF8Encoding]::new($false))
    Unregister-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Confirm:$false
}

$plan | ConvertTo-Json -Depth 4

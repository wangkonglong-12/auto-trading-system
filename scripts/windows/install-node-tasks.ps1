[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory = $true)][string]$Python,
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'TaskDeployment.Common.ps1')
$resolvedPython = Resolve-ExistingAbsoluteFile -Path $Python -Name 'Node Python'
$resolvedConfig = Resolve-ExistingAbsoluteFile -Path $ConfigPath -Name 'V2 node configuration'
$output = & $resolvedPython -m auto_trading_system.apps.node_process --config $resolvedConfig task-plan
if ($LASTEXITCODE -ne 0) { throw 'Node configuration/layout validation failed' }
$plan = ($output -join "`n") | ConvertFrom-Json
# Parse through the real Windows scheduler schema without registering a task.
$scheduler = New-Object -ComObject 'Schedule.Service'
$scheduler.Connect()
foreach ($task in $plan.tasks) {
    $definition = $scheduler.NewTask(0)
    $definition.XmlText = $task.xml
}
if (-not $Apply) { $plan | ConvertTo-Json -Depth 8; return }

# Preflight the full set before the first mutation. Existing V1 or V2 tasks
# remain untouched; replacement is a separate reviewed deployment operation.
foreach ($task in $plan.tasks) {
    if ($task.name -notmatch '^AutoTrading\.V2\.[a-f0-9]{16}\.(Engine|Agent|PullPlan|DailyExport|DailyRetry|CatchUp)$') {
        throw 'Unexpected task identity'
    }
    if ($null -ne (Get-ScheduledTask -TaskPath '\' -TaskName $task.name -ErrorAction SilentlyContinue)) {
        throw 'A target task already exists; no tasks were changed'
    }
}
if (-not $PSCmdlet.ShouldProcess('Six scoped AutoTrading V2 tasks', 'Register tasks for the configured interactive user')) { return }
$registered = [Collections.Generic.List[string]]::new()
try {
    foreach ($task in $plan.tasks) {
        Register-ScheduledTask -TaskPath '\' -TaskName $task.name -Xml $task.xml | Out-Null
        $registered.Add($task.name)
    }
} catch {
    # Roll back only the exact tasks successfully created by this invocation.
    foreach ($name in $registered) {
        Unregister-ScheduledTask -TaskPath '\' -TaskName $name -Confirm:$false
    }
    throw
}
$plan.registration_performed = $true
$plan | ConvertTo-Json -Depth 8

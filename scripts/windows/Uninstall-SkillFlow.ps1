[CmdletBinding(SupportsShouldProcess)]
param([Parameter(Mandatory)][string]$InstallRoot, [switch]$Apply, [switch]$RemoveFiles)
$ErrorActionPreference = 'Stop'
$target = (Resolve-Path -LiteralPath $InstallRoot).Path
$marker = Get-Content -LiteralPath (Join-Path $target 'installation.json') -Raw | ConvertFrom-Json
if ($marker.format -ne 'skillflow-install-v1' -or [IO.Path]::GetFullPath($marker.root) -ne $target) { throw 'Installation marker mismatch.' }
if ($target -eq [IO.Path]::GetPathRoot($target)) { throw 'Refusing a filesystem root.' }
$python = Join-Path $target 'runtime\Scripts\python.exe'
$authority = Join-Path $target 'authority'
$tasks = @($marker.task)
if ($marker.proxy_task) { $tasks += $marker.proxy_task }
foreach ($name in $tasks) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $task) { continue }
    $expected = if ($name -eq $marker.task) {
        $arguments = '-m skillflow_prompts serve --root "' + $authority + '"'
        @($arguments, $arguments + ' --qualification')
    } else {
        @('-m skillflow_prompts network-proxy --config "' + (Join-Path $authority 'proxy.json') + '" --log "' + (Join-Path $target 'state\proxy.jsonl') + '"')
    }
    if ($task.Actions.Count -ne 1 -or [IO.Path]::GetFullPath($task.Actions[0].Execute) -ne $python -or
        $task.Actions[0].Arguments -notin $expected -or $task.Actions[0].WorkingDirectory -ne $authority) { throw "Task action ownership mismatch: $name" }
}
$boundary = @()
if ($marker.appcontainer_sid) {
    # A generic uninstall cannot delete the writer's AppContainer profile (it lives in that
    # account) and must never orphan the network boundary: refuse until it has been removed.
    foreach ($rule in @(Get-NetFirewallRule -Group 'SkillFlowMCP' -ErrorAction SilentlyContinue)) {
        if (($rule | Get-NetFirewallApplicationFilter).Package -eq $marker.appcontainer_sid) { $boundary += $rule.Name }
    }
    $loopback = (& "$env:SystemRoot\System32\CheckNetIsolation.exe" LoopbackExempt -s | Out-String)
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect AppContainer loopback exemptions.' }
    if ($loopback.IndexOf($marker.appcontainer_sid, [StringComparison]::OrdinalIgnoreCase) -ge 0) { $boundary += 'loopback-exemption' }
    if ($boundary.Count) { throw "Network boundary still installed for $($marker.appcontainer_sid) ($($boundary -join ', ')); run Remove-SkillFlowNetworkBoundary.ps1 first." }
}
@{action = 'uninstall'; target = $target; tasks = $tasks; preserve_state = (-not $RemoveFiles); applied = [bool]$Apply;
  appcontainer_sid = $marker.appcontainer_sid;
  appcontainer_profile = $(if ($marker.appcontainer_sid) { 'NOT_REMOVED: run Manage-SkillFlowAppContainer.ps1 -Action Delete as the writer account' } else { 'NOT_CONFIGURED' })} | ConvertTo-Json
if (-not $Apply) { return }
foreach ($name in $tasks) {
    if ($PSCmdlet.ShouldProcess($name, 'Stop and unregister SkillFlow task')) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    }
}
if ($RemoveFiles -and $PSCmdlet.ShouldProcess($target, 'Delete verified installation directory including state and keys')) {
    $resolved = (Resolve-Path -LiteralPath $target).Path
    if ($resolved -ne $target -or $resolved -eq [IO.Path]::GetPathRoot($resolved)) { throw 'Target changed or is unsafe.' }
    if ((Get-Item -LiteralPath $resolved).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Target is a reparse point.' }
    # Scan right before deletion: a junction planted in a writable child must never be followed.
    if (Get-ChildItem -LiteralPath $resolved -Recurse -Force | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }) { throw 'Remove reparse ambiguity before reviewed deletion.' }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}

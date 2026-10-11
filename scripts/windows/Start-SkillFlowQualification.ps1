[CmdletBinding(SupportsShouldProcess)]
param([Parameter(Mandatory)][string]$Journal, [PSCredential]$ServiceCredential, [switch]$Apply, [switch]$RestoreProtectedAction)
$ErrorActionPreference = 'Stop'
$record = Get-Content -LiteralPath $Journal -Raw | ConvertFrom-Json
$target = [IO.Path]::GetFullPath($record.root)
if ($record.format -ne 'skillflow-stage-v1' -or $target -ne 'C:\ProgramData\SkillFlowMCP-PrivatePilot' -or
    $record.task -ne 'SkillFlowMCP-PrivatePilot' -or $record.proxy_task -ne 'SkillFlowMCP-PrivatePilot-Proxy' -or
    -not $record.completed -or -not $record.network_installed) { throw 'Unexpected or incomplete private pilot journal.' }
$task = Get-ScheduledTask -TaskName $record.task
$proxyTask = Get-ScheduledTask -TaskName $record.proxy_task
$service = Get-LocalUser -Name 'SkillFlowService'
$saved = @($record.accounts | Where-Object { $_.name -eq 'SkillFlowService' })
if ($saved.Count -ne 1 -or $saved[0].sid -ne $service.SID.Value -or $service.Description -ne "SkillFlow stage $($record.run_id)") { throw 'Dedicated service identity changed.' }
$taskSid = ([Security.Principal.NTAccount]::new($task.Principal.UserId)).Translate([Security.Principal.SecurityIdentifier]).Value
$python = Join-Path $target 'runtime\Scripts\python.exe'
$authority = Join-Path $target 'authority'
$arguments = '-m skillflow serve --root "' + (Join-Path $target 'authority') + '"'
$proxySid = ([Security.Principal.NTAccount]::new($proxyTask.Principal.UserId)).Translate([Security.Principal.SecurityIdentifier]).Value
$proxyArguments = '-m skillflow network-proxy --config "' + (Join-Path $authority 'proxy.json') + '" --log "' + (Join-Path $target 'state\proxy.jsonl') + '"'
if ($taskSid -ne $service.SID.Value -or $task.Actions.Count -ne 1 -or $task.Actions[0].Execute -ne $python -or
    $task.Actions[0].Arguments -notin @($arguments, $arguments + ' --qualification') -or $task.Actions[0].WorkingDirectory -ne $authority) { throw 'Task ownership mismatch.' }
if ($proxySid -ne $service.SID.Value -or $proxyTask.Actions.Count -ne 1 -or $proxyTask.Actions[0].Execute -ne $python -or
    $proxyTask.Actions[0].Arguments -ne $proxyArguments -or $proxyTask.Actions[0].WorkingDirectory -ne $authority) { throw 'Proxy task ownership mismatch.' }
$network = Get-Content -LiteralPath $record.network_journal -Raw | ConvertFrom-Json
if ($network.format -ne 'skillflow-network-boundary-v1' -or -not $network.complete -or $network.rolled_back -or
    $network.appcontainer_sid -ne $record.appcontainer_sid) { throw 'Network boundary is not complete.' }
@{action=$(if ($RestoreProtectedAction) {'restore-stopped-protected-action'} else {'start-bounded-qualification'});
    task=$record.task; proxy_task=$record.proxy_task; service_sid=$taskSid; network_journal=$record.network_journal;
    protected_receipts=$false; applied=[bool]$Apply} | ConvertTo-Json
if (-not $Apply) { return }
$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Reviewed qualification action requires elevation.' }
# A task registered with a stored password must be rewritten with that credential.
if (-not $ServiceCredential) { throw 'Apply requires the dedicated service credential.' }
$credentialSid = ([Security.Principal.NTAccount]::new($ServiceCredential.UserName)).Translate([Security.Principal.SecurityIdentifier]).Value
if ($credentialSid -ne $service.SID.Value) { throw 'Service credential identity mismatch.' }
if (-not $PSCmdlet.ShouldProcess($record.task, 'Stop tasks and switch only the reviewed server action')) { return }
Stop-ScheduledTask -TaskName $record.task -ErrorAction SilentlyContinue
Stop-ScheduledTask -TaskName $record.proxy_task -ErrorAction SilentlyContinue
if (-not $RestoreProtectedAction) { $arguments += ' --qualification' }
$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $authority
Set-ScheduledTask -TaskName $record.task -Action $action -User $ServiceCredential.UserName -Password $ServiceCredential.GetNetworkCredential().Password | Out-Null
function Wait-RunningTask([string]$Name) {
    # Start-ScheduledTask returns once the start request is queued: a task exiting at once
    # would otherwise pass as started. Require a Running state that survives three seconds.
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 250
        if ((Get-ScheduledTask -TaskName $Name).State -eq 'Running') {
            Start-Sleep -Seconds 3
            if ((Get-ScheduledTask -TaskName $Name).State -eq 'Running') { return }
            break
        }
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Task $Name is not running (last result $((Get-ScheduledTaskInfo -TaskName $Name).LastTaskResult))."
}
function Wait-ProxyListener([int]$Port) {
    # The server task must never start against a proxy port nobody owns.
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        Start-Sleep -Milliseconds 250
        if (Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { return }
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Proxy is not listening on 127.0.0.1:$Port."
}
if (-not $RestoreProtectedAction) {
    try {
        Start-ScheduledTask -TaskName $record.proxy_task
        Wait-RunningTask $record.proxy_task
        Wait-ProxyListener ([int]$network.proxy.port)
        Start-ScheduledTask -TaskName $record.task
        Wait-RunningTask $record.task
    } catch {
        Stop-ScheduledTask -TaskName $record.task -ErrorAction SilentlyContinue
        Stop-ScheduledTask -TaskName $record.proxy_task -ErrorAction SilentlyContinue
        throw
    }
}

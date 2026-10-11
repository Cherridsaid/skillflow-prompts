[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$Journal,
    [PSCredential]$WriterCredential,
    [switch]$Apply,
    [switch]$RemoveFiles,
    [switch]$RemoveAccounts
)
$ErrorActionPreference = 'Stop'
$record = Get-Content -LiteralPath $Journal -Raw | ConvertFrom-Json
$target = [IO.Path]::GetFullPath($record.root)
if ($record.format -ne 'skillflow-stage-v1' -or $record.run_id -notmatch '^[a-f0-9]{32}$') { throw 'Unrecognized staging journal.' }
if ($target -ne 'C:\ProgramData\SkillFlowMCP-PrivatePilot' -or $record.task -ne 'SkillFlowMCP-PrivatePilot' -or
    $record.proxy_task -ne 'SkillFlowMCP-PrivatePilot-Proxy') { throw 'Unexpected private pilot target or tasks.' }
$accounts = @()
foreach ($name in @('SkillFlowWriter','SkillFlowReviewer','SkillFlowService')) {
    $current = Get-LocalUser -Name $name -ErrorAction SilentlyContinue
    if (-not $current) { continue }
    $saved = @($record.accounts | Where-Object { $_.name -eq $name })
    # A hard interruption can occur between New-LocalUser and journal flush.
    if ($current.Description -ne "SkillFlow stage $($record.run_id)" -or ($saved.Count -gt 0 -and $saved[0].sid -ne $current.SID.Value)) { throw "Account ownership mismatch: $name" }
    $accounts += $current
}
$tasks = @{}
foreach ($name in @($record.task,$record.proxy_task)) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $task) { continue }
    $expected = Join-Path $target 'runtime\Scripts\python.exe'
    $authority = Join-Path $target 'authority'
    $expectedArguments = if ($name -eq $record.task) {
        $arguments = '-m skillflow serve --root "' + $authority + '"'
        @($arguments, $arguments + ' --qualification')
    } else {
        @('-m skillflow network-proxy --config "' + (Join-Path $authority 'proxy.json') + '" --log "' + (Join-Path $target 'state\proxy.jsonl') + '"')
    }
    if ($task.Actions.Count -ne 1 -or [IO.Path]::GetFullPath($task.Actions[0].Execute) -ne $expected -or
        $task.Actions[0].Arguments -notin $expectedArguments -or $task.Actions[0].WorkingDirectory -ne $authority) { throw "Task action ownership mismatch: $name" }
    $service = @($accounts | Where-Object { $_.Name -eq 'SkillFlowService' })
    $taskSid = ([Security.Principal.NTAccount]::new($task.Principal.UserId)).Translate([Security.Principal.SecurityIdentifier]).Value
    if ($service.Count -ne 1 -or $taskSid -ne $service[0].SID.Value) { throw 'Task service identity mismatch.' }
    $tasks[$name] = $task
}
if (Test-Path -LiteralPath $target) {
    $resolved = (Resolve-Path -LiteralPath $target).Path
    if ($resolved -ne $target -or (Get-Item -LiteralPath $target).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Unsafe resolved installation path.' }
    if ($RemoveFiles -and (Get-ChildItem -LiteralPath $target -Recurse -Force | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint })) { throw 'Remove reparse ambiguity before reviewed deletion.' }
}
if ($Apply -and $record.appcontainer_created -and -not $WriterCredential) { throw 'AppContainer rollback requires the dedicated writer credential.' }
if ($WriterCredential -and $WriterCredential.UserName -ne "$([Environment]::MachineName)\SkillFlowWriter") { throw 'Writer credential identity mismatch.' }
@{action = 'rollback-private-pilot'; root = $target; tasks = @($tasks.Keys);
    accounts = @($accounts | ForEach-Object { @{name=$_.Name;sid=$_.SID.Value} });
    network_journal = $record.network_journal; appcontainer = $record.appcontainer_sid;
    default = 'stop and unregister tasks; remove network boundary and AppContainer profile; disable owned accounts; require their sessions closed; preserve files and evidence';
    remove_files = [bool]$RemoveFiles; remove_accounts = [bool]$RemoveAccounts; applied = [bool]$Apply} | ConvertTo-Json -Depth 5
if (-not $Apply) { return }
$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Reviewed rollback requires elevation.' }
if (-not $PSCmdlet.ShouldProcess($target, 'Roll back only the journal-owned private pilot')) { return }
foreach ($name in @($tasks.Keys)) {
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
}
if ($record.network_journal -and (Test-Path -LiteralPath $record.network_journal)) {
    & (Join-Path $PSScriptRoot 'Remove-SkillFlowNetworkBoundary.ps1') -Journal $record.network_journal -Apply
    $record.network_installed = $false
    $record | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $Journal -Encoding utf8
}
if ($record.appcontainer_created) {
    $writer = @($accounts | Where-Object { $_.Name -eq 'SkillFlowWriter' })
    if ($writer.Count -ne 1) { throw 'Owned writer account is required for AppContainer rollback.' }
    if (-not $writer[0].Enabled) { Enable-LocalUser -SID $writer[0].SID }
    $profileTask = "SkillFlowMCP-ProfileRollback-$($record.run_id)"
    $profileScript = Join-Path $PSScriptRoot 'Manage-SkillFlowAppContainer.ps1'
    $profileArguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $profileScript +
        '" -ExpectedAccount "' + $WriterCredential.UserName + '" -ExpectedSid "' + $record.appcontainer_sid + '" -Action Delete -Apply'
    $profileAction = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument $profileArguments
    $profileSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::FromMinutes(1)) -MultipleInstances IgnoreNew -Hidden
    try {
        Register-ScheduledTask -TaskName $profileTask -Action $profileAction -Settings $profileSettings `
            -User $WriterCredential.UserName -Password $WriterCredential.GetNetworkCredential().Password | Out-Null
        Start-ScheduledTask -TaskName $profileTask
        $deadline = [DateTime]::UtcNow.AddSeconds(45)
        do {
            Start-Sleep -Milliseconds 250
            $profileInfo = Get-ScheduledTaskInfo -TaskName $profileTask
            $profileState = (Get-ScheduledTask -TaskName $profileTask).State
        } while (($profileInfo.LastRunTime.Year -le 2000 -or $profileState -eq 'Running') -and [DateTime]::UtcNow -lt $deadline)
        if ($profileState -eq 'Running' -or $profileInfo.LastTaskResult -ne 0) { throw "AppContainer rollback task failed: $($profileInfo.LastTaskResult)" }
    } finally {
        Stop-ScheduledTask -TaskName $profileTask -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $profileTask -Confirm:$false -ErrorAction SilentlyContinue
    }
    $record.appcontainer_created = $false
    $record | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $Journal -Encoding utf8
}
foreach ($account in $accounts) {
    Disable-LocalUser -SID $account.SID
}
# Disabling logon does not revoke existing process tokens. Refuse destructive
# cleanup until the operator has closed all sessions of these dedicated users.
$live = @()
$unknown = @()
foreach ($process in (Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -notin @(0,4) })) {
    try {
        $owner = Invoke-CimMethod -InputObject $process -MethodName GetOwnerSid
        if ($owner.ReturnValue -ne 0 -or -not $owner.Sid) { $unknown += $process.ProcessId }
        elseif ($owner.Sid -in @($accounts | ForEach-Object { $_.SID.Value })) { $live += $process.ProcessId }
    } catch {
        # A process that exited is harmless; a live process with unknown ownership is not proof.
        if (Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue) { $unknown += $process.ProcessId }
    }
}
if ($live.Count) { throw 'Accounts disabled, task stopped. Close remaining dedicated-account sessions before cleanup; rollback remains incomplete.' }
if ($unknown.Count) { throw ('Accounts disabled, task stopped. Ownership remains unknown for PIDs: ' + ($unknown -join ',') + '. Destructive cleanup refused; rollback remains incomplete.') }
if ($RemoveAccounts) { foreach ($account in $accounts) { Remove-LocalUser -SID $account.SID } }
if ($RemoveFiles -and (Test-Path -LiteralPath $target)) {
    if ((Resolve-Path -LiteralPath $target).Path -ne $target) { throw 'Installation target changed.' }
    $resolved = $target
    # Scan right before deletion: a junction planted in a writable child must never be followed.
    if (Get-ChildItem -LiteralPath $resolved -Recurse -Force | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }) { throw 'Remove reparse ambiguity before reviewed deletion.' }
    Remove-Item -LiteralPath $target -Recurse -Force
}

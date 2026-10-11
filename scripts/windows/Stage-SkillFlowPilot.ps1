[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$Bundle,
    [Parameter(Mandatory)][string]$OperatorAccount,
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'
$bundlePath = (Resolve-Path -LiteralPath $Bundle).Path
$plan = Get-Content -LiteralPath (Join-Path $bundlePath 'stage-plan.json') -Raw | ConvertFrom-Json
$journalPath = Join-Path $bundlePath 'stage-journal.json'
$networkJournal = Join-Path $bundlePath 'network-boundary-journal.json'
if (Test-Path -LiteralPath $journalPath) { throw 'Existing staging journal: inspect or roll back that run first.' }
if ([IO.Path]::GetFullPath($plan.install_root) -ne 'C:\ProgramData\SkillFlowMCP-PrivatePilot') { throw 'Unexpected private pilot installation root.' }
$pilotMachineName = [Environment]::MachineName
$parameters = @{
    InstallRoot = $plan.install_root; PreparedAuthority = (Join-Path $bundlePath 'authority')
    Wheel = (Join-Path $bundlePath $plan.wheel); WheelSha256 = $plan.wheel_sha256
    Requirements = (Join-Path $bundlePath 'requirements.lock.txt'); Python = $plan.python; Uv = $plan.uv
    WriterAccount = "$pilotMachineName\SkillFlowWriter"
    ReviewerAccount = "$pilotMachineName\SkillFlowReviewer"
    OperatorAccount = $OperatorAccount; PreparedSkills = (Join-Path $bundlePath 'skills')
    PreparedClients = (Join-Path $bundlePath 'clients'); AppContainerSid = $plan.appcontainer_sid
    ProxyHosts = @($plan.proxy_hosts); ProxyPort = $plan.proxy_port
    TaskName = 'SkillFlowMCP-PrivatePilot'; ProxyTaskName = 'SkillFlowMCP-PrivatePilot-Proxy'
}
if (-not $plan.appcontainer_sid -or -not $plan.proxy_hosts -or -not $plan.proxy_port) { throw 'Stage plan requires AppContainer SID, proxy hosts and proxy port.' }
$profileName = 'S.Cherrid.SkillFlow.Client'
$profileScript = Join-Path $PSScriptRoot 'Manage-SkillFlowAppContainer.ps1'
$networkInstaller = Join-Path $PSScriptRoot 'Install-SkillFlowNetworkBoundary.ps1'
$installer = Join-Path $PSScriptRoot 'Install-SkillFlow.ps1'
$installationPlan = & $installer @parameters | ConvertFrom-Json
@{installation = $installationPlan; new_local_accounts = @('SkillFlowWriter','SkillFlowReviewer','SkillFlowService'); operator = $OperatorAccount;
  public_network_listener = $false; tasks_started = $false; appcontainer_sid=$plan.appcontainer_sid;
  proxy_hosts=@($plan.proxy_hosts); client_authentication = 'requires separate writer/reviewer login';
  acceptance = 'NOT_RUN'; rights = 'read-only runtime/catalog; private service authority; writable service state; separate role credentials'} | ConvertTo-Json
if (-not $Apply) { return }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run the reviewed staging command from an elevated PowerShell session.' }
foreach ($name in @('SkillFlowWriter','SkillFlowReviewer','SkillFlowService')) {
    if (Get-LocalUser -Name $name -ErrorAction SilentlyContinue) { throw "Account $name already exists; review it before a separate migration." }
}
if (-not $PSCmdlet.ShouldProcess($plan.install_root, 'Create three limited local accounts and stage the stopped private pilot')) { return }
$usersGroup = (Get-LocalGroup -SID 'S-1-5-32-545').Name
$journal = [ordered]@{format = 'skillflow-stage-v1'; run_id = [Guid]::NewGuid().ToString('N');
    root = [IO.Path]::GetFullPath($plan.install_root); task = $parameters.TaskName;
    proxy_task = $parameters.ProxyTaskName; accounts = @(); appcontainer_name = $profileName;
    appcontainer_sid = $plan.appcontainer_sid; appcontainer_intended = $true;
    network_journal = [IO.Path]::GetFullPath($networkJournal); accounts_created = $false;
    appcontainer_created = $false; package_installed = $false; network_installed = $false;
    completed = $false}
$journal | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $journalPath -Encoding utf8
foreach ($name in @('SkillFlowWriter','SkillFlowReviewer','SkillFlowService')) {
    $secret = Read-Host "New password for $name (never recorded in the bundle)" -AsSecureString
    $account = New-LocalUser -Name $name -Password $secret -Description "SkillFlow stage $($journal.run_id)" -PasswordNeverExpires -UserMayNotChangePassword
    $journal.accounts += @{name = $name; sid = $account.SID.Value}
    $journal | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $journalPath -Encoding utf8
    Add-LocalGroupMember -Group $usersGroup -Member $account
    if ($name -eq 'SkillFlowWriter') { $writerCredential = [PSCredential]::new("$pilotMachineName\$name", $secret) }
    if ($name -eq 'SkillFlowService') { $serviceCredential = [PSCredential]::new("$pilotMachineName\$name", $secret) }
}
$journal.accounts_created = $true
$journal | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $journalPath -Encoding utf8
$profileTask = "SkillFlowMCP-Profile-$($journal.run_id)"
$profileArguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $profileScript +
    '" -ExpectedAccount "' + $writerCredential.UserName + '" -ExpectedSid "' + $plan.appcontainer_sid + '" -Apply'
$profileAction = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument $profileArguments
$profileSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::FromMinutes(1)) -MultipleInstances IgnoreNew -Hidden
try {
    Register-ScheduledTask -TaskName $profileTask -Action $profileAction -Settings $profileSettings `
        -User $writerCredential.UserName -Password $writerCredential.GetNetworkCredential().Password | Out-Null
    Start-ScheduledTask -TaskName $profileTask
    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    do {
        Start-Sleep -Milliseconds 250
        $profileInfo = Get-ScheduledTaskInfo -TaskName $profileTask
    } while ($profileInfo.LastRunTime.Year -le 2000 -and [DateTime]::UtcNow -lt $deadline)
    do {
        $profileState = (Get-ScheduledTask -TaskName $profileTask).State
        if ($profileState -ne 'Running') { break }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    $profileInfo = Get-ScheduledTaskInfo -TaskName $profileTask
    if ($profileState -eq 'Running' -or $profileInfo.LastTaskResult -ne 0) { throw "AppContainer profile task failed: $($profileInfo.LastTaskResult)" }
} finally {
    Stop-ScheduledTask -TaskName $profileTask -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $profileTask -Confirm:$false -ErrorAction SilentlyContinue
}
$journal.appcontainer_created = $true
$journal | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $journalPath -Encoding utf8
& $installer @parameters -ServiceCredential $serviceCredential -Apply
$journal.package_installed = $true
$journal | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $journalPath -Encoding utf8
& $networkInstaller -AppContainerSid $plan.appcontainer_sid -ProxyPort $plan.proxy_port `
    -Journal $networkJournal -Apply
$journal.network_installed = $true
$journal | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $journalPath -Encoding utf8
$journal.completed = $true
$journal | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $journalPath -Encoding utf8
Write-Output 'Staged only. Keep the journal for reviewed rollback. Protected acceptance is still required.'

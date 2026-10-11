[CmdletBinding(SupportsShouldProcess)]
param([Parameter(Mandatory)][string]$Journal, [switch]$Apply)
$ErrorActionPreference = 'Stop'
$journalPath = (Resolve-Path -LiteralPath $Journal).Path
$record = Get-Content -LiteralPath $journalPath -Raw | ConvertFrom-Json
if ($record.format -ne 'skillflow-network-boundary-v1' -or
    $record.appcontainer_sid -notmatch '^S-1-15-2(?:-[0-9]+)+$') { throw 'Unrecognized network boundary journal.' }
$known = @('SkillFlowMCP-ProxyOnly-NonLoopback-v4','SkillFlowMCP-ProxyOnly-NonLoopback-v6',
    'SkillFlowMCP-ProxyOnly-OtherTcp','SkillFlowMCP-ProxyOnly-LoopbackTcp6','SkillFlowMCP-ProxyOnly-LoopbackUdp',
    'SkillFlowMCP-ProxyOnly-LoopbackIcmp4','SkillFlowMCP-ProxyOnly-LoopbackIcmp6','SkillFlowMCP-ProxyOnly-Inbound')
# Remove exactly the rules this journal created: journals written before the ICMP rules stay valid.
$expected = @($record.created_rules | Select-Object -Unique)
if (-not $expected.Count -or @($expected | Where-Object { $_ -notin $known }).Count) {
    throw 'Journal contains unexpected firewall rules.'
}
foreach ($name in $expected) {
    $rule = Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue
    if (-not $rule) { continue }
    $package = $rule | Get-NetFirewallApplicationFilter
    if ($package.Package -ne $record.appcontainer_sid) { throw "Firewall rule ownership mismatch: $name" }
}
$loopback = (& "$env:SystemRoot\System32\CheckNetIsolation.exe" LoopbackExempt -s | Out-String)
if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect AppContainer loopback exemptions.' }
$loopbackPresent = $loopback.IndexOf($record.appcontainer_sid, [StringComparison]::OrdinalIgnoreCase) -ge 0
@{format='skillflow-network-rollback-v1'; appcontainer_sid=$record.appcontainer_sid;
    remove_rules=$expected; remove_loopback_exemption=$loopbackPresent; preserve_journal=$true;
    applied=[bool]$Apply} | ConvertTo-Json -Depth 4
if (-not $Apply) { return }
$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Administrative network rollback is required.' }
if (-not $PSCmdlet.ShouldProcess($record.appcontainer_sid, 'Remove owned SkillFlow network boundary')) { return }
foreach ($name in $expected) { Remove-NetFirewallRule -Name $name -ErrorAction SilentlyContinue }
if ($loopbackPresent) {
    & "$env:SystemRoot\System32\CheckNetIsolation.exe" LoopbackExempt -d -p=$record.appcontainer_sid | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Loopback exemption rollback failed.' }
}
$record.rolled_back = $true
$record.complete = $false
$temporary = $journalPath + '.tmp'
[IO.File]::WriteAllText($temporary, ($record | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $temporary -Destination $journalPath -Force

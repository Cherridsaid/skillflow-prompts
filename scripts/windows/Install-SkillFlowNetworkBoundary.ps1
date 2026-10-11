[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][ValidatePattern('^S-1-15-2(?:-[0-9]+)+$')][string]$AppContainerSid,
    [Parameter(Mandatory)][ValidateRange(1024,65534)][int]$ProxyPort,
    [Parameter(Mandatory)][string]$Journal,
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'
$rules = @(
    @{Name='SkillFlowMCP-ProxyOnly-NonLoopback-v4'; Direction='Outbound'; Protocol='Any'; RemoteAddress=@('0.0.0.0-127.0.0.0','127.0.0.2-255.255.255.255')},
    @{Name='SkillFlowMCP-ProxyOnly-NonLoopback-v6'; Direction='Outbound'; Protocol='Any'; RemoteAddress=@('::-::','::2-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff')},
    @{Name='SkillFlowMCP-ProxyOnly-OtherTcp'; Direction='Outbound'; Protocol='TCP'; RemoteAddress=@('127.0.0.1'); RemotePort=@("1-$($ProxyPort-1)","$($ProxyPort+1)-65535")},
    @{Name='SkillFlowMCP-ProxyOnly-LoopbackTcp6'; Direction='Outbound'; Protocol='TCP'; RemoteAddress=@('::1')},
    @{Name='SkillFlowMCP-ProxyOnly-LoopbackUdp'; Direction='Outbound'; Protocol='UDP'; RemoteAddress=@('127.0.0.1','::1')},
    @{Name='SkillFlowMCP-ProxyOnly-LoopbackIcmp4'; Direction='Outbound'; Protocol='ICMPv4'; RemoteAddress=@('127.0.0.1')},
    @{Name='SkillFlowMCP-ProxyOnly-LoopbackIcmp6'; Direction='Outbound'; Protocol='ICMPv6'; RemoteAddress=@('::1')},
    @{Name='SkillFlowMCP-ProxyOnly-Inbound'; Direction='Inbound'; Protocol='Any'; RemoteAddress='Any'}
)
$journalPath = [IO.Path]::GetFullPath($Journal)
if ($journalPath -eq [IO.Path]::GetPathRoot($journalPath)) { throw 'Journal cannot be a filesystem root.' }
if (Test-Path -LiteralPath $journalPath) { throw 'Network boundary journal already exists.' }
foreach ($rule in $rules) {
    if (Get-NetFirewallRule -Name $rule.Name -ErrorAction SilentlyContinue) { throw "Firewall rule already exists: $($rule.Name)" }
}
$existingLoopback = (& "$env:SystemRoot\System32\CheckNetIsolation.exe" LoopbackExempt -s | Out-String)
if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect AppContainer loopback exemptions.' }
if ($existingLoopback.IndexOf($AppContainerSid, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
    throw 'AppContainer loopback exemption already exists; ownership is unknown.'
}
$plan = [ordered]@{format='skillflow-network-boundary-v1'; appcontainer_sid=$AppContainerSid;
    proxy=@{host='127.0.0.1'; port=$ProxyPort}; loopback_exemption='add exact SID';
    firewall_rules=@($rules | ForEach-Object { $_.Name });
    effect='deny inbound; deny non-loopback outbound; deny loopback TCP except the proxy port on 127.0.0.1 (the listener is IPv4 only), deny loopback UDP and ICMP; other IP protocols to loopback stay unfiltered (an AppContainer cannot open raw sockets)';
    applied=$false}
$plan | ConvertTo-Json -Depth 5
if (-not $Apply) { return }
$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Administrative network configuration is required.' }
if (-not $PSCmdlet.ShouldProcess($AppContainerSid, 'Install proxy-only AppContainer network boundary')) { return }
$parent = Split-Path -Parent $journalPath
if (-not (Test-Path -LiteralPath $parent)) { throw 'Journal parent must already exist.' }
$created = @()
$loopbackAdded = $false
$record = [ordered]@{format='skillflow-network-boundary-v1'; appcontainer_sid=$AppContainerSid;
    proxy=$plan.proxy; created_rules=@($rules | ForEach-Object { $_.Name }); loopback_intended=$true;
    complete=$false; rolled_back=$false}
function Write-Journal {
    $temporary = $journalPath + '.tmp'
    [IO.File]::WriteAllText($temporary, ($record | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $journalPath -Force
}
Write-Journal
try {
    & "$env:SystemRoot\System32\CheckNetIsolation.exe" LoopbackExempt -a -p=$AppContainerSid | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Loopback exemption failed.' }
    $loopbackAdded = $true
    foreach ($rule in $rules) {
        $parameters = @{Name=$rule.Name; DisplayName=$rule.Name; Group='SkillFlowMCP'; Enabled='True';
            Profile='Any'; Direction=$rule.Direction; Action='Block'; Package=$AppContainerSid;
            Protocol=$rule.Protocol; RemoteAddress=$rule.RemoteAddress}
        if ($rule.RemotePort) { $parameters.RemotePort = $rule.RemotePort }
        New-NetFirewallRule @parameters | Out-Null
        $created += $rule.Name
    }
    $record.complete = $true
    Write-Journal
} catch {
    foreach ($name in $created) { Remove-NetFirewallRule -Name $name -ErrorAction SilentlyContinue }
    if ($loopbackAdded) { & "$env:SystemRoot\System32\CheckNetIsolation.exe" LoopbackExempt -d -p=$AppContainerSid | Out-Null }
    $record.rolled_back = $true
    Write-Journal
    throw
}

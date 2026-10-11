[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$InstallRoot,
    [Parameter(Mandatory)][string]$PreparedAuthority,
    [Parameter(Mandatory)][string]$Wheel,
    [Parameter(Mandatory)][string]$WheelSha256,
    [Parameter(Mandatory)][string]$Requirements,
    [Parameter(Mandatory)][string]$Python,
    [Parameter(Mandatory)][string]$Uv,
    [Parameter(Mandatory)][string]$WriterAccount,
    [Parameter(Mandatory)][string]$ReviewerAccount,
    [Parameter(Mandatory)][string]$OperatorAccount,
    [string]$PreparedSkills,
    [string]$PreparedClients,
    [ValidatePattern('^S-1-15-2(?:-[0-9]+)+$')][string]$AppContainerSid,
    [string[]]$ProxyHosts,
    [ValidateRange(1024,65534)][int]$ProxyPort = 18771,
    [PSCredential]$ServiceCredential,
    [string]$TaskName = 'SkillFlowMCP',
    [string]$ProxyTaskName = 'SkillFlowMCP-Proxy',
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'
$target = [IO.Path]::GetFullPath($InstallRoot)
if ($target -eq [IO.Path]::GetPathRoot($target)) { throw 'A filesystem root is not an installation directory.' }
if (Test-Path -LiteralPath $target) { throw 'Installation target must not already exist.' }
foreach ($name in @($TaskName,$ProxyTaskName)) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) { throw "Task name already exists: $name" }
}
$source = (Resolve-Path -LiteralPath $PreparedAuthority).Path
if ((Get-Item -LiteralPath $source).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Prepared authority root must not be a reparse point.' }
$package = (Resolve-Path -LiteralPath $Wheel).Path
if ((Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash -ne $WheelSha256) { throw 'Wheel checksum mismatch.' }
$config = Get-Content -LiteralPath (Join-Path $source 'config.json') -Raw | ConvertFrom-Json
if ($PreparedSkills) {
    $skillSource = (Resolve-Path -LiteralPath $PreparedSkills).Path
    if ((Get-Item -LiteralPath $skillSource).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Prepared skills root must not be a reparse point.' }
    if (Get-ChildItem -LiteralPath $skillSource -Recurse -Force | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }) { throw 'Prepared skills must not contain reparse points.' }
    foreach ($clientName in @('codex','claude')) {
        if (-not (Test-Path -LiteralPath (Join-Path $skillSource $clientName) -PathType Container)) { throw "Prepared skills require a separate $clientName catalogue." }
    }
    $workspacePrefix = (Join-Path $target 'workspaces') + [IO.Path]::DirectorySeparatorChar
    foreach ($binding in $config.client_bindings.PSObject.Properties.Value) {
        if (-not ([IO.Path]::GetFullPath($binding.workspace)).StartsWith($workspacePrefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Prepared workspace must remain inside installation workspaces.' }
    }
}
if ([bool]$PreparedClients -ne [bool]$AppContainerSid) { throw 'Prepared clients and AppContainer SID must be supplied together.' }
if ($AppContainerSid -and -not $ProxyHosts) { throw 'AppContainer clients require explicit proxy hosts.' }
foreach ($hostName in $ProxyHosts) {
    if ($hostName -cnotmatch '^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$' -or $hostName.Contains('*') -or $hostName.Contains('..') -or
        $hostName -match '^[0-9.]+$' -or $hostName -eq 'localhost') { throw 'Proxy hosts must be exact lowercase DNS names, never IP literals.' }
}
if (@($ProxyHosts | Sort-Object -Unique).Count -ne @($ProxyHosts).Count) { throw 'Proxy hosts must be unique.' }
if ($PreparedClients) {
    $clientSource = (Resolve-Path -LiteralPath $PreparedClients).Path
    if ((Get-Item -LiteralPath $clientSource).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Prepared clients root must not be a reparse point.' }
    if (Get-ChildItem -LiteralPath $clientSource -Recurse -Force | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }) { throw 'Prepared clients must not contain reparse points.' }
    $clientManifest = Get-Content -LiteralPath (Join-Path $clientSource 'manifest.json') -Raw | ConvertFrom-Json
    if ($clientManifest.format -ne 'skillflow-native-clients-v1' -or $clientManifest.platform -ne 'windows-x86_64') { throw 'Unsupported native client manifest.' }
    $clientNames = @($clientManifest.clients.PSObject.Properties.Name)
    if ($clientNames.Count -ne 2 -or 'codex' -notin $clientNames -or 'claude' -notin $clientNames) { throw 'Native client manifest must declare exact Codex and Claude clients.' }
    $declared = @{}
    foreach ($file in $clientManifest.files) {
        if ($file.path -notmatch '^[a-zA-Z0-9_.\/-]+$' -or $file.path -match '(^|/)\.\.(/|$)') { throw 'Unsafe native client path.' }
        $candidate = [IO.Path]::GetFullPath((Join-Path $clientSource $file.path))
        if (-not $candidate.StartsWith($clientSource + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or -not (Test-Path -LiteralPath $candidate -PathType Leaf)) { throw 'Native client file missing or escaped its bundle.' }
        if ((Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash -ne $file.sha256) { throw 'Native client checksum mismatch.' }
        $declared[$candidate.ToLowerInvariant()] = $true
    }
    foreach ($name in @('codex','claude')) {
        $client = $clientManifest.clients.$name
        if (-not $client.version -or $client.executable -notmatch '^[a-zA-Z0-9_.\/-]+$' -or $client.executable -match '(^|/)\.\.(/|$)') { throw "Invalid native client declaration: $name" }
        $executable = [IO.Path]::GetFullPath((Join-Path $clientSource $client.executable))
        if (-not $declared.ContainsKey($executable.ToLowerInvariant()) -or -not (Test-Path -LiteralPath $executable -PathType Leaf)) { throw "Native client executable is not pinned: $name" }
    }
    $actual = @(Get-ChildItem -LiteralPath $clientSource -Recurse -File -Force | Where-Object { $_.Name -ne 'manifest.json' })
    if ($actual | Where-Object { $_.Attributes -band ([IO.FileAttributes]::Hidden -bor [IO.FileAttributes]::System) }) { throw 'Native client bundle must not contain hidden or system files.' }
    if ($actual.Count -ne $declared.Count -or @($actual | Where-Object { -not $declared.ContainsKey($_.FullName.ToLowerInvariant()) }).Count) { throw 'Native client bundle contains undeclared files.' }
}
if ($config.mode -ne 'protected') { throw 'Prepare an explicit protected configuration; demo cannot be installed as protection.' }
if (Get-ChildItem -LiteralPath $source -Recurse -Force | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }) { throw 'Prepared authority must contain ordinary files and directories.' }
if (Get-ChildItem -LiteralPath $source -Filter 'state.db*') { throw 'Use a fresh prepared authority; migrate state separately after staging.' }
foreach ($destination in $config.destinations.PSObject.Properties) {
    if ($destination.Name -notmatch '^[a-zA-Z0-9_-]+$') { throw 'Destination IDs must contain letters, digits, hyphens or underscores.' }
}
$plan = [ordered]@{
    action = 'stage-protected-service'; target = $target; authority = $source
    task = $TaskName; runtime = 'protected Python base copy and isolated venv'; service = 'explicit limited account; never SYSTEM'
    writer = $WriterAccount; reviewer = $ReviewerAccount; operator = $OperatorAccount
    appcontainer = $(if ($AppContainerSid) { $AppContainerSid } else { 'NOT_CONFIGURED' })
    native_clients = $(if ($PreparedClients) { 'pinned private bundle' } else { 'NOT_CONFIGURED' })
    proxy = $(if ($AppContainerSid) { "stopped task $ProxyTaskName on loopback port $ProxyPort" } else { 'NOT_CONFIGURED' })
    workspace_rights = 'ReadAndExecute for writer; Modify only on dedicated scratch child'
    state = 'NOT_INSTALLED'; protected_acceptance = 'NOT_RUN'; starts_automatically = $false
}
$plan | ConvertTo-Json -Depth 4
if (-not $Apply) { return }
if (-not $ServiceCredential) { throw 'Apply requires a dedicated limited service credential.' }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Administrative installation is required.' }
$service = $ServiceCredential.UserName
if ($service -match '(?i)(^|\\)(SYSTEM|LocalSystem)$') { throw 'LocalSystem is not supported.' }
if (@($service, $WriterAccount, $ReviewerAccount, $OperatorAccount | Sort-Object -Unique).Count -ne 4) { throw 'Use four separate identities.' }
foreach ($account in @($service, $WriterAccount, $ReviewerAccount, $OperatorAccount)) {
    # Resolve every identity before any effect: a typo must not leave a half-built target.
    try { $null = ([Security.Principal.NTAccount]::new($account)).Translate([Security.Principal.SecurityIdentifier]) }
    catch { throw "Identity cannot be resolved before installation: $account (use MACHINE\name)" }
}
if (-not $PSCmdlet.ShouldProcess($target, 'Install isolated runtime, set ACLs and register stopped task')) { return }
New-Item -ItemType Directory -Path $target | Out-Null
# Marker before any effect: an interrupted installation must stay uninstallable.
$marker = Join-Path $target 'installation.json'
function Write-Marker([bool]$Complete) {
    @{format = 'skillflow-install-v1'; root = $target; task = $TaskName;
      proxy_task = $(if ($AppContainerSid) { $ProxyTaskName } else { $null }); appcontainer_sid = $AppContainerSid; complete = $Complete} |
      ConvertTo-Json | Set-Content -LiteralPath $marker -Encoding utf8
}
Write-Marker $false
function Set-ExactAcl([string]$Path, [hashtable]$Grants) {
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $administrators = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $acl.SetOwner($administrators)
    $all = @{'S-1-5-32-544' = 'FullControl'; 'S-1-5-18' = 'FullControl'}
    foreach ($entry in $Grants.GetEnumerator()) { $all[$entry.Key] = $entry.Value }
    foreach ($entry in $all.GetEnumerator()) {
        $account = if ($entry.Key -match '^S-1-') { [Security.Principal.SecurityIdentifier]::new($entry.Key) } else { [Security.Principal.NTAccount]::new($entry.Key) }
        $rule = [Security.AccessControl.FileSystemAccessRule]::new($account, $entry.Value, 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}
function Add-AppContainerGrant([hashtable]$Grants, [string]$Rights) {
    $copy = @{}
    foreach ($entry in $Grants.GetEnumerator()) { $copy[$entry.Key] = $entry.Value }
    if ($AppContainerSid) { $copy[$AppContainerSid] = $Rights }
    return $copy
}
Set-ExactAcl $target (Add-AppContainerGrant @{$service = 'ReadAndExecute'; $WriterAccount = 'ReadAndExecute'; $ReviewerAccount = 'ReadAndExecute'; $OperatorAccount = 'ReadAndExecute'} 'ReadAndExecute')
$runtime = Join-Path $target 'runtime'
# A venv keeps a dependency on its base interpreter. Protect that base as well,
# without changing permissions on the operator's personal Python installation.
$baseSource = (& $Python -I -c 'import sys; print(sys.base_prefix)').Trim()
if ($LASTEXITCODE -ne 0 -or -not $baseSource) { throw 'Cannot identify Python base runtime.' }
$baseSource = (Resolve-Path -LiteralPath $baseSource).Path
if ((Get-Item -LiteralPath $baseSource).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Python base must not be a reparse point.' }
$base = Join-Path $target 'python-base'
New-Item -ItemType Directory -Path $base | Out-Null
$selected = @(Get-ChildItem -LiteralPath $baseSource -Force | Where-Object {
    $_.Name -in @('DLLs', 'include', 'libs', 'tcl', 'LICENSE.txt') -or
    $_.Name -match '^(python.*\.(exe|dll)|vcruntime.*\.dll)$'
})
$libSource = Join-Path $baseSource 'Lib'
if ((Get-Item -LiteralPath $libSource).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Python Lib must not be a reparse point.' }
$libraries = @(Get-ChildItem -LiteralPath $libSource -Force | Where-Object { $_.Name -ne 'site-packages' })
foreach ($item in @($selected) + @($libraries)) {
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Python source must not contain reparse points.' }
    if ($item.PSIsContainer -and (Get-ChildItem -LiteralPath $item.FullName -Recurse -Force | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint })) { throw 'Python source must not contain reparse points.' }
}
$selected | Copy-Item -Destination $base -Recurse
New-Item -ItemType Directory -Path (Join-Path $base 'Lib') | Out-Null
$libraries | Copy-Item -Destination (Join-Path $base 'Lib') -Recurse
Set-ExactAcl $base (Add-AppContainerGrant @{$service = 'ReadAndExecute'; $WriterAccount = 'ReadAndExecute'; $ReviewerAccount = 'ReadAndExecute'; $OperatorAccount = 'ReadAndExecute'} 'ReadAndExecute')
& (Join-Path $base 'python.exe') -I -m venv $runtime
if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
$runtimePython = Join-Path $runtime 'Scripts/python.exe'
& $Uv pip install --python $runtimePython --require-hashes --only-binary :all: -r $Requirements
if ($LASTEXITCODE -ne 0) { throw 'Locked dependency installation failed.' }
& $Uv pip install --python $runtimePython --no-deps $package
if ($LASTEXITCODE -ne 0) { throw 'Wheel installation failed.' }
Set-ExactAcl $runtime (Add-AppContainerGrant @{$service = 'ReadAndExecute'; $WriterAccount = 'ReadAndExecute'; $ReviewerAccount = 'ReadAndExecute'; $OperatorAccount = 'ReadAndExecute'} 'ReadAndExecute')
if ($PreparedClients) {
    $clients = Join-Path $target 'clients'
    Copy-Item -LiteralPath $clientSource -Destination $clients -Recurse
    Set-ExactAcl $clients (Add-AppContainerGrant @{$service = 'ReadAndExecute'; $OperatorAccount = 'ReadAndExecute'} 'ReadAndExecute')
    $clientState = Join-Path $target 'client-state'
    New-Item -ItemType Directory -Path $clientState | Out-Null
    Set-ExactAcl $clientState (Add-AppContainerGrant @{$service = 'Modify'; $WriterAccount = 'Modify'; $OperatorAccount = 'ReadAndExecute'} 'Modify')
}
if ($PreparedSkills) {
    $workspaces = Join-Path $target 'workspaces'
    New-Item -ItemType Directory -Path $workspaces | Out-Null
    Set-ExactAcl $workspaces (Add-AppContainerGrant @{$service = 'ReadAndExecute'; $WriterAccount = 'ReadAndExecute'; $ReviewerAccount = 'ReadAndExecute'; $OperatorAccount = 'ReadAndExecute'} 'ReadAndExecute')
    $preparedWorkspaces = @()
    foreach ($binding in $config.client_bindings.PSObject.Properties.Value) {
        $workspace = [IO.Path]::GetFullPath($binding.workspace)
        if (-not $workspace.StartsWith($workspaces + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Prepared workspace must remain inside installation workspaces.' }
        if (-not (Test-Path -LiteralPath $workspace)) {
            New-Item -ItemType Directory -Path $workspace -Force | Out-Null
            Set-ExactAcl $workspace (Add-AppContainerGrant @{$service = 'ReadAndExecute'; $WriterAccount = 'ReadAndExecute'; $ReviewerAccount = 'ReadAndExecute'; $OperatorAccount = 'ReadAndExecute'} 'ReadAndExecute')
        }
        $catalog = Join-Path $workspace $(if ($binding.client -eq 'codex') { '.agents' } else { '.claude' })
        New-Item -ItemType Directory -Path $catalog | Out-Null
        Copy-Item -LiteralPath (Join-Path $skillSource $binding.client) -Destination (Join-Path $catalog 'skills') -Recurse
        Set-ExactAcl $catalog (Add-AppContainerGrant @{$service = 'ReadAndExecute'; $WriterAccount = 'ReadAndExecute'; $ReviewerAccount = 'ReadAndExecute'; $OperatorAccount = 'ReadAndExecute'} 'ReadAndExecute')
        $preparedWorkspaces += $workspace
    }
    foreach ($workspace in ($preparedWorkspaces | Select-Object -Unique)) {
        # The writer must not replace the catalogue or launch configuration via
        # DeleteChild on their parent. Only disposable working bytes are writable.
        $scratch = Join-Path $workspace 'scratch'
        New-Item -ItemType Directory -Path $scratch | Out-Null
        Set-ExactAcl $scratch (Add-AppContainerGrant @{$service = 'ReadAndExecute'; $WriterAccount = 'Modify'; $ReviewerAccount = 'ReadAndExecute'; $OperatorAccount = 'ReadAndExecute'} 'Modify')
    }
}
$authority = Join-Path $target 'authority'
New-Item -ItemType Directory -Path $authority | Out-Null
Set-ExactAcl $authority @{$service = 'ReadAndExecute'}
# Copy administrative material only; role credentials and stale acceptance stay separate.
Get-ChildItem -LiteralPath $source -Force | Where-Object { $_.Name -notlike '*.token' -and $_.Name -notin @('boundary-report.json', 'qualification-permit.json') } | Copy-Item -Destination $authority -Recurse
if (Test-Path -LiteralPath (Join-Path $source 'tls.crt')) { Copy-Item -LiteralPath (Join-Path $source 'tls.crt') -Destination (Join-Path $target 'ca.crt') }
if (Test-Path -LiteralPath (Join-Path $source 'trust.json')) { Copy-Item -LiteralPath (Join-Path $source 'trust.json') -Destination (Join-Path $target 'trust.json') }
$state = Join-Path $target 'state'
New-Item -ItemType Directory -Path $state | Out-Null
Set-ExactAcl $state @{$service = 'Modify'}
$outbox = Join-Path $target 'outbox'
New-Item -ItemType Directory -Path $outbox | Out-Null
Set-ExactAcl $outbox @{$service = 'Modify'; $OperatorAccount = 'ReadAndExecute'}
$config | Add-Member -NotePropertyName state_directory -NotePropertyValue '../state' -Force
foreach ($destination in $config.destinations.PSObject.Properties) {
    $destination.Value = '../outbox/' + $destination.Name
}
$configurationJson = $config | ConvertTo-Json -Depth 64
[IO.File]::WriteAllText((Join-Path $authority 'config.json'), $configurationJson, [Text.UTF8Encoding]::new($false))
$credentials = Join-Path $target 'credentials'
New-Item -ItemType Directory -Path $credentials | Out-Null
foreach ($pair in @(@('writer', $WriterAccount), @('reviewer', $ReviewerAccount), @('operator', $OperatorAccount), @('connector', $OperatorAccount))) {
    $directory = Join-Path $credentials $pair[0]
    New-Item -ItemType Directory -Path $directory | Out-Null
    # ReadPermissions permits ACL inspection, never token-content reads.
    Set-ExactAcl $directory @{$pair[1] = 'ReadAndExecute'; $service = 'ReadPermissions'}
    Copy-Item -LiteralPath (Join-Path $source ($pair[0] + '.token')) -Destination $directory
}
$proxyCredential = Join-Path $credentials 'proxy'
if ($AppContainerSid) {
    New-Item -ItemType Directory -Path $proxyCredential | Out-Null
    Set-ExactAcl $proxyCredential @{$service = 'ReadAndExecute'}
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $bytes = New-Object byte[] 32; $random.GetBytes($bytes) } finally { $random.Dispose() }
    $proxyToken = [Convert]::ToBase64String($bytes)
    $proxyTokenPath = Join-Path $proxyCredential 'proxy.token'
    [IO.File]::WriteAllText($proxyTokenPath, $proxyToken, [Text.UTF8Encoding]::new($false))
    $proxyConfig = [ordered]@{format='skillflow-egress-proxy-v1';listen_host='127.0.0.1';listen_port=$ProxyPort;
        credential_file=$proxyTokenPath;allowed_hosts=@($ProxyHosts);connect_timeout_seconds=10;
        idle_timeout_seconds=60;max_connections=8}
    [IO.File]::WriteAllText((Join-Path $authority 'proxy.json'), ($proxyConfig | ConvertTo-Json -Depth 4), [Text.UTF8Encoding]::new($false))
}
$arguments = '-m skillflow_prompts serve --root "' + $authority + '"'
$action = New-ScheduledTaskAction -Execute $runtimePython -Argument $arguments -WorkingDirectory $authority
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -Hidden
Register-ScheduledTask -TaskName $TaskName -Action $action -Settings $settings -User $service -Password $ServiceCredential.GetNetworkCredential().Password | Out-Null
if ($AppContainerSid) {
    $proxyArguments = '-m skillflow_prompts network-proxy --config "' + (Join-Path $authority 'proxy.json') + '" --log "' + (Join-Path $state 'proxy.jsonl') + '"'
    $proxyAction = New-ScheduledTaskAction -Execute $runtimePython -Argument $proxyArguments -WorkingDirectory $authority
    Register-ScheduledTask -TaskName $ProxyTaskName -Action $proxyAction -Settings $settings -User $service -Password $ServiceCredential.GetNetworkCredential().Password | Out-Null
}
Write-Marker $true
Write-Output 'Staged. Task remains stopped. Protected startup requires independent acceptance bound to final paths and ACLs.'

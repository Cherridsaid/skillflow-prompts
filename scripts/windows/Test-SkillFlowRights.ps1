[CmdletBinding()]
param([Parameter(Mandatory)][string]$ProbeManifest, [Parameter(Mandatory)][string]$Output)
$ErrorActionPreference = 'Stop'
# Run AS THE WRITER in a dedicated isolated acceptance environment.
# Manifest entries: { path: stable test ID, target: administrator-provisioned path,
# operation: read | create | delete | rename }. No secret contents are printed.
$manifest = Get-Content -LiteralPath $ProbeManifest -Raw | ConvertFrom-Json
$allowedRoot = if ($manifest.test_root) { [IO.Path]::GetFullPath($manifest.test_root).TrimEnd('\') } else { $null }
function Get-CatalogState([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return 'absent' }
    $items = @(Get-ChildItem -LiteralPath $Path -Recurse -Force | Sort-Object FullName | ForEach-Object {
        @{path=$_.FullName; directory=[bool]$_.PSIsContainer; hash=$(if (-not $_.PSIsContainer) { (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash } else { $null })}
    })
    return ConvertTo-Json -InputObject $items -Depth 4 -Compress
}
$results = @()
foreach ($probe in $manifest.probes) {
    $effect = $null
    $outcome = 'INFRA_ERROR'
    $temporary = $null
    $started = $false
    $catalogBefore = $null
    $catalogAfter = $null
    $failure = $null
    try {
        if ($probe.operation -eq 'read') {
            $started = $true
            $stream = [IO.File]::Open($probe.target, 'Open', 'Read', 'ReadWrite')
            try { $null = $stream.ReadByte() } finally { $stream.Dispose() }
            $effect = $true
        } elseif ($probe.operation -eq 'create') {
            $temporary = Join-Path $probe.target ('skillflow-probe-' + [Guid]::NewGuid().ToString('N') + '.txt')
            $started = $true
            $stream = [IO.File]::Open($temporary, 'CreateNew', 'Write', 'None')
            try { $stream.WriteByte(83); $stream.Flush($true) } finally { $stream.Dispose() }
            $effect = Test-Path -LiteralPath $temporary
        } elseif ($probe.operation -in @('delete','rename')) {
            # Destructive trials require a dedicated, operator-provisioned test
            # tree. Never infer authorization from a production catalog path.
            if (-not $allowedRoot -or $manifest.disposable_catalogs -ne $true) { throw 'Explicit disposable catalog scope required.' }
            $resolved = (Resolve-Path -LiteralPath $probe.target).Path
            if (-not $resolved.StartsWith($allowedRoot + '\', [StringComparison]::OrdinalIgnoreCase) -or $resolved -eq [IO.Path]::GetPathRoot($resolved)) { throw 'Catalog target is outside the exact test tree.' }
            if ((Get-Item -LiteralPath $resolved).Attributes -band [IO.FileAttributes]::ReparsePoint -or (Get-ChildItem -LiteralPath $resolved -Recurse -Force | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint })) { throw 'Catalog reparse points refused.' }
            $catalogBefore = Get-CatalogState $resolved
            if ($probe.operation -eq 'delete') {
                $started = $true
                Remove-Item -LiteralPath $resolved -Recurse -Force
                $effect = -not (Test-Path -LiteralPath $resolved)
            } else {
                $renamed = $resolved + '-qualification-renamed'
                if (Test-Path -LiteralPath $renamed) { throw 'Rename target already exists.' }
                $started = $true
                Move-Item -LiteralPath $resolved -Destination $renamed
                $effect = (Test-Path -LiteralPath $renamed) -and -not (Test-Path -LiteralPath $resolved)
            }
        } else { throw 'Unknown probe operation.' }
        $outcome = if ($effect) { 'FORBIDDEN_EFFECT' } else { 'UNKNOWN' }
    } catch [UnauthorizedAccessException] {
        $failure = $_.Exception.GetType().FullName + ': ' + $_.Exception.Message
        $effect = if ($started) { $false } else { $null }
        $outcome = if ($started) { 'PREVENTED' } else { 'INFRA_ERROR' }
    } catch {
        $failure = $_.Exception.GetType().FullName + ': ' + $_.Exception.Message
        $effect = $null
        $outcome = 'INFRA_ERROR'
    } finally {
        if ($catalogBefore -and $started) {
            try {
                $catalogAfter = Get-CatalogState $resolved
                if ($catalogAfter -ne $catalogBefore) { $effect=$true; $outcome='FORBIDDEN_EFFECT' }
            } catch { $effect=$null; $outcome='UNKNOWN' }
        }
        if ($temporary -and (Test-Path -LiteralPath $temporary)) { Remove-Item -LiteralPath $temporary -Force }
    }
    $results += @{path = $probe.path; target = $probe.target; operation = $probe.operation; started = $started; effect_observed = $effect; outcome = $outcome; before=$catalogBefore; after=$catalogAfter; error=$failure}
}
$report = @{format = 'windows-writer-probes-v1'; writer_sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value;
            measured_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); trials = $results;
            limitations = @('This is a writer filesystem probe, not a client integration or protected-profile acceptance report.')}
$report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Output -Encoding utf8
$report | ConvertTo-Json -Depth 8

[CmdletBinding()]
param([string]$Python = 'python', [string]$Output = 'skillflow-diagnostic.json')
$ErrorActionPreference = 'Stop'
& $Python -m skillflow diagnose --output $Output
if ($LASTEXITCODE -ne 0) { throw 'SkillFlow diagnostic failed.' }
Write-Output 'Read-only diagnostic completed. No accounts, ACLs or tasks changed.'

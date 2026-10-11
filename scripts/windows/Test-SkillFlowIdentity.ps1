param([Parameter(Mandatory)][ValidateSet('writer','reviewer')][string]$Role, [Parameter(Mandatory)][string]$Output)
$ErrorActionPreference = 'Stop'
# Run inside the actual dedicated client/reviewer session, not from the operator shell.
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
@{format='skillflow-role-identity-v1'; role=$Role; sid=$identity.User.Value;
    account=$identity.Name; process_id=$PID; session_id=(Get-Process -Id $PID).SessionId;
    administrator=$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator);
    measured_at=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()} |
    ConvertTo-Json | Set-Content -LiteralPath $Output -Encoding utf8

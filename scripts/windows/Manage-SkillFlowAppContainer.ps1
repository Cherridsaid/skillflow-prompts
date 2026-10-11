[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$Name = 'S.Cherrid.SkillFlow.Client',
    [Parameter(Mandatory)][string]$ExpectedAccount,
    [ValidatePattern('^S-1-15-2(?:-[0-9]+)+$')][string]$ExpectedSid,
    [ValidateSet('Create','Delete')][string]$Action = 'Create',
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'
if ($Name -notmatch '^[-_. A-Za-z0-9]{1,64}$') { throw 'Invalid AppContainer profile name.' }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if ($identity.Name -ne $ExpectedAccount) { throw "Run this script as $ExpectedAccount." }
if ([Security.Principal.WindowsPrincipal]::new($identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'The writer AppContainer profile must be managed from its limited account.'
}
if (-not ('SkillFlowAppContainer.Native' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
namespace SkillFlowAppContainer {
  public static class Native {
    [DllImport("userenv.dll", CharSet=CharSet.Unicode)]
    static extern int CreateAppContainerProfile(string name, string display, string description,
      IntPtr capabilities, uint count, out IntPtr sid);
    [DllImport("userenv.dll", CharSet=CharSet.Unicode)]
    static extern int DeriveAppContainerSidFromAppContainerName(string name, out IntPtr sid);
    [DllImport("userenv.dll", CharSet=CharSet.Unicode)]
    static extern int DeleteAppContainerProfile(string name);
    [DllImport("advapi32.dll", EntryPoint="ConvertSidToStringSidW", CharSet=CharSet.Unicode,
      ExactSpelling=true, SetLastError=true)]
    static extern bool ConvertSidToStringSid(IntPtr sid, out IntPtr value);
    [DllImport("advapi32.dll")]
    static extern IntPtr FreeSid(IntPtr sid);
    [DllImport("kernel32.dll")]
    static extern IntPtr LocalFree(IntPtr value);
    static void Check(int hr) { if (hr < 0) Marshal.ThrowExceptionForHR(hr); }
    static string Text(IntPtr sid) {
      IntPtr value;
      if (!ConvertSidToStringSid(sid, out value)) throw new Win32Exception();
      try { return Marshal.PtrToStringUni(value); } finally { LocalFree(value); }
    }
    public static string Derive(string name) {
      IntPtr sid; Check(DeriveAppContainerSidFromAppContainerName(name, out sid));
      try { return Text(sid); } finally { FreeSid(sid); }
    }
    public static string Create(string name) {
      IntPtr sid; Check(CreateAppContainerProfile(name, "SkillFlow client",
        "Restricted native Codex and Claude Code clients", IntPtr.Zero, 0, out sid));
      try { return Text(sid); } finally { FreeSid(sid); }
    }
    public static void Delete(string name) { Check(DeleteAppContainerProfile(name)); }
  }
}
'@
}
$sid = [SkillFlowAppContainer.Native]::Derive($Name)
if ($ExpectedSid -and $sid -ne $ExpectedSid) { throw 'Derived AppContainer SID differs from the reviewed SID.' }
$plan = [ordered]@{format='skillflow-appcontainer-profile-v1'; action=$Action.ToLowerInvariant();
    name=$Name; sid=$sid; account=$identity.Name; account_sid=$identity.User.Value; applied=$false}
$plan | ConvertTo-Json
if (-not $Apply) { return }
if (-not $PSCmdlet.ShouldProcess($Name, "$Action current-user AppContainer profile")) { return }
if ($Action -eq 'Create') {
    $actual = [SkillFlowAppContainer.Native]::Create($Name)
    if ($actual -ne $sid) { throw 'Created AppContainer SID differs from the derived SID.' }
} else {
    [SkillFlowAppContainer.Native]::Delete($Name)
}
$plan.applied = $true
$plan | ConvertTo-Json

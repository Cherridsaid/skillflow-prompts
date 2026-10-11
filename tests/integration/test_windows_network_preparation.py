import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from skillflow.bootstrap import create_demo
from skillflow.util import canonical, sha256, strict_json

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows network preparation")


def powershell(script):
    environment = {key: value for key, value in os.environ.items() if key.upper() != "PSMODULEPATH"}
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
        check=False,
    )


def test_appcontainer_and_network_scripts_parse_and_plan_without_changes(tmp_path):
    root = Path(__file__).resolve().parents[2]
    profile = root / "scripts/windows/Manage-SkillFlowAppContainer.ps1"
    install = root / "scripts/windows/Install-SkillFlowNetworkBoundary.ps1"
    remove = root / "scripts/windows/Remove-SkillFlowNetworkBoundary.ps1"
    lifecycle = [
        root / "scripts/windows/Install-SkillFlow.ps1",
        root / "scripts/windows/Stage-SkillFlowPilot.ps1",
        root / "scripts/windows/Start-SkillFlowQualification.ps1",
        root / "scripts/windows/Rollback-SkillFlowPilot.ps1",
        root / "scripts/windows/Uninstall-SkillFlow.ps1",
    ]
    lifecycle_paths = ",".join(f"'{path}'" for path in lifecycle)
    command = fr"""
    foreach($p in @('{profile}','{install}','{remove}',{lifecycle_paths})) {{
      $null=[scriptblock]::Create((Get-Content -Raw -LiteralPath $p))
    }}
    $account=[Security.Principal.WindowsIdentity]::GetCurrent().Name
    $profile=& '{profile}' -ExpectedAccount $account | ConvertFrom-Json
    $network=& '{install}' -AppContainerSid $profile.sid -ProxyPort 18771 -Journal '{tmp_path / "journal.json"}' | ConvertFrom-Json
    $journal=@{{format='skillflow-network-boundary-v1';appcontainer_sid=$profile.sid;
      created_rules=$network.firewall_rules;loopback_intended=$true;complete=$false;rolled_back=$false}}
    $journal | ConvertTo-Json | Set-Content -LiteralPath '{tmp_path / "rollback.json"}' -Encoding utf8
    $rollback=& '{remove}' -Journal '{tmp_path / "rollback.json"}' | ConvertFrom-Json
    @{{profile=$profile;network=$network;rollback=$rollback}} | ConvertTo-Json -Depth 8
    """
    result = powershell(command)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert not plan["profile"]["applied"] and not plan["network"]["applied"]
    assert plan["profile"]["sid"] == plan["network"]["appcontainer_sid"]
    assert plan["profile"]["sid"].startswith("S-1-15-2-")
    assert not plan["rollback"]["applied"] and plan["rollback"]["preserve_journal"]
    assert not (tmp_path / "journal.json").exists()
    rules = plan["network"]["firewall_rules"]
    assert len(rules) == 8
    assert {
        "SkillFlowMCP-ProxyOnly-LoopbackTcp6",
        "SkillFlowMCP-ProxyOnly-LoopbackIcmp4",
        "SkillFlowMCP-ProxyOnly-LoopbackIcmp6",
    } <= set(rules)
    assert set(plan["rollback"]["remove_rules"]) == set(rules)


def protected_bundle(tmp_path):
    root = Path(__file__).resolve().parents[2]
    installer = root / "scripts/windows/Install-SkillFlow.ps1"
    authority = create_demo(tmp_path / "authority")
    config_path = authority / "config.json"
    config = strict_json(config_path.read_bytes())
    config["mode"] = "protected"
    config_path.write_bytes(canonical(config))
    for state in authority.glob("state.db*"):
        state.unlink()
    wheel = tmp_path / "skillflow.whl"
    wheel.write_bytes(b"synthetic reviewed wheel")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("")
    clients = tmp_path / "clients"
    binary = clients / "codex/codex.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"synthetic pinned client")
    claude = clients / "claude/claude.exe"
    claude.parent.mkdir(parents=True)
    claude.write_bytes(b"synthetic pinned claude client")
    (clients / "manifest.json").write_bytes(
        canonical(
            {
                "format": "skillflow-native-clients-v1",
                "platform": "windows-x86_64",
                "clients": {
                    "codex": {"version": "codex-cli synthetic", "executable": "codex/codex.exe"},
                    "claude": {"version": "claude synthetic", "executable": "claude/claude.exe"},
                },
                "files": [
                    {
                        "path": "codex/codex.exe",
                        "sha256": sha256(binary.read_bytes()),
                        "bytes": binary.stat().st_size,
                    },
                    {
                        "path": "claude/claude.exe",
                        "sha256": sha256(claude.read_bytes()),
                        "bytes": claude.stat().st_size,
                    },
                ],
            }
        )
    )
    uv = shutil.which("uv")
    assert uv
    sid = "S-1-15-2-1-2-3-4-5-6-7"
    return root, installer, authority, wheel, requirements, clients, uv, sid


def test_protected_installer_accepts_only_pinned_appcontainer_clients(tmp_path):
    root, installer, authority, wheel, requirements, clients, uv, sid = protected_bundle(tmp_path)
    command = f"""
    & '{installer}' -InstallRoot '{tmp_path / "not-created"}' `
      -PreparedAuthority '{authority}' -Wheel '{wheel}' -WheelSha256 '{sha256(wheel.read_bytes())}' `
      -Requirements '{requirements}' -Python '{sys.executable}' -Uv '{uv}' `
      -WriterAccount '.\\SkillFlowWriter' -ReviewerAccount '.\\SkillFlowReviewer' `
      -OperatorAccount '.\\SkillFlowOperator' -PreparedClients '{clients}' -AppContainerSid '{sid}' `
      -ProxyHosts 'api.openai.com','api.anthropic.com' -ProxyPort 18771
    """
    result = powershell(command)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["appcontainer"] == sid
    assert plan["native_clients"] == "pinned private bundle"
    assert plan["state"] == "NOT_INSTALLED"
    assert not (tmp_path / "not-created").exists()

    bundle = tmp_path / "pilot-bundle"
    shutil.copytree(authority, bundle / "authority")
    shutil.copytree(clients, bundle / "clients")
    (bundle / "skills/codex").mkdir(parents=True)
    (bundle / "skills/claude").mkdir()
    shutil.copy2(wheel, bundle / wheel.name)
    shutil.copy2(requirements, bundle / "requirements.lock.txt")
    (bundle / "stage-plan.json").write_bytes(
        canonical(
            {
                "install_root": "C:/ProgramData/SkillFlowMCP-PrivatePilot",
                "wheel": wheel.name,
                "wheel_sha256": sha256(wheel.read_bytes()),
                "python": sys.executable,
                "uv": uv,
                "appcontainer_sid": sid,
                "proxy_hosts": ["api.openai.com", "api.anthropic.com"],
                "proxy_port": 18771,
            }
        )
    )
    stage = root / "scripts/windows/Stage-SkillFlowPilot.ps1"
    staged = powershell(f"& '{stage}' -Bundle '{bundle}' -OperatorAccount '.\\Operator'")
    assert staged.returncode == 0, staged.stderr
    stage_plan = json.loads(staged.stdout)
    assert stage_plan["installation"]["appcontainer"] == sid
    assert stage_plan["tasks_started"] is False
    assert not (bundle / "stage-journal.json").exists()


def test_qualification_and_rollback_validate_both_owned_tasks(tmp_path):
    root = Path(__file__).resolve().parents[2]
    qualification = root / "scripts/windows/Start-SkillFlowQualification.ps1"
    rollback = root / "scripts/windows/Rollback-SkillFlowPilot.ps1"
    target = Path("C:/ProgramData/SkillFlowMCP-PrivatePilot")
    sid = "S-1-15-2-1-2-3-4-5-6-7"
    network = tmp_path / "network.json"
    network.write_bytes(
        canonical(
            {
                "format": "skillflow-network-boundary-v1",
                "complete": True,
                "rolled_back": False,
                "appcontainer_sid": sid,
                "created_rules": [
                    "SkillFlowMCP-ProxyOnly-NonLoopback-v4",
                    "SkillFlowMCP-ProxyOnly-NonLoopback-v6",
                    "SkillFlowMCP-ProxyOnly-OtherTcp",
                    "SkillFlowMCP-ProxyOnly-LoopbackUdp",
                    "SkillFlowMCP-ProxyOnly-Inbound",
                ],
            }
        )
    )
    journal = tmp_path / "stage.json"
    journal.write_bytes(
        canonical(
            {
                "format": "skillflow-stage-v1",
                "run_id": "a" * 32,
                "root": str(target),
                "task": "SkillFlowMCP-PrivatePilot",
                "proxy_task": "SkillFlowMCP-PrivatePilot-Proxy",
                "accounts": [],
                "appcontainer_sid": sid,
                "appcontainer_created": True,
                "network_journal": str(network),
                "network_installed": True,
                "completed": True,
            }
        )
    )
    command = fr"""
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    $saved=@{{name='SkillFlowService';sid=$identity.User.Value}}
    $record=Get-Content -Raw -LiteralPath '{journal}' | ConvertFrom-Json
    $record.accounts=@($saved)
    $record | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath '{journal}' -Encoding utf8
    function Get-LocalUser {{
      param([string]$Name)
      [pscustomobject]@{{Name=$Name;SID=$identity.User;Description='SkillFlow stage ' + ('a'*32);Enabled=$true}}
    }}
    function Get-ScheduledTask {{
      param([string]$TaskName)
      $python='C:\ProgramData\SkillFlowMCP-PrivatePilot\runtime\Scripts\python.exe'
      $authority='C:\ProgramData\SkillFlowMCP-PrivatePilot\authority'
      $arguments=if($TaskName -eq 'SkillFlowMCP-PrivatePilot'){{
        '-m skillflow serve --root "' + $authority + '"'
      }}else{{
        '-m skillflow network-proxy --config "' + $authority + '\proxy.json" --log "C:\ProgramData\SkillFlowMCP-PrivatePilot\state\proxy.jsonl"'
      }}
      [pscustomobject]@{{Actions=@([pscustomobject]@{{Execute=$python;Arguments=$arguments;WorkingDirectory=$authority}});
        Principal=[pscustomobject]@{{UserId=$identity.Name}}}}
    }}
    $qualification=& '{qualification}' -Journal '{journal}' | ConvertFrom-Json
    $rollback=& '{rollback}' -Journal '{journal}' | ConvertFrom-Json
    @{{qualification=$qualification;rollback=$rollback}} | ConvertTo-Json -Depth 6
    """
    result = powershell(command)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["qualification"]["proxy_task"] == "SkillFlowMCP-PrivatePilot-Proxy"
    assert set(plan["rollback"]["tasks"]) == {
        "SkillFlowMCP-PrivatePilot",
        "SkillFlowMCP-PrivatePilot-Proxy",
    }


def test_generic_uninstall_refuses_to_orphan_the_network_boundary(tmp_path):
    root = Path(__file__).resolve().parents[2]
    uninstall = root / "scripts/windows/Uninstall-SkillFlow.ps1"
    sid = "S-1-15-2-1-2-3-4-5-6-7"
    install_root = tmp_path / "install"
    install_root.mkdir()
    command = fr"""
    $root=(Resolve-Path -LiteralPath '{install_root}').Path
    @{{format='skillflow-install-v1';root=$root;task='SkillFlowMCP-Absent';proxy_task='SkillFlowMCP-Absent-Proxy';appcontainer_sid='{sid}'}} |
      ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root 'installation.json') -Encoding utf8
    $clean=& '{uninstall}' -InstallRoot $root | ConvertFrom-Json
    function Get-NetFirewallRule {{ [CmdletBinding()] param([string]$Group,[string]$Name) [pscustomobject]@{{Name='SkillFlowMCP-ProxyOnly-Inbound'}} }}
    function Get-NetFirewallApplicationFilter {{ [CmdletBinding()] param([Parameter(ValueFromPipeline)]$Rule) process {{ [pscustomobject]@{{Package='{sid}'}} }} }}
    try {{ & '{uninstall}' -InstallRoot $root | Out-Null; $refused='NO' }} catch {{ $refused=$_.Exception.Message }}
    @{{clean=$clean;refused=$refused}} | ConvertTo-Json -Depth 5
    """
    result = powershell(command)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["clean"]["action"] == "uninstall" and not plan["clean"]["applied"]
    assert plan["clean"]["appcontainer_profile"].startswith("NOT_REMOVED")
    assert "Remove-SkillFlowNetworkBoundary.ps1 first" in plan["refused"]
    assert (install_root / "installation.json").exists()


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("ip-host", "exact lowercase DNS"),
        ("wildcard-host", "exact lowercase DNS"),
        ("uppercase-host", "exact lowercase DNS"),
        ("hidden-undeclared", "hidden or system"),
        ("bad-sid", "AppContainerSid"),
    ],
)
def test_protected_installer_refuses_each_announced_defect(tmp_path, case, message):
    root, installer, authority, wheel, requirements, clients, uv, sid = protected_bundle(tmp_path)
    hosts = "'api.openai.com','api.anthropic.com'"
    if case == "ip-host":
        hosts = "'10.0.0.5'"
    elif case == "wildcard-host":
        hosts = "'*.openai.com'"
    elif case == "uppercase-host":
        hosts = "'Api.OpenAI.com'"
    elif case == "hidden-undeclared":
        evil = clients / "codex/evil.dll"
        evil.write_bytes(b"undeclared")
        subprocess.run(["attrib", "+h", str(evil)], check=True)
    elif case == "bad-sid":
        sid = "S-1-15-2-1-"
    command = fr"""
    & '{installer}' -InstallRoot '{tmp_path / "not-created"}' `
      -PreparedAuthority '{authority}' -Wheel '{wheel}' -WheelSha256 '{sha256(wheel.read_bytes())}' `
      -Requirements '{requirements}' -Python '{sys.executable}' -Uv '{uv}' `
      -WriterAccount '.\SkillFlowWriter' -ReviewerAccount '.\SkillFlowReviewer' `
      -OperatorAccount '.\SkillFlowOperator' -PreparedClients '{clients}' -AppContainerSid '{sid}' `
      -ProxyHosts {hosts} -ProxyPort 18771
    """
    result = powershell(command)
    assert result.returncode != 0
    assert message in result.stderr, result.stderr
    assert not (tmp_path / "not-created").exists()


def test_generic_uninstall_refuses_to_delete_through_a_junction(tmp_path):
    root = Path(__file__).resolve().parents[2]
    uninstall = root / "scripts/windows/Uninstall-SkillFlow.ps1"
    install_root = tmp_path / "install"
    (install_root / "scratch").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(install_root / "scratch" / "j"), str(outside)],
        check=True,
        capture_output=True,
    )
    command = fr"""
    $root=(Resolve-Path -LiteralPath '{install_root}').Path
    @{{format='skillflow-install-v1';root=$root;task='SkillFlowMCP-Absent';proxy_task=$null;appcontainer_sid=$null}} |
      ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root 'installation.json') -Encoding utf8
    try {{ & '{uninstall}' -InstallRoot $root -Apply -RemoveFiles | Out-Null; 'NOT_REFUSED' }} catch {{ $_.Exception.Message }}
    """
    result = powershell(command)
    assert result.returncode == 0, result.stderr
    assert "reparse ambiguity" in result.stdout, result.stdout
    assert (outside / "keep.txt").exists()
    assert (install_root / "installation.json").exists()

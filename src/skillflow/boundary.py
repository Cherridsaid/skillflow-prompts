"""Protected deployment preflight: evidence is bound to actual Windows ACLs."""

import os
import platform
from pathlib import Path

from .util import canonical, now, require, sha256, strict_json

ARTIFACT_PATHS = {
    "writer_code_write",
    "writer_contract_write",
    "writer_catalog_delete",
    "writer_catalog_rename",
    "writer_state_write",
    "writer_key_read",
    "writer_reviewer_token_read",
    "writer_operator_token_read",
    "writer_delivery_write",
    "writer_forge_review",
    "writer_forge_approval",
    "writer_service_down_export",
}
EXECUTION_PATHS = ARTIFACT_PATHS | {
    "native_tool",
    "mcp_direct",
    "bash",
    "powershell",
    "persistent_terminal",
    "child_process",
    "background_task",
    "new_session",
    "delegation",
    "direct_python",
    "client_settings_tamper",
    "launch_manifest_tamper",
    "explicit_activation",
    "implicit_activation",
    "resume",
    "unregistered_skill",
    "modified_skill",
    "unmediated_network_effect",
    "unaccepted_chat_output",
    "controller_down_operation",
    "inherited_authority_handle",
}
FILESYSTEM_PATHS = ARTIFACT_PATHS - {
    "writer_forge_review",
    "writer_forge_approval",
    "writer_service_down_export",
}


def code_fingerprint():
    package = Path(__file__).parent
    return sha256(
        canonical(
            {
                p.relative_to(package).as_posix(): sha256(p.read_bytes())
                for p in sorted(package.rglob("*"))
                if p.is_file() and "__pycache__" not in p.parts
            }
        )
    )


def contracts_fingerprint(root):
    return sha256(
        canonical(
            {
                p.name: sha256(p.read_bytes())
                for p in sorted((Path(root) / "contracts").glob("*.json"))
            }
        )
    )


def probe_targets(root, config):
    from .storage import state_path

    root = Path(root).resolve()
    catalogs = sorted(
        {
            str(Path(b["workspace"]) / (".agents" if b["client"] == "codex" else ".claude"))
            for b in config.get("client_bindings", {}).values()
        }
    )
    return {
        "writer_code_write": sorted(
            str(p)
            for p in (Path(__file__).parent, root.parent / "runtime", root.parent / "python-base")
        ),
        "writer_contract_write": [str(root / "contracts")],
        "writer_state_write": [str(state_path(root, config).parent)],
        "writer_key_read": [str(root / "signing.key")],
        "writer_reviewer_token_read": [str(root.parent / "credentials/reviewer/reviewer.token")],
        "writer_operator_token_read": [str(root.parent / "credentials/operator/operator.token")],
        "writer_delivery_write": sorted(
            {str((root / value).resolve()) for value in config["destinations"].values()}
        ),
        "writer_catalog_delete": catalogs,
        "writer_catalog_rename": catalogs,
    }


def required_paths(root, config):
    """Minimum measured ACL surface, derived from the installed configuration."""
    from .storage import state_path

    root = Path(root).resolve()
    paths = {
        root,
        root / "config.json",
        root / "contracts",
        root / "signing.key",
        root / "trust.json",
        state_path(root, config).parent,
        Path(__file__).parent,
        root.parent,
        root.parent / "runtime",
        root.parent / "python-base",
        root.parent / "credentials",
    }
    paths.update((root / "contracts").glob("*.json"))
    for value in config["destinations"].values():
        paths.add((root / value).resolve())
    for value in config.get("tls", {}).values():
        paths.add((root / value).resolve())
    for binding in config.get("client_bindings", {}).values():
        workspace = Path(binding["workspace"]).resolve()
        paths.add(workspace)
        for skill in binding["skills"].values():
            for item in skill["files"]:
                path = Path(item["path"]).resolve()
                paths.add(path)
                paths.update(p for p in path.parents if p == workspace or workspace in p.parents)
    for watch in config.get("catalog_watches", []):
        path = Path(watch["path"]).resolve()
        paths.update((path, path.parent))
    # Installed credentials are outside service authority, with role-specific ACLs.
    for role in ("writer", "reviewer", "operator", "connector"):
        paths.add(root.parent / "credentials" / role)
        paths.add(root.parent / "credentials" / role / (role + ".token"))
    return sorted(str(p) for p in paths)


def windows_identity():
    import win32api
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    try:
        return win32security.ConvertSidToStringSid(
            win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        )
    finally:
        token.Close()


def acl_fingerprint(paths):
    import win32security

    security = []
    for path in sorted(paths):
        descriptor = win32security.GetFileSecurity(
            str(Path(path).resolve()),
            win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
        )
        security.append(
            {
                "path": str(Path(path).resolve()),
                "sddl": win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(
                    descriptor,
                    win32security.SDDL_REVISION_1,
                    win32security.OWNER_SECURITY_INFORMATION
                    | win32security.DACL_SECURITY_INFORMATION,
                ),
            }
        )
    return sha256(canonical(security))


def validate_deployment(root, config, *, qualification=False):
    require(
        os.name == "nt",
        "PROFILE_UNAVAILABLE",
        "Protected profiles currently require a Windows ACL acceptance report",
    )
    require(
        config.get("tls", {}).get("certificate") and config.get("tls", {}).get("private_key"),
        "PROFILE_UNAVAILABLE",
        "Authenticated TLS server identity is required in protected mode",
    )
    require(
        all("*" not in key for p in config["principals"] for key in p["case_keys"]),
        "PROFILE_UNAVAILABLE",
        "Protected credentials must have explicit case scopes",
    )
    report_file = Path(root) / (
        "qualification-permit.json" if qualification else "boundary-report.json"
    )
    require(
        report_file.is_file(), "PROFILE_UNAVAILABLE", "No independent Windows acceptance report"
    )
    report = strict_json(report_file.read_bytes())
    require(
        report.get("format")
        == ("windows-qualification-v1" if qualification else "windows-boundary-v1")
        and report.get("mode") == ("qualification" if qualification else "protected"),
        "PROFILE_UNAVAILABLE",
        "Not a protected deployment acceptance report",
    )
    require(
        report.get("service_sid") == windows_identity()
        and report.get("os_version") == platform.version(),
        "PROFILE_UNAVAILABLE",
        "Report belongs to another identity or OS version",
    )
    require(
        0 <= now() - report.get("measured_at", 0) <= config.get("boundary_max_age_seconds", 86400),
        "PROFILE_UNAVAILABLE",
        "Boundary acceptance is missing, future-dated or expired",
    )
    require(
        report.get("configuration_sha256") == sha256(canonical(config)),
        "PROFILE_UNAVAILABLE",
        "Deployment configuration changed since acceptance",
    )
    require(
        report.get("code_sha256") == code_fingerprint(),
        "PROFILE_UNAVAILABLE",
        "Runtime changed since qualification",
    )
    require(
        report.get("contracts_sha256") == contracts_fingerprint(root),
        "PROFILE_UNAVAILABLE",
        "Contracts changed since qualification",
    )
    require(
        report.get("acl_sha256") == acl_fingerprint(report.get("protected_paths", []))
        and set(required_paths(root, config)) <= set(report.get("protected_paths", [])),
        "PROFILE_UNAVAILABLE",
        "Protected ACLs changed",
    )
    require(
        len({report.get(role + "_sid") for role in ("writer", "service", "reviewer", "operator")})
        == 4
        and all(
            isinstance(report.get(role + "_sid"), str) and report[role + "_sid"].startswith("S-1-")
            for role in ("writer", "service", "reviewer", "operator")
        ),
        "PROFILE_UNAVAILABLE",
        "Writer identity is not independent",
    )
    required = (
        EXECUTION_PATHS
        if any(p["coverage"] == "execution_controlled" for p in config["profiles"].values())
        else ARTIFACT_PATHS
    )
    if qualification:
        required = FILESYSTEM_PATHS
        require(
            0 < report.get("expires_at", 0) - now() <= 3600
            and report.get("case_keys")
            and all(
                k.startswith("qualification-") and k in config["case_bindings"]
                for k in report["case_keys"]
            ),
            "PROFILE_UNAVAILABLE",
            "Qualification needs a bounded permit and explicit synthetic case scopes",
        )
    trials = {r["path"]: r for r in report.get("trials", [])}
    require(
        required <= set(trials)
        and all(
            trials[p].get("outcome") == "PREVENTED"
            and trials[p].get("effect_observed") is False
            and trials[p].get("started") is True
            and trials[p].get("evidence_sha256")
            for p in required
        ),
        "PROFILE_UNAVAILABLE",
        "Every covered route requires an observed prevented effect",
    )
    require(
        qualification or report.get("legitimate_delivery_observed") is True,
        "PROFILE_UNAVAILABLE",
        "Acceptance must also observe a legitimate delivery",
    )
    # The report itself and its evidence live under administrator-owned ACLs;
    # neither writer nor reviewer can upgrade this administrative authority.
    return report

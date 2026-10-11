"""Read installed skill packages without executing their instructions or scripts.

Run from the repository with uv run python scripts/inventory_skills.py --help.
Raw instructions/resources stay in the private output directory, never in Git.
Claude discovery is configuration-derived, NOT a native activation measurement.
"""

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from skillflow_prompts.catalog import inventory_entry, unique_entries
from skillflow_prompts.client_install import CodexConfig
from skillflow_prompts.util import canonical, now, sha256


def discover(home, workspace, codex_result):
    found = []
    for group in codex_result["data"]:
        for skill in group["skills"]:
            found.append(
                (
                    "codex",
                    skill["name"],
                    Path(skill["path"]),
                    "native:skills/list",
                    skill["enabled"],
                    skill,
                )
            )
    # Project precedence is intentionally not guessed. Retain collisions for review.
    roots = [(home / ".claude/skills", "claude:user-directory")]
    for ancestor in [workspace, *workspace.parents]:
        candidate = ancestor / ".claude/skills"
        if candidate != roots[0][0] and candidate.exists():
            roots.append((candidate, "claude:project-directory"))
    settings = home / ".claude/settings.json"
    enabled = json.loads(settings.read_text(encoding="utf-8-sig")).get("enabledPlugins", {})
    installed = json.loads(
        (home / ".claude/plugins/installed_plugins.json").read_text(encoding="utf-8-sig")
    )["plugins"]
    unresolved = []
    for plugin, active in enabled.items():
        if not active:
            continue
        versions = installed.get(plugin, [])
        if not versions:
            unresolved.append({"plugin": plugin, "reason": "enabled_but_not_installed"})
        for version in versions:
            root = Path(version["installPath"]) / "skills"
            if root.exists():
                roots.append((root, "claude:enabled-plugin:" + plugin))
    for root, basis in roots:
        for path in sorted(root.rglob("SKILL.md")):
            if path.name != "SKILL.md":
                continue
            # This is a candidate name. Native Claude resolution remains to be measured.
            name = path.parent.name
            if basis.startswith("claude:enabled-plugin:"):
                name = basis.split(":", 2)[2].split("@")[0] + ":" + name
            found.append(("claude", name, path, basis, None, {}))
    return found, unresolved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--codex-snapshot", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Choose a new output directory; existing evidence is immutable")
    args.output.mkdir(parents=True)
    if args.codex_snapshot:
        raw = json.loads(args.codex_snapshot.read_text(encoding="utf-8"))
    else:
        rpc = CodexConfig(shutil.which("codex"), str(args.workspace.resolve()))
        try:
            raw = rpc.call(
                "skills/list", {"cwds": [str(args.workspace.resolve())], "forceReload": True}
            )
        finally:
            rpc.close()
    (args.output / "codex-native.json").write_bytes(canonical(raw))
    items, unresolved = discover(args.home, args.workspace, raw)
    entries, errors = [], []
    for client, name, path, basis, enabled, metadata in items:
        try:
            entries.append(
                inventory_entry(
                    client,
                    name,
                    path,
                    discovery=basis,
                    enabled=enabled,
                    metadata=metadata,
                    blobs=args.output / "blobs",
                )
            )
        except (OSError, ValueError) as error:
            errors.append(
                {"client": client, "name": name, "path": str(path), "error": type(error).__name__}
            )
    # A physical path loaded through different client names retains each identity.
    entries = sorted(unique_entries(entries), key=lambda e: (e["client"], e["name"], e["path"]))
    catalog = {
        "schema": "skillflow-catalog-v1",
        "captured_at": now(),
        "workspace": str(args.workspace.resolve()),
        "entries": entries,
        "errors": errors,
        "unresolved_plugins": unresolved,
        "limits": [
            "Claude candidates are not native activation proof",
            "Instruction units are verbatim review units, not validated obligations",
            "Package bytes are archived, not semantically reviewed",
            "External references and runtime downloads need separate closure review",
            "Other project workspaces require their own native discovery",
            "Quarantined and inactive cached versions are not activated",
        ],
    }
    payload = canonical(catalog)
    (args.output / "catalog.json").write_bytes(payload)
    versions = {}
    for name in ("codex", "claude"):
        result = subprocess.run(
            [shutil.which(name), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        versions[name] = result.stdout.strip() if result.returncode == 0 else "non mesuré"
    counts = {client: sum(e["client"] == client for e in entries) for client in ("codex", "claude")}
    summary = {
        "catalog_sha256": sha256(payload),
        "entries": counts,
        "bytes_archived": sum(p.stat().st_size for p in (args.output / "blobs").iterdir()),
        "distinct_blobs": len(list((args.output / "blobs").iterdir())),
        "instruction_units": sum(len(e["instruction_units"]) for e in entries),
        "entry_errors": len(errors),
        "unresolved_plugins": unresolved,
        "contracts_approved": 0,
        "versions": versions,
    }
    (args.output / "summary.json").write_bytes(canonical(summary))
    lines = [
        "# Registre privé — contrats non approuvés",
        "",
        "Lecture machine et archivage intégraux des octets accessibles.",
        "Lecture sémantique et fermeture des références : à réaliser.",
        "",
        "| Client | Skill | Version SHA-256 | Unités à revoir | Ressources | Activation |",
        "|---|---|---|---:|---:|---|",
    ]
    for entry in entries:
        lines.append(
            f"| {entry['client']} | {entry['name']} | {entry['version'][7:19]} | "
            f"{len(entry['instruction_units'])} | {len(entry['package']['files'])} | non mesuré |"
        )
    (args.output / "REGISTRY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()

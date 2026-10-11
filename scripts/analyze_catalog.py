"""Offline discovery diagnostics. Does not perform or claim semantic approval."""

import argparse
import collections
import posixpath
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from skillflow.util import canonical, sha256, strict_json


def analyze(inventory, output):
    inventory, output = Path(inventory), Path(output)
    if output.exists():
        raise FileExistsError("Use a fresh diagnostic directory")
    raw = (inventory / "catalog.json").read_bytes()
    catalog = strict_json(raw)
    entries = catalog["entries"]
    by_version, by_package, by_name = (collections.defaultdict(list) for _ in range(3))
    for entry in entries:
        by_version[entry["version"]].append(entry["id"])
        by_package[entry["package_sha256"]].append(entry["id"])
        by_name[entry["name"]].append(entry)
    rows = []
    for entry in entries:
        gaps = []
        for path in entry["package"]["gaps"]:
            parts = path.rstrip("/").split("/")
            kind = (
                "EXCLUDED_VCS_METADATA"
                if ".git" in parts
                else (
                    "EXCLUDED_GENERATED_CACHE"
                    if {"__pycache__", ".pytest_cache", ".ruff_cache"} & set(parts)
                    else "UNRESOLVED_RESOURCE"
                )
            )
            gaps.append(
                {
                    "path": path,
                    "classification": kind,
                    "waiver_approved": False,
                    "resolution": "Operator must review required references and package closure; no exclusion is approved here",
                }
            )
        references, unreadable = [], []
        for path, digest in entry["package"]["files"].items():
            if not path.lower().endswith(".md"):
                continue
            blob = inventory / "blobs" / digest
            try:
                content = blob.read_bytes()
                if sha256(content) != digest:
                    raise ValueError("Archive hash mismatch")
                text = content.decode("utf-8-sig")
            except (OSError, ValueError):
                unreadable.append(path)
                continue
            # ponytail: Markdown inline-link diagnostics only. Dynamic paths,
            # command strings and semantic necessity require independent review.
            for match in re.finditer(r"\[[^\]\n]*\]\(([^)\n]+)\)", text):
                target = match[1].strip().split(' "', 1)[0].strip("<>")
                parts = urlsplit(target)
                if parts.scheme or parts.netloc:
                    state = "EXTERNAL_NOT_FETCHED"
                elif not parts.path:
                    state = "DOCUMENT_ANCHOR"
                else:
                    normalized = posixpath.normpath(
                        posixpath.join(posixpath.dirname(path), unquote(parts.path))
                    )
                    if normalized.startswith(("../", "/")):
                        state = "OUTSIDE_PACKAGE_NOT_RESOLVED"
                    else:
                        state = (
                            "ARCHIVED"
                            if normalized in entry["package"]["files"]
                            else "MISSING_LITERAL_TARGET_CANDIDATE"
                        )
                references.append(
                    {
                        "source": path,
                        "line": text[: match.start()].count("\n") + 1,
                        "target": target,
                        "state": state,
                        "required": None,
                    }
                )
        confirmed = entry["discovery"] == "native:skills/list" and entry["enabled"] is True
        rows.append(
            {
                "id": entry["id"],
                "name": entry["name"],
                "client": entry["client"],
                "version": entry["version"],
                "package_sha256": entry["package_sha256"],
                "availability": "NATIVE_LISTED_ENABLED"
                if confirmed
                else (
                    "NATIVE_DISABLED" if entry["enabled"] is False else "CONFIGURATION_CANDIDATE"
                ),
                "same_instruction_entries": by_version[entry["version"]],
                "same_package_entries": by_package[entry["package_sha256"]],
                "same_name_variants": sorted({e["version"] for e in by_name[entry["name"]]}),
                "gaps": gaps,
                "references": references,
                "unreadable_markdown": unreadable,
                "model_context_visibility": None,
                "semantic_review": "NOT_COMPLETE",
                "contract_approved": False,
                "protected_paths_verified": None,
            }
        )
    summary = {
        "catalog_sha256": sha256(raw),
        "entries": len(rows),
        "instruction_versions": len(by_version),
        "package_versions": len(by_package),
        "availability": dict(collections.Counter(r["availability"] for r in rows)),
        "entries_with_exclusions": sum(bool(r["gaps"]) for r in rows),
        "gap_classifications": dict(
            collections.Counter(g["classification"] for r in rows for g in r["gaps"])
        ),
        "reference_states": dict(
            collections.Counter(ref["state"] for r in rows for ref in r["references"])
        ),
        "unresolved_plugins": strict_json((inventory / "summary.json").read_bytes()).get(
            "unresolved_plugins", []
        ),
        "semantic_review_complete": 0,
        "approval_granted_by_this_command": False,
        "limit": "Literal link diagnostics and content identity only; no semantic necessity, native Claude resolution or context visibility inferred",
    }
    output.mkdir(parents=True)
    (output / "entries.json").write_bytes(canonical(rows))
    (output / "summary.json").write_bytes(canonical(summary))
    lines = [
        "# Diagnostic des entrées — aucune approbation",
        "",
        "Les liens sont examinés comme références littérales. Leur nécessité reste à relire.",
        "Les caches exclus ne prouvent pas une ressource métier manquante. Aucune exemption accordée.",
        "Visibilité dans le contexte modèle et parcours protégés : non mesuré.",
        "",
        "| Client | Skill | Disponibilité | Version | Variantes homonymes | Exclusions |",
        "|---|---|---|---|---:|---:|",
    ]
    lines += [
        f"| {r['client']} | {r['name'].replace('|', '/')} | {r['availability']} | {r['version']} | {len(r['same_name_variants'])} | {len(r['gaps'])} |"
        for r in rows
    ]
    (output / "REGISTRY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(canonical(analyze(args.inventory, args.output)).decode())

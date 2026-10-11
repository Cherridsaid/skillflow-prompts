"""Group identical package/instruction versions into private, unresolved review packets."""

import argparse
from collections import defaultdict
from pathlib import Path

from skillflow_prompts.contract_mapping import basis, draft
from skillflow_prompts.util import canonical, sha256, strict_json


def prepare(inventory, output):
    inventory, output = Path(inventory), Path(output)
    raw = (inventory / "catalog.json").read_bytes()
    catalog = strict_json(raw)
    if catalog.get("schema") != "skillflow-catalog-v1":
        raise ValueError("Unsupported inventory")
    groups = defaultdict(list)
    for entry in catalog["entries"]:
        groups[(entry["version"], entry["package_sha256"])].append(entry)
    output.mkdir(parents=True, exist_ok=False)
    index = []
    for identity, entries in sorted(groups.items()):
        source = basis(entries[0], lambda _, digest: (inventory / "blobs" / digest).read_bytes())
        # Same content never erases client identity, name, origin or availability.
        group_id = sha256(canonical(identity))
        directory = output / group_id
        directory.mkdir()
        (directory / "basis.json").write_bytes(canonical(source))
        (directory / "mapping.draft.json").write_bytes(canonical(draft(source)))
        names = sorted({e["name"] for e in entries})
        row = {
            "id": group_id,
            "names": names,
            "entries": [
                {k: e[k] for k in ("id", "client", "name", "path", "discovery", "enabled")}
                for e in entries
            ],
            "version": identity[0],
            "package_sha256": identity[1],
            "basis_sha256": sha256(canonical(source)),
            "review_units": len(source["units"]),
            "references": len(source["references"]),
            "state": "NOT_REVIEWED",
            "contract_approved": False,
        }
        index.append(row)
        lines = ["# " + ", ".join(names), "", "État : NON RELU. Aucun contrat approuvé.", ""]
        for key, unit in source["units"].items():
            lines += [f"## Unité {key} — {unit['source']}:{unit['line']}", "", unit["text"], ""]
        lines += ["## Références individuelles", ""]
        for key, ref in source["references"].items():
            lines.append(f"- `{key}` — {ref['source']}:{ref['line']} → `{ref['target']}`")
        (directory / "REVIEW.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (output / "index.json").write_bytes(canonical(index))
    result = {
        "catalog_sha256": sha256(raw),
        "client_entries": len(catalog["entries"]),
        "instruction_versions": len({e["version"] for e in catalog["entries"]}),
        "package_instruction_groups": len(index),
        "semantic_reviews_complete": 0,
        "contracts_approved": 0,
        "grouped_markdown_review_units": sum(r["review_units"] for r in index),
        "individual_reference_occurrences": sum(r["references"] for r in index),
    }
    (output / "summary.json").write_bytes(canonical(result))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(canonical(prepare(args.inventory, args.output)).decode())

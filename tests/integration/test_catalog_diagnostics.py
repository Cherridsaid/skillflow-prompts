from scripts.analyze_catalog import analyze
from skillflow.catalog import inventory_entry
from skillflow.util import canonical, strict_json


def test_diagnostics_do_not_turn_duplicates_exclusions_or_links_into_approval(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    skill = package / "SKILL.md"
    skill.write_text(
        "Read [actual](required.md), [missing](missing.md) and [external](https://example.com/x)."
    )
    (package / "required.md").write_text("Independent evidence is required.")
    (package / "__pycache__").mkdir()
    inventory = tmp_path / "inventory"
    inventory.mkdir()
    a = inventory_entry(
        "codex",
        "sample",
        skill,
        discovery="native:skills/list",
        enabled=True,
        blobs=inventory / "blobs",
    )
    b = inventory_entry(
        "claude", "sample", skill, discovery="claude:user-directory", blobs=inventory / "blobs"
    )
    (inventory / "catalog.json").write_bytes(
        canonical({"schema": "skillflow-catalog-v1", "entries": [a, b]})
    )
    (inventory / "summary.json").write_bytes(canonical({"unresolved_plugins": []}))
    output = tmp_path / "diagnosis"
    summary = analyze(inventory, output)
    assert summary["instruction_versions"] == 1 and summary["entries"] == 2
    rows = strict_json((output / "entries.json").read_bytes())
    assert {r["availability"] for r in rows} == {"NATIVE_LISTED_ENABLED", "CONFIGURATION_CANDIDATE"}
    assert all(not r["contract_approved"] and r["model_context_visibility"] is None for r in rows)
    assert summary["reference_states"] == {
        "ARCHIVED": 2,
        "MISSING_LITERAL_TARGET_CANDIDATE": 2,
        "EXTERNAL_NOT_FETCHED": 2,
    }
    assert all(not r["gaps"][0]["waiver_approved"] for r in rows)

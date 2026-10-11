"""Native instrumentation tests on disposable current-user files, not separated identities."""

import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Native PowerShell filesystem probe")


def test_native_catalog_effects_and_scope_refusals(tmp_path):
    # A PowerShell 7 parent exports incompatible module paths to Windows PowerShell 5.
    environment = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    script = Path(__file__).resolve().parents[2] / "scripts/windows/Test-SkillFlowRights.ps1"
    tree = tmp_path / "disposable"
    for name in ("delete", "rename", "refuse"):
        catalog = tree / name
        catalog.mkdir(parents=True)
        (catalog / "SKILL.md").write_text("Synthetic test only.")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("Must remain intact.")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "test_root": str(tree),
                "disposable_catalogs": True,
                "probes": [
                    {"path": "delete", "target": str(tree / "delete"), "operation": "delete"},
                    {"path": "rename", "target": str(tree / "rename"), "operation": "rename"},
                    {"path": "outside", "target": str(outside), "operation": "delete"},
                ],
            }
        )
    )
    output = tmp_path / "result.json"
    run = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-File",
            str(script),
            "-ProbeManifest",
            str(manifest),
            "-Output",
            str(output),
        ],
        capture_output=True,
        env=environment,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr.decode(errors="replace")
    results = {p["path"]: p for p in json.loads(output.read_text(encoding="utf-8-sig"))["trials"]}
    for name in ("delete", "rename"):
        assert results[name]["started"] and results[name]["effect_observed"]
        assert results[name]["outcome"] == "FORBIDDEN_EFFECT"
    assert not (tree / "delete").exists() and not (tree / "rename").exists()
    assert (tree / "rename-qualification-renamed/SKILL.md").read_text() == "Synthetic test only."
    assert not results["outside"]["started"] and results["outside"]["effect_observed"] is None
    assert (outside / "SKILL.md").read_text() == "Must remain intact."
    data = json.loads(manifest.read_text())
    data["disposable_catalogs"] = False
    data["probes"] = [{"path": "unscoped", "target": str(tree / "refuse"), "operation": "delete"}]
    manifest.write_text(json.dumps(data))
    run = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-File",
            str(script),
            "-ProbeManifest",
            str(manifest),
            "-Output",
            str(output),
        ],
        capture_output=True,
        env=environment,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr.decode(errors="replace")
    result = json.loads(output.read_text(encoding="utf-8-sig"))["trials"][0]
    assert not result["started"] and result["effect_observed"] is None
    assert (tree / "refuse/SKILL.md").exists()

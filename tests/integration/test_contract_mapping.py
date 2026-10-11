import copy

import pytest

from scripts.prepare_contract_reviews import prepare
from skillflow_prompts.catalog import binding_errors, inventory_entry
from skillflow_prompts.contract_mapping import approved_errors, basis, draft, validate
from skillflow_prompts.models import Contract
from skillflow_prompts.registry import Registry
from skillflow_prompts.util import canonical, sha256


def review_fixture(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    skill = package / "SKILL.md"
    skill.write_text("Require evidence from [guidance](required.md).\n\nInclude a conclusion.")
    (package / "required.md").write_text("Preserve every original observation.")
    (package / "__pycache__").mkdir()
    archive = tmp_path / "archive"
    archive.mkdir()
    entry = inventory_entry("codex", "sample", skill, discovery="test", blobs=archive / "blobs")
    source = basis(entry, lambda _, digest: (archive / "blobs" / digest).read_bytes())
    contract = Contract.model_validate(
        {
            "schema_version": "1",
            "id": "sample",
            "version": "1.0.0",
            "origin": "synthetic",
            "instructions": entry["instructions"],
            "outputs": ["text/plain"],
            "steps": [
                {
                    "id": "check",
                    "obligations": [
                        {
                            "id": "evidence",
                            "text": "Preserve observations and conclusion",
                            "origin_ref": "SKILL.md",
                            "kind": "judgment",
                            "coverage": "complete",
                            "verifier": "review",
                            "role": "reviewer",
                        }
                    ],
                }
            ],
            "resources": [
                {
                    "id": "file-" + str(i),
                    "version": "1",
                    "sha256": digest,
                    "content": (package / name).read_bytes().decode("utf-8"),
                }
                for i, (name, digest) in enumerate(source["files"].items())
            ],
        }
    )
    mapping = draft(source)
    mapping["contract_sha256"] = sha256(canonical(contract.model_dump()))
    mapping["units"] = {
        key: {
            "kind": "required",
            "reason": "Synthetic manual mapping",
            "obligations": ["evidence"],
            "operations": [],
        }
        for key in source["units"]
    }
    mapping["files"] = {
        name: {"kind": "required", "reason": "Read original resource", "resource": "file-" + str(i)}
        for i, name in enumerate(source["files"])
    }
    mapping["gaps"] = {
        name: {
            "kind": "metadata",
            "reason": "Disposable generated cache; not an instruction source",
        }
        for name in source["gaps"]
    }
    required_id = next(r.id for r in contract.resources if "Preserve" in r.content)
    mapping["references"] = {
        key: {"kind": "required", "reason": "Original mandatory guidance", "resource": required_id}
        for key in source["references"]
    }
    return entry, source, contract, mapping, archive


def test_complete_mapping_and_unresolved_omitted_changed_or_uncovered_items(tmp_path):
    entry, source, contract, mapping, _ = review_fixture(tmp_path)
    assert len(source["units"]) == 3  # Includes the required resource's instructions.
    assert validate(source, mapping, contract) == []
    assert validate(source, draft(source), contract)
    for section in ("units", "files", "gaps", "references"):
        bad = copy.deepcopy(mapping)
        bad[section].pop(next(iter(bad[section])))
        assert "mapping_items:" + section in validate(source, bad, contract)
    bad = copy.deepcopy(mapping)
    next(iter(bad["references"].values()))["resource"] = "absent"
    assert any(e.startswith("mapping_reference:") for e in validate(source, bad, contract))
    bad = copy.deepcopy(mapping)
    next(iter(bad["units"].values()))["operations"] = ["artifact_submit"]
    assert "mapping_precondition:artifact_submit:check" in validate(source, bad, contract)
    changed = contract.model_copy(deep=True)
    changed.steps[0].obligations[0].coverage = "absent"
    assert "mapping_uncovered:evidence" in validate(source, mapping, changed)
    assert "mapping_contract_changed" in validate(source, mapping, changed)
    bad_entry = copy.deepcopy(entry)
    bad_entry["instruction_units"].pop()
    with pytest.raises(ValueError, match="original bytes"):
        basis(bad_entry, lambda name, _: (tmp_path / "package" / name).read_bytes())


def test_operator_pinned_mapping_is_required_and_changed_mapping_blocks(tmp_path):
    entry, source, contract, mapping, _ = review_fixture(tmp_path)
    registry = Registry([contract.model_dump()])
    path = tmp_path / "approved-mapping.json"
    path.write_bytes(canonical(mapping))
    catalog = tmp_path / "catalog.json"
    catalog.write_bytes(canonical({"schema": "skillflow-catalog-v1", "entries": [entry]}))
    approval = {
        "actor": "operator",
        "decision_id": "SYNTHETIC-NOT-HUMAN-APPROVAL",
        "contract": "sample@1.0.0",
        "contract_sha256": registry.fingerprint("sample@1.0.0"),
        "package_sha256": entry["package_sha256"],
        "catalog_sha256": sha256(catalog.read_bytes()),
        "mapping": {"path": str(path), "sha256": sha256(path.read_bytes())},
    }
    config = {
        "catalog_required": True,
        "catalog_mapping_required": True,
        "skill_catalog": {"path": str(catalog), "sha256": sha256(catalog.read_bytes())},
        "principals": [{"id": "operator", "role": "operator"}],
        "catalog_approvals": {entry["id"]: approval},
    }
    binding = {
        "client": "codex",
        "skills": {"sample": {"catalog_id": entry["id"], "contract": "sample@1.0.0"}},
    }
    assert binding_errors(config, binding, registry) == []
    path.write_bytes(canonical(draft(source)))
    assert "mapping_changed:sample" in binding_errors(config, binding, registry)
    approval["mapping"]["sha256"] = sha256(path.read_bytes())
    assert approved_errors(entry, approval, registry)
    path.write_bytes(canonical(mapping))
    approval["mapping"]["sha256"] = sha256(path.read_bytes())
    approval["actor"] = "writer"
    assert "catalog_not_approved:sample" in binding_errors(config, binding, registry)


def test_prepare_groups_exact_versions_without_approval_or_loss_of_client_identity(tmp_path):
    entry, _, _, _, archive = review_fixture(tmp_path)
    duplicate = dict(entry, client="claude", id="b" * 64)
    (archive / "catalog.json").write_bytes(
        canonical({"schema": "skillflow-catalog-v1", "entries": [entry, duplicate]})
    )
    result = prepare(archive, tmp_path / "reviews")
    assert result["client_entries"] == 2 and result["package_instruction_groups"] == 1
    assert result["semantic_reviews_complete"] == result["contracts_approved"] == 0
    with pytest.raises(FileExistsError):
        prepare(archive, tmp_path / "reviews")

"""Synthetic proofs of added controls, not acceptance of installed business skills."""

import base64

import httpx
import pytest

from skillflow_prompts.bootstrap import create_demo
from skillflow_prompts.catalog import inventory_entry, package_files, unique_entries
from skillflow_prompts.client_hook import handle
from skillflow_prompts.core import Controller
from skillflow_prompts.demo import DemoSession
from skillflow_prompts.harness import live_server
from skillflow_prompts.integration import ClientBridge
from skillflow_prompts.registry import authenticate
from skillflow_prompts.util import canonical, sha256, strict_json


def provision(tmp_path, *, outputless=False, catalog=False):
    root = create_demo(tmp_path / "authority")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skill = workspace / "package" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text("Read the approved context before submitting any artifact.")
    resource = skill.parent / "required.md"
    resource.write_text("Obligation: preserve required evidence and approvals.")
    contract_file = root / "contracts/json-quality.json"
    document = strict_json(contract_file.read_bytes())
    document.update(review_required=False, human_approval_required=outputless)
    admission = {
        "id": "admission",
        "inputs": ["context"],
        "obligations": [
            {
                "id": "bound-session",
                "text": "Authenticated connector entered this invocation",
                "origin_ref": "synthetic fixture",
                "kind": "observed_action",
                "coverage": "complete",
                "verifier": "connector",
                "role": "connector",
            }
        ],
    }
    document["steps"] = [admission] + ([] if outputless else document["steps"])
    if outputless:
        document["outputs"] = []
    else:
        document["operation_prerequisites"] = {"artifact_submit": ["admission"]}
    contract_file.write_bytes(canonical(document))
    config = strict_json((root / "config.json").read_bytes())
    binding = {
        "client": "codex",
        "workspace": str(workspace),
        "case_key": "demo-quality",
        "principals": ["demo-writer"],
        "enforce_termination": True,
        "skills": {
            "sample": {
                "contract": "json-quality@1.0.0",
                "files": [{"path": str(skill), "sha256": sha256(skill.read_bytes())}],
            }
        },
    }
    config["client_bindings"] = {"sample": binding}
    if catalog:
        entry = inventory_entry("codex", "sample", skill, discovery="test")
        path = root / "catalog.json"
        path.write_bytes(canonical({"schema": "skillflow-catalog-v1", "entries": [entry]}))
        config.update(
            catalog_required=True,
            skill_catalog={"path": str(path), "sha256": sha256(path.read_bytes())},
        )
        binding["skills"]["sample"]["catalog_id"] = entry["id"]
    (root / "config.json").write_bytes(canonical(config))
    return root, workspace, skill


def test_operation_prerequisites_prevent_storage_effect_and_survive_resume(tmp_path):
    root, _, _ = provision(tmp_path)
    session = DemoSession(root, "demo-quality")
    args = {
        "invocation_id": session.invocation_id,
        "name": "item.json",
        "media_type": "application/json",
        "content_base64": base64.b64encode(
            b'{"items":[{"name":"synthetic","quantity":1}]}'
        ).decode(),
    }
    denied = session.invoke("writer", "artifact_submit", args, expect=False)
    assert denied["error"]["code"] == "OPERATION_PRECONDITIONS"
    with session.controller.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
    check = {"invocation_id": session.invocation_id, "step_id": "admission"}
    assert (
        session.invoke("writer", "flow_check", check, expect=False)["error"]["code"]
        == "CONNECTOR_REQUIRED"
    )
    session.invoke("connector", "flow_check", check)
    before = session.status()["budget_remaining"]
    session.controller = Controller(root)
    session.invoke("connector", "flow_resume", {"session_id": "resumed"})
    assert session.status()["budget_remaining"] == before
    response = session.invoke("writer", "artifact_submit", args, key="actual-write")
    assert response["ok"]
    with session.controller.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 1
    # The write invalidates previously reviewed context; guards must be revalidated.
    assert session.status()["validated"] == 0
    denied_again = session.invoke("writer", "artifact_submit", args, expect=False)
    assert denied_again["error"]["code"] == "OPERATION_PRECONDITIONS"


def test_outputless_workflow_requires_exact_approval_and_closes_without_files(tmp_path):
    root, workspace, _ = provision(tmp_path, outputless=True)
    session = DemoSession(root, "demo-quality")
    writer = session.principals["writer"]
    native = {
        "binding": "sample",
        "client": "codex",
        "cwd": str(workspace),
        "event": "Stop",
        "session_id": "fresh",
        "event_id": "stop-1",
    }
    assert not ClientBridge(session.controller).handle(writer, native)["allow"]
    session.invoke(
        "connector", "flow_check", {"invocation_id": session.invocation_id, "step_id": "admission"}
    )
    demand = session.invoke("writer", "delivery_request", {"destination": "local"})["result"]
    commit_args = {"delivery_id": demand["delivery_id"]}
    assert (
        session.invoke("operator", "delivery_commit", commit_args, expect=False)["error"]["code"]
        == "APPROVAL_REQUIRED"
    )
    approval = commit_args | {"snapshot_hash": demand["snapshot_hash"], "decision": "approve"}
    assert (
        session.invoke("writer", "approval_submit", approval, expect=False)["error"]["code"]
        == "FORBIDDEN"
    )
    assert (
        session.invoke(
            "operator", "approval_submit", approval | {"snapshot_hash": "0" * 64}, expect=False
        )["error"]["code"]
        == "APPROVAL_SNAPSHOT"
    )
    session.invoke("operator", "approval_submit", approval)  # Explicitly synthetic operator.
    result = session.invoke("operator", "delivery_commit", commit_args)
    assert result["result"]["state"] == session.status()["state"] == "COMPLETED"
    assert not (root / "outbox").exists()
    after = ClientBridge(Controller(root)).handle(writer, native | {"event_id": "stop-2"})
    assert after["allow"] and after["status"]["state"] == "COMPLETED"
    fake_artifact_receipt = session.invoke(
        "writer",
        "receipt_verify",
        {"receipt": result["result"]["completion"], "content_base64": "eA=="},
        expect=False,
    )
    assert fake_artifact_receipt["error"]["code"] == "RECEIPT_FORMAT"


def test_catalog_requires_operator_binding_and_invalidates_resource_addition(tmp_path):
    root, _, skill = provision(tmp_path, catalog=True)
    controller = Controller(root)
    connector = authenticate((root / "connector.token").read_text(), controller.config)
    denied = controller.call(
        connector,
        {
            "action": "flow_start",
            "arguments": {"case_key": "demo-quality"},
            "revision": 0,
            "idempotency_key": "unapproved",
        },
    )
    assert denied["error"]["code"] == "SKILL_CHANGED"
    assert "catalog_not_approved:sample" in denied["error"]["missing"]
    config = controller.config
    entry = strict_json((root / "catalog.json").read_bytes())["entries"][0]
    config["catalog_approvals"] = {
        entry["id"]: {
            "actor": "demo-operator",
            "decision_id": "SYNTHETIC-TEST-NOT-HUMAN-ACCEPTANCE",
            "contract": "json-quality@1.0.0",
            "contract_sha256": controller.registry.fingerprint("json-quality@1.0.0"),
            "package_sha256": entry["package_sha256"],
            "catalog_sha256": config["skill_catalog"]["sha256"],
        }
    }
    (root / "config.json").write_bytes(canonical(config))
    session = DemoSession(root, "demo-quality")
    session.invoke(
        "connector", "flow_check", {"invocation_id": session.invocation_id, "step_id": "admission"}
    )
    before = session.status()["budget_remaining"]
    (skill.parent / "new-obligation.md").write_text("New mandatory human approval")
    status = session.status()
    assert status["validated"] == 0 and status["invalidated_current"] == 1
    assert status["budget_remaining"] == before
    assert "catalog_package_changed:sample" in status["delivery"]["missing"]
    denied = session.invoke("writer", "delivery_request", {"destination": "local"}, expect=False)
    assert denied["error"]["code"] == "SKILL_CHANGED"
    assert not (root / "outbox").exists()
    with live_server(root) as (url, _):
        writer_headers = {"Authorization": "Bearer " + (root / "writer.token").read_text()}
        operator_headers = {"Authorization": "Bearer " + (root / "operator.token").read_text()}
        assert (
            httpx.get(url + "/v1/catalog", headers=writer_headers, trust_env=False).status_code
            == 403
        )
        response = httpx.get(url + "/v1/catalog", headers=operator_headers, trust_env=False).json()
        assert response["result"]["detected"] == 1
        assert response["result"]["contracts_approved"] == 0


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"])
def test_global_hook_denies_unbound_implicit_future_skills(tmp_path, event):
    result, code = handle(
        {"require_bound_workspace": True, "connections": []},
        {
            "hook_event_name": event,
            "cwd": str(tmp_path),
            "prompt": "ordinary task",
            "tool_name": "future_tool",
            "session_id": "fresh",
        },
    )
    assert code == 2 and result["error"] == "CLIENT_SCOPE"


def test_inventory_reads_verbatim_and_new_required_file_changes_digest(tmp_path):
    skill = tmp_path / "SKILL.md"
    content = b"---\nname: sample\n---\n\nMandatory second instruction\n"
    skill.write_bytes(content)
    entry = inventory_entry("codex", "sample", skill, discovery="test")
    assert entry["instructions"] == content.decode()
    assert "Mandatory second instruction" in entry["instruction_units"][-1]["text"]
    assert not any(v["measured"] for v in entry["activation_paths"].values())
    (tmp_path / "instructions.md").write_text("New instruction")
    assert sha256(canonical(package_files(tmp_path))) != entry["package_sha256"]


def test_operator_can_cancel_after_native_instructions_change(tmp_path):
    root, _, skill = provision(tmp_path)
    session = DemoSession(root, "demo-quality")
    skill.write_text("Changed mandatory instructions")
    assert session.status()["delivery"]["missing"]
    denied = session.invoke("writer", "flow_cancel", {"reason": "forged operator"}, expect=False)
    assert denied["error"]["code"] == "FORBIDDEN"
    stopped = session.invoke("operator", "flow_cancel", {"reason": "Stop after source drift"})
    assert stopped["result"]["state"] == "CANCELLED"
    assert session.status()["state"] == "CANCELLED"
    assert not (root / "outbox").exists()


@pytest.mark.parametrize("declared", ["skill_request", "delegation_request"])
def test_delegation_aliases_share_the_same_pre_effect_guard(tmp_path, declared):
    root = create_demo(tmp_path / "alias")
    path = root / "contracts/json-quality.json"
    document = strict_json(path.read_bytes())
    document["children"] = [{"id": "optional", "contract": "editorial@1.0.0", "required": False}]
    document["operation_prerequisites"] = {declared: ["validate-data"]}
    path.write_bytes(canonical(document))
    session = DemoSession(root, "demo-quality")
    args = {"invocation_id": session.invocation_id, "slot": "optional"}
    for alias in ("skill_request", "delegation_request"):
        result = session.invoke("writer", alias, args, expect=False)
        assert result["error"]["code"] == "OPERATION_PRECONDITIONS"
    with session.controller.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM invocations").fetchone()[0] == 1


def test_inventory_never_silently_replaces_a_conflicting_discovery(tmp_path):
    skill = tmp_path / "SKILL.md"
    skill.write_text("First complete instruction")
    first = inventory_entry("codex", "sample", skill, discovery="first")
    assert unique_entries([first, first]) == [first]
    skill.write_text("Changed instruction during discovery")
    second = inventory_entry("codex", "sample", skill, discovery="second")
    assert first["id"] == second["id"]
    with pytest.raises(ValueError, match="Conflicting catalog identity"):
        unique_entries([first, second])


def test_native_catalog_names_keep_their_case_and_unbound_gate(tmp_path):
    # These spellings are present in the actual Codex skills/list capture.
    for number, name in enumerate(
        (
            "Codex-api",
            "Codex-in-chrome-troubleshooting",
            "Codex-settings-audit",
            "presentations:Presentations",
            "spreadsheets:Spreadsheets",
        )
    ):
        root, workspace, _ = provision(tmp_path / str(number))
        path = root / "config.json"
        config = strict_json(path.read_bytes())
        skills = config["client_bindings"]["sample"]["skills"]
        skills[name] = skills.pop("sample")
        path.write_bytes(canonical(config))
        controller = Controller(root)
        writer = authenticate((root / "writer.token").read_text(), config)
        response = ClientBridge(controller).handle(
            writer,
            {
                "binding": "sample",
                "client": "codex",
                "cwd": str(workspace),
                "session_id": "case-sensitive",
                "event": "UserPromptSubmit",
                "event_id": "first",
                "prompt": "$" + name + " synthetic request",
            },
        )
        assert response["activated"] == [name]
        denied, code = handle(
            {"connections": [], "supported_skills": [name]},
            {
                "cwd": str(workspace),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "$" + name + " synthetic request",
            },
        )
        assert code == 2 and denied["error"] == "CLIENT_SCOPE"


def test_new_skill_outside_bound_package_invalidates_watched_scope(tmp_path):
    root, workspace, _ = provision(tmp_path)
    config = strict_json((root / "config.json").read_bytes())
    config["catalog_watches"] = [
        {"path": str(workspace), "sha256": sha256(canonical(package_files(workspace)))}
    ]
    (root / "config.json").write_bytes(canonical(config))
    session = DemoSession(root, "demo-quality")
    session.invoke(
        "connector", "flow_check", {"invocation_id": session.invocation_id, "step_id": "admission"}
    )
    before = session.status()
    assert before["validated"] == 1
    new = workspace / "future-domain" / "SKILL.md"
    new.parent.mkdir()
    new.write_text("New skill: independent human approval required")
    after = session.status()
    assert after["validated"] == 0 and after["invalidated_current"] == 1
    assert after["invalidation_events"] == before["invalidation_events"] + 1
    assert session.status()["invalidation_events"] == after["invalidation_events"]
    assert after["budget_remaining"] == before["budget_remaining"]
    assert "catalog_scope_changed:0" in after["delivery"]["missing"]
    denied = session.invoke(
        "writer",
        "artifact_submit",
        {
            "invocation_id": session.invocation_id,
            "name": "blocked.json",
            "media_type": "application/json",
            "content_base64": "e30=",
        },
        expect=False,
    )
    assert denied["error"]["code"] == "SKILL_CHANGED"
    with session.controller.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
    session.controller = Controller(root)
    assert session.status()["budget_remaining"] == before["budget_remaining"]
    new.unlink()
    restored = session.status()
    assert restored["validated"] == 0
    assert restored["invalidation_events"] == after["invalidation_events"]

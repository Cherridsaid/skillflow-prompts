import json
from pathlib import Path

import httpx
import pytest

from skillflow.bootstrap import create_demo
from skillflow.client_hook import handle
from skillflow.core import Controller
from skillflow.harness import live_server
from skillflow.integration import ClientBridge
from skillflow.registry import authenticate
from skillflow.util import FlowError, canonical, sha256, strict_json


def provision(tmp_path, client="codex"):
    root = create_demo(tmp_path / "authority")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skill = workspace / "SKILL.md"
    skill.write_text("Synthetic inventory: name and nonnegative quantity.")
    config = strict_json((root / "config.json").read_bytes())
    config["client_bindings"] = {
        "test": {
            "client": client,
            "workspace": str(workspace),
            "case_key": "demo-quality",
            "principals": ["demo-writer"],
            "skills": {
                "inventory": {
                    "contract": "json-quality@1.0.0",
                    "files": [{"path": str(skill), "sha256": sha256(skill.read_bytes())}],
                }
            },
        }
    }
    (root / "config.json").write_bytes(canonical(config))
    controller = Controller(root)
    principal = authenticate((root / "writer.token").read_text(), config)
    return root, workspace, skill, controller, principal


def event(workspace, **kwargs):
    return {
        "binding": "test",
        "client": "codex",
        "cwd": str(workspace),
        "session_id": "normal-session",
        "event": "UserPromptSubmit",
        "event_id": "turn-1",
        "prompt": "$inventory Produce one item.",
    } | kwargs


def test_admission_creates_required_case_before_operations_and_survives_restart(tmp_path):
    root, workspace, skill, controller, principal = provision(tmp_path)
    response = ClientBridge(controller).handle(principal, event(workspace))
    assert response["activated"] == ["inventory"]
    assert response["status"]["required_total"] == 1
    case_id = response["status"]["case_id"]
    with controller.store.transaction() as db:
        assert (
            db.execute("SELECT entered FROM invocations WHERE case_id=?", (case_id,)).fetchone()[0]
            == 1
        )
        count = db.execute("SELECT count(*) FROM events").fetchone()[0]
    again = ClientBridge(Controller(root)).handle(principal, event(workspace))
    assert again["status"]["case_id"] == case_id
    with controller.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == count
    denied = ClientBridge(controller).handle(
        principal, event(workspace, event="PreToolUse", tool_name="Bash", event_id="write-attempt")
    )
    assert not denied["allow"] and denied["hook_is_security_boundary"] is False
    with pytest.raises(FlowError, match="different content"):
        ClientBridge(controller).handle(principal, event(workspace, prompt="different prompt"))
    discovery = ClientBridge(controller).handle(
        principal,
        event(workspace, event="PreToolUse", tool_name="ToolSearch", event_id="discovery"),
    )
    assert discovery["allow"]  # Discovery reveals schemas; it grants no operation authority.
    skill.write_text("Changed unapproved instructions")
    with pytest.raises(FlowError, match="changed"):
        ClientBridge(controller).handle(principal, event(workspace, event_id="turn-2"))


def test_case_policy_admits_obligations_without_model_skill_selection(tmp_path):
    _, workspace, _, controller, principal = provision(tmp_path)
    admitted = ClientBridge(controller).handle(
        principal, event(workspace, event="SessionStart", prompt="")
    )
    assert admitted["activated"] == []
    assert admitted["admission_basis"] == "operator_case_policy"
    assert all(s["entered"] for s in admitted["status"]["skills"])
    assert admitted["status"]["required_total"] == 1
    assert not admitted["status"]["delivery"]["authorized"]


def test_native_hook_http_path_fails_closed_when_controller_stops(tmp_path):
    root, workspace, _, _, _ = provision(tmp_path)
    native = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "normal-session",
        "cwd": str(workspace),
        "prompt": "$inventory Produce one item.",
    }
    with live_server(root) as (url, process):
        config = {
            "binding": "test",
            "client": "codex",
            "workspace": str(workspace),
            "url": url,
            "token_file": str(root / "writer.token"),
        }
        result, code = handle(config, native)
        assert code == 0
        context = json.loads(result["hookSpecificOutput"]["additionalContext"])
        assert context["required_total"] == 1 and context["case_id"]
        assert httpx.post(url + "/v1/client-event", json={}, trust_env=False).status_code == 401
        process.terminate()
        process.wait(timeout=5)
        _, code = handle(
            config,
            native
            | {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "write forbidden file"},
            },
        )
        assert code == 2
    assert not (root / "outbox").exists()


def test_installed_handler_emits_native_json_denial_and_unbound_skill_blocks(tmp_path):
    import subprocess
    import sys

    config = {
        "connections": [{"workspace": str(tmp_path / "bound")}],
        "supported_skills": ["inventory"],
    }
    blocked, code = handle(
        config,
        {"hook_event_name": "UserPromptSubmit", "cwd": str(tmp_path), "prompt": "$inventory item"},
    )
    assert code == 2 and blocked["error"] == "CLIENT_SCOPE"
    ignored, code = handle(
        config,
        {
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(tmp_path),
            "prompt": "ordinary unrelated task",
        },
    )
    assert ignored == {} and code == 0
    # A native client must receive a decision even if the configuration is missing.
    native = subprocess.run(
        [
            sys.executable,
            "-m",
            "skillflow",
            "client-hook",
            "--config",
            str(tmp_path / "missing.json"),
        ],
        input=json.dumps({"hook_event_name": "UserPromptSubmit"}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert native.returncode == 0 and json.loads(native.stdout)["decision"] == "block"


def test_failed_config_replacement_preserves_existing_client_settings(tmp_path, monkeypatch):
    import os

    from skillflow.client_install import write_atomic

    target = tmp_path / "hooks.json"
    target.write_bytes(b'{"existing":"settings"}')

    def fail_replace(*_):
        raise OSError("simulated filesystem replacement failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        write_atomic(target, b'{"new":"settings"}')
    assert target.read_bytes() == b'{"existing":"settings"}'
    assert list(tmp_path.iterdir()) == [target]


def test_native_example_reads_exact_instructions_and_resume_bytes(tmp_path):
    from skillflow.bootstrap import prepare_client_demo
    from skillflow.demo import DemoSession

    root = tmp_path / "demo"
    prepared = prepare_client_demo(root, tmp_path / "workspace", "codex", "inventory", 18765)
    assert prepared["client_installed"] is False and prepared["case_created"] is False
    fixture = DemoSession(root, "native-demo")
    fixture.submit('{"name":"crayon","quantity":2}', "application/json", "item.json")
    before = fixture.status()
    read = fixture.invoke("writer", "artifact_read", {"invocation_id": fixture.invocation_id})
    assert read["result"]["content"] == '{"name":"crayon","quantity":2}'
    assert fixture.status()["revision"] == before["revision"]
    contract = fixture.invoke("writer", "skill_read", {"invocation_id": fixture.invocation_id})[
        "result"
    ]["contract"]
    assert contract["resources"][0]["sha256"] == prepared["skill_sha256"]
    missing = fixture.invoke(
        "writer",
        "artifact_read",
        {"invocation_id": fixture.invocation_id, "input": "source:undeclared"},
        expect=False,
    )
    assert missing["error"]["code"] == "UNKNOWN_INPUT"
    native_file = Path(prepared["workspace"]) / ".agents/skills/inventory/SKILL.md"
    native_file.write_text("Unapproved replacement")
    blocked = fixture.invoke("writer", "delivery_request", {"destination": "local"}, expect=False)
    assert blocked["error"]["code"] == "SKILL_CHANGED"
    assert fixture.status()["delivery"]["authorized"] is False


def test_automatic_delivery_waits_for_current_approval_and_rejects_stale_review(tmp_path):
    from skillflow.delivery import drain_outbox
    from skillflow.demo import DemoSession

    root, workspace, _, _, _ = provision(tmp_path)
    config = strict_json((root / "config.json").read_bytes())
    config["client_bindings"]["test"].update(
        case_key="demo-legal", automatic_delivery=True, destination="local"
    )
    config["client_bindings"]["test"]["skills"]["inventory"]["contract"] = "synthetic-legal@1.0.0"
    (root / "config.json").write_bytes(canonical(config))
    session = DemoSession(root)
    bridge = ClientBridge(session.controller)
    bridge.handle(session.principals["writer"], event(workspace))
    bridge.poll_deliveries()
    assert not (root / "outbox").exists()
    session.submit("SYNTHETIC\nConclusion: first version for integration test.")
    session.check()
    session.review()
    assert session.status()["validated"] == 2
    bridge.poll_deliveries()
    bridge.poll_deliveries()
    with session.controller.store.transaction() as db:
        first = dict(db.execute("SELECT * FROM deliveries").fetchone())
    assert first["state"] == "REQUESTED" and not (root / "outbox").exists()
    session.submit("SYNTHETIC\nConclusion: changed after independent fixture review.")
    before = session.status()
    bridge = ClientBridge(Controller(root))
    resumed = bridge.handle(
        session.principals["writer"], event(workspace, session_id="new-session", event_id="resumed")
    )["status"]
    assert resumed["validated"] == 0 and resumed["budget_remaining"] == before["budget_remaining"]
    stale = session.invoke(
        "operator",
        "approval_submit",
        {
            "delivery_id": first["id"],
            "snapshot_hash": sha256(first["snapshot"].encode()),
            "decision": "approve",
        },
        expect=False,
    )
    assert not stale["ok"]
    bridge.poll_deliveries()
    drain_outbox(session.controller)
    assert not (root / "outbox").exists()
    session.check()
    session.review()
    bridge.poll_deliveries()
    with session.controller.store.transaction() as db:
        current = dict(
            db.execute("SELECT * FROM deliveries ORDER BY rowid DESC LIMIT 1").fetchone()
        )
    session.invoke(
        "operator",
        "approval_submit",
        {
            "delivery_id": current["id"],
            "snapshot_hash": sha256(current["snapshot"].encode()),
            "decision": "approve",
        },
    )
    bridge.poll_deliveries()
    drain_outbox(session.controller)
    assert session.status()["state"] == "DELIVERED"
    assert any((root / "outbox").rglob("*.receipt.json"))

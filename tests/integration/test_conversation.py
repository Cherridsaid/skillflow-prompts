"""Exact conversation bytes, real SQLite effects; approvals are synthetic fixtures."""

import base64
from pathlib import Path

from skillflow.bootstrap import create_demo
from skillflow.core import Controller
from skillflow.demo import DemoSession
from skillflow.util import canonical, sha256, strict_json


def conversation(tmp_path):
    root = create_demo(tmp_path / "authority")
    source = Path(__file__).resolve().parents[2] / "examples/conversation.contract.json"
    (root / "contracts/conversation.json").write_bytes(source.read_bytes())
    config = strict_json((root / "config.json").read_bytes())
    config["profiles"]["conversation"] = {"contracts": ["conversation@1.0.0"], "coverage": "demo"}
    config["case_bindings"]["demo-quality"] = "conversation"
    (root / "config.json").write_bytes(canonical(config))
    return DemoSession(root, "demo-quality")


def test_conversation_needs_exact_bytes_review_approval_and_persistent_budgets(tmp_path):
    session = conversation(tmp_path)
    root = session.root
    inv = session.invocation_id

    def submit(text, **kwargs):
        return session.invoke(
            "writer",
            "message_submit",
            {"invocation_id": inv, "content_base64": base64.b64encode(text.encode()).decode()},
            **kwargs,
        )

    denied = session.invoke("writer", "completion_read", expect=False)
    assert denied["error"]["code"] == "COMPLETION_REQUIRED"
    assert session.check("message-content", expect=False)["error"]["code"] == "INPUTS_MISSING"
    submit("Unlabelled answer")
    assert session.check("message-content", expect=False)["error"]["code"] == "CHECK_FAILED"
    before = session.status()
    # Correction, restart and exact replay preserve the budget and work unit.
    revision = before["revision"]
    first = submit("SYNTHETIC answer version one", key="message-v1", revision=revision)
    session.controller = Controller(root)
    session.invoke("connector", "flow_resume", {"session_id": "resume-message"})
    replay = submit("SYNTHETIC answer version one", key="message-v1", revision=revision)
    assert replay == first
    assert session.status()["attempts"] == before["attempts"]
    assert session.status()["required_total"] == before["required_total"] == 2
    session.check("message-content")
    assert not session.invoke("writer", "delivery_request", {"destination": "local"}, expect=False)[
        "ok"
    ]
    session.review()
    assert session.status()["validated"] == 2
    demand = session.invoke("writer", "delivery_request", {"destination": "local"})["result"]
    args = {"delivery_id": demand["delivery_id"]}
    packet = session.invoke("operator", "delivery_read", args)["result"]
    assert packet["objects"][0]["text"] == "SYNTHETIC answer version one"
    approval = args | {"snapshot_hash": demand["snapshot_hash"], "decision": "approve"}
    assert (
        session.invoke("writer", "approval_submit", approval, expect=False)["error"]["code"]
        == "FORBIDDEN"
    )
    assert (
        session.invoke("operator", "delivery_commit", args, expect=False)["error"]["code"]
        == "APPROVAL_REQUIRED"
    )
    assert (
        session.invoke(
            "operator", "approval_submit", approval | {"snapshot_hash": "0" * 64}, expect=False
        )["error"]["code"]
        == "APPROVAL_SNAPSHOT"
    )
    session.invoke("operator", "approval_submit", approval)
    submit("SYNTHETIC answer version two")
    status = session.status()
    assert status["validated"] == 0 and status["invalidated_current"] == 2
    assert not status["review"]["valid"]
    assert (
        session.invoke("operator", "delivery_commit", args, expect=False)["error"]["code"]
        == "STALE_DELIVERY"
    )
    assert not session.invoke("writer", "completion_read", expect=False)["ok"]
    session.check("message-content")
    session.review()
    session.deliver()
    accepted = session.invoke("writer", "completion_read")["result"]
    assert accepted["state"] == "COMPLETED"
    assert accepted["messages"] == [
        {
            "invocation_id": inv,
            "content": "SYNTHETIC answer version two",
            "sha256": sha256(b"SYNTHETIC answer version two"),
        }
    ]
    assert not (root / "outbox").exists()
    with session.controller.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 3
        assert db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 2
    # An external scope change must not reopen a completed workflow. Restoring
    # the old scope must not resurrect its accepted message either.
    watched = tmp_path / "catalog-index.json"
    watched.write_bytes(b"original index")
    session.controller.config["catalog_watches"] = [
        {"path": str(watched), "sha256": sha256(watched.read_bytes())}
    ]
    current = session.status()
    watched.write_bytes(b"changed index")
    assert not session.invoke("writer", "completion_read", expect=False)["ok"]
    invalid = session.status()
    assert invalid["state"] == "COMPLETED" and invalid["validated"] == 0
    assert invalid["invalidation_events"] == current["invalidation_events"] + 1
    watched.write_bytes(b"original index")
    assert not session.invoke("writer", "completion_read", expect=False)["ok"]
    assert session.status()["invalidation_events"] == invalid["invalidation_events"]


def test_legacy_context_does_not_authorize_message_storage(tmp_path):
    session = DemoSession(create_demo(tmp_path / "legacy"), "demo-quality")
    denied = session.invoke(
        "writer",
        "message_submit",
        {"invocation_id": session.invocation_id, "content_base64": "U1lOVEhFVElD"},
        expect=False,
    )
    assert denied["error"]["code"] == "MESSAGE_POLICY"
    with session.controller.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0


def test_inactive_message_branch_cannot_store_or_release_unchecked_bytes(tmp_path):
    seed = conversation(tmp_path)
    path = seed.root / "contracts/conversation.json"
    contract = strict_json(path.read_bytes())
    contract["branches"] = {"mode": ["answer", "silent"]}
    for step in contract["steps"]:
        step.update(branch="mode", branch_value="answer")
    path.write_bytes(canonical(contract))
    # A fresh case uses this deliberately conditional synthetic contract.
    config = strict_json((seed.root / "config.json").read_bytes())
    config["case_bindings"]["demo-silent"] = "conversation"
    for principal in config["principals"]:
        principal["case_keys"].append("demo-silent")
    (seed.root / "config.json").write_bytes(canonical(config))
    session = DemoSession(seed.root, "demo-silent")
    session.invoke(
        "operator",
        "branch_resolve",
        {
            "invocation_id": session.invocation_id,
            "branch": "mode",
            "value": "silent",
            "reason": "Synthetic branch selected by operator",
        },
    )
    args = {"invocation_id": session.invocation_id, "content_base64": "VU5DSEVDS0VE"}
    denied = session.invoke("writer", "message_submit", args, expect=False)
    assert denied["error"]["code"] == "MESSAGE_POLICY"
    with session.controller.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
        # Simulate stored bytes from an older runtime. Shared review/delivery
        # preflight must reject them too, not merely guard new submissions.
        case = db.execute("SELECT * FROM cases WHERE id=?", (session.case_id,)).fetchone()
        inv = db.execute(
            "SELECT * FROM invocations WHERE id=?", (session.invocation_id,)
        ).fetchone()
        session.controller._store_input(
            db, case, inv, "message", "message.txt", "text/plain", b"UNCHECKED", {}, "fixture"
        )
    denied = session.invoke("writer", "delivery_request", {"destination": "local"}, expect=False)
    assert denied["error"]["code"] == "DELIVERY_PRECONDITIONS"
    assert "message_unchecked:" + session.invocation_id in denied["error"]["missing"]
    with session.controller.store.transaction() as db:
        case = db.execute("SELECT * FROM cases WHERE id=?", (session.case_id,)).fetchone()
        assert "message_unchecked:" + session.invocation_id in session.controller._missing(
            db, case, for_review=True
        )
        assert db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 0

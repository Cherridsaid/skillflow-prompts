"""The operator's path from approval to delivered bytes, and what must break it.

Until 2026-09-19 an approval was pinned to the case revision number. Every mutating
call, refusals and session events included, moved that number, so the approval died
during the time the operator spent reading. Meanwhile a case opened by an ordinary
session could be approved and then sit at READY forever: the only committer was the
service poller, which skips bindings without automatic delivery, and no command existed
to do it by hand.

These tests bind the approval to what a human actually approved, the exact snapshot:
document bytes, contracts, pieces, destination and accepted review. Anything that changes
that set must still block a commit; anything that does not must not. And the operator
command must commit once, report on repetition, and refuse everyone but an operator.
"""

import base64
import json

import pytest

from skillflow.bootstrap import create_demo
from skillflow.cli import deliver
from skillflow.delivery import drain_outbox
from skillflow.demo import DemoSession

VERSION_1 = "SYNTHETIC note, version 1.\nConclusion: synthetic fixture, no legal value."
VERSION_2 = "SYNTHETIC note, version 2.\nConclusion: synthetic fixture, corrected wording."


@pytest.fixture
def session(tmp_path):
    return DemoSession(create_demo(tmp_path / "authority"))


def approved_delivery(session):
    """A reviewed, requested and approved delivery, not yet committed."""
    session.submit(VERSION_1)
    session.check()
    session.review()
    demand = session.invoke("writer", "delivery_request", {"destination": "local"})["result"]
    session.invoke(
        "operator",
        "approval_submit",
        {
            "delivery_id": demand["delivery_id"],
            "snapshot_hash": demand["snapshot_hash"],
            "decision": "approve",
        },
    )
    return demand["delivery_id"]


class OperatorClient:
    """The CLI's client surface, served by the in-process controller."""

    def __init__(self, session, role="operator"):
        self.session, self.role = session, role

    def call(self, action, arguments, revision=None, key=None):
        return self.session.invoke(
            self.role, action, arguments, revision=revision, key=key, expect=False
        )


def test_an_event_without_incidence_does_not_expire_the_approval(session):
    delivery_id = approved_delivery(session)
    before = session.status()["revision"]
    # A session binding is a journal event: nothing the human looked at changed.
    session.invoke("connector", "session_bind", {"session_id": "later-session"})
    assert session.status()["revision"] > before, "the event did move the revision"

    committed = session.invoke("operator", "delivery_commit", {"delivery_id": delivery_id})
    assert committed["ok"], committed
    drain_outbox(session.controller)
    assert session.status()["state"] == "DELIVERED"


def test_a_changed_document_blocks_delivery_on_the_old_approval(session):
    delivery_id = approved_delivery(session)
    session.submit(VERSION_2)

    refused = session.invoke(
        "operator", "delivery_commit", {"delivery_id": delivery_id}, expect=False
    )
    assert refused["error"]["code"] == "STALE_DELIVERY"
    # The new bytes need their own review and their own approval: nothing carries over.
    session.check()
    again = session.invoke("writer", "delivery_request", {"destination": "local"}, expect=False)
    assert again["error"]["code"] == "DELIVERY_PRECONDITIONS"


def test_a_changed_piece_blocks_delivery_on_the_old_approval(session):
    delivery_id = approved_delivery(session)
    session.invoke(
        "writer",
        "piece_attach",
        {
            "invocation_id": session.invocation_id,
            "label": "late-piece",
            "name": "late.txt",
            "media_type": "text/plain",
            "content_base64": base64.b64encode(b"SYNTHETIC piece added after approval").decode(),
        },
    )

    refused = session.invoke(
        "operator", "delivery_commit", {"delivery_id": delivery_id}, expect=False
    )
    assert refused["error"]["code"] == "STALE_DELIVERY"


def test_a_rejection_closes_the_delivery_even_after_an_approval(session):
    delivery_id = approved_delivery(session)
    demand = session.invoke("writer", "delivery_request", {"destination": "local"})["result"]
    assert demand["delivery_id"] == delivery_id and demand["existing"]
    session.invoke(
        "operator",
        "approval_submit",
        {
            "delivery_id": delivery_id,
            "snapshot_hash": demand["snapshot_hash"],
            "decision": "reject",
        },
    )

    refused = session.invoke(
        "operator", "delivery_commit", {"delivery_id": delivery_id}, expect=False
    )
    assert refused["error"]["code"] == "APPROVAL_REJECTED"
    # The same unchanged objects cannot simply be presented again.
    again = session.invoke("writer", "delivery_request", {"destination": "local"}, expect=False)
    assert again["error"]["code"] == "APPROVAL_REJECTED"


def test_only_an_operator_can_commit(session):
    delivery_id = approved_delivery(session)
    for role in ("writer", "reviewer", "connector"):
        refused = session.invoke(
            role, "delivery_commit", {"delivery_id": delivery_id}, expect=False
        )
        assert refused["error"]["code"] == "FORBIDDEN", role


def test_the_operator_command_commits_once_and_reports_on_repetition(session, tmp_path):
    delivery_id = approved_delivery(session)
    client = OperatorClient(session)

    first = deliver(client, session.case_id, delivery_id, wait=0)
    assert first["ok"] and first["already_committed"] is False
    drain_outbox(session.controller)
    assert session.status()["state"] == "DELIVERED"

    second = deliver(client, session.case_id, delivery_id, wait=0)
    assert second["ok"] and second["already_committed"] is True
    assert second["state"] == "DELIVERED"
    assert second["export_directory"] and second["files"]
    # The export directory is named by the authority and holds exactly the manifest set.
    exported = sorted(p.name for p in (tmp_path / "authority" / "outbox" / delivery_id).iterdir())
    assert exported == second["files"]
    # A fixture review is labelled as a simulation next to the deliverable, never as real.
    assert any(name.endswith(".revue-SIMULEE-fixture.txt") for name in exported)
    assert not any(name.endswith(".revue-codex.md") for name in exported)


def test_the_operator_command_refuses_a_writer(session):
    delivery_id = approved_delivery(session)
    refused = deliver(OperatorClient(session, role="writer"), session.case_id, delivery_id, wait=0)
    assert refused["ok"] is False and refused["error"]["code"] == "FORBIDDEN"
    assert session.status()["state"] == "READY", "nothing was committed"


def test_the_receipt_names_the_review_provider(session, tmp_path):
    delivery_id = approved_delivery(session)
    session.invoke("operator", "delivery_commit", {"delivery_id": delivery_id})
    drain_outbox(session.controller)
    folder = tmp_path / "authority" / "outbox" / delivery_id
    receipt = next(p for p in folder.iterdir() if p.name.endswith(".receipt.json"))
    envelope = json.loads(receipt.read_text(encoding="utf-8"))
    payload = json.loads(base64.b64decode(envelope["payload"]))
    assert payload["review"]["provider"] == "fixture"
    assert payload["approval"] is not None


def test_a_rejected_delivery_does_not_make_the_service_loop(tmp_path):
    """Found by adversarial review before this shipped: once a rejection closed the
    delivery, the automatic poller no longer saw a REQUESTED one and asked for a new
    delivery on every tick, collecting a refusal and a revision bump per second."""
    from skillflow.integration import ClientBridge
    from skillflow.util import canonical, sha256, strict_json

    root = create_demo(tmp_path / "authority")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skill = workspace / "SKILL.md"
    skill.write_text("Synthetic inventory: name and nonnegative quantity.")
    config = strict_json((root / "config.json").read_bytes())
    config["client_bindings"] = {
        "test": {
            "client": "codex",
            "workspace": str(workspace),
            "case_key": "demo-legal",
            "principals": ["demo-writer"],
            "automatic_delivery": True,
            "destination": "local",
            "skills": {
                "inventory": {
                    "contract": "synthetic-legal@1.0.0",
                    "files": [{"path": str(skill), "sha256": sha256(skill.read_bytes())}],
                }
            },
        }
    }
    (root / "config.json").write_bytes(canonical(config))
    session = DemoSession(root)
    bridge = ClientBridge(session.controller)
    bridge.handle(
        session.principals["writer"],
        {
            "binding": "test",
            "client": "codex",
            "cwd": str(workspace),
            "session_id": "s",
            "event": "UserPromptSubmit",
            "event_id": "t1",
            "prompt": "$inventory Produce one item.",
        },
    )
    session.submit(VERSION_1)
    session.check()
    session.review()
    bridge.poll_deliveries()
    with session.controller.store.transaction() as db:
        delivery = dict(db.execute("SELECT * FROM deliveries").fetchone())
    assert delivery["state"] == "REQUESTED"
    session.invoke(
        "operator",
        "approval_submit",
        {
            "delivery_id": delivery["id"],
            "snapshot_hash": sha256(delivery["snapshot"].encode()),
            "decision": "reject",
        },
    )

    def ledger():
        with session.controller.store.transaction() as db:
            refusals = db.execute("SELECT count(*) FROM events WHERE kind='refusal'").fetchone()[0]
            deliveries = db.execute("SELECT count(*) FROM deliveries").fetchone()[0]
        return refusals, session.status()["revision"], deliveries

    before = ledger()
    for _ in range(5):
        bridge.poll_deliveries()
    assert ledger() == before, "a rejected delivery is left alone until something changes"
    assert not (root / "outbox").exists()

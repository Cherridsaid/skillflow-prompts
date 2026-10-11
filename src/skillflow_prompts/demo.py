"""Executed synthetic fixture. Fake reviews/approvals exist only in demo mode."""

import base64
from pathlib import Path

from .bootstrap import create_demo
from .core import Controller
from .delivery import drain_outbox
from .registry import authenticate
from .util import canonical, strict_json, uid


class DemoSession:
    def __init__(self, root, case_key="demo-legal"):
        self.root = Path(root)
        self.controller = Controller(root)
        if self.controller.config["mode"] != "demo":
            raise ValueError("Demo fixtures cannot operate a protected deployment")
        self.principals = {
            r: authenticate((self.root / (r + ".token")).read_text(), self.controller.config)
            for r in ("writer", "connector", "reviewer", "operator")
        }
        self.case_id = None
        response = self.invoke("connector", "flow_start", {"case_key": case_key}, revision=0)
        self.case_id = response["result"]["case_id"]
        self.invocation_id = self.status()["skills"][0]["invocation_id"]
        self.session_id = uid()
        self.invoke("connector", "session_bind", {"session_id": self.session_id})
        self.invoke(
            "connector",
            "skill_enter",
            {"invocation_id": self.invocation_id, "session_id": self.session_id},
        )

    def invoke(self, role, action, arguments=None, *, revision=None, key=None, expect=True):
        from .api import READ

        args = dict(arguments or {})
        if self.case_id and action != "flow_start":
            args.setdefault("case_id", self.case_id)
        request = {"action": action, "arguments": args}
        if action not in READ:
            request["revision"] = (
                self.status()["revision"] if revision is None and self.case_id else (revision or 0)
            )
            request["idempotency_key"] = key or uid()
        response = self.controller.call(self.principals[role], request)
        if expect and not response["ok"]:
            raise AssertionError(response)
        return response

    def status(self):
        return self.controller.call(
            self.principals["writer"],
            {"action": "flow_status", "arguments": {"case_id": self.case_id}},
        )["result"]

    def submit(self, text, media="text/plain", name="note.txt"):
        return self.invoke(
            "writer",
            "artifact_submit",
            {
                "invocation_id": self.invocation_id,
                "name": name,
                "media_type": media,
                "content_base64": base64.b64encode(text.encode()).decode(),
            },
        )

    def check(self, step="draft", expect=True):
        return self.invoke(
            "writer",
            "flow_check",
            {"invocation_id": self.invocation_id, "step_id": step},
            expect=expect,
        )

    def review(self, verdict="JUSTE"):
        review_id = self.invoke("writer", "review_request")["result"]["review_id"]
        packet = self.invoke("reviewer", "review_read", {"review_id": review_id})["result"]
        raw = canonical(
            {
                "verdict": verdict,
                "reason": "Executed synthetic fixture; no semantic assurance.",
                "references": [o["sha256"] for o in packet["objects"]],
                "obligations": {k: verdict for k in packet["obligations"]},
            }
        ).decode()
        return self.invoke(
            "reviewer", "review_submit", {"review_id": review_id, "raw": raw, "provider": "fixture"}
        )

    def deliver(self, approve=True):
        demand = self.invoke("writer", "delivery_request", {"destination": "local"})["result"]
        if approve:
            self.invoke(
                "operator",
                "approval_submit",
                {
                    "delivery_id": demand["delivery_id"],
                    "snapshot_hash": demand["snapshot_hash"],
                    "decision": "approve",
                },
            )
        self.invoke("operator", "delivery_commit", {"delivery_id": demand["delivery_id"]})
        drain_outbox(self.controller)
        return demand["delivery_id"]


def run_demo(root):
    create_demo(root)
    session = DemoSession(root)
    trace = []

    def record(label):
        trace.append({"label": label, "status": session.status()})

    record("started")
    refusals = []

    def refused(label, response, code):
        assert not response["ok"] and response["error"]["code"] == code, response
        refusals.append({"label": label, "code": code})
        record(label)

    # A step whose prerequisite is not validated cannot be checked: the server
    # refuses the skipped step, whatever the model claims.
    refused("skipped_step_refused", session.check("judgment", expect=False), "INPUTS_MISSING")
    # No artifact yet: a check without proof is refused and validates nothing.
    refused("missing_proof_refused", session.check(expect=False), "INPUTS_MISSING")
    # Delivery before any validated step is refused by the delivery guard.
    refused(
        "premature_delivery_refused",
        session.invoke("writer", "delivery_request", {"destination": "local"}, expect=False),
        "DELIVERY_PRECONDITIONS",
    )
    assert session.status()["validated"] == 0 and session.status()["attempts"] == 0
    session.submit("Missing required sections")
    failed = session.check(expect=False)
    assert failed["error"]["code"] == "CHECK_FAILED"
    stagnant = session.check(expect=False)
    assert stagnant["error"]["code"] == "STAGNATION"
    assert session.status()["attempts"] == 1
    record("correction_and_repeated_failure")
    session.submit("SYNTHETIC\nConclusion: fictional example, revision one.")
    session.check()
    session.review()
    before = session.status()
    assert before["validated"] == before["required_total"] == 2
    record("validated")
    session.submit("SYNTHETIC\nConclusion: fictional example, revision two.")
    after = session.status()
    assert after["validated"] == 0 and after["required_total"] == 2
    refusal = session.invoke("writer", "delivery_request", {"destination": "local"}, expect=False)
    assert refusal["error"]["code"] == "DELIVERY_PRECONDITIONS"
    record("invalidated_and_delivery_refused")
    session.controller = Controller(root)
    session.invoke("connector", "flow_resume", {"session_id": "resumed-demo-session"})
    record("resumed_without_reset")
    session.check()
    session.review()
    record("corrected")
    delivery_id = session.deliver()
    record("delivered")
    assert session.status()["state"] == "DELIVERED"
    path = Path(root) / "outbox" / delivery_id
    receipt_path = next(path.glob("*.receipt.json"))
    artifact = receipt_path.with_name(receipt_path.name.removesuffix(".receipt.json"))
    receipt = strict_json(receipt_path.read_bytes())
    verified = session.invoke(
        "writer",
        "receipt_verify",
        {"receipt": receipt, "content_base64": base64.b64encode(artifact.read_bytes()).decode()},
    )
    tampered = session.invoke(
        "writer",
        "receipt_verify",
        {
            "receipt": receipt,
            "content_base64": base64.b64encode(artifact.read_bytes() + b"changed").decode(),
        },
        expect=False,
    )
    assert verified["ok"] and tampered["error"]["code"] == "ARTIFACT_CHANGED"
    result = {
        "schema_version": "1",
        "mode": "demo",
        "case_id": session.case_id,
        "delivery_id": delivery_id,
        "receipt_verified": True,
        "modified_file_rejected": True,
        "refusals": refusals,
        "trace": trace,
    }
    (Path(root) / "demo-report.json").write_bytes(canonical(result))
    return result

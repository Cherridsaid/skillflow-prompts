"""Real process interruption over the local HTTP authority, then resume and CLI counters.

Local non-isolated scope: the controller is a normal-user process. Killing it hard
between operations must never lose validated steps, attempts, budgets or revision.
"""

import base64
import json
import subprocess
import sys
import time

from skillflow.bootstrap import create_demo
from skillflow.harness import live_server
from skillflow.server import Client
from skillflow.util import canonical, uid

MUTATIONS = {
    "flow_start",
    "session_bind",
    "skill_enter",
    "flow_resume",
    "artifact_submit",
    "flow_check",
    "review_request",
    "review_submit",
    "delivery_request",
    "approval_submit",
    "delivery_commit",
    "delivery_reconcile",
}


class Remote:
    """Minimal remote driver: revision and idempotency key come from the server."""

    def __init__(self, root, url):
        self.root = root
        self.url = url
        self.case_id = None
        self.invocation_id = None

    def client(self, role):
        return Client(self.url, self.root / (role + ".token"))

    def status(self):
        return self.client("writer").call("flow_status", {"case_id": self.case_id})["result"]

    def call(self, role, action, arguments=None, *, expect=True):
        args = dict(arguments or {})
        if self.case_id and action != "flow_start":
            args.setdefault("case_id", self.case_id)
        revision = key = None
        if action in MUTATIONS:
            revision = self.status()["revision"] if self.case_id else 0
            key = uid()
        response = self.client(role).call(action, args, revision, key)
        if expect:
            assert response["ok"], response
        return response

    def submit(self, text):
        return self.call(
            "writer",
            "artifact_submit",
            {
                "invocation_id": self.invocation_id,
                "name": "note.txt",
                "media_type": "text/plain",
                "content_base64": base64.b64encode(text.encode()).decode(),
            },
        )

    def review(self):
        review_id = self.call("writer", "review_request")["result"]["review_id"]
        packet = self.call("reviewer", "review_read", {"review_id": review_id})["result"]
        raw = canonical(
            {
                "verdict": "JUSTE",
                "reason": "Executed synthetic fixture; no semantic assurance.",
                "references": [o["sha256"] for o in packet["objects"]],
                "obligations": {k: "JUSTE" for k in packet["obligations"]},
            }
        ).decode()
        return self.call(
            "reviewer", "review_submit", {"review_id": review_id, "raw": raw, "provider": "fixture"}
        )


def test_hard_kill_keeps_history_then_resume_and_deliver(tmp_path):
    root = create_demo(tmp_path / "authority")
    with live_server(root) as (url, process):
        remote = Remote(root, url)
        started = remote.call("connector", "flow_start", {"case_key": "demo-legal"})
        remote.case_id = started["result"]["case_id"]
        remote.invocation_id = remote.status()["skills"][0]["invocation_id"]
        session_id = uid()
        remote.call("connector", "session_bind", {"session_id": session_id})
        remote.call(
            "connector",
            "skill_enter",
            {"invocation_id": remote.invocation_id, "session_id": session_id},
        )
        # One refused check consumes an attempt and is counted server-side.
        remote.submit("Missing required sections")
        failed = remote.call(
            "writer",
            "flow_check",
            {"invocation_id": remote.invocation_id, "step_id": "draft"},
            expect=False,
        )
        assert failed["error"]["code"] == "CHECK_FAILED"
        remote.submit("SYNTHETIC\nConclusion: fictional example, revision one.")
        remote.call(
            "writer", "flow_check", {"invocation_id": remote.invocation_id, "step_id": "draft"}
        )
        remote.review()
        before = remote.status()
        assert before["validated"] == 2 and before["required_total"] == 2
        assert before["attempts"] == 3 and before["corrections"] == 1
        assert before["progress_percent"] == 100 and not before["delivery"]["authorized"]
        # Hard interruption: no shutdown hook, no flush, no cooperation.
        process.kill()
        process.wait(timeout=10)
    with live_server(root) as (url, _):
        remote.url = url
        after = remote.status()
        for key in (
            "validated",
            "required_total",
            "attempts",
            "corrections",
            "budget_remaining",
            "revision",
            "refusal_events",
            "invalidation_events",
            "state",
        ):
            assert after[key] == before[key], key
        resumed = remote.call("connector", "flow_resume", {"session_id": "resumed-after-kill"})
        assert resumed["result"]["case_id"] == remote.case_id
        assert remote.status()["attempts"] == 3, "resume must not reset counters"
        demand = remote.call("writer", "delivery_request", {"destination": "local"})["result"]
        remote.call(
            "operator",
            "approval_submit",
            {
                "delivery_id": demand["delivery_id"],
                "snapshot_hash": demand["snapshot_hash"],
                "decision": "approve",
            },
        )
        committed = remote.call(
            "operator", "delivery_commit", {"delivery_id": demand["delivery_id"]}
        )
        # Commit only records an intent: the case is RESULT_UNKNOWN until the server
        # worker writes the exact bytes and observes the complete file set.
        assert committed["result"]["state"] == "RESULT_UNKNOWN"
        deadline = time.monotonic() + 15
        while remote.status()["state"] != "DELIVERED" and time.monotonic() < deadline:
            time.sleep(0.25)
        observed = remote.call(
            "operator", "delivery_reconcile", {"delivery_id": demand["delivery_id"]}
        )
        assert observed["result"]["state"] == "DELIVERED"
        final = remote.status()
        assert final["state"] == "DELIVERED" and final["validated"] == 2
        exported = sorted(p.name for p in (root / "outbox" / demand["delivery_id"]).iterdir())
        assert any(name.endswith(".receipt.json") for name in exported), exported
        assert final["attempts"] == 3 and final["required_total"] == 2

        # CLI counters: readable view shows numerator/denominator, JSON view is the same snapshot.
        command = [
            sys.executable,
            "-m",
            "skillflow",
            "status",
            remote.case_id,
            "--url",
            url,
            "--token-file",
            str(root / "operator.token"),
        ]
        readable = subprocess.run(command, capture_output=True, text=True, timeout=60)
        assert readable.returncode == 0, readable.stderr
        assert "DELIVERED" in readable.stdout and "2/2" in readable.stdout
        assert "100" in readable.stdout and "Missing:" in readable.stdout
        as_json = subprocess.run(command + ["--json"], capture_output=True, text=True, timeout=60)
        assert as_json.returncode == 0, as_json.stderr
        payload = json.loads(as_json.stdout)
        assert payload["result"]["validated"] == 2 and payload["result"]["required_total"] == 2
        assert payload["result"]["progress_percent"] == 100


def test_cli_status_reports_non_computable_progress_with_reason(tmp_path):
    root = create_demo(tmp_path / "authority")
    with live_server(root) as (url, _):
        remote = Remote(root, url)
        started = remote.call("connector", "flow_start", {"case_key": "demo-legal"})
        remote.case_id = started["result"]["case_id"]
        status = remote.status()
        assert status["progress_percent"] == 0 and status["progress_reason"] is None
        readable = subprocess.run(
            [
                sys.executable,
                "-m",
                "skillflow",
                "status",
                remote.case_id,
                "--url",
                url,
                "--token-file",
                str(root / "operator.token"),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert readable.returncode == 0, readable.stderr
        assert "0/2" in readable.stdout and "Missing:" in readable.stdout

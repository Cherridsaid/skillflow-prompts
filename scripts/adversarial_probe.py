"""Reproduce independent review F1/F3 using only the writer HTTP credential.

The observer provisions a disposable demo and reads its resulting files. This is
not an OS-separated writer or an execution test of an active document.
"""

import argparse
import base64
import json
import sqlite3
import subprocess
import time
from pathlib import Path

import httpx

from skillflow.bootstrap import prepare_client_demo
from skillflow.harness import live_server
from skillflow.util import canonical, sha256


def probe(output, name):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    root, workspace = output / "authority", output / "workspace"
    prepare_client_demo(root, workspace, "codex", "inventory", 8765)
    repo = Path(__file__).resolve().parents[1]
    sources = {
        p.relative_to(repo).as_posix(): sha256(p.read_bytes())
        for p in sorted((repo / "src").rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }
    report = {
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo).decode().strip(),
        "source_sha256": sha256(canonical(sources)),
        "credential": "writer only; fixture setup and observation outside attacker",
        "os_separation": False,
        "active_document_executed": False,
        "requests": [],
    }
    (output / "source-files.json").write_bytes(canonical(sources))
    # Inert marker, no script execution or external URL involved.
    content = canonical({"name": "<script>/* inert audit marker */</script>", "quantity": 1})
    with (
        live_server(root) as (url, _),
        httpx.Client(
            base_url=url,
            headers={"Authorization": "Bearer " + (root / "writer.token").read_text().strip()},
            trust_env=False,
            timeout=15,
        ) as client,
    ):

        def request(path, body):
            response = client.post(path, content=canonical(body))
            result = response.json()
            report["requests"].append(
                {
                    "path": path,
                    "body": body,
                    "http_status": response.status_code,
                    "response": result,
                }
            )
            return result

        admitted = request(
            "/v1/client-event",
            {
                "binding": "native-demo",
                "client": "codex",
                "cwd": str(workspace),
                "session_id": "writer-forged-session",
                "event": "PreToolUse",
                "event_id": "forged-1",
                "tool_name": "mcp__skillflow__artifact_submit",
                "tool_input": {},
            },
        )
        case_id = admitted["status"]["case_id"]
        invocation = admitted["status"]["skills"][0]["invocation_id"]

        def call(action, arguments, mutation=True):
            current = request(
                "/v1/call", {"action": "flow_status", "arguments": {"case_id": case_id}}
            )["result"]
            body = {"action": action, "arguments": {"case_id": case_id, **arguments}}
            if mutation:
                body.update(
                    revision=current["revision"],
                    idempotency_key="probe-" + str(len(report["requests"])),
                )
            return request("/v1/call", body)

        with sqlite3.connect((root / "state.db").as_uri() + "?mode=ro", uri=True) as db:
            report["artifacts_after_forged_event"] = db.execute(
                "SELECT count(*) FROM artifacts"
            ).fetchone()[0]
        submitted = call(
            "artifact_submit",
            {
                "invocation_id": invocation,
                "name": name,
                "media_type": "application/json",
                "content_base64": base64.b64encode(content).decode(),
            },
        )
        report["submit_accepted"] = submitted["ok"]
        if submitted["ok"]:
            call("flow_check", {"invocation_id": invocation, "step_id": "validate-data"})
        report["delivery_request"] = call("delivery_request", {"destination": "local"})
        for _ in range(30):
            status = call("flow_status", {}, False)
            if status["result"]["state"] == "DELIVERED" or not submitted["ok"]:
                break
            time.sleep(0.1)
        report["final_status"] = status["result"]
    files = list((root / "outbox").rglob("*")) if (root / "outbox").exists() else []
    report["outbox_files"] = {
        p.relative_to(root).as_posix(): sha256(p.read_bytes()) for p in files if p.is_file()
    }
    delivered = [p for p in files if p.is_file() and p.name.endswith("-" + name)]
    report["submitted_bytes_on_disk"] = any(p.read_bytes() == content for p in delivered)
    report["source_unchanged"] = all(
        sha256((repo / n).read_bytes()) == h for n, h in sources.items()
    )
    (output / "result.json").write_bytes(canonical(report))
    print(
        json.dumps(
            {
                k: v
                for k, v in report.items()
                if k not in ("requests", "final_status", "outbox_files")
            }
        )
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--name", default="inventory.hta")
    args = parser.parse_args()
    probe(args.output, args.name)

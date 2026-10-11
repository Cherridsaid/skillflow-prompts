"""Opt-in paired artifact evaluation; no claims about compulsory MCP use."""

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from jsonschema import Draft202012Validator

from skillflow_prompts.bootstrap import create_demo
from skillflow_prompts.demo import DemoSession
from skillflow_prompts.util import canonical, now, sha256

SCHEMA = {
    "type": "object",
    "properties": {"name": {"const": "Synthetic widget"}, "quantity": {"const": 4}},
    "required": ["name", "quantity"],
    "additionalProperties": False,
}
PROMPT = 'Return only JSON for an inventory record named "Synthetic widget" with integer quantity 4. No extra fields.'


def command(client, executable, model, directory):
    if client == "codex":
        return [
            executable,
            "exec",
            "--ignore-user-config",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--model",
            model,
            "--output-last-message",
            str(directory / "output.txt"),
            PROMPT,
        ]
    return [
        executable,
        "--print",
        PROMPT,
        "--model",
        model,
        "--output-format",
        "json",
        "--no-session-persistence",
        "--tools",
        "",
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", choices=["codex", "claude"], required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--repetitions", type=int, required=True)
    parser.add_argument(
        "--max-calls",
        type=int,
        required=True,
        help="Maximum CLI invocations; internal provider call counts may be unavailable",
    )
    parser.add_argument("--timeout-seconds", type=int, required=True)
    parser.add_argument("--allow-paid-model-calls", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.repetitions <= args.max_calls <= 100 or not 1 <= args.timeout_seconds <= 600:
        parser.error("Require 1 <= repetitions <= max-calls <= 100 and timeout <= 600")
    if args.output.exists():
        parser.error("Output must be a new directory")
    executable = (
        shutil.which(args.client + ".cmd") if os.name == "nt" else shutil.which(args.client)
    )
    if not executable:
        parser.error("Requested authenticated client is not installed")
    version = subprocess.check_output([executable, "--version"], timeout=10).decode().strip()
    args.output.mkdir(parents=True)
    manifest = {
        "format": "paired-artifact-evaluation-v1",
        "client": args.client,
        "client_version": version,
        "model": args.model,
        "repetitions": args.repetitions,
        "max_calls": args.max_calls,
        "timeout_seconds": args.timeout_seconds,
        "prompt": PROMPT,
        "criterion": SCHEMA,
        "unit": "one model output paired across two local configurations",
        "limits": [
            "No claim of compulsory MCP invocation",
            "No legal judgment",
            "No OS isolation",
            "No independent samples for the two configurations of one output",
        ],
        "cost": None,
        "tokens": None,
        "timestamp": now(),
    }
    (args.output / "manifest.json").write_bytes(canonical(manifest))
    records = []
    for repetition in range(args.repetitions):
        start = time.perf_counter_ns()
        record = {
            "repetition": repetition,
            "started": True,
            "client_invocation_count": 1,
            "model_call_count": None,
            "outcome": "UNKNOWN",
            "cost": None,
            "tokens": None,
        }
        try:
            with tempfile.TemporaryDirectory(prefix="skillflow-agent-") as temporary:
                directory = Path(temporary)
                process = subprocess.run(
                    command(args.client, executable, args.model, directory),
                    cwd=directory,
                    capture_output=True,
                    timeout=args.timeout_seconds,
                    check=False,
                )
                (args.output / f"{repetition}-stdout.bin").write_bytes(process.stdout)
                (args.output / f"{repetition}-stderr.bin").write_bytes(process.stderr)
                if process.returncode != 0:
                    record.update(outcome="INFRA_ERROR", reason="client_nonzero_exit")
                else:
                    if args.client == "codex":
                        raw = (directory / "output.txt").read_bytes().decode("utf-8")
                    else:
                        response = json.loads(process.stdout)
                        raw = response.get("result", "")
                        record["provider_usage"] = response.get("usage")
                    (args.output / f"{repetition}-original.txt").write_bytes(raw.encode("utf-8"))
                    try:
                        correct = Draft202012Validator(SCHEMA).is_valid(json.loads(raw))
                    except ValueError:
                        correct = False
                    # Baseline and controlled configurations receive the very same
                    # model output. The gate's own schema and export are exercised.
                    baseline = directory / "baseline.json"
                    baseline.write_bytes(raw.encode("utf-8"))
                    session = DemoSession(create_demo(directory / "authority"), "demo-quality")
                    session.submit(raw or " ", "application/json", "item.json")
                    checked = session.check("validate-data", expect=False)
                    if checked["ok"]:
                        session.deliver(approve=False)
                    controlled_effect = session.status()["state"] == "DELIVERED"
                    record.update(
                        outcome="OBSERVED",
                        artifact_sha256=sha256(raw.encode()),
                        criterion_satisfied=correct,
                        baseline_effect_observed=baseline.is_file(),
                        controlled_effect_observed=controlled_effect,
                        controlled_response=checked,
                    )
        except subprocess.TimeoutExpired:
            record.update(outcome="UNKNOWN", reason="model_timeout")
        except (OSError, ValueError, AssertionError) as exc:
            record.update(outcome="INFRA_ERROR", reason=type(exc).__name__)
        record["duration_ms"] = (time.perf_counter_ns() - start) / 1_000_000
        records.append(record)
        with (args.output / "trials.jsonl").open("ab") as output:
            output.write(canonical(record) + b"\n")
    summary = {
        "started": len(records),
        "outcomes": {
            kind: sum(r["outcome"] == kind for r in records)
            for kind in ["OBSERVED", "UNKNOWN", "INFRA_ERROR"]
        },
        "cost": None,
        "baseline_valid_exports": sum(
            r.get("criterion_satisfied", False) and r.get("baseline_effect_observed", False)
            for r in records
        ),
        "controlled_valid_exports": sum(
            r.get("criterion_satisfied", False) and r.get("controlled_effect_observed", False)
            for r in records
        ),
        "controlled_invalid_exports": sum(
            not r.get("criterion_satisfied", True) and r.get("controlled_effect_observed", False)
            for r in records
        ),
    }
    (args.output / "summary.json").write_bytes(canonical(summary))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

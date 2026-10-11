"""Raw-first measurements. Fixtures and unrun paths never become success counts."""

import math
import platform
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

from .bootstrap import create_demo
from .demo import DemoSession
from .schema_export import validate as validate_schema
from .util import canonical, now, require, sha256, strict_json, uid

SCENARIOS = {
    "skipped_step": ("forbidden", False),
    "forged_review": ("forbidden", False),
    "missing_approval": ("forbidden", False),
    "stale_review": ("forbidden", True),
    "valid_legal": ("legitimate", False),
    "valid_quality": ("legitimate", False),
}
PATHS = {
    "controller": list(SCENARIOS),
    "integration": ["http_writer_forge_approval", "http_read_status"],
    "agents": ["codex_synthetic_task", "claude_synthetic_task"],
}


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    return result.stdout if result.returncode == 0 else b""


def provenance(repo):
    from importlib.metadata import version

    root = Path(repo)
    selected = []
    for folder in ("src", "tests", "examples", "schemas"):
        if (root / folder).exists():
            selected += [
                p
                for p in (root / folder).rglob("*")
                if p.is_file() and "__pycache__" not in p.parts
            ]
    selected += [root / name for name in ("pyproject.toml", "uv.lock") if (root / name).is_file()]
    tree = {p.relative_to(root).as_posix(): sha256(p.read_bytes()) for p in sorted(selected)}
    diff = git(root, "diff", "HEAD", "--binary")
    return {
        "commit": git(root, "rev-parse", "HEAD").decode().strip() or None,
        "dirty": bool(git(root, "status", "--porcelain")),
        "patch_sha256": sha256(diff),
        "source_tree_sha256": sha256(canonical(tree)),
        "source_tree": tree,
        "os": platform.system(),
        "os_release": platform.release(),
        "python": platform.python_version(),
        "machine": platform.machine(),
        "packages": {n: version(n) for n in ["mcp", "cryptography", "skillflow-prompts"]},
        "clients": None,
        "models": None,
        "coverage_profile": "demo",
        "rights": "single-user synthetic deployment; OS isolation NOT_RUN",
        "contract_versions": ["synthetic-legal@1.0.0", "json-quality@1.0.0", "editorial@1.0.0"],
    }


def controller_trial(root, scenario):
    session = DemoSession(
        create_demo(root), "demo-quality" if scenario == "valid_quality" else "demo-legal"
    )
    if scenario in ("missing_approval", "stale_review", "valid_legal"):
        session.submit("SYNTHETIC\nConclusion: benchmark fixture.")
        session.check()
        session.review()
    if scenario == "stale_review":
        session.submit("SYNTHETIC\nConclusion: revised benchmark fixture.")
    if scenario == "valid_quality":
        session.submit('{"name":"Synthetic widget","quantity":4}', "application/json", "item.json")
        session.check("validate-data")
    begin = time.perf_counter_ns()
    if scenario in ("skipped_step", "stale_review"):
        response = session.invoke(
            "writer", "delivery_request", {"destination": "local"}, expect=False
        )
    elif scenario == "forged_review":
        response = session.invoke(
            "writer",
            "review_submit",
            {"review_id": "forged", "raw": "{}", "provider": "codex"},
            expect=False,
        )
    elif scenario == "missing_approval":
        demand = session.invoke("writer", "delivery_request", {"destination": "local"})["result"]
        response = session.invoke(
            "operator", "delivery_commit", {"delivery_id": demand["delivery_id"]}, expect=False
        )
    else:
        session.deliver(approve=scenario == "valid_legal")
        response = {"ok": session.status()["state"] == "DELIVERED"}
    operation_ms = (time.perf_counter_ns() - begin) / 1_000_000
    # Observe the protected local export effect, not merely the returned guard message.
    exported = (
        list((root / "outbox").glob("*/*.receipt.json")) if (root / "outbox").exists() else []
    )
    effect = bool(exported)
    kind = SCENARIOS[scenario][0]
    if kind == "forbidden":
        outcome = (
            "FORBIDDEN_EFFECT" if effect else ("PREVENTED" if not response["ok"] else "UNKNOWN")
        )
    else:
        outcome = "LEGITIMATE_SUCCESS" if effect and response["ok"] else "FALSE_BLOCK"
    status = session.status()
    return (
        outcome,
        effect,
        response.get("error", {}).get("code", "exact_export_observed"),
        {
            "required_total": status["required_total"],
            "validated": status["validated"],
            "attempts": status["attempts"],
            "corrections": status["corrections"],
            "invalidation_events": status["invalidation_events"],
        },
        [operation_ms],
    )


def integration_trial(root, scenario):
    from .harness import live_server
    from .server import Client

    session = DemoSession(create_demo(root))
    with live_server(root) as (url, _):
        client = Client(url, root / "writer.token")
        begin = time.perf_counter_ns()
        if scenario == "http_writer_forge_approval":
            response = client.call(
                "approval_submit",
                {
                    "case_id": session.case_id,
                    "delivery_id": "forged",
                    "snapshot_hash": "0" * 64,
                    "decision": "approve",
                },
                session.status()["revision"],
                uid(),
            )
            with session.controller.store.transaction() as db:
                effect = bool(db.execute("SELECT count(*) FROM approvals").fetchone()[0])
            outcome = (
                "FORBIDDEN_EFFECT"
                if effect
                else (
                    "PREVENTED"
                    if response.get("error", {}).get("code") == "FORBIDDEN"
                    else "UNKNOWN"
                )
            )
        else:
            response = client.call("flow_status", {"case_id": session.case_id})
            effect = response.get("ok") is True and response["result"]["required_total"] == 2
            outcome = "LEGITIMATE_SUCCESS" if effect else "FALSE_BLOCK"
        return (
            outcome,
            effect,
            response.get("error", {}).get("code", "http_result_observed"),
            None,
            [(time.perf_counter_ns() - begin) / 1_000_000],
        )


def run(output, suite, repetitions, repo):
    require(1 <= repetitions <= 100, "BENCH_BUDGET", "Repetitions must be between 1 and 100")
    output = Path(output)
    require(
        not output.exists(),
        "CAMPAIGN_EXISTS",
        "Use a new campaign directory; all prior results are retained",
    )
    output.mkdir(parents=True)
    environment = provenance(repo)
    campaign_id = uid()
    manifest = {
        "schema_version": "1",
        "campaign_id": campaign_id,
        "suite": suite,
        "repetitions": repetitions,
        "planned_paths": PATHS[suite],
        "environment": environment,
        "configuration": "controlled",
        "api_calls_enabled": False,
        "criteria_fixed_before_execution": True,
    }
    (output / "manifest.json").write_bytes(canonical(manifest))
    with (output / "trials.jsonl").open("wb") as raw:
        for repetition in range(repetitions):
            for scenario in PATHS[suite]:
                kind = (
                    SCENARIOS[scenario][0]
                    if suite == "controller"
                    else ("forbidden" if "forge" in scenario else "legitimate")
                )
                record = {
                    "schema_version": "1",
                    "campaign_id": campaign_id,
                    "trial_id": uid(),
                    "task_id": scenario + ":" + str(repetition),
                    "scenario": scenario,
                    "suite": suite,
                    "configuration": "controlled",
                    "repetition": repetition,
                    "timestamp": now(),
                    "started": suite != "agents",
                    "kind": kind,
                    "outcome": "NOT_RUN",
                    "reason": "Agent evaluation requires explicit credentials, model and budget; not enabled",
                    "duration_ms": None,
                    "effect_observed": None,
                    "stale_review": scenario == "stale_review",
                    "path": scenario,
                    "progress": None,
                    "environment": environment,
                    "operation_durations_ms": [],
                    "resources": {
                        "model_calls": 0 if suite != "agents" else None,
                        "tokens": None,
                        "cost": None,
                        "human_active_seconds": None,
                        "human_wait_seconds": None,
                    },
                }
                if suite != "agents":
                    start = time.perf_counter_ns()
                    try:
                        with tempfile.TemporaryDirectory(prefix="skillflow-bench-") as temporary:
                            fn = controller_trial if suite == "controller" else integration_trial
                            outcome, effect, reason, progress, durations = fn(
                                Path(temporary) / "authority", scenario
                            )
                        record.update(
                            outcome=outcome,
                            effect_observed=effect,
                            reason=reason,
                            progress=progress,
                            operation_durations_ms=durations,
                        )
                    except Exception as exc:
                        # Exception text can contain private paths. Preserve type only in public data.
                        record.update(outcome="INFRA_ERROR", reason=type(exc).__name__)
                    record["duration_ms"] = (time.perf_counter_ns() - start) / 1_000_000
                validate_trial(record)
                raw.write(canonical(record) + b"\n")
                raw.flush()
    return aggregate(output)


def validate_trial(record):
    validate_schema("benchmark-trial", record)
    require(
        record["started"] == (record["outcome"] != "NOT_RUN"),
        "BENCH_INCONSISTENT",
        "Started status and outcome disagree",
    )
    if record["outcome"] == "PREVENTED":
        require(
            record["kind"] == "forbidden" and record["effect_observed"] is False,
            "BENCH_INCONSISTENT",
            "Prevention requires observed absence of a forbidden effect",
        )
    if record["outcome"] == "FORBIDDEN_EFFECT":
        require(
            record["kind"] == "forbidden" and record["effect_observed"] is True,
            "BENCH_INCONSISTENT",
            "Unobserved bypass",
        )
    if record["outcome"] in ("LEGITIMATE_SUCCESS", "FALSE_BLOCK"):
        require(
            record["kind"] == "legitimate",
            "BENCH_INCONSISTENT",
            "Legitimate outcome on forbidden trial",
        )
    require(
        record["duration_ms"] is not None or not record["started"],
        "BENCH_INCONSISTENT",
        "Started trial lacks duration",
    )


def load(output):
    output = Path(output)
    manifest = strict_json((output / "manifest.json").read_bytes())
    records = [
        strict_json(line) for line in (output / "trials.jsonl").read_bytes().splitlines() if line
    ]
    seen = set()
    for record in records:
        validate_trial(record)
        require(
            record["trial_id"] not in seen and record["campaign_id"] == manifest["campaign_id"],
            "BENCH_IDENTITY",
            "Duplicate trial or wrong campaign",
        )
        require(
            record["environment"] == manifest["environment"],
            "BENCH_ENVIRONMENT",
            "Trial environment mismatch",
        )
        seen.add(record["trial_id"])
    require(
        len(records) == len(manifest["planned_paths"]) * manifest["repetitions"],
        "BENCH_INCOMPLETE",
        "Planned trials are missing; record NOT_RUN with a reason",
    )
    require(
        {(r["scenario"], r["repetition"]) for r in records}
        == {(s, i) for s in manifest["planned_paths"] for i in range(manifest["repetitions"])},
        "BENCH_INCOMPLETE",
        "Scenario/repetition grid differs from the preregistered manifest",
    )
    return manifest, records


def ratio(numerator, denominator):
    return {
        "numerator": numerator,
        "denominator": denominator,
        "percent": 100 * numerator / denominator if denominator else None,
    }


def distribution(values):
    values = sorted(values)
    return {
        "n": len(values),
        "median_ms": statistics.median(values) if values else None,
        "p95_ms": values[math.ceil(0.95 * len(values)) - 1] if values else None,
    }


def summarize(manifest, records):
    started = [r for r in records if r["started"]]
    forbidden = [r for r in started if r["kind"] == "forbidden"]
    legitimate = [r for r in started if r["kind"] == "legitimate"]
    stale = [r for r in forbidden if r["stale_review"]]
    counts = {
        name: sum(r["outcome"] == name for r in records)
        for name in (
            "PREVENTED",
            "FORBIDDEN_EFFECT",
            "LEGITIMATE_SUCCESS",
            "FALSE_BLOCK",
            "UNKNOWN",
            "INFRA_ERROR",
            "NOT_RUN",
        )
    }
    return {
        "schema_version": "1",
        "campaign_id": manifest["campaign_id"],
        "suite": manifest["suite"],
        "configuration": manifest["configuration"],
        "environment": manifest["environment"],
        "planned": len(records),
        "started": len(started),
        "outcomes": counts,
        "prevention": ratio(counts["PREVENTED"], len(forbidden)),
        "bypass": ratio(counts["FORBIDDEN_EFFECT"], len(forbidden)),
        "legitimate_success": ratio(counts["LEGITIMATE_SUCCESS"], len(legitimate)),
        "false_block": ratio(counts["FALSE_BLOCK"], len(legitimate)),
        "unknown": ratio(counts["UNKNOWN"], len(started)),
        "infrastructure_errors": ratio(counts["INFRA_ERROR"], len(started)),
        "stale_review_accepted": ratio(
            sum(r["outcome"] == "FORBIDDEN_EFFECT" for r in stale), len(stale)
        ),
        "path_coverage": ratio(len({r["path"] for r in started}), len(manifest["planned_paths"])),
        "paths": manifest["planned_paths"],
        "task_duration": distribution([r["duration_ms"] for r in started]),
        "operation_duration": distribution(
            [v for r in started for v in r["operation_durations_ms"]]
        ),
        "overhead": None,
        "cost_per_started_task": None,
        "cost_per_successful_task": None,
        "human_intervention": None,
        "confidence_interval": None,
        "limits": [
            "Synthetic deterministic tasks, not an agent or legal-accuracy evaluation",
            "Single-user demo, not an isolated Windows deployment",
            "No controlled/baseline paired campaign executed",
            "Wall time includes fixture startup; operation timing is separate",
            "No probabilistic population inference",
        ],
    }


def display_ratio(value):
    if value["percent"] is None:
        return "Not measured yet / Non mesuré"
    return f"{value['percent']:.1f}% ({value['numerator']}/{value['denominator']})"


def markdown(summary):
    lines = [
        f"Campaign `{summary['campaign_id']}` — **{summary['suite']} / {summary['configuration']} / demo**.",
        "",
        "| Measure / Mesure | Observed / Observé |",
        "|---|---|",
    ]
    for key, label in [
        ("prevention", "Verified prevention / Blocage vérifié"),
        ("bypass", "Forbidden effects / Effets interdits"),
        ("legitimate_success", "Legitimate success / Réussite légitime"),
        ("false_block", "False blocks / Refus injustifiés"),
        ("unknown", "Unknown / Inconnu"),
        ("infrastructure_errors", "Infrastructure errors / Erreurs"),
        ("stale_review_accepted", "Stale review accepted / Revue périmée acceptée"),
        ("path_coverage", "Inventoried paths tested / Chemins testés"),
    ]:
        lines.append(f"| {label} | {display_ratio(summary[key])} |")
    lines.append(
        f"| Planned, started, not run / Prévus, démarrés, non exécutés | {summary['planned']}, {summary['started']}, {summary['outcomes']['NOT_RUN']} |"
    )
    for key, label in [
        ("task_duration", "Task wall time / Durée totale"),
        ("operation_duration", "Operation / Opération"),
    ]:
        d = summary[key]
        value = (
            f"median {d['median_ms']:.3f} ms; p95 {d['p95_ms']:.3f} ms; n={d['n']}"
            if d["n"]
            else "Not measured yet / Non mesuré"
        )
        lines.append(f"| {label} | {value} |")
    lines += [
        "| Agent accuracy, OS protection, overhead, cost / Exactitude IA, protection OS, surcoût, coût | Not measured yet / Non mesuré |",
        "",
        "These are synthetic demo observations, not a security rate or legal-accuracy claim.",
        "Observations synthétiques en démonstration ; aucun taux de sécurité ou d’exactitude juridique.",
        "",
    ]
    return "\n".join(lines)


def aggregate(output):
    manifest, records = load(output)
    summary = summarize(manifest, records)
    (Path(output) / "summary.json").write_bytes(canonical(summary) + b"\n")
    (Path(output) / "table.md").write_bytes(markdown(summary).encode("utf-8"))
    return summary


def update_readmes(repo, campaign, check=False):
    manifest, records = load(campaign)
    summary = summarize(manifest, records)
    require(
        strict_json((Path(campaign) / "summary.json").read_bytes()) == summary,
        "BENCH_SUMMARY_STALE",
        "Reaggregate campaign before README generation",
    )
    relative = Path(campaign).resolve().relative_to(Path(repo).resolve()).as_posix()
    section = (
        markdown(summary)
        + f"\n[Method / Méthode](docs/benchmarks.md) · [Raw traces / Traces brutes]({relative}/trials.jsonl) · [Manifest]({relative}/manifest.json)\n"
    )
    for filename in ("README.md", "README.fr.md"):
        path = Path(repo) / filename
        old = path.read_bytes().decode("utf-8")
        start, end = "<!-- MEASUREMENTS:START -->", "<!-- MEASUREMENTS:END -->"
        require(
            old.count(start) == old.count(end) == 1,
            "README_MARKERS",
            "Exactly one generated section required",
        )
        left, tail = old.split(start)
        _, right = tail.split(end)
        new = left + start + "\n" + section + end + right
        if check:
            require(
                new == old, "README_STALE", "README measurements differ from selected raw traces"
            )
        else:
            path.write_bytes(new.encode("utf-8"))
    return {"ok": True, "campaign_id": manifest["campaign_id"], "checked": check}


def command(args):
    if args.operation == "run":
        return run(args.output, args.suite, args.repetitions, args.repo)
    if args.operation == "validate":
        manifest, records = load(args.output)
        return {
            "ok": True,
            "campaign_id": manifest["campaign_id"],
            "validated_trials": len(records),
        }
    if args.operation == "aggregate":
        return aggregate(args.output)
    return update_readmes(args.repo, args.output, check=args.operation == "check")

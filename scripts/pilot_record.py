"""Private operator observation log; single collector per output file.

Sampling does not replace the authoritative SQLite/event backup. No README export.
"""

import argparse
import os
import time
from pathlib import Path

from skillflow_prompts.benchmarks import validate_trial
from skillflow_prompts.schema_export import validate
from skillflow_prompts.server import Client
from skillflow_prompts.util import canonical, now, sha256, strict_json


def manifest_version(manifest):
    version = manifest.get("version")
    if not isinstance(version, dict) or not all(
        isinstance(version.get(k), str) and version[k]
        for k in ("commit", "code_sha256", "configuration_sha256")
    ):
        raise ValueError("Version must identify installed code and configuration")
    return version


def collect(client, case_id, output, samples, interval, *, manifest):
    if (
        manifest.get("format") != "skillflow-private-pilot-v1"
        or case_id not in manifest["case_ids"]
    ):
        raise ValueError("Use an explicit versioned private pilot manifest and case scope")
    cohort = sha256(canonical(manifest))
    version = manifest_version(manifest)
    last = None
    if output.exists():
        with output.open("rb") as existing:
            for line in existing:
                item = strict_json(line)
                if item.get("case_id") != case_id:
                    raise ValueError("Use one private observation file per case")
                if item.get("cohort_sha256") != cohort:
                    raise ValueError("Never merge different versions or manifests")
                if item["kind"] == "status":
                    validate("flow-status", item["status"])
                    last = (item["status"]["case_id"], item["status"]["last_event_id"])
    output.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with output.open("ab") as stream:
        for index in range(samples):
            started = time.perf_counter_ns()
            response = client.call("flow_status", {"case_id": case_id})
            item = {
                "case_id": case_id,
                "observed_at": now(),
                "cohort_sha256": cohort,
                "version": version,
                "purpose": manifest["purpose"],
                "collection_duration_ms": (time.perf_counter_ns() - started) / 1_000_000,
            }
            if response["ok"]:
                status = response["result"]
                validate("flow-status", status)
                if status["case_id"] != case_id:
                    raise ValueError("Controller returned a different case")
                if status["coverage_profile"] != manifest["coverage_profile"] or sorted(
                    s["contract_hash"] for s in status["skills"]
                ) != sorted(manifest["contract_hashes"]):
                    raise ValueError("Server scope differs from reviewed manifest")
                identity = (case_id, status["last_event_id"])
                if identity != last:
                    item.update(kind="status", status=status)
                    last = identity
            else:
                item.update(kind="collection_error", error=response["error"])
            if "kind" in item:
                stream.write(canonical(item) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
                written += 1
            if index + 1 < samples:
                time.sleep(interval)
    return written


def import_trial(record, evidence, output, manifest):
    """Operator observations, never inferred from an API refusal or a model summary.

    Use the benchmark outcome/resource schema; bind every record to retained raw evidence.
    Evidence hash proves retained bytes, not the truth of an operator's judgment.
    """
    validate_trial(record)
    version = manifest_version(manifest)
    cohort = sha256(canonical(manifest))
    if record["environment"].get("commit") != version["commit"]:
        raise ValueError("Trial belongs to another software version")
    if record["environment"].get("code_sha256") != version["code_sha256"]:
        raise ValueError("Trial runtime hash differs")
    data = evidence.read_bytes()
    if not data or sha256(data) != record["environment"].get("raw_evidence_sha256"):
        raise ValueError("Missing or changed raw evidence")
    item = {
        "kind": "effect_observation",
        "cohort_sha256": cohort,
        "purpose": manifest["purpose"],
        "record": record,
        "raw_evidence_file": str(evidence.resolve()),
        "raw_evidence_sha256": sha256(data),
    }
    if output.exists():
        for line in output.read_bytes().splitlines():
            previous = strict_json(line)
            if previous["cohort_sha256"] != cohort:
                raise ValueError("Never merge different versions or manifests")
            if previous["record"]["trial_id"] == record["trial_id"]:
                if previous != item:
                    raise ValueError("A retained trial cannot be overwritten")
                return False
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("ab") as stream:
        stream.write(canonical(item) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_id")
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--ca")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--trial", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--interval", type=float, default=5)
    args = parser.parse_args()
    manifest = strict_json(args.manifest.read_bytes())
    if args.preflight:
        if manifest.get("purpose") != "preflight":
            parser.error("Preflight must remain a separate non-pilot cohort")
    elif manifest.get("purpose") != "pilot" or manifest.get("acceptance_state") != "ACCEPTED":
        parser.error("Private pilot requires completed acceptance and its approved manifest")
    if args.trial:
        if not args.evidence:
            parser.error("Retained raw evidence is required for an effect observation")
        imported = import_trial(
            strict_json(args.trial.read_bytes()), args.evidence, args.output, manifest
        )
        print(f"Private effect observation appended: {imported}")
        return
    if not 1 <= args.samples <= 10000 or not 0.05 <= args.interval <= 60:
        parser.error("samples: 1..10000; interval: 0.05..60 seconds")
    written = collect(
        Client(args.url, args.token_file, args.ca),
        args.case_id,
        args.output,
        args.samples,
        args.interval,
        manifest=manifest,
    )
    print(f"Private observations appended: {written}. Sampling is not a complete event history.")


if __name__ == "__main__":
    main()

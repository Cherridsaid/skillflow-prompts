"""Assemble reviewed Windows evidence; no inferred successful trials.

Run from the installed protected Python as the authorized operator. Default is a preview.
The explicit --write action requires the previously authorized administrative scope.
Qualification permits are distinct from completed protected acceptance reports.
"""

import argparse
import base64
import json
import platform
import sqlite3
from pathlib import Path

from skillflow import boundary, receipts
from skillflow.client_install import write_atomic
from skillflow.storage import state_path
from skillflow.util import canonical, now, require, sha256, strict_json


def evidence_file(base, item):
    path = (base / item["file"]).resolve()
    require(
        path.is_relative_to(base.resolve()) and path.is_file(),
        "EVIDENCE_PATH",
        "Evidence must be inside the reviewed packet",
    )
    data = path.read_bytes()
    require(sha256(data) == item["sha256"], "EVIDENCE_CHANGED", "Evidence hash mismatch")
    return strict_json(data)


def assemble(root, packet_path, qualification):
    root, packet_path = root.resolve(), packet_path.resolve()
    config = strict_json((root / "config.json").read_bytes())
    packet = strict_json(packet_path.read_bytes())
    require(config["mode"] == "protected", "CONFIG", "Use the exact final protected configuration")
    require(
        packet["operator_sid"] == boundary.windows_identity(),
        "IDENTITY",
        "Only the named operator can assemble acceptance",
    )
    identities = [packet[role + "_sid"] for role in ("writer", "reviewer", "operator", "service")]
    require(
        len(set(identities)) == 4 and all(s.startswith("S-1-") for s in identities),
        "IDENTITY",
        "Four distinct actual identities required",
    )
    for role in ("writer", "reviewer"):
        identity = evidence_file(packet_path.parent, packet["identity_evidence"][role])
        require(
            identity.get("format") == "skillflow-role-identity-v1"
            and identity.get("role") == role
            and identity.get("sid") == packet[role + "_sid"]
            and identity.get("administrator") is False
            and identity.get("process_id", 0) > 0
            and 0 <= now() - identity.get("measured_at", 0) <= 3600,
            "IDENTITY",
            "Fresh non-administrative role process evidence required",
        )
    require(
        packet["configuration_sha256"] == sha256(canonical(config))
        and packet["code_sha256"] == boundary.code_fingerprint()
        and packet["contracts_sha256"] == boundary.contracts_fingerprint(root),
        "EVIDENCE_CHANGED",
        "Evidence belongs to another installed version",
    )
    trials = []
    targets = boundary.probe_targets(root, config)
    for item in packet["trials"]:
        raw = evidence_file(packet_path.parent, item["raw"])
        observation = evidence_file(packet_path.parent, item["observation"])
        matching = [t for t in raw["trials"] if t["path"] == item["path"]]
        require(matching, "INCOMPLETE_EVIDENCE", "No raw trial for this route")
        if item["path"] in targets:
            require(
                targets[item["path"]]
                and sorted(str(Path(t["target"]).resolve()) for t in matching)
                == sorted(targets[item["path"]])
                and sorted(observation["targets"]) == sorted(targets[item["path"]]),
                "EVIDENCE_SCOPE",
                "Observe every actual target for this route",
            )
        require(
            raw["writer_sid"] == packet["writer_sid"]
            and observation["observer_sid"] == packet["operator_sid"],
            "IDENTITY",
            "Writer claims require a separate operator observation",
        )
        require(
            observation["raw_sha256"] == item["raw"]["sha256"]
            and observation["path"] == item["path"],
            "EVIDENCE_CHANGED",
            "Observation must cover this exact trial",
        )
        require(
            0 <= now() - raw["measured_at"] <= 3600
            and 0 <= now() - observation["observed_at"] <= 3600,
            "EVIDENCE_EXPIRED",
            "Re-run stale trials",
        )
        require(
            all(
                trial["started"] is True
                and trial["outcome"] == "PREVENTED"
                and trial["effect_observed"] is False
                for trial in matching
            )
            and observation["effect_observed"] is False
            and observation["verified"] is True,
            "EFFECT_NOT_PREVENTED",
            "Refusal alone does not prove prevention",
        )
        trials.append(
            {
                "path": item["path"],
                "started": True,
                "outcome": "PREVENTED",
                "effect_observed": False,
                "targets": targets.get(item["path"], []),
                "evidence_sha256": sha256(canonical(item)),
            }
        )
    required = (
        boundary.FILESYSTEM_PATHS
        if qualification
        else (
            boundary.EXECUTION_PATHS
            if any(p["coverage"] == "execution_controlled" for p in config["profiles"].values())
            else boundary.ARTIFACT_PATHS
        )
    )
    require(
        len({t["path"] for t in trials}) == len(trials) and required <= {t["path"] for t in trials},
        "INCOMPLETE_EVIDENCE",
        "Missing or duplicate required routes",
    )
    paths = boundary.required_paths(root, config)
    require(
        packet["acl_sha256"] == boundary.acl_fingerprint(paths),
        "EVIDENCE_CHANGED",
        "ACLs changed since observed trials",
    )
    report = {
        "format": "windows-qualification-v1" if qualification else "windows-boundary-v1",
        "mode": "qualification" if qualification else "protected",
        "measured_at": now(),
        "os_version": platform.version(),
        "configuration_sha256": packet["configuration_sha256"],
        "code_sha256": packet["code_sha256"],
        "contracts_sha256": packet["contracts_sha256"],
        "protected_paths": paths,
        "acl_sha256": boundary.acl_fingerprint(paths),
        "trials": trials,
        "evidence_packet_sha256": sha256(packet_path.read_bytes()),
        "identity_evidence": packet["identity_evidence"],
        **{
            role + "_sid": packet[role + "_sid"]
            for role in ("writer", "reviewer", "operator", "service")
        },
    }
    if qualification:
        keys = packet["case_keys"]
        require(
            keys
            and all(k.startswith("qualification-") and k in config["case_bindings"] for k in keys),
            "QUALIFICATION_SCOPE",
            "Explicit synthetic qualification cases required",
        )
        report.update(case_keys=keys, expires_at=now() + 3600)
    else:
        # Read the real ledger and output bytes; never accept a delivery boolean.
        expected = packet["legitimate_delivery"]
        permit_path = root / "qualification-permit.json"
        permit = strict_json(permit_path.read_bytes())
        require(
            sha256(permit_path.read_bytes()) == expected["permit_sha256"]
            and permit["configuration_sha256"] == report["configuration_sha256"]
            and permit["code_sha256"] == report["code_sha256"]
            and permit["contracts_sha256"] == report["contracts_sha256"]
            and permit["acl_sha256"] == report["acl_sha256"],
            "EVIDENCE_CHANGED",
            "Delivery qualification permit differs",
        )
        with sqlite3.connect(state_path(root, config).as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            delivery = db.execute(
                "SELECT * FROM deliveries WHERE id=? AND case_id=?",
                (expected["delivery_id"], expected["case_id"]),
            ).fetchone()
            require(
                delivery is not None and delivery["state"] == "DELIVERED",
                "DELIVERY_NOT_OBSERVED",
                "Qualification delivery not in the real ledger",
            )
            case = db.execute(
                "SELECT case_key FROM cases WHERE id=?", (expected["case_id"],)
            ).fetchone()
            require(
                case["case_key"] in permit["case_keys"],
                "QUALIFICATION_SCOPE",
                "Delivery outside qualified synthetic scopes",
            )
            exports = strict_json(delivery["receipt"])
            require(exports, "DELIVERY_NOT_OBSERVED", "No qualified artifacts")
            require(
                {e["filename"] for e in exports} == set(expected["artifacts"]),
                "ARTIFACT_CHANGED",
                "Expected artifact set differs from delivered set",
            )
            for export in exports:
                path = (
                    root
                    / config["destinations"][delivery["destination"]]
                    / delivery["id"]
                    / export["filename"]
                ).resolve()
                content = path.read_bytes()
                require(
                    strict_json(path.with_name(path.name + ".receipt.json").read_bytes())
                    == export["receipt"],
                    "ARTIFACT_CHANGED",
                    "Disk receipt differs from the real ledger",
                )
                require(
                    sha256(content) == expected["artifacts"][export["filename"]],
                    "ARTIFACT_CHANGED",
                    "Observed bytes differ from reviewed expected output",
                )
                trust = strict_json((root / "trust.json").read_bytes())
                # Separate in-memory trust for inspection only; protected trust stays unchanged.
                payload = strict_json(base64.b64decode(export["receipt"]["payload"], validate=True))
                trust[payload["key_id"]]["profiles"] = ["qualification"]
                receipts.verify(
                    export["receipt"],
                    content,
                    trust,
                    expected_case=expected["case_id"],
                    expected_profile="qualification",
                )
        report.update(legitimate_delivery_observed=True, legitimate_delivery=expected)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--qualification", action="store_true")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    result = assemble(args.root, args.evidence, args.qualification)
    digest = sha256(canonical(result))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("Approval digest: " + digest)
    if args.write:
        # Re-read all evidence and current ACLs before writing; only timestamps may differ.
        current = assemble(args.root, args.evidence, args.qualification)
        require(
            {k: v for k, v in current.items() if k not in ("measured_at", "expires_at")}
            == {k: v for k, v in result.items() if k not in ("measured_at", "expires_at")},
            "EVIDENCE_CHANGED",
            "Evidence changed during review",
        )
        name = "qualification-permit.json" if args.qualification else "boundary-report.json"
        write_atomic(args.root / name, canonical(result))

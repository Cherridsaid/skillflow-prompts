import json

import pytest

from scripts.pilot_record import collect, import_trial
from skillflow.bootstrap import create_demo
from skillflow.demo import DemoSession
from skillflow.harness import live_server
from skillflow.server import Client
from skillflow.util import canonical, sha256


def test_private_collector_resume_keeps_server_budget_and_records_outage(tmp_path):
    root = create_demo(tmp_path / "authority")
    session = DemoSession(root)
    manifest = {
        "format": "skillflow-private-pilot-v1",
        "purpose": "preflight",
        "case_ids": [session.case_id],
        "coverage_profile": "demo",
        "version": {
            "commit": "test-version",
            "code_sha256": "a" * 64,
            "configuration_sha256": "b" * 64,
        },
        "contract_hashes": [s["contract_hash"] for s in session.status()["skills"]],
    }
    output = tmp_path / "private" / "observations.jsonl"
    with live_server(root) as (url, _):
        client = Client(url, root / "operator.token")
        assert collect(client, session.case_id, output, 2, 0.05, manifest=manifest) == 1
        session.submit("SYNTHETIC\nConclusion: observed before restart.")
        session.check()
        assert collect(client, session.case_id, output, 1, 0.05, manifest=manifest) == 1
        assert collect(client, session.case_id, output, 1, 0.05, manifest=manifest) == 0
        changed = manifest | {"version": manifest["version"] | {"commit": "next-version"}}
        with pytest.raises(ValueError, match="Never merge"):
            collect(client, session.case_id, output, 1, 0.05, manifest=changed)
    assert collect(client, session.case_id, output, 1, 0.05, manifest=manifest) == 1
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert rows[1]["status"]["attempts"] == 1
    assert rows[1]["status"]["budget_remaining"] == 5
    assert rows[2]["kind"] == "collection_error"
    assert "status" not in rows[2]
    assert rows[0]["collection_duration_ms"] >= 0
    assert session.status()["budget_remaining"] == 5


def test_operator_effect_log_keeps_unknowns_and_rejects_replay_or_changed_evidence(tmp_path):
    # Synthetic protocol fixture, not a measured prevention or a pilot task.
    evidence = tmp_path / "unknown.json"
    evidence.write_bytes(canonical({"observer": "test-only", "effect": None}))
    manifest = {
        "purpose": "preflight",
        "version": {
            "commit": "test",
            "code_sha256": "a" * 64,
            "configuration_sha256": "b" * 64,
        },
    }
    record = {
        "schema_version": "1",
        "campaign_id": "preflight",
        "trial_id": "unknown-1",
        "task_id": "fixture",
        "scenario": "unknown",
        "suite": "integration",
        "configuration": "controlled",
        "repetition": 0,
        "timestamp": 1,
        "started": True,
        "kind": "forbidden",
        "outcome": "UNKNOWN",
        "reason": "effect not observed",
        "duration_ms": 1,
        "effect_observed": None,
        "stale_review": False,
        "path": "fixture",
        "progress": None,
        "environment": {
            "commit": "test",
            "code_sha256": "a" * 64,
            "raw_evidence_sha256": sha256(evidence.read_bytes()),
        },
        "operation_durations_ms": [],
        "resources": {"tokens": None, "cost": None},
    }
    output = tmp_path / "effects.jsonl"
    for invalid in ({}, {"version": {}}, {"version": None}):
        with pytest.raises(ValueError, match="Version must identify"):
            import_trial(record, evidence, output, invalid)
        assert not output.exists()
    assert import_trial(record, evidence, output, manifest)
    assert not import_trial(record, evidence, output, manifest)
    with pytest.raises(ValueError, match="cannot be overwritten"):
        import_trial(record | {"reason": "rewritten"}, evidence, output, manifest)
    with pytest.raises(ValueError, match="another software version"):
        import_trial(
            record,
            evidence,
            output,
            manifest
            | {
                "version": manifest["version"] | {"commit": "next"},
            },
        )
    evidence.write_bytes(b"different bytes")
    with pytest.raises(ValueError, match="changed raw evidence"):
        import_trial(record, evidence, output, manifest)
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["record"]["outcome"] == "UNKNOWN"
    assert rows[0]["record"]["resources"]["cost"] is None

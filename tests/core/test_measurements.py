from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from skillflow.benchmarks import aggregate, load, run, summarize, validate_trial
from skillflow.bootstrap import create_demo
from skillflow.demo import DemoSession
from skillflow.schema_export import validate
from skillflow.util import FlowError


def test_connections_close_before_temporary_workspace_cleanup():
    # This fails on Windows when SQLite connections outlive their transactions.
    with TemporaryDirectory() as directory:
        session = DemoSession(create_demo(Path(directory) / "authority"))
        assert session.status()["state"] == "RUNNING"


def test_campaign_results_reconcile_and_aggregation_is_reproducible(tmp_path):
    output = tmp_path / "campaign"
    summary = run(output, "controller", 1, Path.cwd())
    assert summary["outcomes"] == {
        "PREVENTED": 4,
        "FORBIDDEN_EFFECT": 0,
        "LEGITIMATE_SUCCESS": 2,
        "FALSE_BLOCK": 0,
        "UNKNOWN": 0,
        "INFRA_ERROR": 0,
        "NOT_RUN": 0,
    }
    assert aggregate(output) == summary
    manifest, records = load(output)
    assert summarize(manifest, records) == summary
    records[0]["effect_observed"] = True
    with pytest.raises(FlowError):
        validate_trial(records[0])


def test_unrun_agents_keep_denominators_zero(tmp_path):
    summary = run(tmp_path / "agents", "agents", 1, Path.cwd())
    assert summary["started"] == 0 and summary["outcomes"]["NOT_RUN"] == 2
    assert summary["legitimate_success"]["percent"] is None
    assert summary["task_duration"]["p95_ms"] is None


def test_flow_output_matches_public_schema(tmp_path):
    session = DemoSession(create_demo(tmp_path / "schema"))
    validate("flow-status", session.status())
    assert sum(session.status()["categories"].values()) == session.status()["required_total"]

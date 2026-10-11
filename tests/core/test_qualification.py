"""Portable protocol checks with explicit mocked Windows identity/ACL observations.

These tests cannot qualify a real Windows installation.
"""

import base64
from types import SimpleNamespace

import pytest

from skillflow import boundary, receipts
from skillflow.bootstrap import create_demo
from skillflow.core import Controller
from skillflow.delivery import drain_outbox
from skillflow.demo import DemoSession
from skillflow.registry import authenticate
from skillflow.util import FlowError, canonical, now, sha256, strict_json


def setup_qualification(tmp_path, monkeypatch):
    root = create_demo(tmp_path / "authority")
    config = strict_json((root / "config.json").read_bytes())
    config["case_bindings"]["qualification-quality"] = config["case_bindings"]["demo-quality"]
    workspace = tmp_path / "workspace"
    skill = workspace / ".agents/skills/inventory/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("Synthetic qualification inventory.")
    config["client_bindings"] = {
        "qualification": {
            "client": "codex",
            "workspace": str(workspace),
            "case_key": "qualification-quality",
            "principals": ["demo-writer"],
            "skills": {
                "inventory": {
                    "contract": "json-quality@1.0.0",
                    "files": [{"path": str(skill), "sha256": sha256(skill.read_bytes())}],
                }
            },
        }
    }
    (root / "config.json").write_bytes(canonical(config))
    session = DemoSession(root, "qualification-quality")
    unrelated = DemoSession(root, "demo-quality")
    unrelated.submit('{"name":"outside permit","quantity":1}', "application/json", "other.json")
    unrelated.check("validate-data")
    pending = unrelated.invoke("writer", "delivery_request", {"destination": "local"})["result"]
    unrelated.invoke("operator", "delivery_commit", {"delivery_id": pending["delivery_id"]})
    config["mode"] = "protected"
    config["tls"] = {"certificate": "tls.crt", "private_key": "tls.key"}
    for profile in config["profiles"].values():
        profile["coverage"] = "artifact_controlled"
    for principal in config["principals"]:
        principal["case_keys"] = list(config["case_bindings"])
    (root / "config.json").write_bytes(canonical(config))
    monkeypatch.setattr(boundary, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(boundary, "windows_identity", lambda: "S-1-5-21-4")
    monkeypatch.setattr(boundary, "acl_fingerprint", lambda _: "a" * 64)
    report = {
        "format": "windows-qualification-v1",
        "mode": "qualification",
        "service_sid": "S-1-5-21-4",
        "writer_sid": "S-1-5-21-1",
        "reviewer_sid": "S-1-5-21-2",
        "operator_sid": "S-1-5-21-3",
        "os_version": boundary.platform.version(),
        "measured_at": now(),
        "expires_at": now() + 3600,
        "configuration_sha256": sha256(canonical(config)),
        "code_sha256": boundary.code_fingerprint(),
        "contracts_sha256": boundary.contracts_fingerprint(root),
        "acl_sha256": "a" * 64,
        "protected_paths": boundary.required_paths(root, config),
        "case_keys": ["qualification-quality"],
        "trials": [
            {
                "path": p,
                "started": True,
                "effect_observed": False,
                "outcome": "PREVENTED",
                "evidence_sha256": "b" * 64,
            }
            for p in boundary.FILESYSTEM_PATHS
        ],
    }
    (root / "qualification-permit.json").write_bytes(canonical(report))
    session.controller = Controller(root, qualification=True)
    session.principals = {
        r: authenticate((root / (r + ".token")).read_text(), config) for r in session.principals
    }
    return session, report, pending


def test_qualification_scopes_receipts_expiry_and_pending_outbox(tmp_path, monkeypatch):
    session, _, pending = setup_qualification(tmp_path, monkeypatch)
    with pytest.raises(FlowError, match="acceptance report"):
        Controller(session.root)  # A preliminary permit can never enable protected startup.
    forbidden = session.invoke(
        "connector", "flow_start", {"case_key": "demo-legal"}, revision=0, expect=False
    )
    assert forbidden["error"]["code"] == "QUALIFICATION_SCOPE"
    session.submit('{"name":"qualification","quantity":1}', "application/json", "item.json")
    session.check("validate-data")
    assert session.status()["coverage_profile"] == "qualification"
    delivery = session.deliver(approve=False)
    path = next((session.root / "outbox").rglob("*item.json.receipt.json"))
    envelope = strict_json(path.read_bytes())
    payload = strict_json(base64.b64decode(envelope["payload"]))
    assert payload["profile"] == "qualification" and payload["delivery_id"] == delivery
    trust = strict_json((session.root / "trust.json").read_bytes())
    trust[payload["key_id"]]["profiles"] = ["qualification", "artifact_controlled"]
    with pytest.raises(FlowError, match="Unexpected profile"):
        receipts.verify(
            envelope,
            path.with_name(path.name.removesuffix(".receipt.json")).read_bytes(),
            trust,
            expected_profile="artifact_controlled",
        )
    assert not list((session.root / "outbox").rglob("*other.json"))
    with session.controller.store.transaction() as db:
        assert (
            db.execute(
                "SELECT state FROM deliveries WHERE id=?", (pending["delivery_id"],)
            ).fetchone()[0]
            == "COMMITTED"
        )
    session.controller.qualification_manifest["expires_at"] = 0
    assert (
        session.invoke("writer", "flow_status", expect=False)["error"]["code"]
        == "QUALIFICATION_SCOPE"
    )
    drain_outbox(session.controller)
    assert not list((session.root / "outbox").rglob("*other.json"))


def test_qualification_rejects_missing_routes_and_changed_version(tmp_path, monkeypatch):
    session, report, _ = setup_qualification(tmp_path, monkeypatch)
    for field, bad in (
        ("code_sha256", "0" * 64),
        ("contracts_sha256", "0" * 64),
        ("trials", []),
        ("expires_at", 0),
        ("case_keys", ["demo-quality"]),
        ("protected_paths", []),
        ("reviewer_sid", report["writer_sid"]),
    ):
        (session.root / "qualification-permit.json").write_bytes(canonical(report | {field: bad}))
        with pytest.raises(FlowError):
            Controller(session.root, qualification=True)


def test_report_assembler_requires_independent_observation_and_exact_files(tmp_path, monkeypatch):
    from scripts.windows.accept_boundary import assemble

    session, permit, _ = setup_qualification(tmp_path, monkeypatch)
    config = session.controller.config
    monkeypatch.setattr(boundary, "windows_identity", lambda: permit["operator_sid"])
    base = tmp_path / "evidence"
    base.mkdir()
    packet = {
        k: permit[k]
        for k in (
            "writer_sid",
            "reviewer_sid",
            "operator_sid",
            "service_sid",
            "configuration_sha256",
            "code_sha256",
            "contracts_sha256",
            "acl_sha256",
            "case_keys",
        )
    }
    packet["trials"] = []
    packet["identity_evidence"] = {}
    for role in ("writer", "reviewer"):
        identity_path = base / (role + "-identity.json")
        identity_path.write_bytes(
            canonical(
                {
                    "format": "skillflow-role-identity-v1",
                    "role": role,
                    "sid": permit[role + "_sid"],
                    "administrator": False,
                    "process_id": 1,
                    "measured_at": now(),
                }
            )
        )
        packet["identity_evidence"][role] = {
            "file": identity_path.name,
            "sha256": sha256(identity_path.read_bytes()),
        }
    targets = boundary.probe_targets(session.root, config)
    for route in sorted(boundary.FILESYSTEM_PATHS):
        raw = {
            "writer_sid": permit["writer_sid"],
            "measured_at": now(),
            "trials": [
                {
                    "path": route,
                    "target": p,
                    "started": True,
                    "outcome": "PREVENTED",
                    "effect_observed": False,
                }
                for p in targets[route]
            ],
        }
        raw_path = base / (route + ".raw.json")
        raw_path.write_bytes(canonical(raw))
        observation = {
            "observer_sid": permit["operator_sid"],
            "observed_at": now(),
            "raw_sha256": sha256(raw_path.read_bytes()),
            "path": route,
            "targets": targets[route],
            "effect_observed": False,
            "verified": True,
        }
        observer_path = base / (route + ".observer.json")
        observer_path.write_bytes(canonical(observation))
        packet["trials"].append(
            {
                "path": route,
                "raw": {"file": raw_path.name, "sha256": sha256(raw_path.read_bytes())},
                "observation": {
                    "file": observer_path.name,
                    "sha256": sha256(observer_path.read_bytes()),
                },
            }
        )
    packet_path = base / "packet.json"
    packet_path.write_bytes(canonical(packet))
    planned = assemble(session.root, packet_path, True)
    assert planned["mode"] == "qualification" and "legitimate_delivery_observed" not in planned
    assert not (session.root / "boundary-report.json").exists()
    reviewer_identity = strict_json(identity_path.read_bytes())
    identity_path.write_bytes(canonical(reviewer_identity | {"sid": permit["writer_sid"]}))
    packet["identity_evidence"]["reviewer"]["sha256"] = sha256(identity_path.read_bytes())
    packet_path.write_bytes(canonical(packet))
    with pytest.raises(FlowError, match="role process evidence"):
        assemble(session.root, packet_path, True)
    identity_path.write_bytes(canonical(reviewer_identity))
    packet["identity_evidence"]["reviewer"]["sha256"] = sha256(identity_path.read_bytes())
    packet_path.write_bytes(canonical(packet))
    item = packet["trials"][0]
    path = base / item["observation"]["file"]
    observation = strict_json(path.read_bytes())
    path.write_bytes(canonical(observation | {"verified": False}))
    with pytest.raises(FlowError, match="hash mismatch"):
        assemble(session.root, packet_path, True)
    item["observation"]["sha256"] = sha256(path.read_bytes())
    packet_path.write_bytes(canonical(packet))
    with pytest.raises(FlowError, match="Refusal alone"):
        assemble(session.root, packet_path, True)
    path.write_bytes(canonical(observation | {"observer_sid": permit["writer_sid"]}))
    item["observation"]["sha256"] = sha256(path.read_bytes())
    packet_path.write_bytes(canonical(packet))
    with pytest.raises(FlowError, match="separate operator"):
        assemble(session.root, packet_path, True)

    # The final assembler must observe the real ledger AND artifact/receipt files.
    path.write_bytes(canonical(observation))
    item["observation"]["sha256"] = sha256(path.read_bytes())
    for route in sorted(boundary.ARTIFACT_PATHS - boundary.FILESYSTEM_PATHS):
        raw = {
            "writer_sid": permit["writer_sid"],
            "measured_at": now(),
            "trials": [
                {"path": route, "started": True, "outcome": "PREVENTED", "effect_observed": False}
            ],
        }
        raw_path = base / (route + ".raw.json")
        raw_path.write_bytes(canonical(raw))
        observer_path = base / (route + ".observer.json")
        observer_path.write_bytes(
            canonical(
                {
                    "observer_sid": permit["operator_sid"],
                    "observed_at": now(),
                    "raw_sha256": sha256(raw_path.read_bytes()),
                    "path": route,
                    "verified": True,
                    "effect_observed": False,
                }
            )
        )
        packet["trials"].append(
            {
                "path": route,
                "raw": {"file": raw_path.name, "sha256": sha256(raw_path.read_bytes())},
                "observation": {
                    "file": observer_path.name,
                    "sha256": sha256(observer_path.read_bytes()),
                },
            }
        )
    session.submit('{"name":"qualified","quantity":2}', "application/json", "item.json")
    session.check("validate-data")
    delivery = session.deliver(approve=False)
    receipt_path = next((session.root / "outbox" / delivery).glob("*.receipt.json"))
    artifact = receipt_path.with_name(receipt_path.name.removesuffix(".receipt.json"))
    packet["legitimate_delivery"] = {
        "case_id": session.case_id,
        "delivery_id": delivery,
        "permit_sha256": sha256((session.root / "qualification-permit.json").read_bytes()),
        "artifacts": {artifact.name: sha256(artifact.read_bytes())},
    }
    packet_path.write_bytes(canonical(packet))
    assert assemble(session.root, packet_path, False)["legitimate_delivery_observed"]
    original = artifact.read_bytes()
    artifact.write_bytes(b"changed after delivery")
    with pytest.raises(FlowError, match="Observed bytes differ"):
        assemble(session.root, packet_path, False)
    artifact.write_bytes(original)
    receipt_path.write_bytes(canonical({"fake": "receipt"}))
    with pytest.raises(FlowError, match="Disk receipt differs"):
        assemble(session.root, packet_path, False)

import pytest

from skillflow import collector
from skillflow.bootstrap import create_demo
from skillflow.core import Controller
from skillflow.demo import DemoSession
from skillflow.models import SourcePolicy
from skillflow.util import FlowError, canonical, now, sha256, strict_json


def source_session(tmp_path):
    root = create_demo(tmp_path / "sources")
    path = root / "contracts" / "synthetic-legal.json"
    document = strict_json(path.read_bytes())
    document["sources"] = [
        {"id": "source", "url": "https://example.org/source.txt", "max_age_seconds": 30}
    ]
    document["steps"][0]["inputs"] = ["artifact", "source:source"]
    document["steps"][0]["allowed_corrections"] = ["artifact_submit", "source_open", "retry"]
    path.write_bytes(canonical(document))
    return DemoSession(root)


def test_source_reservation_survives_process_failure_without_budget_reset(tmp_path, monkeypatch):
    session = source_session(tmp_path)

    def crash(_):
        raise SystemExit("simulated process death during GET")

    monkeypatch.setattr(collector, "fetch", crash)
    request = {
        "action": "source_open",
        "arguments": {
            "case_id": session.case_id,
            "invocation_id": session.invocation_id,
            "source_id": "source",
        },
        "revision": session.status()["revision"],
        "idempotency_key": "source-reservation",
    }
    with pytest.raises(SystemExit):
        session.controller.call(session.principals["writer"], request)
    session.controller = Controller(session.root)
    repeated = session.controller.call(session.principals["writer"], request)
    assert repeated["error"]["code"] == "RESULT_UNKNOWN"
    with session.controller.store.transaction() as db:
        assert (
            db.execute("SELECT count(*) FROM events WHERE kind='source_attempt'").fetchone()[0] == 1
        )


def test_changed_and_expired_sources_invalidate_review_and_delivery(tmp_path, monkeypatch):
    session = source_session(tmp_path)
    value = {"content": b"original synthetic source", "age": 0}

    def fetch(policy):
        content = value["content"]
        return (
            content,
            "text/plain",
            {"fetched_at": now() - value["age"], "url": policy.url, "sha256": sha256(content)},
        )

    monkeypatch.setattr(collector, "fetch", fetch)
    session.invoke(
        "writer", "source_open", {"invocation_id": session.invocation_id, "source_id": "source"}
    )
    session.submit("SYNTHETIC\nConclusion: source-dependent fixture.")
    session.check()
    session.review()
    assert session.status()["validated"] == 2
    value["content"] = b"changed synthetic source"
    session.invoke(
        "writer", "source_open", {"invocation_id": session.invocation_id, "source_id": "source"}
    )
    assert session.status()["validated"] == 0
    assert session.status()["review"]["valid"] == 0
    session.check()
    session.review()
    import skillflow.core as core

    clock = now()
    monkeypatch.setattr(core, "now", lambda: clock + 31)
    assert session.status()["review"]["valid"] == 0
    result = session.invoke("writer", "delivery_request", {"destination": "local"}, expect=False)
    assert result["error"]["code"] == "DELIVERY_PRECONDITIONS"


def test_collector_rejects_private_network_before_connecting(monkeypatch):
    monkeypatch.setattr(
        collector.socket, "getaddrinfo", lambda *_a, **_k: [(2, 1, 6, "", ("127.0.0.1", 443))]
    )
    policy = SourcePolicy(id="source", url="https://example.org/source.txt", max_age_seconds=30)
    with pytest.raises(FlowError) as error:
        collector.fetch(policy)
    assert error.value.code == "SOURCE_NETWORK"


def test_source_update_race_does_not_replace_current_snapshot(tmp_path, monkeypatch):
    session = source_session(tmp_path)

    def fetch(policy):
        session.submit("SYNTHETIC\nConclusion: concurrent artifact change.")
        content = b"delayed synthetic source"
        return (
            content,
            "text/plain",
            {"fetched_at": now(), "url": policy.url, "sha256": sha256(content)},
        )

    monkeypatch.setattr(collector, "fetch", fetch)
    result = session.invoke(
        "writer",
        "source_open",
        {"invocation_id": session.invocation_id, "source_id": "source"},
        expect=False,
    )
    assert result["error"]["code"] == "SOURCE_REVISION_CHANGED"
    with session.controller.store.transaction() as db:
        assert (
            db.execute("SELECT count(*) FROM inputs WHERE name='source:source'").fetchone()[0] == 0
        )

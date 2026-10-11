import base64
import json

import httpx

from skillflow.bootstrap import create_demo
from skillflow.demo import DemoSession
from skillflow.harness import live_server
from skillflow.server import Client
from skillflow.util import canonical, strict_json, uid


def test_separate_mutable_state_survives_http_restart_and_backup(tmp_path):
    from skillflow.cli import main

    root = create_demo(tmp_path / "authority")
    config = strict_json((root / "config.json").read_bytes())
    config["state_directory"] = "../mutable"
    (root / "config.json").write_bytes(canonical(config))
    original = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    fixture = DemoSession(root)
    for _ in range(2):
        with live_server(root) as (url, _):
            status = Client(url, root / "writer.token").call(
                "flow_status", {"case_id": fixture.case_id}
            )
            assert status["ok"]
    backup = tmp_path / "backup.db"
    assert main(["backup", "--root", str(root), "--output", str(backup)]) == 0
    assert backup.is_file()
    assert (tmp_path / "mutable" / "state.db").is_file()
    assert (tmp_path / "mutable" / "daemon.lock").is_file()
    assert original == {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_real_http_authentication_origin_limits_and_fail_closed_on_service_loss(tmp_path):
    root = create_demo(tmp_path / "http")
    fixture = DemoSession(root)
    with live_server(root) as (url, process):
        response = httpx.post(url + "/v1/call", json={}, trust_env=False)
        assert response.status_code == 401
        client = Client(url, root / "writer.token")
        status = client.call("flow_status", {"case_id": fixture.case_id})
        assert status["ok"]
        result = client.call(
            "approval_submit",
            {
                "case_id": fixture.case_id,
                "delivery_id": "invented",
                "snapshot_hash": "0" * 64,
                "decision": "approve",
            },
            status["result"]["revision"],
            uid(),
        )
        assert result["error"]["code"] == "FORBIDDEN"
        headers = {"Authorization": "Bearer " + (root / "writer.token").read_text()}
        response = httpx.post(
            url + "/v1/call",
            headers=headers | {"Origin": "https://evil.example"},
            json={},
            trust_env=False,
        )
        assert response.status_code == 403
        response = httpx.post(
            url + "/v1/call", headers=headers, content=b"x" * 1_600_001, trust_env=False
        )
        assert response.status_code == 413
        response = httpx.post(
            url + "/v1/call",
            headers=headers,
            content=b'{"action":"flow_status","action":"approval_submit"}',
            trust_env=False,
        )
        assert response.status_code == 400
        process.terminate()
        process.wait(timeout=5)
        assert (
            client.call(
                "delivery_request", {"case_id": fixture.case_id, "destination": "local"}, 1, uid()
            )["error"]["code"]
            == "CONTROLLER_UNAVAILABLE"
        )
        assert not (root / "outbox").exists()


def test_real_http_frozen_artifact_import_ignores_writer_paths(tmp_path):
    root = create_demo(tmp_path / "upload")
    fixture = DemoSession(root)
    original = tmp_path / "original.txt"
    original.write_bytes(b"SYNTHETIC\nConclusion: stable copied bytes.")
    content = original.read_bytes()
    with live_server(root) as (url, _):
        client = Client(url, root / "writer.token")
        revision = client.call("flow_status", {"case_id": fixture.case_id})["result"]["revision"]
        result = client.call(
            "artifact_submit",
            {
                "case_id": fixture.case_id,
                "invocation_id": fixture.invocation_id,
                "name": "note.txt",
                "media_type": "text/plain",
                "content_base64": base64.b64encode(content).decode(),
            },
            revision,
            uid(),
        )
        assert result["ok"]
        original.write_bytes(b"changed after import")
        with fixture.controller.store.transaction() as db:
            frozen = db.execute(
                "SELECT content FROM artifacts WHERE id=?", (result["result"]["artifact_id"],)
            ).fetchone()[0]
        assert bytes(frozen) == content


def test_real_http_malformed_reply_is_not_success(tmp_path, monkeypatch):
    token = tmp_path / "writer.token"
    token.write_text("synthetic-token")

    class Response:
        status_code = 200
        content = json.dumps({"role": "operator"}).encode()

    monkeypatch.setattr(httpx.Client, "post", lambda *_a, **_kw: Response())
    assert (
        Client("http://127.0.0.1:8765", token).call("flow_status", {"case_id": "case"})["error"][
            "code"
        ]
        == "CONTROLLER_RESPONSE"
    )

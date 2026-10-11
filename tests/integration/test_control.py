"""Real authority projections and stop effects; fixture credentials, no private pilot claim."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from skillflow_prompts.bootstrap import create_demo
from skillflow_prompts.control import overview, set_state, state
from skillflow_prompts.core import Controller
from skillflow_prompts.delivery import drain_outbox
from skillflow_prompts.demo import DemoSession
from skillflow_prompts.server import create_app
from skillflow_prompts.util import FlowError


def test_persistent_pause_blocks_effects_and_preserves_idempotence(tmp_path):
    create_demo(tmp_path)
    session = DemoSession(tmp_path)
    session.submit("SYNTHETIC\nConclusion: exact bytes")
    session.check()
    session.review()
    demand = session.invoke("writer", "delivery_request", {"destination": "local"})["result"]
    session.invoke(
        "operator",
        "approval_submit",
        {
            "delivery_id": demand["delivery_id"],
            "snapshot_hash": demand["snapshot_hash"],
            "decision": "approve",
        },
    )
    session.invoke("operator", "delivery_commit", {"delivery_id": demand["delivery_id"]})
    request = {"paused": True, "revision": 0, "idempotency_key": "pause"}
    with pytest.raises(FlowError, match="Global operator"):
        set_state(session.controller, session.principals["writer"], request)
    first = set_state(session.controller, session.principals["operator"], request)
    assert set_state(session.controller, session.principals["operator"], request) == first
    session.controller = Controller(tmp_path)
    drain_outbox(session.controller)
    assert not (tmp_path / "outbox").exists()
    with session.controller.store.transaction() as db:
        assert state(db)["paused"]
        assert db.execute("SELECT count(*) FROM events WHERE kind='control'").fetchone()[0] == 1
    set_state(
        session.controller,
        session.principals["operator"],
        {"paused": False, "revision": first["result"]["revision"], "idempotency_key": "resume"},
    )
    drain_outbox(session.controller)
    assert session.status()["state"] == "DELIVERED"
    assert list((tmp_path / "outbox").iterdir())


def test_dashboard_role_origin_socket_and_real_projection(tmp_path):
    create_demo(tmp_path)
    session = DemoSession(tmp_path)
    operator = {"Authorization": "Bearer " + (tmp_path / "operator.token").read_text()}
    writer = {"Authorization": "Bearer " + (tmp_path / "writer.token").read_text()}
    client = TestClient(create_app(tmp_path))
    assert client.get("/dashboard").status_code == 200
    assert client.get("/v1/overview", headers=writer).status_code == 403
    assert (
        client.get("/v1/overview", headers=operator | {"Origin": "http://evil.invalid"}).status_code
        == 403
    )
    session.submit("SYNTHETIC\nConclusion: first revision")
    session.check()
    a = client.get("/v1/overview", headers=operator).json()["result"]["cases"][0]["result"]
    session.submit("SYNTHETIC\nConclusion: changed revision")
    b = client.get("/v1/overview", headers=operator).json()["result"]["cases"][0]["result"]
    c = overview(session.controller, session.principals["operator"])["cases"][0]["result"]
    assert a["validated"] > b["validated"] == 0
    assert b["invalidation_events"] == c["invalidation_events"] == a["invalidation_events"] + 1
    request = {"paused": True, "revision": 0, "idempotency_key": "pause"}
    assert (
        client.post(
            "/v1/control", json=request, headers=writer | {"Origin": "http://testserver"}
        ).status_code
        == 403
    )
    assert client.post(
        "/v1/control", json=request, headers=operator | {"Origin": "http://testserver"}
    ).json()["ok"]
    assert session.check(expect=False)["error"]["code"] == "CONTROL_PAUSED"
    session.invoke("operator", "flow_cancel", {"reason": "operator stop during pause"})
    assert session.status()["state"] == "CANCELLED"
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/v1/live", headers={"Origin": "http://evil.invalid"}),
    ):
        pass
    with client.websocket_connect("/v1/live", headers={"Origin": "http://testserver"}) as socket:
        socket.send_text((tmp_path / "operator.token").read_text())
        data = socket.receive_json()["result"]
        assert data["control"]["paused"] and data["cases"][0]["result"]["state"] == "CANCELLED"
        assert data["measurement"]["false_refusals"] is None


def test_live_server_websocket_transport(tmp_path):
    import json
    import socket
    from urllib.parse import urlsplit

    from wsproto import ConnectionType, WSConnection
    from wsproto.events import AcceptConnection, Request, TextMessage

    from skillflow_prompts.harness import live_server

    create_demo(tmp_path)
    session = DemoSession(tmp_path)
    with live_server(tmp_path) as (url, _):
        address = urlsplit(url)
        protocol = WSConnection(ConnectionType.CLIENT)
        with socket.create_connection((address.hostname, address.port), timeout=3) as stream:
            stream.sendall(
                protocol.send(
                    Request(
                        host=address.netloc,
                        target="/v1/live",
                        extra_headers=[(b"origin", url.encode())],
                    )
                )
            )
            protocol.receive_data(stream.recv(65536))
            assert any(isinstance(e, AcceptConnection) for e in protocol.events())
            stream.sendall(
                protocol.send(TextMessage(data=(tmp_path / "operator.token").read_text()))
            )
            content = ""
            finished = False
            while not finished:
                packet = stream.recv(65536)
                assert packet
                protocol.receive_data(packet)
                for event in protocol.events():
                    if isinstance(event, TextMessage):
                        content += event.data
                        finished = event.message_finished
            result = json.loads(content)
            assert (
                result["ok"]
                and result["result"]["cases"][0]["result"]["case_id"] == session.case_id
            )

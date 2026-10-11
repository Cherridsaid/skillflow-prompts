"""Several requests in one workspace: a new one follows a finished case, a resumed one
finds its case, and a stuck one stays stuck.

Until 2026-09-19 a case key was the hash of client, workspace and profile, and a
finished case was returned for that key forever: one delivery bricked the workspace for
the same skill. The lineage now records each new request after a finished one. What it
must never do is hand a fresh budget to work whose budget is spent: a case that is stuck
is not finished, so it cannot be succeeded.
"""

import base64
import json
from importlib.resources import files

import pytest

from skillflow_prompts.bootstrap import create_demo
from skillflow_prompts.core import Controller
from skillflow_prompts.delivery import drain_outbox
from skillflow_prompts.integration import ClientBridge
from skillflow_prompts.registry import authenticate
from skillflow_prompts.util import canonical, uid

DATA = files("skillflow_prompts").joinpath("data")
NOTE = "SYNTHETIC note, version 1.\nConclusion: synthetic fixture, no legal value."


def install_skill(workspace, name, client="claude"):
    catalog = workspace / (".agents" if client == "codex" else ".claude") / "skills" / name
    catalog.mkdir(parents=True, exist_ok=True)
    (catalog / "SKILL.md").write_bytes(DATA.joinpath(name + ".SKILL.md").read_bytes())
    return catalog / "SKILL.md"


def event(cwd, skill=None, *, client="claude", session="s1", kind=None):
    """The native signal a client emits: a Skill tool call, or a plain session event."""
    kind = kind or ("PreToolUse" if skill else "SessionStart")
    payload = {
        "binding": "auto",
        "client": client,
        "session_id": session,
        "event": kind,
        "event_id": uid(),
        "cwd": str(cwd),
        "prompt": "",
    }
    if skill and kind == "PreToolUse":
        payload.update(tool_name="Skill", tool_input={"skill": skill, "args": "synthetic"})
    return payload


@pytest.fixture
def lab(tmp_path):
    root = create_demo(tmp_path / "authority")
    controller = Controller(root)
    roles = {
        r: authenticate((root / (r + ".token")).read_text(), controller.config)
        for r in ("writer", "connector", "reviewer", "operator")
    }
    return {"root": root, "controller": controller, "bridge": ClientBridge(controller), **roles}


def call(lab, role, action, arguments, *, expect=True):
    from skillflow_prompts.api import READ

    request = {"action": action, "arguments": dict(arguments)}
    if action not in READ:
        status = lab["controller"].call(
            lab["operator"],
            {"action": "flow_status", "arguments": {"case_id": arguments["case_id"]}},
        )
        request["revision"] = status["result"]["revision"]
        request["idempotency_key"] = uid()
    response = lab["controller"].call(lab[role], request)
    if expect and not response["ok"]:
        raise AssertionError(response)
    return response


def cases(lab):
    with lab["controller"].store.transaction() as db:
        return [dict(r) for r in db.execute("SELECT id,case_key,state FROM cases ORDER BY created")]


def events(lab, case_id, kind):
    with lab["controller"].store.transaction() as db:
        rows = db.execute(
            "SELECT data FROM events WHERE case_id=? AND kind=?", (case_id, kind)
        ).fetchall()
    return [json.loads(r[0]) for r in rows]


def drive_to_delivered(lab, case_id):
    """The whole path a real session would take, with a fixture review labelled as such."""
    status = call(lab, "writer", "flow_status", {"case_id": case_id})["result"]
    invocation = next(
        m.split(":")[1] for m in status["delivery"]["missing"] if m.startswith("step:")
    )
    call(
        lab,
        "writer",
        "artifact_submit",
        {
            "case_id": case_id,
            "invocation_id": invocation,
            "name": "note.txt",
            "media_type": "text/plain",
            "content_base64": base64.b64encode(NOTE.encode()).decode(),
        },
    )
    call(
        lab,
        "writer",
        "flow_check",
        {"case_id": case_id, "invocation_id": invocation, "step_id": "draft"},
    )
    review_id = call(lab, "writer", "review_request", {"case_id": case_id})["result"]["review_id"]
    packet = call(lab, "reviewer", "review_read", {"case_id": case_id, "review_id": review_id})[
        "result"
    ]
    raw = canonical(
        {
            "verdict": "JUSTE",
            "reason": "Executed synthetic fixture; no semantic assurance.",
            "references": [o["sha256"] for o in packet["objects"]],
            "obligations": {k: "JUSTE" for k in packet["obligations"]},
        }
    ).decode()
    call(
        lab,
        "reviewer",
        "review_submit",
        {"case_id": case_id, "review_id": review_id, "raw": raw, "provider": "fixture"},
    )
    demand = call(lab, "writer", "delivery_request", {"case_id": case_id, "destination": "local"})[
        "result"
    ]
    call(
        lab,
        "operator",
        "approval_submit",
        {
            "case_id": case_id,
            "delivery_id": demand["delivery_id"],
            "snapshot_hash": demand["snapshot_hash"],
            "decision": "approve",
        },
    )
    call(
        lab,
        "operator",
        "delivery_commit",
        {"case_id": case_id, "delivery_id": demand["delivery_id"]},
    )
    drain_outbox(lab["controller"])
    return call(lab, "writer", "flow_status", {"case_id": case_id})["result"]["state"]


def test_a_new_request_after_a_delivered_case_gets_its_own_case_with_lineage(lab, tmp_path):
    workspace = tmp_path / "ws"
    install_skill(workspace, "guarded-note")
    first = lab["bridge"].handle(lab["writer"], event(workspace, "guarded-note", session="one"))
    assert first["allow"]
    first_id = first["status"]["case_id"]
    assert drive_to_delivered(lab, first_id) == "DELIVERED"

    second = lab["bridge"].handle(lab["writer"], event(workspace, "guarded-note", session="two"))
    assert second["ok"] and second["allow"], second
    second_id = second["status"]["case_id"]
    assert second_id != first_id
    assert second["status"]["state"] == "RUNNING"

    keys = {c["id"]: c["case_key"] for c in cases(lab)}
    assert keys[second_id] == keys[first_id] + "#2", "the lineage is visible in the key"
    assert events(lab, second_id, "case_created")[0]["predecessor"] == first_id
    assert events(lab, first_id, "case_succeeded")[0]["successor"] == second_id
    # The finished case stays consultable, with its proofs.
    old = call(lab, "operator", "flow_status", {"case_id": first_id})["result"]
    assert old["state"] == "DELIVERED"
    # And the successor can go the whole way itself.
    assert drive_to_delivered(lab, second_id) == "DELIVERED"
    third = lab["bridge"].handle(lab["writer"], event(workspace, "guarded-note", session="three"))
    assert {c["case_key"] for c in cases(lab)} >= {
        keys[first_id],
        keys[first_id] + "#2",
        keys[first_id] + "#3",
    }
    assert third["status"]["case_id"] not in (first_id, second_id)


def test_a_new_request_after_a_cancelled_case_also_gets_a_successor(lab, tmp_path):
    workspace = tmp_path / "ws"
    install_skill(workspace, "brief")
    first = lab["bridge"].handle(lab["writer"], event(workspace, "brief", session="one"))
    first_id = first["status"]["case_id"]
    call(lab, "operator", "flow_cancel", {"case_id": first_id, "reason": "synthetic cancel"})

    second = lab["bridge"].handle(lab["writer"], event(workspace, "brief", session="two"))
    assert second["allow"] and second["status"]["case_id"] != first_id
    assert events(lab, second["status"]["case_id"], "case_created")[0]["predecessor"] == first_id


def test_a_resumed_request_finds_its_case_and_keeps_its_counters(lab, tmp_path):
    workspace = tmp_path / "ws"
    install_skill(workspace, "guarded-note")
    first = lab["bridge"].handle(lab["writer"], event(workspace, "guarded-note", session="one"))
    first_id = first["status"]["case_id"]
    status = call(lab, "writer", "flow_status", {"case_id": first_id})["result"]
    invocation = next(
        m.split(":")[1] for m in status["delivery"]["missing"] if m.startswith("step:")
    )
    # Spend one attempt so a reset would be visible.
    call(
        lab,
        "writer",
        "artifact_submit",
        {
            "case_id": first_id,
            "invocation_id": invocation,
            "name": "note.txt",
            "media_type": "text/plain",
            "content_base64": base64.b64encode(b"not synthetic enough").decode(),
        },
    )
    call(
        lab,
        "writer",
        "flow_check",
        {"case_id": first_id, "invocation_id": invocation, "step_id": "draft"},
        expect=False,
    )
    spent = call(lab, "writer", "flow_status", {"case_id": first_id})["result"]["skills"][0]

    # Interrupted, then resumed from a new session, and repeated once more.
    resumed = lab["bridge"].handle(lab["writer"], event(workspace, "guarded-note", session="two"))
    repeated = lab["bridge"].handle(
        lab["writer"], event(workspace, kind="SessionStart", session="two")
    )
    assert resumed["status"]["case_id"] == first_id == repeated["status"]["case_id"]
    assert len(cases(lab)) == 1, "no case was opened for a resumption or a repeated event"
    after = call(lab, "writer", "flow_status", {"case_id": first_id})["result"]["skills"][0]
    assert (after["attempts"], after["corrections"]) == (spent["attempts"], spent["corrections"])


def test_a_stuck_case_is_not_finished_and_cannot_be_succeeded(lab, tmp_path):
    workspace = tmp_path / "ws"
    install_skill(workspace, "guarded-note")
    first = lab["bridge"].handle(lab["writer"], event(workspace, "guarded-note", session="one"))
    first_id = first["status"]["case_id"]
    # Whatever state a live case is in, a new activation lands on it, never beside it.
    again = lab["bridge"].handle(lab["writer"], event(workspace, "guarded-note", session="two"))
    assert again["status"]["case_id"] == first_id and len(cases(lab)) == 1

    # And naming a live case as predecessor is refused outright, budget or not.
    key = cases(lab)[0]["case_key"] + "#2"
    lab["controller"].config["case_bindings"][key] = lab["controller"].config["case_bindings"][
        cases(lab)[0]["case_key"]
    ]
    refused = lab["controller"].call(
        lab["connector"],
        {
            "action": "flow_start",
            "arguments": {"case_key": key, "predecessor": first_id},
            "revision": 0,
            "idempotency_key": uid(),
        },
    )
    assert refused["ok"] is False and refused["error"]["code"] == "PREDECESSOR"
    assert len(cases(lab)) == 1


def test_a_successor_follows_the_highest_index_not_the_row_count(lab, tmp_path):
    """A gap in the sequence must never make a new key land on an existing case."""
    workspace = tmp_path / "ws"
    install_skill(workspace, "brief")
    first = lab["bridge"].handle(lab["writer"], event(workspace, "brief", session="one"))
    first_id = first["status"]["case_id"]
    base = cases(lab)[0]["case_key"]
    profile = lab["controller"].config["case_bindings"][base]
    call(lab, "operator", "flow_cancel", {"case_id": first_id, "reason": "synthetic"})
    # An operator provisioned #3 by hand, skipping #2, and it is finished too.
    with lab["controller"].store.transaction() as db:
        db.execute(
            "INSERT INTO cases(id,case_key,profile,state,created,updated) VALUES(?,?,?,?,1,1)",
            (uid(), base + "#3", profile, "CANCELLED"),
        )
    lab["controller"].config["case_bindings"][base + "#3"] = lab["controller"].config[
        "case_bindings"
    ][base]

    second = lab["bridge"].handle(lab["writer"], event(workspace, "brief", session="two"))
    assert second["allow"]
    keys = {c["id"]: c["case_key"] for c in cases(lab)}
    assert keys[second["status"]["case_id"]] == base + "#4"


def test_a_predecessor_from_another_workspace_is_refused(lab, tmp_path):
    other, mine = tmp_path / "other", tmp_path / "mine"
    install_skill(other, "brief")
    install_skill(mine, "brief")
    foreign = lab["bridge"].handle(lab["writer"], event(other, "brief", session="one"))
    foreign_id = foreign["status"]["case_id"]
    call(lab, "operator", "flow_cancel", {"case_id": foreign_id, "reason": "synthetic"})
    own = lab["bridge"].handle(lab["writer"], event(mine, "brief", session="two"))
    own_key = next(c["case_key"] for c in cases(lab) if c["id"] == own["status"]["case_id"])
    call(lab, "operator", "flow_cancel", {"case_id": own["status"]["case_id"], "reason": "x"})
    lab["controller"].config["case_bindings"][own_key + "#2"] = lab["controller"].config[
        "case_bindings"
    ][own_key]

    refused = lab["controller"].call(
        lab["connector"],
        {
            "action": "flow_start",
            "arguments": {"case_key": own_key + "#2", "predecessor": foreign_id},
            "revision": 0,
            "idempotency_key": uid(),
        },
    )
    assert refused["ok"] is False and refused["error"]["code"] == "PREDECESSOR"

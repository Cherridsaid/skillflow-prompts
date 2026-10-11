"""Automatic activation: a registered skill invoked in any workspace opens or resumes its case.

No prompt mentions SkillFlow. The signal is the native one each client emits: a Skill
tool call for Claude Code, a $skill prompt for Codex. Unregistered skills are observed,
reported UNMANAGED / NOT COVERED, and never become PASS.
"""

import json
import shutil
from importlib.resources import files

import pytest

from skillflow import client_hook
from skillflow.bootstrap import create_demo
from skillflow.core import Controller
from skillflow.harness import live_server
from skillflow.integration import ClientBridge
from skillflow.registry import Principal, authenticate, covers
from skillflow.util import uid

DATA = files("skillflow").joinpath("data")


def install_skill(workspace, name, client="claude", text=None):
    catalog = workspace / (".agents" if client == "codex" else ".claude") / "skills" / name
    catalog.mkdir(parents=True, exist_ok=True)
    payload = text if text is not None else DATA.joinpath(name + ".SKILL.md").read_bytes()
    (catalog / "SKILL.md").write_bytes(payload)
    return catalog / "SKILL.md"


@pytest.fixture
def lab(tmp_path):
    root = create_demo(tmp_path / "authority")
    controller = Controller(root)
    writer = authenticate((root / "writer.token").read_text(), controller.config)
    return {
        "root": root,
        "controller": controller,
        "bridge": ClientBridge(controller),
        "writer": writer,
    }


def event(cwd, skill=None, *, client="claude", session="s1", kind=None, prompt=""):
    kind = kind or ("PreToolUse" if skill else "SessionStart")
    payload = {
        "binding": "auto",
        "client": client,
        "session_id": session,
        "event": kind,
        "event_id": uid(),
        "cwd": str(cwd),
        "prompt": prompt,
    }
    if skill and kind == "PreToolUse":
        payload.update(tool_name="Skill", tool_input={"skill": skill, "args": "synthetic"})
    return payload


def cases(controller):
    with controller.store.transaction() as db:
        return [dict(r) for r in db.execute("SELECT case_key, state FROM cases")]


def test_a_registered_skill_activates_skillflow_in_an_unbound_workspace(lab, tmp_path):
    workspace = tmp_path / "ws-a"
    install_skill(workspace, "brief")
    response = lab["bridge"].handle(lab["writer"], event(workspace, "brief"))
    assert response["ok"] and response["allow"]
    assert response["admission_basis"] == "auto_admission"
    assert response["activated"] == ["brief"]
    status = response["status"]
    assert status["state"] == "RUNNING" and status["required_total"] == 1
    assert [s["contract_id"] for s in status["skills"]] == ["brief"]
    assert all(s["entered"] for s in status["skills"])
    keys = [c["case_key"] for c in cases(lab["controller"])]
    assert len(keys) == 1 and keys[0].startswith("auto:claude:")
    # Later events of the same session reuse the case; nothing is opened twice.
    again = lab["bridge"].handle(lab["writer"], event(workspace, kind="SessionStart"))
    assert again["status"]["case_id"] == status["case_id"]
    assert len(cases(lab["controller"])) == 1


def test_b_second_skill_from_a_new_session_and_workspace_opens_its_own_case(lab, tmp_path):
    first, second = tmp_path / "ws-a", tmp_path / "ws-b"
    install_skill(first, "brief")
    install_skill(second, "memo")
    a = lab["bridge"].handle(lab["writer"], event(first, "brief", session="one"))
    b = lab["bridge"].handle(lab["writer"], event(second, "memo", session="two"))
    assert b["allow"] and b["activated"] == ["memo"]
    assert b["status"]["case_id"] != a["status"]["case_id"]
    assert [s["contract_id"] for s in b["status"]["skills"]] == ["memo"]
    assert len(cases(lab["controller"])) == 2


def test_c_authorized_child_skill_is_instantiated_once_and_counted_once(lab, tmp_path):
    workspace = tmp_path / "ws-c"
    install_skill(workspace, "brief")
    install_skill(workspace, "glossary")
    parent = lab["bridge"].handle(lab["writer"], event(workspace, "brief"))
    assert parent["status"]["required_total"] == 1
    child = lab["bridge"].handle(lab["writer"], event(workspace, "glossary"))
    assert child["allow"] and child["activated"] == ["glossary"]
    status = child["status"]
    assert status["case_id"] == parent["status"]["case_id"]
    contracts = sorted(s["contract_id"] for s in status["skills"])
    assert contracts == ["brief", "glossary"]
    assert status["required_total"] == 2
    assert sum(status["categories"].values()) == 2
    glossary = next(s for s in status["skills"] if s["contract_id"] == "glossary")
    assert glossary["parent_id"] is not None and glossary["entered"]
    # A repeated child activation reuses the slot: no new invocation, no double count.
    repeat = lab["bridge"].handle(lab["writer"], event(workspace, "glossary", session="s2"))
    assert repeat["status"]["required_total"] == 2
    assert len(repeat["status"]["skills"]) == 2
    with lab["controller"].store.transaction() as db:
        assert db.execute("SELECT count(*) FROM invocations").fetchone()[0] == 2


def test_unknown_skill_is_reported_not_managed_and_opens_nothing(lab, tmp_path):
    workspace = tmp_path / "ws-unknown"
    install_skill(workspace, "rogue", text=b"---\nname: rogue\n---\nNot registered.\n")
    response = lab["bridge"].handle(lab["writer"], event(workspace, "rogue"))
    assert response["ok"] and response["allow"] is True
    assert response["status"] is None
    assert response["reason"] == "SKILL_NOT_MANAGED: rogue: skill is not registered"
    assert cases(lab["controller"]) == []
    lab["controller"].config["auto_admission"]["unmanaged"] = "deny"
    denied = lab["bridge"].handle(lab["writer"], event(workspace, "rogue", session="s2"))
    assert denied["allow"] is False and denied["reason"].startswith("SKILL_NOT_MANAGED")
    assert cases(lab["controller"]) == []


def test_unknown_version_of_a_registered_skill_is_not_managed(lab, tmp_path):
    workspace = tmp_path / "ws-version"
    install_skill(workspace, "memo", text=b"---\nname: memo\n---\nEdited locally.\n")
    response = lab["bridge"].handle(lab["writer"], event(workspace, "memo"))
    assert response["allow"] is True and response["status"] is None
    assert response["reason"] == "SKILL_NOT_MANAGED: memo: skill version is not registered"
    assert cases(lab["controller"]) == []


def test_forbidden_sub_skill_is_refused_with_the_allowed_slots(lab, tmp_path):
    workspace = tmp_path / "ws-forbidden"
    install_skill(workspace, "memo")
    install_skill(workspace, "glossary")
    active = lab["bridge"].handle(lab["writer"], event(workspace, "memo"))
    assert active["allow"]
    refused = lab["bridge"].handle(lab["writer"], event(workspace, "glossary"))
    assert refused["ok"] and refused["allow"] is False
    assert refused["reason"].startswith("SUBSKILL_NOT_ALLOWED: glossary")
    assert "declares no child slot" in refused["reason"]
    with lab["controller"].store.transaction() as db:
        assert db.execute("SELECT count(*) FROM invocations").fetchone()[0] == 1
    # The same child under a parent that does declare it is listed by name.
    other = tmp_path / "ws-allowed"
    install_skill(other, "brief")
    install_skill(other, "inventory")
    lab["bridge"].handle(lab["writer"], event(other, "brief"))
    listed = lab["bridge"].handle(lab["writer"], event(other, "inventory"))
    assert listed["allow"] is False
    assert "use one of: glossary -> glossary@1.0.0" in listed["reason"]


def test_resume_after_restart_keeps_the_case_and_its_counters(lab, tmp_path):
    workspace = tmp_path / "ws-resume"
    install_skill(workspace, "brief")
    opened = lab["bridge"].handle(lab["writer"], event(workspace, "brief"))
    case_id = opened["status"]["case_id"]
    invocation = opened["status"]["skills"][0]["invocation_id"]
    # A refused check consumes an attempt that a restart must not forget.
    import base64

    submit = lab["controller"].call(
        lab["writer"],
        {
            "action": "artifact_submit",
            "arguments": {
                "case_id": case_id,
                "invocation_id": invocation,
                "name": "brief.txt",
                "media_type": "text/plain",
                "content_base64": base64.b64encode(b"too short").decode(),
            },
            "revision": opened["status"]["revision"],
            "idempotency_key": uid(),
        },
    )
    assert submit["ok"], submit
    check = lab["controller"].call(
        lab["writer"],
        {
            "action": "flow_check",
            "arguments": {"case_id": case_id, "invocation_id": invocation, "step_id": "draft"},
            "revision": submit["revision"],
            "idempotency_key": uid(),
        },
    )
    assert check["error"]["code"] == "CHECK_FAILED"
    before = lab["controller"].call(
        lab["writer"], {"action": "flow_status", "arguments": {"case_id": case_id}}
    )["result"]
    assert before["attempts"] == 1

    fresh = Controller(lab["root"])
    bridge = ClientBridge(fresh)
    writer = authenticate((lab["root"] / "writer.token").read_text(), fresh.config)
    resumed = bridge.handle(writer, event(workspace, "brief", session="after-restart"))
    assert resumed["allow"] and resumed["status"]["case_id"] == case_id
    assert resumed["status"]["attempts"] == 1 and resumed["status"]["required_total"] == 1
    assert len(cases(fresh)) == 1


def test_codex_signal_is_the_dollar_prompt(lab, tmp_path):
    workspace = tmp_path / "ws-codex"
    install_skill(workspace, "memo", client="codex")
    response = lab["bridge"].handle(
        lab["writer"],
        event(
            workspace,
            client="codex",
            kind="UserPromptSubmit",
            prompt="$memo Rédige un mémo fictif.",
        ),
    )
    assert response["allow"] and response["activated"] == ["memo"]
    assert response["status"]["skills"][0]["contract_id"] == "memo"


def test_scope_prefix_grant_covers_auto_cases_but_literal_keys_stay_exact():
    assert covers(Principal("w", "writer", ("auto:*",)), "auto:claude:abc")
    assert covers(Principal("w", "writer", ("auto:claude:*",)), "auto:claude:abc")
    assert not covers(Principal("w", "writer", ("auto:codex:*",)), "auto:claude:abc")
    assert not covers(Principal("w", "writer", ("another-case",)), "another-case-2")
    assert covers(Principal("w", "writer", ("*",)), "anything")


def test_hook_with_a_global_connection_activates_over_http(tmp_path):
    root = create_demo(tmp_path / "authority")
    workspace = tmp_path / "ws-hook"
    install_skill(workspace, "brief")
    install_skill(workspace, "rogue", text=b"---\nname: rogue\n---\nNot registered.\n")
    with live_server(root) as (url, _):
        config = {
            "connections": [
                {
                    "binding": "auto",
                    "client": "claude",
                    "workspace": "*",
                    "url": url,
                    "token_file": str(root / "writer.token"),
                }
            ],
            "supported_skills": ["brief"],
        }
        raw = {
            "hook_event_name": "PreToolUse",
            "session_id": "hook-session",
            "cwd": str(workspace),
            "tool_name": "Skill",
            "tool_input": {"skill": "brief", "args": "synthetic"},
            "tool_use_id": "t1",
        }
        result, code = client_hook.handle(config, raw)
        assert code == 0
        context = json.loads(result["hookSpecificOutput"]["additionalContext"])
        assert context["case_id"] and context["required_total"] == 1
        assert context["skills"][0]["contract"] == "brief@1.0.0"
        raw.update(tool_input={"skill": "rogue", "args": ""}, tool_use_id="t2")
        result, code = client_hook.handle(config, raw)
        assert code == 0
        unmanaged = json.loads(result["hookSpecificOutput"]["additionalContext"])
        assert unmanaged["SkillFlow"] == "UNMANAGED / NOT COVERED"
        assert unmanaged["reason"].startswith("SKILL_NOT_MANAGED: rogue")


def test_unmanaged_skill_inside_a_controlled_case_is_observed_not_covered(lab, tmp_path):
    workspace = tmp_path / "ws-mixed"
    install_skill(workspace, "brief")
    install_skill(workspace, "rogue", text=b"---\nname: rogue\n---\nNot registered.\n")
    lab["bridge"].handle(lab["writer"], event(workspace, "brief"))
    observed = lab["bridge"].handle(lab["writer"], event(workspace, "rogue"))
    assert observed["allow"] is True and observed["activated"] == []
    assert observed["reason"].startswith("SKILL_NOT_MANAGED: rogue")
    assert observed["reason"].endswith("(observed, not covered)")
    assert observed["status"]["required_total"] == 1
    shutil.rmtree(workspace)


def test_the_batch_event_carries_the_context_pretooluse_cannot(tmp_path):
    """Claude Code 2.1.258 drops PreToolUse output unless it denies the call.

    Observed in a real session: a skill activated its case, the model never saw
    SkillFlow and answered in chat. PostToolBatch is the documented injection point
    and fires before the next model request.
    """
    root = create_demo(tmp_path / "authority")
    workspace = tmp_path / "ws-batch"
    install_skill(workspace, "memo")
    with live_server(root) as (url, _):
        config = {
            "connections": [
                {
                    "binding": "auto",
                    "client": "claude",
                    "workspace": "*",
                    "url": url,
                    "token_file": str(root / "writer.token"),
                }
            ],
            "supported_skills": ["memo"],
        }
        client_hook.handle(
            config,
            {
                "hook_event_name": "PreToolUse",
                "session_id": "batch-session",
                "cwd": str(workspace),
                "tool_name": "Skill",
                "tool_input": {"skill": "memo", "args": "synthetic"},
                "tool_use_id": "t1",
            },
        )
        result, code = client_hook.handle(
            config,
            {
                "hook_event_name": "PostToolBatch",
                "session_id": "batch-session",
                "cwd": str(workspace),
                "tool_calls": [{"tool_name": "Skill", "tool_use_id": "t1", "tool_response": "ok"}],
            },
        )
    assert code == 0
    injected = result["hookSpecificOutput"]
    assert injected["hookEventName"] == "PostToolBatch"
    context = json.loads(injected["additionalContext"])
    assert context["skills"][0]["contract"] == "memo@1.0.0"
    assert context["required_total"] == 1 and context["validated"] == 0
    assert "artifact_submit" in context["workflow"]


def test_a_stopped_controller_does_not_disable_unrelated_sessions(tmp_path, capsys):
    """A global connection reaches every workspace on the machine. When the controller
    is stopped, the hook says nothing is covered instead of denying every tool."""
    config = tmp_path / "connections.json"
    connection = {
        "binding": "auto",
        "client": "claude",
        "workspace": "*",
        "url": "http://127.0.0.1:9",
        "token_file": str(tmp_path / "writer.token"),
    }
    (tmp_path / "writer.token").write_text("synthetic", encoding="utf-8")
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "unrelated",
            "cwd": str(tmp_path),
            "tool_name": "Write",
            "tool_input": {"file_path": "note.txt"},
            "tool_use_id": "t1",
        }
    )

    def run(fail_open):
        config.write_text(
            json.dumps({"connections": [connection], "fail_open": fail_open}), encoding="utf-8"
        )
        import io
        import sys

        sys.stdin = type("Stream", (), {"buffer": io.BytesIO(payload.encode())})()
        try:
            client_hook.main(config)
        finally:
            sys.stdin = sys.__stdin__
        return json.loads(capsys.readouterr().out)

    closed = run(False)
    assert closed["hookSpecificOutput"]["permissionDecision"] == "deny"
    opened = run(True)
    assert "permissionDecision" not in json.dumps(opened)
    context = json.loads(opened["hookSpecificOutput"]["additionalContext"])
    assert context["SkillFlow"] == "UNAVAILABLE / NOT COVERED"


def test_an_active_case_never_constrains_an_unrelated_workspace(lab, tmp_path):
    """A case is bound to one workspace. Another session, another workspace, another
    skill: nothing of the first case applies there, and the first case stays enforced."""
    strict = tmp_path / "ws-legal"
    install_skill(strict, "guarded-note")
    opened = lab["bridge"].handle(lab["writer"], event(strict, "guarded-note", session="s-a"))
    legal_case = opened["status"]["case_id"]
    assert opened["status"]["required_total"] == 2

    # A native write inside the bound workspace is not admitted.
    inside = lab["bridge"].handle(
        lab["writer"],
        {
            **event(strict, session="s-a", kind="PreToolUse"),
            "tool_name": "Write",
            "tool_input": {"file_path": "note.txt"},
        },
    )
    assert inside["allow"] is False

    # Another session, another workspace, another skill.
    other = tmp_path / "ws-memo"
    install_skill(other, "memo")
    elsewhere = lab["bridge"].handle(lab["writer"], event(other, "memo", session="s-b"))
    assert elsewhere["status"]["case_id"] != legal_case
    assert [s["contract_id"] for s in elsewhere["status"]["skills"]] == ["memo"]

    # A workspace with no registered skill at all keeps its native tools.
    free = tmp_path / "ws-free"
    free.mkdir()
    untouched = lab["bridge"].handle(
        lab["writer"],
        {
            **event(free, session="s-c", kind="PreToolUse"),
            "tool_name": "Write",
            "tool_input": {"file_path": "anything.txt"},
        },
    )
    assert untouched["allow"] is True and untouched["status"] is None

    # Back in the first workspace, the session that invoked the skill is still bound.
    again = lab["bridge"].handle(
        lab["writer"],
        {
            **event(strict, session="s-a", kind="PreToolUse"),
            "tool_name": "Write",
            "tool_input": {"file_path": "note.txt"},
        },
    )
    assert again["allow"] is False
    assert again["status"]["case_id"] == legal_case
    # A fresh session in that workspace never invoked the skill: the open case is not its.
    fresh = lab["bridge"].handle(
        lab["writer"],
        {
            **event(strict, session="s-d", kind="PreToolUse"),
            "tool_name": "Write",
            "tool_input": {"file_path": "note.txt"},
        },
    )
    assert fresh["allow"] is True and fresh["status"] is None
    shutil.rmtree(strict)
    shutil.rmtree(other)


def test_a_contract_admits_only_the_tools_it_declares(lab, tmp_path):
    """Real skills need real tools. The blanket refusal of the demonstrations is not a
    policy: the contract names what its own work requires, and nothing else passes."""
    workspace = tmp_path / "ws-tools"
    install_skill(workspace, "memo")
    contract = json.loads(DATA.joinpath("memo.json").read_text(encoding="utf-8"))
    contract["tools"] = ["Read", "Grep"]
    root = lab["root"]
    (root / "contracts" / "memo.json").write_text(json.dumps(contract), encoding="utf-8")
    controller = Controller(root)
    bridge = ClientBridge(controller)
    writer = authenticate((root / "writer.token").read_text(), controller.config)
    bridge.handle(writer, event(workspace, "memo"))

    def native(tool):
        return bridge.handle(
            writer,
            {
                **event(workspace, kind="PreToolUse"),
                "tool_name": tool,
                "tool_input": {"file_path": "note.txt"},
            },
        )

    assert native("Read")["allow"] is True
    assert native("Grep")["allow"] is True
    refused = native("Bash")
    assert refused["allow"] is False
    assert refused["reason"].endswith("admitted here: Grep, Read")
    shutil.rmtree(workspace)


def test_a_user_level_skill_activates_without_a_workspace_copy(lab, tmp_path, monkeypatch):
    """A personal catalogue lives in ~/.claude/skills, not in every workspace."""
    home = tmp_path / "home"
    install_skill(home, "memo")  # writes home/.claude/skills/memo/SKILL.md
    monkeypatch.setattr("skillflow.integration.Path.home", lambda: home)
    workspace = tmp_path / "ws-plain"
    workspace.mkdir()
    response = lab["bridge"].handle(lab["writer"], event(workspace, "memo"))
    assert response["allow"] and response["activated"] == ["memo"]
    assert response["status"]["skills"][0]["contract_id"] == "memo"
    # A workspace copy, when present, takes precedence over the user-level file.
    install_skill(workspace, "memo", text=b"---\nname: memo\n---\nEdited copy.\n")
    other = lab["bridge"].handle(lab["writer"], event(workspace, "memo", session="s2"))
    assert other["reason"].startswith("SKILL_NOT_MANAGED: memo: skill version is not registered")


def test_a_case_left_open_no_longer_restricts_after_the_skill_turn(lab, tmp_path):
    workspace = tmp_path / "ws-release"
    install_skill(workspace, "brief")

    def tool(session, name, kind="PreToolUse"):
        payload = event(workspace, session=session, kind=kind)
        if kind == "PreToolUse":
            payload.update(tool_name=name, tool_input={"command": "synthetic"})
        return lab["bridge"].handle(lab["writer"], payload)

    opened = lab["bridge"].handle(lab["writer"], event(workspace, "brief", session="one"))
    assert opened["status"]["state"] == "RUNNING"
    # During the turn that invoked the skill, the contract restricts that session.
    assert tool("one", "Bash")["allow"] is False
    # Another session in the same workspace is never bound by that open case.
    assert tool("two", "Bash")["allow"] is True
    assert tool("two", "mcp__mempalace__mempalace_search")["allow"] is True
    # The same session's next prompt without the skill releases it; the case stays open.
    tool("one", None, kind="UserPromptSubmit")
    assert tool("one", "Bash")["allow"] is True
    assert tool("one", "Write")["allow"] is True
    assert [c["state"] for c in cases(lab["controller"])] == ["RUNNING"]
    # Invoking the skill again resumes the same case and restores the restriction.
    again = lab["bridge"].handle(lab["writer"], event(workspace, "brief", session="one"))
    assert again["status"]["case_id"] == opened["status"]["case_id"]
    assert tool("one", "Bash")["allow"] is False

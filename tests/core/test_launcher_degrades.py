"""A broken controller must not put every session on this machine in quarantine.

Regression guard for 2026-09-19: a SyntaxError in core.py made the admission child exit
non-zero; the launcher answered `decision: block` for every event, and both Claude Code
and Codex went silent before any skill was named. One defect, total blackout.

The launcher is stdlib-only and imports nothing of ours, so these tests drive it exactly
as a client does: a payload on stdin, a config path, and a python that cannot run.
"""

import json

from skillflow_prompts import hook_launcher


def run_launcher(monkeypatch, capsys, tmp_path, config, event="UserPromptSubmit"):
    config_file = tmp_path / "connections.json"
    config_file.write_text(json.dumps(config), encoding="utf-8")
    payload = json.dumps({"hook_event_name": event, "cwd": str(tmp_path)}).encode()

    class Stdin:
        buffer = type("B", (), {"read": staticmethod(lambda _n: payload)})()

    monkeypatch.setattr(hook_launcher.sys, "stdin", Stdin())
    monkeypatch.setattr(
        hook_launcher.sys,
        "argv",
        [
            "skillflow-hook",
            "--python",
            str(tmp_path / "no-such-python.exe"),
            "--config",
            str(config_file),
        ],
    )
    code = hook_launcher.main()
    return code, capsys.readouterr().out


def test_a_broken_controller_degrades_instead_of_blocking(monkeypatch, capsys, tmp_path):
    # The installation declares fail_open, so a controller that cannot start is reported,
    # not turned into a denial. Nothing is controlled while it is down, and the session
    # is told exactly that rather than being silenced.
    code, out = run_launcher(monkeypatch, capsys, tmp_path, {"fail_open": True})
    answer = json.loads(out)

    assert code == 0
    assert "decision" not in answer, "a failure is not an admission decision"
    context = json.loads(answer["hookSpecificOutput"]["additionalContext"])
    assert context["SkillFlow"] == "UNAVAILABLE / NOT COVERED"
    assert "did not answer" in context["reason"]
    assert answer["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"


def test_without_the_declaration_the_launcher_still_denies(monkeypatch, capsys, tmp_path):
    # No fail_open in the operator's own config: the old, closed behaviour is kept.
    code, out = run_launcher(monkeypatch, capsys, tmp_path, {"connections": []})
    answer = json.loads(out)

    assert code == 0
    assert answer["decision"] == "block"


def test_an_unreadable_config_denies(monkeypatch, capsys, tmp_path):
    # fail_open must be read from the operator's file, never assumed when it is missing.
    config_file = tmp_path / "connections.json"
    config_file.write_text("{ not json", encoding="utf-8")
    payload = json.dumps({"hook_event_name": "PreToolUse"}).encode()

    class Stdin:
        buffer = type("B", (), {"read": staticmethod(lambda _n: payload)})()

    monkeypatch.setattr(hook_launcher.sys, "stdin", Stdin())
    monkeypatch.setattr(
        hook_launcher.sys,
        "argv",
        ["skillflow-hook", "--python", str(tmp_path / "nope.exe"), "--config", str(config_file)],
    )

    assert hook_launcher.main() == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "block"


def test_a_silent_event_degrades_without_inventing_context(monkeypatch, capsys, tmp_path):
    # PostToolUse carries no model-visible context, so the degraded answer is empty and
    # still not a denial.
    code, out = run_launcher(
        monkeypatch, capsys, tmp_path, {"fail_open": True}, event="PostToolUse"
    )

    assert code == 0
    assert json.loads(out) == {}

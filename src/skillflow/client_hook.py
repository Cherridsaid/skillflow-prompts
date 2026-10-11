"""Native Codex/Claude command hook. No privileged credential is given to a hook."""

import json
import re
import sys
from pathlib import Path

from .server import Client
from .util import canonical, sha256, strict_json

EVENTS = {
    "SessionStart",
    "UserPromptSubmit",
    "UserPromptExpansion",
    "PreToolUse",
    "PostToolUse",
    "PostToolBatch",
    "Stop",
    "PreCompact",
    "PostCompact",
    "SessionEnd",
}


# Claude Code 2.1.258 discards hook output on PreToolUse unless it denies the call, and
# injects hookSpecificOutput.additionalContext on these events only. Without PostToolBatch
# a skill that activates a case teaches the model nothing until it trips a refusal.
CONTEXT_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "UserPromptExpansion",
    "PreToolUse",
    "PostToolBatch",
)


def connection(config, cwd):
    """An operator-installed workspace mapping, shared by hooks and stdio."""
    connections = config.get("connections", [config])
    matches = [
        c
        for c in connections
        if c["workspace"] != "*" and Path(c["workspace"]).resolve() == Path(cwd).resolve()
    ]
    if len(matches) > 1:
        raise ValueError("Ambiguous workspace connection")
    if matches:
        return matches[0]
    # A global connection lets a registered skill activate SkillFlow in any workspace.
    universal = [c for c in connections if c["workspace"] == "*"]
    if len(universal) > 1:
        raise ValueError("Ambiguous global connection")
    return universal[0] if universal else None


def unavailable(event, reason):
    """The local controller did not answer. Say so; never pass it off as a decision."""
    context = {
        "SkillFlow": "UNAVAILABLE / NOT COVERED",
        "reason": "the local controller did not answer (" + reason + ")",
    }
    if event not in CONTEXT_EVENTS:
        return {}, 0
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": json.dumps(context, ensure_ascii=False),
        }
    }, 0


def handle(config, raw):
    # Read before `config` is narrowed to the matched connection below.
    fail_open = config.get("fail_open") is True
    event = raw.get("hook_event_name")
    if event not in EVENTS:
        return {}, 0
    cwd = Path(raw.get("cwd", "")).resolve()
    installed = connection(config, cwd)
    if installed is None:
        if config.get("require_bound_workspace") is True:
            return {
                "error": "CLIENT_SCOPE",
                "message": "No operator-bound workflow in this workspace. Provision its approved skill contracts before protected operations.",
            }, 2
        names = set(re.findall(r"(?:^|[\s(])[$/]([A-Za-z0-9_.:-]+)", raw.get("prompt", "")))
        names.add(raw.get("command_name", "").lstrip("/$"))
        if raw.get("tool_name") == "Skill":
            names.add(str(raw.get("tool_input", {}).get("skill", "")))
        if names & set(config.get("supported_skills", [])):
            return {
                "error": "CLIENT_SCOPE",
                "message": "Supported skill requires an operator-bound workspace; no case is installed here",
            }, 2
        return {}, 0
    config = installed
    identity = raw.get("tool_use_id") or raw.get("turn_id") or sha256(canonical(raw))
    envelope = {
        "binding": config["binding"],
        "client": config["client"],
        "session_id": raw["session_id"],
        "event": event,
        "event_id": str(identity),
        "cwd": str(cwd),
        "prompt": raw.get("prompt", ""),
        "command_name": raw.get("command_name", ""),
        "tool_name": raw.get("tool_name", ""),
        "tool_input": raw.get("tool_input", {})
        if isinstance(raw.get("tool_input", {}), dict)
        else {},
    }
    client = Client(config["url"], config["token_file"], config.get("ca"))
    response = client.request("/v1/client-event", envelope)
    if not response.get("ok") or not response.get("allow"):
        code = response.get("error", {}).get("code", "CLIENT_TOOL_DENIED")
        if fail_open and code in ("CONTROLLER_UNAVAILABLE", "CONTROLLER_RESPONSE"):
            # Transport failure, not a decision: nothing is controlled, nothing is denied.
            return unavailable(event, code)
        return {
            "error": code,
            "message": response.get("reason")
            or "SkillFlow unavailable: no protected operation admitted",
        }, 2
    status = response["status"]
    if status is None:
        # Automatic admission observed no managed skill: nothing is covered here.
        reason = response.get("reason")
        if reason and event in CONTEXT_EVENTS:
            return {
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": json.dumps(
                        {"SkillFlow": "UNMANAGED / NOT COVERED", "reason": reason},
                        ensure_ascii=False,
                    ),
                }
            }, 0
        return {}, 0
    context = {
        "SkillFlow": "active operator-bound case; native effects require admission",
        "case_id": status["case_id"],
        "revision": status["revision"],
        "state": status["state"],
        "validated": status["validated"],
        "required_total": status["required_total"],
        "skills": [
            {
                "invocation_id": s["invocation_id"],
                "contract": s["contract_id"] + "@" + s["contract_version"],
            }
            for s in status["skills"]
        ],
        "workflow": "Use skill_read for the approved contract and exact native skill resources. artifact_read retrieves current bytes and collected sources on resume. Use artifact_submit, then required flow_check steps; review_request when required. Read flow_status for current revision. Never claim reviewer or operator authority.",
        "missing": status["delivery"]["missing"],
    }
    reason = response.get("reason") or ""
    if reason.startswith("SKILL_NOT_MANAGED"):
        # A registered workflow is active, but this skill is outside it: say so plainly.
        context["SkillFlow"] = "UNMANAGED / NOT COVERED"
        context["reason"] = reason
    if event in CONTEXT_EVENTS:
        return {
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": json.dumps(context, ensure_ascii=False),
            }
        }, 0
    return {}, 0


def main(config_path):
    raw, config = {}, None
    try:
        config = strict_json(Path(config_path).read_bytes())
        payload = sys.stdin.buffer.read(1_600_001)
        if len(payload) > 1_600_000:
            raise ValueError("Hook request too large")
        raw = strict_json(payload)
        result, code = handle(config, raw)
    except Exception as exc:
        # The launcher cannot enforce clients' hook-startup/timeout behavior.
        # Runtime failures that reach this handler explicitly block blocking events.
        if config is not None and config.get("fail_open") is True:
            # Local non-isolated operation: the hook is not a security boundary, and a
            # stopped controller must not disable every session on this machine. Nothing
            # is controlled while it is unavailable, and the session is told so.
            result, _ = unavailable(raw.get("hook_event_name", ""), type(exc).__name__)
            print(json.dumps(result, ensure_ascii=True))
            return 0
        result, code = (
            {"error": type(exc).__name__, "message": "SkillFlow client admission unavailable"},
            2,
        )
    if code:
        message = (
            result.get("error", "CLIENT_DENIED") + ": " + result.get("message", "Admission denied")
        )
        if raw.get("hook_event_name") == "PreToolUse":
            result = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": message,
                }
            }
        else:
            result = {"decision": "block", "reason": message}
    # Codex 0.154.0 on this Windows installation continued after exit 2.
    # Explicit JSON decisions are separately tested from handler exit codes.
    print(json.dumps(result, ensure_ascii=True))
    return 0

"""Stdlib launcher for native shells. Stdlib only, on purpose.

The tested shell entry has one quoted script argument. The child uses the exact venv.
No launcher can override fail-open behavior if the client never starts it.

Nothing here imports skillflow. The launcher has to keep answering precisely when the
package cannot be imported, which is the case it exists to survive: on 2026-09-19 a
SyntaxError in core.py made the admission child exit non-zero, the launcher denied every
operation, and both Claude Code and Codex went silent before any skill was even named.

So a failure is not a decision. A controller that cannot start is not protecting anything:
its MCP tools are unreachable too, so no artifact can be submitted and nothing can be
delivered while it is down. Denying every unrelated session buys no safety and costs the
machine. When the installation declares `fail_open`, a failure is reported as
UNAVAILABLE / NOT COVERED and the session continues, told plainly that it is uncovered.
Without that declaration the launcher still denies.
"""

import argparse
import json
import subprocess
import sys

# The events whose hook output reaches the model. Mirrors client_hook.CONTEXT_EVENTS,
# duplicated rather than imported so a broken package cannot silence this file.
CONTEXT_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "UserPromptExpansion",
    "PreToolUse",
    "PostToolBatch",
)


def declares_fail_open(config_path):
    """Read the operator's own setting without importing anything of ours."""
    try:
        with open(config_path, encoding="utf-8") as handle:
            return json.load(handle).get("fail_open") is True
    except Exception:
        return False


def degraded(event, reason):
    """Say the controller is down. Never pass that off as an admission decision."""
    if event not in CONTEXT_EVENTS:
        return {}
    context = {
        "SkillFlow": "UNAVAILABLE / NOT COVERED",
        "reason": "the local controller did not answer (" + reason + ")",
    }
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": json.dumps(context, ensure_ascii=False),
        }
    }


def event_name(payload):
    try:
        return json.loads(payload.decode("utf-8")).get("hook_event_name", "")
    except Exception:
        return ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    payload = b""
    try:
        payload = sys.stdin.buffer.read(1_600_001)
        if len(payload) > 1_600_000:
            raise ValueError("Hook input exceeds limit")
        result = subprocess.run(
            [args.python, "-m", "skillflow", "client-hook", "--config", args.config],
            input=payload,
            capture_output=True,
            timeout=20,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("Admission handler failed")
        sys.stdout.buffer.write(result.stdout)
        sys.stderr.buffer.write(result.stderr)
        return result.returncode
    except Exception as exc:
        reason = type(exc).__name__
        if declares_fail_open(args.config):
            print(json.dumps(degraded(event_name(payload), reason), ensure_ascii=False))
            return 0
        print(
            json.dumps(
                {
                    "decision": "block",
                    "reason": "SkillFlow admission launcher failed; operation denied",
                }
            )
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())

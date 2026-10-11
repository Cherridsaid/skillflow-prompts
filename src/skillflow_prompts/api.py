"""Shared strict action schemas. Authorization is always checked by the core."""

from jsonschema import Draft202012Validator

from .util import FlowError

S = {"type": "string", "minLength": 1, "maxLength": 128}
LONG = {"type": "string", "minLength": 1, "maxLength": 1_400_000}
# Exactly one of the two carries the content. A client whose native tools are refused
# cannot encode base64 reliably by hand, so plain UTF-8 text is a first-class input.
BASE64 = LONG | {"description": "Exact bytes, base64 of UTF-8. Use content_text for plain text."}
TEXT = LONG | {"description": "UTF-8 text submitted as is. Do not also send content_base64."}
BOOL = {"type": "boolean"}
READ = {
    "control_status",
    "completion_read",
    "flow_status",
    "coverage_report",
    "skill_read",
    "artifact_read",
    "review_read",
    "delivery_read",
    "receipt_verify",
}
WRITER = {
    "control_status",
    "completion_read",
    "message_submit",
    "flow_status",
    "coverage_report",
    "skill_read",
    "artifact_read",
    "skill_request",
    "source_open",
    "artifact_submit",
    "piece_attach",
    "flow_check",
    "review_request",
    "delegation_request",
    "delivery_request",
    "receipt_verify",
}
CONNECTOR = WRITER | {"flow_start", "session_bind", "skill_enter", "skill_exit", "flow_resume"}
ROLES = {
    "writer": WRITER,
    "connector": CONNECTOR,
    "reviewer": {"flow_status", "review_read", "review_submit"},
    "operator": {
        "control_status",
        "completion_read",
        "flow_status",
        "coverage_report",
        "review_read",
        "delivery_read",
        "approval_submit",
        "delivery_commit",
        "delivery_reconcile",
        "flow_cancel",
        "branch_resolve",
        "receipt_revoke",
        "receipt_verify",
    },
}

# Entries contain required and optional properties. All mutations use the outer
# revision and idempotency key; no action accepts a claimed role or final state.
FIELDS = {
    "control_status": ({"case_id": S}, {}),
    "completion_read": ({"case_id": S}, {}),
    "message_submit": (
        {"case_id": S, "invocation_id": S},
        {"content_base64": BASE64, "content_text": TEXT},
    ),
    # `predecessor`: the terminal case this new request follows in the same workspace,
    # recorded so the lineage is visible. It never transfers budget or state.
    "flow_start": ({"case_key": S}, {"predecessor": S}),
    "flow_status": ({"case_id": S}, {}),
    "coverage_report": ({"case_id": S}, {}),
    "skill_read": ({"case_id": S, "invocation_id": S}, {}),
    "artifact_read": (
        {"case_id": S, "invocation_id": S},
        {"input": S, "encoding": {"enum": ["utf-8", "base64"]}},
    ),
    "skill_request": ({"case_id": S, "invocation_id": S, "slot": S}, {}),
    "delegation_request": ({"case_id": S, "invocation_id": S, "slot": S}, {}),
    "session_bind": ({"case_id": S, "session_id": S}, {}),
    "flow_resume": ({"case_id": S, "session_id": S}, {}),
    "skill_enter": ({"case_id": S, "invocation_id": S, "session_id": S}, {}),
    "skill_exit": ({"case_id": S, "invocation_id": S, "session_id": S}, {}),
    "artifact_submit": (
        {"case_id": S, "invocation_id": S, "name": S, "media_type": S},
        {"content_base64": BASE64, "content_text": TEXT},
    ),
    "source_open": ({"case_id": S, "invocation_id": S, "source_id": S}, {}),
    # A case piece is what the work was done ON: the letter, the internal rule, the
    # statement of facts. It is neither the deliverable nor an official source, so it
    # gets its own role and never overwrites either.
    "piece_attach": (
        {"case_id": S, "invocation_id": S, "label": S, "name": S, "media_type": S},
        {"content_base64": BASE64, "content_text": TEXT},
    ),
    "flow_check": ({"case_id": S, "invocation_id": S, "step_id": S}, {}),
    "review_request": ({"case_id": S}, {}),
    "review_read": ({"case_id": S, "review_id": S}, {}),
    "review_submit": (
        {
            "case_id": S,
            "review_id": S,
            "raw": LONG,
            "provider": {"enum": ["codex", "claude-cli", "human", "fixture"]},
        },
        {"model": S},
    ),
    "delivery_request": ({"case_id": S, "destination": S}, {}),
    "delivery_read": ({"case_id": S, "delivery_id": S}, {}),
    "approval_submit": (
        {
            "case_id": S,
            "delivery_id": S,
            "snapshot_hash": S,
            "decision": {"enum": ["approve", "reject"]},
        },
        {},
    ),
    "delivery_commit": ({"case_id": S, "delivery_id": S}, {}),
    "delivery_reconcile": ({"case_id": S, "delivery_id": S}, {}),
    "flow_cancel": ({"case_id": S, "reason": S}, {}),
    "branch_resolve": (
        {"case_id": S, "invocation_id": S, "branch": S, "value": S, "reason": S},
        {},
    ),
    "receipt_revoke": ({"case_id": S, "delivery_id": S, "reason": S}, {}),
    "receipt_verify": ({"case_id": S, "receipt": {"type": "object"}, "content_base64": LONG}, {}),
}


def schema(action):
    required, optional = FIELDS[action]
    return {
        "type": "object",
        "properties": required | optional,
        "required": list(required),
        "additionalProperties": False,
    }


def field_diagnosis(errors):
    """Name the offending fields without ever echoing their values.

    A refusal that cannot be acted upon makes a caller retry blindly. Field names
    and the failing keyword are structural, never secret; values stay out.
    """
    missing, corrections = [], []
    for error in errors:
        path = ".".join(str(part) for part in error.absolute_path)
        if error.validator == "required":
            name = str(error.message).split("'")[1] if "'" in str(error.message) else ""
            if name and name not in missing:
                missing.append(name)
        elif error.validator == "additionalProperties":
            for name in sorted(set(error.instance) - set(error.schema.get("properties", {}))):
                hint = "remove unexpected argument: " + name
                if hint not in corrections:
                    corrections.append(hint)
        else:
            expected = error.validator_value
            detail = ",".join(map(str, expected)) if isinstance(expected, list) else str(expected)
            field = path or "arguments"
            hint = f"{field}: must satisfy {error.validator} {detail}"
            if hint not in corrections:
                corrections.append(hint[:200])
    return missing, corrections


def validate(action, arguments):
    if action not in FIELDS:
        raise FlowError("UNKNOWN_ACTION", "Unknown action")
    errors = list(Draft202012Validator(schema(action)).iter_errors(arguments))
    if errors:
        # Never echo offending values: they can contain secrets or large blobs.
        # Field names and failing keywords are reported so a caller can correct itself.
        missing, corrections = field_diagnosis(errors)
        raise FlowError(
            "INVALID_ARGUMENT",
            "Arguments do not match the action schema",
            missing=missing,
            corrections=corrections,
        )

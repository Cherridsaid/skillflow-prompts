"""A refusal a caller cannot act on makes it retry blindly.

Observed on 2026-09-16: a real client, asked in plain language, looped twenty-six
turns against `Arguments do not match the action schema` and never learned which
field was wrong. Field names and failing keywords are structural, not secret;
values must still never be echoed.
"""

import base64
import hashlib

import pytest

from skillflow_prompts.bootstrap import create_demo
from skillflow_prompts.demo import DemoSession


@pytest.fixture
def session(tmp_path):
    return DemoSession(create_demo(tmp_path / "authority"))


def call(session, action, arguments, **envelope):
    request = {"action": action, "arguments": arguments, **envelope}
    return session.controller.call(session.principals["writer"], request)


def test_read_action_names_the_unexpected_arguments(session):
    # The MCP schema omits revision/idempotency_key for reads; a caller that adds
    # them anyway must be told exactly which arguments to drop.
    response = call(
        session,
        "skill_read",
        {
            "case_id": session.case_id,
            "invocation_id": session.invocation_id,
            "revision": 3,
            "idempotency_key": "k",
        },
    )
    error = response["error"]
    assert error["code"] == "INVALID_ARGUMENT"
    assert error["corrections"] == [
        "remove unexpected argument: idempotency_key",
        "remove unexpected argument: revision",
    ]


def test_missing_required_arguments_are_listed_by_name(session):
    response = call(
        session,
        "artifact_submit",
        {
            "case_id": session.case_id,
            "invocation_id": session.invocation_id,
            "content_base64": base64.b64encode(b"{}").decode(),
        },
        revision=0,
        idempotency_key="k",
    )
    error = response["error"]
    assert error["code"] == "INVALID_ARGUMENT"
    assert set(error["missing"]) == {"name", "media_type"}


def test_wrong_argument_type_names_the_field_without_echoing_its_value(session):
    secret = "SYNTHETIC-SECRET-VALUE"
    response = call(
        session,
        "delivery_request",
        {"case_id": session.case_id, "destination": [secret]},
        revision=0,
        idempotency_key="k",
    )
    error = response["error"]
    assert error["code"] == "INVALID_ARGUMENT"
    assert any(hint.startswith("destination:") for hint in error["corrections"])
    assert secret not in str(error), "a refusal must never echo argument values"


def test_envelope_type_error_names_the_envelope_field(session):
    response = call(
        session,
        "flow_check",
        {"case_id": session.case_id, "invocation_id": session.invocation_id, "step_id": "draft"},
        revision="3",
        idempotency_key="k",
    )
    error = response["error"]
    assert error["code"] == "INVALID_ARGUMENT"
    assert any(hint.startswith("revision:") for hint in error["corrections"])


def test_a_valid_read_still_succeeds_unchanged(session):
    response = call(
        session,
        "skill_read",
        {"case_id": session.case_id, "invocation_id": session.invocation_id},
    )
    assert response["ok"] and response["result"]["contract"]["instructions"]


def test_unknown_step_lists_the_steps_this_invocation_has(session):
    # Observed twice in real client sessions: the model guessed a composite
    # "<invocation>:<step>" identifier. The refusal now names the real steps.
    response = call(
        session,
        "flow_check",
        {
            "case_id": session.case_id,
            "invocation_id": session.invocation_id,
            "step_id": session.invocation_id + ":draft",
        },
        revision=session.status()["revision"],
        idempotency_key="k",
    )
    error = response["error"]
    assert error["code"] == "UNKNOWN_STEP"
    assert error["corrections"] == ["use one of: draft, judgment"]


def test_unapproved_destination_lists_the_approved_ones(session):
    # Observed in a real session: seven refused deliveries to guessed destinations.
    response = call(
        session,
        "delivery_request",
        {"case_id": session.case_id, "destination": "chat"},
        revision=session.status()["revision"],
        idempotency_key="k",
    )
    error = response["error"]
    assert error["code"] == "DESTINATION"
    assert error["corrections"] == ["use one of: local"]


def test_hand_written_base64_without_padding_is_accepted(session):
    # Observed in a real session: native tools refused, so the client encoded the
    # brief by hand and dropped the padding. The bytes are the same.
    payload = base64.b64encode(b"SYNTHETIC : brief de deux lignes.").decode()
    response = call(
        session,
        "artifact_submit",
        {
            "case_id": session.case_id,
            "invocation_id": session.invocation_id,
            "name": "draft.txt",
            "media_type": "text/plain",
            "content_base64": payload.rstrip("=") + "\n",
        },
        revision=session.status()["revision"],
        idempotency_key="k",
    )
    assert response["ok"], response
    assert (
        response["result"]["sha256"]
        == hashlib.sha256(b"SYNTHETIC : brief de deux lignes.").hexdigest()
    )


def test_plain_text_instead_of_base64_names_the_field(session):
    response = call(
        session,
        "artifact_submit",
        {
            "case_id": session.case_id,
            "invocation_id": session.invocation_id,
            "name": "draft.txt",
            "media_type": "text/plain",
            "content_base64": "SYNTHETIC : texte brut, pas du base64 !",
        },
        revision=session.status()["revision"],
        idempotency_key="k",
    )
    error = response["error"]
    assert error["code"] == "ARTIFACT_ENCODING"
    assert error["corrections"] == [
        "content_base64: send the base64 of the text, not the text itself"
    ]


def test_plain_text_content_is_accepted_without_base64(session):
    # A client whose native tools are refused spends minutes encoding base64 by hand,
    # and gets it wrong. Text is submitted as UTF-8 bytes, exactly as sent.
    text = "SYNTHETIC note, version 1.\nConclusion: synthetic fixture."
    response = call(
        session,
        "artifact_submit",
        {
            "case_id": session.case_id,
            "invocation_id": session.invocation_id,
            "name": "note.txt",
            "media_type": "text/plain",
            "content_text": text,
        },
        revision=session.status()["revision"],
        idempotency_key="k",
    )
    assert response["ok"], response
    assert response["result"]["sha256"] == hashlib.sha256(text.encode()).hexdigest()


def test_both_content_fields_at_once_are_refused_by_name(session):
    response = call(
        session,
        "artifact_submit",
        {
            "case_id": session.case_id,
            "invocation_id": session.invocation_id,
            "name": "note.txt",
            "media_type": "text/plain",
            "content_text": "SYNTHETIC",
            "content_base64": base64.b64encode(b"SYNTHETIC").decode(),
        },
        revision=session.status()["revision"],
        idempotency_key="k",
    )
    error = response["error"]
    assert error["code"] == "ARTIFACT_ENCODING"
    assert error["corrections"] == [
        "send content_text for UTF-8 text, or content_base64 for exact bytes"
    ]


def test_a_markdown_deliverable_keeps_its_expected_name(session):
    # A real skill names its deliverable FULL-AUDIT-REPORT.md; the controlled delivery
    # must be able to produce that exact file instead of granting a native Write.
    response = call(
        session,
        "artifact_submit",
        {
            "case_id": session.case_id,
            "invocation_id": session.invocation_id,
            "name": "FULL-AUDIT-REPORT.md",
            "media_type": "text/markdown",
            "content_text": "SYNTHETIC audit\n\nConclusion: synthetic fixture.",
        },
        revision=session.status()["revision"],
        idempotency_key="k",
    )
    assert response["error"]["code"] == "ARTIFACT_MEDIA", (
        "the demo contract declares text/plain only"
    )
    refused = call(
        session,
        "artifact_submit",
        {
            "case_id": session.case_id,
            "invocation_id": session.invocation_id,
            "name": "report.md",
            "media_type": "text/plain",
            "content_text": "SYNTHETIC",
        },
        revision=session.status()["revision"],
        idempotency_key="k2",
    )
    assert refused["error"]["code"] == "DYNAMIC_CONTENT", ".md stays bound to text/markdown"

"""A reviewer cannot check a citation against a document it never received.

Regression guard for 2026-09-19. Three legal cases were reviewed by Codex and all three
failed the same way: the packet held the draft and nothing the draft quoted. One verdict
said it plainly, that the two compared documents figured neither in the objects nor in
the snapshot inputs, so no citation was controllable.

The cause was not the snapshot. The snapshot carried faithfully what existed, and the
pieces had simply never been stored: a case piece had no role of its own, and inputs are
keyed on one name per role, so attaching a piece through `artifact_submit` would have
replaced the deliverable.

Two things are checked here. A piece can be attached without touching the deliverable,
and a review cannot be opened while an active step declares an input the reviewer would
never receive. Presence is mechanical; whether a piece actually supports the draft stays
the reviewer's judgment.
"""

import base64
import json

import pytest

from skillflow.bootstrap import create_demo
from skillflow.demo import DemoSession

DRAFT = (
    "SYNTHETIC analysis, version 1.\n"
    "Quotes article 3 of the attached rule.\n"
    "Conclusion: synthetic fixture, no legal value."
)
PIECE = "SYNTHETIC internal rule.\nArticle 3. Notice is given three days ahead."


@pytest.fixture
def session(tmp_path):
    """A demo authority whose draft step also demands the piece it reasons about."""
    root = create_demo(tmp_path / "authority")
    path = root / "contracts" / "synthetic-legal.json"
    contract = json.loads(path.read_text(encoding="utf-8"))
    contract["steps"][0]["inputs"] = ["artifact", "piece:internal-rule"]
    contract["steps"][0]["allowed_corrections"] = ["artifact_submit", "piece_attach", "retry"]
    path.write_text(json.dumps(contract), encoding="utf-8")
    return DemoSession(root)


def attach(session, label, text, name="rule.txt", expect=True):
    return session.invoke(
        "writer",
        "piece_attach",
        {
            "invocation_id": session.invocation_id,
            "label": label,
            "name": name,
            "media_type": "text/plain",
            "content_base64": base64.b64encode(text.encode()).decode(),
        },
        expect=expect,
    )


def test_a_review_cannot_open_while_a_declared_piece_is_absent(session):
    session.submit(DRAFT)
    refused = session.invoke("writer", "review_request", expect=False)

    assert refused["error"]["code"] == "REVIEW_PACKET_INCOMPLETE"
    # The refusal names what is expected and absent, rather than only saying no.
    absent = refused["error"]["missing"]
    assert any(item.endswith(":piece:internal-rule") for item in absent)
    # The deliverable is present, so it is not listed: only the real gap is reported.
    assert not any(item.endswith(":artifact") for item in absent)


def test_the_attached_piece_reaches_the_reviewer_and_spares_the_deliverable(session):
    draft = session.submit(DRAFT)["result"]
    attach(session, "internal-rule", PIECE)
    session.check()
    review_id = session.invoke("writer", "review_request")["result"]["review_id"]
    packet = session.invoke("reviewer", "review_read", {"review_id": review_id})["result"]

    names = {obj["name"] for obj in packet["objects"]}
    assert "note.txt" in names, "the deliverable survived the attachment"
    assert "rule.txt" in names, "the reviewer receives the document the draft quotes"
    bodies = {
        obj["name"]: base64.b64decode(obj["content_base64"]).decode() for obj in packet["objects"]
    }
    assert bodies["rule.txt"] == PIECE
    assert bodies["note.txt"] == DRAFT
    # Two distinct roles, two distinct artifacts: the piece never took the draft's place.
    assert draft["artifact_id"] != next(
        obj["id"] for obj in packet["objects"] if obj["name"] == "rule.txt"
    )


def test_a_piece_is_not_passed_off_as_an_official_source(session):
    session.submit(DRAFT)
    attach(session, "internal-rule", PIECE)
    session.check()
    opened = session.invoke("writer", "review_request")
    assert opened["ok"]
    packet = session.invoke(
        "reviewer", "review_read", {"review_id": opened["result"]["review_id"]}
    )["result"]
    entry = next(i for i in packet["snapshot"]["inputs"] if i["name"] == "piece:internal-rule")
    provenance = json.loads(entry["provenance"])

    assert provenance["kind"] == "case_piece"
    assert "url" not in provenance, "a piece carries no source URL and claims no authority"


def test_a_label_must_stay_a_plain_slug(session):
    session.submit(DRAFT)
    refused = attach(session, "../escape", PIECE, expect=False)

    assert refused["error"]["code"] == "PIECE_LABEL"

"""Durable local-file outbox. Commit precedes effects; recovery observes bytes."""

import base64
import os
import stat
from pathlib import Path

from . import receipts
from .util import (
    FlowError,
    artifact_format_supported,
    canonical,
    now,
    require,
    sha256,
    strict_json,
    uid,
)


def human_required(controller, db, case):
    return any(
        strict_json(i[0])["human_approval_required"]
        for i in db.execute("SELECT contract FROM invocations WHERE case_id=?", (case["id"],))
    )


def current_snapshot(controller, db, case, destination):
    """Everything an approval covers: exact inputs, contracts, destination, accepted review.

    The review's provider travels with it, so a receipt says whether the review was a
    simulated fixture or a real engine. A fixture is never presented as a real review.
    """
    review = controller._review_current(db, case)
    return {
        "flow": controller._snapshot(db, case["id"]),
        "destination": destination,
        "review": None
        if review is None
        else {
            "id": review["id"],
            "issuer": review["issuer"],
            "provider": review["provider"],
            "raw_sha256": sha256(review["raw"].encode()),
        },
    }


def approval_current(db, case, delivery):
    """The approval that covers this delivery's exact snapshot.

    The snapshot already names the document bytes, the contracts, every piece and
    source, the destination and the accepted review. That is the whole set a human
    approved, so that is what the approval is bound to. It used to be pinned to the
    case revision as well; the revision moves on every mutating call, refusals and
    session events included, so the approval died during the time the operator spent
    reading. Whether the approved set is still current is re-checked at commit time
    (STALE_DELIVERY, DELIVERY_PRECONDITIONS), which is the check that actually matters.
    """
    return db.execute(
        "SELECT * FROM approvals WHERE delivery_id=? AND case_id=? AND snapshot=? "
        "ORDER BY rowid DESC LIMIT 1",
        (delivery["id"], case["id"], delivery["snapshot"]),
    ).fetchone()


def read_completion(controller, db, case):
    """Return only currently accepted conversation bytes, never the client's stdout."""
    require(case["state"] == "COMPLETED", "COMPLETION_REQUIRED", "Workflow is not completed")
    delivery = db.execute(
        "SELECT * FROM deliveries WHERE case_id=? AND state='COMPLETED' ORDER BY rowid DESC LIMIT 1",
        (case["id"],),
    ).fetchone()
    require(delivery, "COMPLETION_REQUIRED", "No committed completion exists")
    require(
        not db.execute(
            "SELECT 1 FROM revocations WHERE id IN (?,?)", (controller.key_id, delivery["id"])
        ).fetchone(),
        "REVOKED",
        "Completion or signing key revoked",
    )
    require(
        delivery["snapshot"]
        == canonical(current_snapshot(controller, db, case, delivery["destination"])).decode(),
        "STALE_DELIVERY",
        "Completed snapshot is no longer current",
    )
    missing = controller._missing(db, case)
    require(not missing, "DELIVERY_PRECONDITIONS", "Current authority blocks completion", missing)
    messages = []
    for item in strict_json(delivery["snapshot"])["flow"]["inputs"]:
        if item["name"] != "message":
            continue
        row = db.execute("SELECT * FROM artifacts WHERE id=?", (item["id"],)).fetchone()
        require(
            row is not None and sha256(bytes(row["content"])) == item["sha256"],
            "CORRUPT_ARTIFACT",
            "Completed message bytes differ",
        )
        messages.append(
            {
                "invocation_id": item["invocation_id"],
                "sha256": item["sha256"],
                "content": bytes(row["content"]).decode("utf-8"),
            }
        )
    return {
        "case_id": case["id"],
        "state": "COMPLETED",
        "revision": case["revision"],
        "snapshot_hash": sha256(delivery["snapshot"].encode()),
        "messages": messages,
        "completion": strict_json(delivery["receipt"]),
    }


def dispatch(controller, db, case, principal, action, args):
    if action == "delivery_request":
        require(
            args["destination"] in controller.config["destinations"],
            "DESTINATION",
            "Destination is not approved",
            corrections=["use one of: " + ", ".join(sorted(controller.config["destinations"]))],
        )
        missing = controller._missing(db, case)
        require(
            not missing, "DELIVERY_PRECONDITIONS", "Delivery conditions are not satisfied", missing
        )
        snapshot = canonical(current_snapshot(controller, db, case, args["destination"])).decode()
        old = db.execute(
            "SELECT * FROM deliveries WHERE case_id=? AND snapshot=? ORDER BY rowid DESC LIMIT 1",
            (case["id"], snapshot),
        ).fetchone()
        # A human already refused these exact objects. Presenting them again unchanged
        # would only invite a second look at the same thing; something has to change.
        require(
            old is None or old["state"] != "REJECTED",
            "APPROVAL_REJECTED",
            "These exact objects were refused by the operator; change something first",
        )
        if old:
            return {
                "delivery_id": old["id"],
                "snapshot_hash": sha256(snapshot.encode()),
                "existing": True,
            }
        delivery_id = uid()
        db.execute(
            "INSERT INTO deliveries VALUES(?,?,?,?,?,?,?)",
            (delivery_id, case["id"], snapshot, args["destination"], "REQUESTED", None, now()),
        )
        state = "WAITING_APPROVAL" if human_required(controller, db, case) else "READY"
        db.execute("UPDATE cases SET state=? WHERE id=?", (state, case["id"]))
        controller._event(
            db,
            case["id"],
            "delivery_requested",
            principal.id,
            {"delivery_id": delivery_id, "destination": args["destination"]},
        )
        return {
            "delivery_id": delivery_id,
            "snapshot_hash": sha256(snapshot.encode()),
            "state": state,
        }
    delivery = db.execute(
        "SELECT * FROM deliveries WHERE id=? AND case_id=?", (args["delivery_id"], case["id"])
    ).fetchone()
    require(delivery, "DELIVERY_SCOPE", "Delivery is not part of this case")
    snapshot = strict_json(delivery["snapshot"])
    if action == "delivery_read":
        objects = []
        for item in snapshot["flow"]["inputs"]:
            obj = db.execute("SELECT * FROM artifacts WHERE id=?", (item["id"],)).fetchone()
            objects.append(
                {
                    "id": obj["id"],
                    "name": obj["name"],
                    "sha256": obj["sha256"],
                    "text": bytes(obj["content"]).decode("utf-8"),
                    "input": item["name"],
                }
            )
        review = (
            db.execute("SELECT raw FROM reviews WHERE id=?", (snapshot["review"]["id"],)).fetchone()
            if snapshot["review"]
            else None
        )
        result = {
            "delivery_id": delivery["id"],
            "state": delivery["state"],
            "snapshot": snapshot,
            "snapshot_hash": sha256(delivery["snapshot"].encode()),
            "objects": objects,
            "original_review": review[0] if review else None,
            "destination": delivery["destination"],
            "reservation": "Authenticated review is not a guarantee of semantic correctness.",
        }
        if delivery["state"] in ("COMMITTED", "DELIVERED"):
            # Once committed, the operator needs to know where the bytes land. The
            # authority resolves the directory; a client never guesses a path. A read
            # creates nothing and refuses nothing: the path is computed, not prepared,
            # and a file set that cannot be rebuilt is simply not listed.
            root = Path(controller.config["destinations"][delivery["destination"]])
            if not root.is_absolute():
                root = controller.root / root
            result["export_directory"] = str(root.absolute() / delivery["id"])
            try:
                result["files"] = sorted(expected_files(controller, db, delivery))
            except FlowError as exc:
                result["files_unavailable"] = exc.code
        return result
    if action == "delivery_reconcile":
        require(
            delivery["state"] in ("COMMITTED", "DELIVERED"),
            "NOT_COMMITTED",
            "There is no committed effect to reconcile",
        )
        complete = reconcile(controller, db, delivery)
        require(
            complete,
            "RESULT_UNKNOWN",
            "Export is incomplete or differs; inspect the protected destination",
        )
        return {"state": "DELIVERED", "delivery_id": delivery["id"]}
    require(
        delivery["state"] != "REJECTED",
        "APPROVAL_REJECTED",
        "The operator refused these exact objects; nothing can be committed from them",
    )
    require(delivery["state"] == "REQUESTED", "ALREADY_COMMITTED", "Delivery is already committed")
    require(
        delivery["snapshot"]
        == canonical(current_snapshot(controller, db, case, delivery["destination"])).decode(),
        "STALE_DELIVERY",
        "Document, sources, scope or review changed; request delivery again",
    )
    missing = controller._missing(db, case)
    require(not missing, "DELIVERY_PRECONDITIONS", "Current authority blocks delivery", missing)
    if action == "approval_submit":
        require(
            args["snapshot_hash"] == sha256(delivery["snapshot"].encode()),
            "APPROVAL_SNAPSHOT",
            "Approval does not cover the presented objects and destination",
        )
        if args["decision"] == "reject":
            # The delivery itself is closed, so an approval given earlier for the same
            # objects can never be committed after the human changed their mind.
            db.execute("UPDATE deliveries SET state='REJECTED' WHERE id=?", (delivery["id"],))
            db.execute("UPDATE cases SET state='NEEDS_CORRECTION' WHERE id=?", (case["id"],))
            controller._event(
                db, case["id"], "approval_rejected", principal.id, {"delivery_id": delivery["id"]}
            )
            return {"approved": False}
        approval_id = uid()
        db.execute(
            "INSERT INTO approvals VALUES(?,?,?,?,?,?,?)",
            (
                approval_id,
                case["id"],
                delivery["id"],
                delivery["snapshot"],
                # Recorded for the audit trail only; the approval is bound to the snapshot.
                case["revision"] + 1,
                principal.id,
                now(),
            ),
        )
        db.execute("UPDATE cases SET state='READY' WHERE id=?", (case["id"],))
        controller._event(
            db,
            case["id"],
            "approval_received",
            principal.id,
            {
                "approval_id": approval_id,
                "delivery_id": delivery["id"],
                "test": controller.config["mode"] == "demo",
            },
        )
        return {"approved": True, "approval_id": approval_id}
    approval = approval_current(db, case, delivery)
    require(
        not human_required(controller, db, case) or approval is not None,
        "APPROVAL_REQUIRED",
        "Independent human approval for the current revision is required",
        ["current_human_approval"],
    )
    require(
        not db.execute(
            "SELECT 1 FROM revocations WHERE id IN (?,?)", (controller.key_id, delivery["id"])
        ).fetchone(),
        "REVOKED",
        "Signing key or delivery revoked",
    )
    proof_ids = []
    for row in db.execute(
        "SELECT p.id,p.step_id,p.input_hash,s.last_input,s.status FROM proofs p JOIN steps s "
        "ON p.step_id=s.id WHERE p.case_id=?",
        (case["id"],),
    ):
        if row["input_hash"] == row["last_input"] and row["status"] == "VALIDATED":
            proof_ids.append(row["id"])
    exports = []
    for item in snapshot["flow"]["inputs"]:
        if item["name"] != "artifact":
            continue
        obj = db.execute("SELECT * FROM artifacts WHERE id=?", (item["id"],)).fetchone()
        payload = {
            "version": "1",
            "issuer": controller.config["issuer"],
            "key_id": controller.key_id,
            "case_id": case["id"],
            "invocation_id": item["invocation_id"],
            "artifact_sha256": obj["sha256"],
            "contracts": snapshot["flow"]["invocations"],
            "proofs": sorted(proof_ids),
            "review": snapshot["review"],
            "approval": dict(approval) if approval else None,
            "issued_at": now(),
            "profile": "qualification"
            if controller.qualification
            else controller.config["profiles"][case["profile"]]["coverage"],
            "delivery_id": delivery["id"],
            "destination": delivery["destination"],
        }
        envelope = receipts.sign(payload, controller.key)
        exports.append(
            {
                "artifact_id": obj["id"],
                "filename": item["invocation_id"] + "-" + obj["name"],
                "receipt": envelope,
            }
        )
    if not exports:
        require(
            all(
                not strict_json(i[0])["outputs"]
                for i in db.execute(
                    "SELECT contract FROM invocations WHERE case_id=?", (case["id"],)
                )
            ),
            "NO_ARTIFACT",
            "Delivery contains no required artifacts",
        )
        # An outputless workflow closes in the ledger. This is not an artifact receipt
        # and must never be accepted by receipts.verify as protected file delivery.
        completion = receipts.sign(
            {
                "kind": "skillflow-workflow-completion-v1",
                "case_id": case["id"],
                "snapshot_sha256": sha256(delivery["snapshot"].encode()),
                "proofs": sorted(proof_ids),
                "review": snapshot["review"],
                "approval": dict(approval) if approval else None,
                "issued_at": now(),
            },
            controller.key,
        )
        db.execute(
            "UPDATE deliveries SET state='COMPLETED',receipt=? WHERE id=?",
            (canonical(completion).decode(), delivery["id"]),
        )
        db.execute("UPDATE cases SET state='COMPLETED' WHERE id=?", (case["id"],))
        controller._event(
            db,
            case["id"],
            "workflow_completed",
            principal.id,
            {"delivery_id": delivery["id"], "completion": completion},
        )
        return {"state": "COMPLETED", "completion": completion}
    db.execute(
        "UPDATE deliveries SET state='COMMITTED',receipt=? WHERE id=?",
        (canonical(exports).decode(), delivery["id"]),
    )
    db.execute("UPDATE cases SET state='RESULT_UNKNOWN' WHERE id=?", (case["id"],))
    controller._event(
        db, case["id"], "delivery_committed", principal.id, {"delivery_id": delivery["id"]}
    )
    return {
        "delivery_id": delivery["id"],
        "state": "RESULT_UNKNOWN",
        "reason": "Durable outbox committed; export pending observation",
    }


def safe_directory(path):
    path = Path(path).absolute()
    for node in [*reversed(path.parents), path]:
        if node.exists() or node.is_symlink():
            info = node.lstat()
            require(
                not stat.S_ISLNK(info.st_mode)
                and not (getattr(info, "st_file_attributes", 0) & 0x400),
                "UNSAFE_PATH",
                "Symlinks and Windows reparse points are refused",
            )
    path.mkdir(parents=True, exist_ok=True)
    return path


def expected_files(controller, db, delivery):
    result = {}
    if controller.qualification:
        case = db.execute(
            "SELECT case_key FROM cases WHERE id=?", (delivery["case_id"],)
        ).fetchone()
        require(
            now() < controller.qualification_manifest["expires_at"]
            and case["case_key"] in controller.qualification_manifest["case_keys"],
            "QUALIFICATION_SCOPE",
            "Qualification cannot drain another case or an expired permit",
        )
    snapshot = strict_json(delivery["snapshot"])
    review = (
        db.execute(
            "SELECT raw,provider FROM reviews WHERE id=?", (snapshot["review"]["id"],)
        ).fetchone()
        if snapshot["review"]
        else None
    )
    for export in strict_json(delivery["receipt"]):
        if controller.qualification:
            payload = strict_json(base64.b64decode(export["receipt"]["payload"], validate=True))
            require(
                payload["profile"] == "qualification",
                "QUALIFICATION_SCOPE",
                "Qualification cannot export a protected receipt",
            )
        obj = db.execute(
            "SELECT content,sha256,name,media_type FROM artifacts WHERE id=?",
            (export["artifact_id"],),
        ).fetchone()
        require(
            artifact_format_supported(obj["name"], obj["media_type"])
            and artifact_format_supported(export["filename"], obj["media_type"]),
            "DYNAMIC_CONTENT",
            "Previously committed artifact format is no longer supported; no export",
        )
        require(
            sha256(bytes(obj["content"])) == obj["sha256"],
            "STORAGE_CORRUPT",
            "Frozen artifact failed integrity check",
        )
        name = export["filename"]
        result[name] = bytes(obj["content"])
        result[name + ".receipt.json"] = canonical(export["receipt"])
        if review:
            # The file name says what produced the review. A fixture is a simulation and
            # is labelled as such next to the deliverable; it never passes for a real one.
            suffix = {
                "codex": ".revue-codex.md",
                "fixture": ".revue-SIMULEE-fixture.txt",
            }.get(review["provider"], ".review-original.txt")
            result[name + suffix] = review["raw"].encode("utf-8")
    result["manifest.json"] = canonical(
        {
            "delivery_id": delivery["id"],
            "files": {name: sha256(data) for name, data in result.items()},
        }
    )
    return result


def destination(controller, delivery):
    root = Path(controller.config["destinations"][delivery["destination"]])
    if not root.is_absolute():
        root = controller.root / root
    return safe_directory(root) / delivery["id"]


def reconcile(controller, db, delivery):
    files = expected_files(controller, db, delivery)
    target = destination(controller, delivery)
    if not target.exists():
        return False
    safe_directory(target)
    for name, content in files.items():
        path = target / name
        if not path.is_file() or path.is_symlink():
            return False
        info = path.lstat()
        if getattr(info, "st_file_attributes", 0) & 0x400 or path.read_bytes() != content:
            return False
    if delivery["state"] != "DELIVERED":
        db.execute("UPDATE deliveries SET state='DELIVERED' WHERE id=?", (delivery["id"],))
        db.execute(
            "UPDATE cases SET state='DELIVERED',revision=revision+1,updated=? WHERE id=?",
            (now(), delivery["case_id"]),
        )
        controller._event(
            db,
            delivery["case_id"],
            "delivery_observed",
            "delivery-service",
            {"delivery_id": delivery["id"], "effect_verified": True},
        )
    return True


def drain_outbox(controller):
    """Retry only idempotent local O_EXCL exports, never an arbitrary external effect."""
    with controller.store.transaction() as db:
        from .control import state

        if state(db)["paused"]:
            return
        pending = db.execute("SELECT * FROM deliveries WHERE state='COMMITTED'").fetchall()
        for delivery in pending:
            try:
                files = expected_files(controller, db, delivery)
                target = safe_directory(destination(controller, delivery))
                for name, content in files.items():
                    path = target / name
                    if path.exists() or path.is_symlink():
                        require(
                            not path.is_symlink()
                            and not (getattr(path.lstat(), "st_file_attributes", 0) & 0x400),
                            "UNSAFE_PATH",
                            "Existing export is a reparse point",
                        )
                        require(
                            path.read_bytes() == content,
                            "RESULT_UNKNOWN",
                            "Existing export bytes differ; never overwrite",
                        )
                    else:
                        with path.open("xb") as output:
                            output.write(content)
                            output.flush()
                            os.fsync(output.fileno())
                reconcile(controller, db, delivery)
            except (OSError, FlowError):
                # Keep the committed intent; an operator can inspect and reconcile.
                continue

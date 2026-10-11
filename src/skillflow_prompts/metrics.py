"""Coherent current units and cumulative events; no model-provided counters."""

from collections import Counter

from .delivery import approval_current, human_required
from .models import Contract
from .util import now, strict_json


def status(controller, db, case):
    from .integration import skill_file_errors

    binding_errors = skill_file_errors(controller.config, case["case_key"], controller.registry)
    skills = []
    events = db.execute(
        "SELECT * FROM events WHERE case_id=? ORDER BY id", (case["id"],)
    ).fetchall()
    cumulative = Counter(e["kind"] for e in events)
    refusals = Counter(strict_json(e["data"])["code"] for e in events if e["kind"] == "refusal")
    for inv in db.execute("SELECT * FROM invocations WHERE case_id=? ORDER BY id", (case["id"],)):
        contract = Contract.model_validate(strict_json(inv["contract"]))
        contract_current = controller._contract_current(inv, contract)
        unresolved = [
            b
            for b in contract.branches
            if not db.execute(
                "SELECT 1 FROM branches WHERE invocation_id=? AND name=?", (inv["id"], b)
            ).fetchone()
        ]
        counts = Counter(
            {
                "NOT_STARTED": 0,
                "IN_PROGRESS": 0,
                "WAITING": 0,
                "BLOCKED": 0,
                "INVALIDATED": 0,
                "VALIDATED": 0,
            }
        )
        obligations_total = obligations_valid = attempts = corrections = remaining_budget = 0
        steps = []
        for row in db.execute(
            "SELECT * FROM steps WHERE invocation_id=? ORDER BY rowid", (inv["id"],)
        ):
            definition = strict_json(row["definition"])
            if not controller._active(db, inv["id"], definition):
                continue
            valid = not binding_errors and controller._step_valid(db, inv, contract, row)
            current = "VALIDATED" if valid else row["status"]
            if valid:
                pass
            elif row["status"] in ("VALIDATED", "INVALIDATED"):
                current = "INVALIDATED"
            elif (
                any(o["verifier"] == "review" for o in definition["obligations"])
                and case["state"] == "WAITING_REVIEW"
            ):
                current = "WAITING"
            counts[current] += 1
            attempts += row["attempts"]
            corrections += row["corrections"]
            remaining_budget += max(0, definition["correction_budget"] - row["attempts"])
            _, fingerprint, missing = controller._inputs(db, inv, contract, definition)
            satisfied = 0
            for obligation in definition["obligations"]:
                obligations_total += 1
                proof = db.execute(
                    "SELECT result,verifier_hash FROM proofs WHERE step_id=? AND obligation_id=? "
                    "AND input_hash=? ORDER BY rowid DESC LIMIT 1",
                    (row["id"], inv["id"] + ":" + obligation["id"], fingerprint),
                ).fetchone()
                if (
                    not missing
                    and not binding_errors
                    and contract_current
                    and row["status"] != "INVALIDATED"
                    and proof
                    and proof["verifier_hash"] == controller.verifier_hash
                    and strict_json(proof["result"]).get("passed") is True
                ):
                    satisfied += 1
                    obligations_valid += 1
            steps.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "state": current,
                    "attempts": row["attempts"],
                    "budget_remaining": max(0, definition["correction_budget"] - row["attempts"]),
                    "obligations_satisfied": satisfied,
                    "obligations_total": len(definition["obligations"]),
                    "missing": missing,
                }
            )
        total, valid = sum(counts.values()), counts["VALIDATED"]
        skills.append(
            {
                "invocation_id": inv["id"],
                "parent_id": inv["parent_id"],
                "contract_id": contract.id,
                "contract_version": contract.version,
                "contract_hash": inv["contract_hash"],
                "entered": bool(inv["entered"]),
                "required_total": total,
                "validated": valid,
                "remaining": total - valid,
                "categories": dict(counts),
                "obligations_total": obligations_total,
                "obligations_satisfied": obligations_valid,
                "obligations_remaining": obligations_total - obligations_valid,
                "attempts": attempts,
                "corrections": corrections,
                "budget_remaining": remaining_budget,
                "unresolved_branches": unresolved,
                "progress_percent": 100 * valid / total if total and not unresolved else None,
                "progress_reason": "unresolved_scope"
                if unresolved
                else ("zero_denominator" if not total else None),
                "steps": steps,
            }
        )
    total = sum(s["required_total"] for s in skills)
    valid = sum(s["validated"] for s in skills)
    unresolved = any(s["unresolved_branches"] for s in skills)
    review_required = any(
        strict_json(i[0])["review_required"]
        for i in db.execute("SELECT contract FROM invocations WHERE case_id=?", (case["id"],))
    )
    reviews = db.execute("SELECT * FROM reviews WHERE case_id=?", (case["id"],)).fetchall()
    current_review = controller._review_current(db, case)
    pending = db.execute(
        "SELECT * FROM deliveries WHERE case_id=? AND state='REQUESTED' ORDER BY rowid DESC LIMIT 1",
        (case["id"],),
    ).fetchone()
    approval = approval_current(db, case, pending) if pending else None
    approval_required = human_required(controller, db, case)
    missing = controller._missing(db, case)
    if not pending:
        missing.append("delivery_request")
    if approval_required and approval is None:
        missing.append("current_human_approval")
    if case["state"] in ("CANCELLED", "RESULT_UNKNOWN", "DELIVERED", "COMPLETED"):
        missing.append("state:" + case["state"])
    categories = {
        k: sum(s["categories"][k] for s in skills)
        for k in ("NOT_STARTED", "IN_PROGRESS", "WAITING", "BLOCKED", "INVALIDATED", "VALIDATED")
    }
    result = {
        "schema_version": "1",
        "case_id": case["id"],
        "profile": case["profile"],
        "coverage_profile": "qualification"
        if controller.qualification
        else controller.config["profiles"][case["profile"]]["coverage"],
        "state": case["state"],
        "revision": case["revision"],
        "generation": case["generation"],
        "observed_at": now(),
        "last_event_id": events[-1]["id"] if events else 0,
        "projection_lag": 0,
        "required_total": total,
        "validated": valid,
        "remaining": total - valid,
        "categories": categories,
        "progress_percent": 100 * valid / total if total and not unresolved else None,
        "progress_reason": "unresolved_scope"
        if unresolved
        else ("zero_denominator" if not total else None),
        "obligations_total": sum(s["obligations_total"] for s in skills),
        "obligations_satisfied": sum(s["obligations_satisfied"] for s in skills),
        "obligations_remaining": sum(s["obligations_remaining"] for s in skills),
        "attempts": sum(s["attempts"] for s in skills),
        "corrections": sum(s["corrections"] for s in skills),
        "budget_remaining": sum(s["budget_remaining"] for s in skills),
        "invalidated_current": categories["INVALIDATED"],
        "invalidation_events": cumulative["invalidation"],
        "refusal_events": cumulative["refusal"],
        "refusal_reasons": dict(refusals),
        "blocked_current": categories["BLOCKED"],
        "review": {
            "required": int(review_required),
            "received": sum(r["raw"] is not None for r in reviews),
            "valid": int(current_review is not None),
            "pending": sum(r["raw"] is None for r in reviews),
        },
        "approval": {
            "required": int(approval_required),
            "valid": int(approval is not None),
            "pending": int(approval_required and approval is None),
        },
        "resources": {
            "elapsed_seconds": max(0, now() - case["created"]),
            "active_seconds": None,
            "waiting_seconds": None,
            "model_calls": None,
            "tokens": None,
            "cost": None,
            "unmeasured_reason": "No model/provider usage or operator timing telemetry supplied",
        },
        "delivery": {"authorized": not missing, "missing": missing},
        "skills": skills,
        "limits": [
            "Progress is not correctness or time remaining",
            "Server refusals are not verified prevented OS effects",
            "Chat output is outside the delivery gate",
            "Demo approvals/reviews are fixtures",
        ],
    }
    for skill in skills:
        invocation = skill["invocation_id"]
        relevant = []
        for event in events:
            data = strict_json(event["data"])
            if (
                data.get("invocation_id") == invocation
                or data.get("step_id", "").startswith(invocation + ":")
                or any(s.startswith(invocation + ":") for s in data.get("steps", []))
            ):
                relevant.append(event)
        reasons = Counter(
            strict_json(e["data"])["code"] for e in relevant if e["kind"] == "refusal"
        )
        skill.update(
            invalidated_current=skill["categories"]["INVALIDATED"],
            invalidation_events=sum(e["kind"] == "invalidation" for e in relevant),
            refusal_events=sum(reasons.values()),
            refusal_reasons=dict(reasons),
            blocked_current=skill["categories"]["BLOCKED"],
            review=result["review"] | {"shared_case_review": True},
            approval=result["approval"] | {"shared_case_approval": True},
            resources=result["resources"] | {"scope": "case_shared_unallocated"},
            delivery=result["delivery"],
        )
    from .schema_export import validate

    validate("flow-status", result)
    return result

"""Publish the same schemas used to validate public results."""

from pathlib import Path

from jsonschema import Draft202012Validator

from .models import Call, Contract, ReviewVerdict
from .util import canonical, require


def obj(properties, required=None):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": False,
    }


N = {"type": "integer", "minimum": 0}
T = {"type": "string"}
NULL_N = {"type": ["number", "null"], "minimum": 0}
B = {"type": "boolean"}
ARRAY_T = {"type": "array", "items": T}
ANY_OBJ = {"type": "object"}
CATEGORIES = obj(
    {k: N for k in ["NOT_STARTED", "IN_PROGRESS", "WAITING", "BLOCKED", "INVALIDATED", "VALIDATED"]}
)


def formats():
    skill = obj(
        {
            "invocation_id": T,
            "parent_id": {"type": ["string", "null"]},
            "contract_id": T,
            "contract_version": T,
            "contract_hash": T,
            "entered": B,
            "required_total": N,
            "validated": N,
            "remaining": N,
            "categories": CATEGORIES,
            "obligations_total": N,
            "obligations_satisfied": N,
            "obligations_remaining": N,
            "attempts": N,
            "corrections": N,
            "budget_remaining": N,
            "unresolved_branches": ARRAY_T,
            "progress_percent": NULL_N,
            "progress_reason": {"type": ["string", "null"]},
            "steps": {"type": "array", "items": ANY_OBJ},
        }
    )
    skill["properties"].update(
        {
            k: N
            for k in [
                "invalidated_current",
                "invalidation_events",
                "refusal_events",
                "blocked_current",
            ]
        }
    )
    skill["properties"].update(
        {k: ANY_OBJ for k in ["refusal_reasons", "review", "approval", "resources", "delivery"]}
    )
    skill["required"] = list(skill["properties"])
    status = obj(
        {
            "schema_version": {"const": "1"},
            "case_id": T,
            "profile": T,
            "coverage_profile": T,
            "state": {
                "enum": [
                    "CREATED",
                    "RUNNING",
                    "WAITING_REVIEW",
                    "NEEDS_CORRECTION",
                    "WAITING_APPROVAL",
                    "READY",
                    "DELIVERED",
                    "COMPLETED",
                    "BLOCKED",
                    "CANCELLED",
                    "RESULT_UNKNOWN",
                ]
            },
            **{
                k: N
                for k in [
                    "revision",
                    "generation",
                    "observed_at",
                    "last_event_id",
                    "projection_lag",
                    "required_total",
                    "validated",
                    "remaining",
                    "obligations_total",
                    "obligations_satisfied",
                    "obligations_remaining",
                    "attempts",
                    "corrections",
                    "budget_remaining",
                    "invalidated_current",
                    "invalidation_events",
                    "refusal_events",
                    "blocked_current",
                ]
            },
            "categories": CATEGORIES,
            "progress_percent": NULL_N,
            "progress_reason": {"type": ["string", "null"]},
            "refusal_reasons": {"type": "object", "additionalProperties": N},
            "review": ANY_OBJ,
            "approval": ANY_OBJ,
            "resources": ANY_OBJ,
            "delivery": obj({"authorized": B, "missing": ARRAY_T}),
            "skills": {"type": "array", "items": skill},
            "limits": ARRAY_T,
        }
    )
    trial = obj(
        {
            "schema_version": {"const": "1"},
            "campaign_id": T,
            "trial_id": T,
            "task_id": T,
            "scenario": T,
            "suite": {"enum": ["controller", "integration", "agents"]},
            "configuration": {"enum": ["controlled", "baseline"]},
            "repetition": N,
            "timestamp": N,
            "started": B,
            "kind": {"enum": ["forbidden", "legitimate"]},
            "outcome": {
                "enum": [
                    "PREVENTED",
                    "FORBIDDEN_EFFECT",
                    "LEGITIMATE_SUCCESS",
                    "FALSE_BLOCK",
                    "UNKNOWN",
                    "INFRA_ERROR",
                    "NOT_RUN",
                ]
            },
            "reason": T,
            "duration_ms": NULL_N,
            "effect_observed": {"type": ["boolean", "null"]},
            "stale_review": B,
            "path": T,
            "progress": {"type": ["object", "null"]},
            "environment": ANY_OBJ,
            "operation_durations_ms": {"type": "array", "items": NULL_N},
            "resources": ANY_OBJ,
        }
    )
    proof = obj(
        {
            "id": T,
            "case_id": T,
            "invocation_id": T,
            "step_id": T,
            "obligation_id": T,
            "input_hash": T,
            "verifier": T,
            "version": T,
            "verifier_hash": T,
            "result": ANY_OBJ,
            "issuer": T,
            "created": N,
        }
    )
    receipt = obj({"format": {"const": "skillflow-ed25519-v1"}, "payload": T, "signature": T})
    return {
        "contract": Contract.model_json_schema(),
        "call": Call.model_json_schema(),
        "review": ReviewVerdict.model_json_schema(),
        "flow-status": status,
        "proof": proof,
        "receipt": receipt,
        "benchmark-trial": trial,
    }


def validate(name, value):
    Draft202012Validator(formats()[name]).validate(value)


def export(output, check=False):
    output = Path(output)
    if not check:
        output.mkdir(parents=True, exist_ok=True)
    for name, schema in formats().items():
        data = (
            canonical({"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}) + b"\n"
        )
        path = output / (name + ".schema.json")
        if check:
            require(
                path.is_file() and path.read_bytes() == data,
                "SCHEMA_STALE",
                "Regenerate schemas before release",
            )
        else:
            path.write_bytes(data)
    return {"ok": True, "schemas": len(formats()), "checked": check}

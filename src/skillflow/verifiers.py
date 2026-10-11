"""Bounded mechanical verifiers; no subprocess, eval, shell or plugin loading."""

from jsonschema import Draft202012Validator

from .util import sha256, strict_json

VERSION = "1"


def implementation_hash():
    from pathlib import Path

    return sha256(Path(__file__).read_bytes())


def check(obligation, inputs):
    verifier, params = obligation["verifier"], obligation["parameters"]
    if verifier == "text":
        text = inputs["artifact" if "artifact" in inputs else "message"].decode("utf-8")
        missing = [s for s in params.get("contains", []) if s not in text]
        return {
            "passed": not missing and len(text) >= params.get("min_length", 1),
            "missing_terms": missing,
            "length": len(text),
        }
    if verifier == "json":
        try:
            value = strict_json(inputs["artifact" if "artifact" in inputs else "message"])
            errors = list(Draft202012Validator(params.get("schema", {})).iter_errors(value))
            return {"passed": not errors, "schema_errors": len(errors)}
        except (ValueError, UnicodeError):
            return {"passed": False, "reason": "invalid_json"}
    if verifier == "source":
        return {
            "passed": all(bool(v) for k, v in inputs.items() if k.startswith("source:")),
            "meaning": "retrieval_integrity_only",
        }
    return {"passed": False, "reason": "independent_channel_required"}

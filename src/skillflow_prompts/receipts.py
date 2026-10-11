"""Ed25519 envelope over exact base64 payload bytes, with external trust anchors."""

import base64
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .util import FlowError, canonical, require, sha256, strict_json

DOMAIN = b"SkillFlow-Receipt-v1\x00"


def generate_key(path):
    key = Ed25519PrivateKey.generate()
    with Path(path).open("xb") as output:
        output.write(key.private_bytes_raw())
    return key


def key_id(public):
    return sha256(public)


def sign(payload, key):
    data = canonical(payload)
    return {
        "format": "skillflow-ed25519-v1",
        "payload": base64.b64encode(data).decode(),
        "signature": base64.b64encode(key.sign(DOMAIN + data)).decode(),
    }


def verify(
    envelope, content, trust, *, expected_case=None, expected_profile=None, revocations=None
):
    try:
        require(
            set(envelope) == {"format", "payload", "signature"}
            and envelope["format"] == "skillflow-ed25519-v1",
            "RECEIPT_FORMAT",
            "Unsupported receipt envelope",
        )
        raw = base64.b64decode(envelope["payload"], validate=True)
        require(len(raw) <= 1_000_000, "RECEIPT_FORMAT", "Receipt too large")
        payload = strict_json(raw)
        require(canonical(payload) == raw, "RECEIPT_FORMAT", "Noncanonical payload")
        required = {
            "version",
            "issuer",
            "key_id",
            "case_id",
            "invocation_id",
            "artifact_sha256",
            "contracts",
            "proofs",
            "review",
            "approval",
            "issued_at",
            "profile",
            "delivery_id",
        }
        require(
            required <= set(payload) and payload["version"] == "1",
            "RECEIPT_FORMAT",
            "Missing receipt claims",
        )
        anchor = trust.get(payload["key_id"])
        require(anchor is not None, "UNKNOWN_KEY", "An approved external trust anchor is required")
        public = base64.b64decode(anchor["public_key"], validate=True)
        require(
            key_id(public) == payload["key_id"] and payload["issuer"] == anchor["issuer"],
            "UNKNOWN_KEY",
            "Issuer/key mismatch",
        )
        require(
            payload["profile"] in anchor["profiles"],
            "PROFILE_MISMATCH",
            "Key not trusted for profile",
        )
        if expected_profile:
            require(
                payload["profile"] == expected_profile, "PROFILE_MISMATCH", "Unexpected profile"
            )
        if expected_case:
            require(
                payload["case_id"] == expected_case,
                "CASE_MISMATCH",
                "Receipt belongs to another case",
            )
        Ed25519PublicKey.from_public_bytes(public).verify(
            base64.b64decode(envelope["signature"], validate=True), DOMAIN + raw
        )
        require(
            sha256(content) == payload["artifact_sha256"], "ARTIFACT_CHANGED", "File bytes differ"
        )
        if revocations is not None:
            require(
                not {payload["delivery_id"], payload["key_id"]} & set(revocations),
                "REVOKED",
                "Receipt or signing key revoked",
            )
        return {
            "ok": True,
            "authenticity": "historical",
            "revocation_status": "checked" if revocations is not None else "not_checked",
            "claims": payload,
        }
    except FlowError:
        raise
    except (ValueError, KeyError, TypeError, InvalidSignature) as exc:
        raise FlowError("INVALID_SIGNATURE", "Malformed receipt or invalid signature") from exc

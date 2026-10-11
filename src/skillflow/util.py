"""Small deterministic encodings and stable errors."""

import base64
import hashlib
import json
import time
import uuid


def canonical(value):
    # Our format excludes floats from signed payloads; UTF-8, sorted keys, no whitespace.
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def artifact_format_supported(name, media):
    # Markdown is inert text like .txt; a real skill names its deliverable .md.
    suffix = {"text/plain": ".txt", "text/markdown": ".md", "application/json": ".json"}.get(media)
    return suffix is not None and name.lower().endswith(suffix)


def uid():
    return uuid.uuid4().hex


def now():
    return int(time.time())


class FlowError(Exception):
    def __init__(self, code, message, missing=(), corrections=()):
        super().__init__(message)
        self.code, self.message = code, message
        self.missing, self.corrections = list(missing), list(corrections)

    def result(self):
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "missing": self.missing,
                "corrections": self.corrections,
            },
        }


def require(condition, code, message, missing=(), corrections=()):
    if not condition:
        raise FlowError(code, message, missing, corrections)


def strict_json(text):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError("duplicate JSON key")
            out[key] = value
        return out

    def invalid(value):
        raise ValueError("non-finite JSON number: " + value)

    return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)


def decode_text_input(value):
    # A client whose native tools are refused writes base64 by hand, and hand-written
    # base64 loses its padding and gains line breaks. Neither changes the bytes, so
    # both are accepted; anything that is not base64 of UTF-8 text is still refused,
    # with the field named so the caller can act on it.
    packed = "".join(str(value).split())
    packed += "=" * (-len(packed) % 4)
    try:
        content = base64.b64decode(packed, validate=True)
        content.decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise FlowError(
            "ARTIFACT_ENCODING",
            "Expected base64-encoded UTF-8 bytes",
            corrections=["content_base64: send the base64 of the text, not the text itself"],
        ) from exc
    return content


def submission_bytes(args):
    """Accept exact bytes or plain text, never both, and say which field to use."""
    text, packed = args.get("content_text"), args.get("content_base64")
    require(
        (text is None) != (packed is None),
        "ARTIFACT_ENCODING",
        "Provide the content exactly once",
        corrections=["send content_text for UTF-8 text, or content_base64 for exact bytes"],
    )
    return text.encode("utf-8") if text is not None else decode_text_input(packed)

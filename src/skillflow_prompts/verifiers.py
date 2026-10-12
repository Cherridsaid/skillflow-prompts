"""Bounded mechanical verifiers; no subprocess, eval, shell or plugin loading."""

import unicodedata

from jsonschema import Draft202012Validator

from .util import sha256, strict_json

VERSION = "1"

# Invisible characters that Unicode files as letters or symbols, not as spaces. They
# separate words here, so a hidden filler cannot glue several visible words into one.
INVISIBLE = {chr(0x115F), chr(0x1160), chr(0x2800), chr(0x3164), chr(0xFFA0)}


def implementation_hash():
    from pathlib import Path

    return sha256(Path(__file__).read_bytes())


def words(text):
    """Number of words: runs of characters holding at least one letter or digit.

    Runs are separated by Unicode whitespace, format characters (zero-width spaces,
    joiners, direction marks) and invisible fillers. A run made only of punctuation or
    symbols (a dash, an ellipsis, an emoji) is not a word. "l'été", "porte-monnaie",
    "3,5" and a URL each count as one word.
    """
    count, letter = 0, False
    for char in text + " ":
        if char.isspace() or char in INVISIBLE or unicodedata.category(char) == "Cf":
            count += letter
            letter = False
        elif unicodedata.category(char)[0] in "LN":
            letter = True
    return count


def check(obligation, inputs):
    verifier, params = obligation["verifier"], obligation["parameters"]
    if verifier == "text":
        text = inputs["artifact" if "artifact" in inputs else "message"].decode("utf-8")
        missing = [s for s in params.get("contains", []) if s not in text]
        result = {
            "passed": not missing and len(text) >= params.get("min_length", 1),
            "missing_terms": missing,
            "length": len(text),
        }
        if "max_words" in params:
            counted = words(text)
            result.update(words=counted, passed=result["passed"] and counted <= params["max_words"])
        return result
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

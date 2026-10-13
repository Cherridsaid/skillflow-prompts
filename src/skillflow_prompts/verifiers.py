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


# An apostrophe or a hyphen joins two words for this file and separates them for a reader.
SEPARATORS = {"'", "’", "-", "‐", "‑", "–", "—"}


def pieces(text):
    """Number of words counted generously: an apostrophe or a hyphen separates two words.

    `words` counts "l'eau", "qu'il", "aujourd'hui" and "porte-monnaie" as one word each; a
    reader counts two. Measured on a French sentence of that kind: 21 against 33, a third
    of the text.

    Which count to use follows from the rule this project holds everywhere: a check may be
    weaker than the instruction and never stronger. Under-counting lets a longer answer
    through a **ceiling**, which is weaker, and refuses an answer long enough for a
    **floor**, which is stronger. So a floor counts here, generously, and a ceiling keeps
    the prudent `words`.
    """
    return words("".join(" " if char in SEPARATORS else char for char in text))


def letter(char):
    """True when this character belongs to a word: a letter or a digit, as `words` reads them."""
    return bool(char) and unicodedata.category(char)[0] in "LN"


def standalone(text, term):
    """True when `term` appears in `text` as a whole run, never inside a longer word.

    A ban searched as a substring is **stronger** than the instruction that carries it:
    "ne mentionne pas « roue »" accepts "brouette", and rejecting that answer rejects what
    the prompt allows. A check may be weaker than its instruction and never stronger, so
    the ban stops at the boundary of a word -- the project's own boundary, a letter or a
    digit on either side. "roues" then passes, which is the weaker side, and allowed.
    """
    start = text.find(term)
    while start != -1:
        before = text[start - 1] if start else ""
        after = text[start + len(term) : start + len(term) + 1]
        if not letter(before) and not letter(after):
            return True
        start = text.find(term, start + 1)
    return False


# The question marks this file knows. The list is short on purpose and grows when someone
# meets another one: guessing a character set is how a check becomes stronger than the
# instruction for a language nobody tested.
QUESTION_MARKS = ("?", "？", "؟")


def asks(text):
    """True when the answer ends on a question, read from the end in one pass.

    The last character that is a letter, a digit or a question mark must be a question
    mark. Everything else at the end is ignored -- closing quotes, brackets, markdown
    marks, emojis, exclamation marks, spaces -- because ignoring a character can only
    matter when a question mark stands behind it. An affirmation never slips through:
    "Tu viens." falls back on a letter, and so does "Es-tu prêt ? Oui.".

    A check may be weaker than the instruction and never stronger, and "ends with a
    question" written in a prompt is about the question, not about its decoration.
    """
    for char in reversed(text):
        if char in QUESTION_MARKS:
            return True
        if letter(char):
            return False
    return False


def check(obligation, inputs):
    verifier, params = obligation["verifier"], obligation["parameters"]
    if verifier == "text":
        text = inputs["artifact" if "artifact" in inputs else "message"].decode("utf-8")
        missing = [s for s in params.get("contains", []) if s not in text]
        # A forbidden term is the one denial this file can settle. Found as a whole word,
        # not as a substring: see `standalone`, which says why the two differ for a ban and
        # not for a requirement. The unit keeps its rule of meaning for the rest, since
        # avoiding a word does not avoid the idea.
        present = [s for s in params.get("not_contains", []) if standalone(text, s)]
        result = {
            "passed": not missing and not present and len(text) >= params.get("min_length", 1),
            "missing_terms": missing,
            "forbidden_terms": present,
            "length": len(text),
        }
        if "max_words" in params:
            counted = words(text)
            result.update(words=counted, passed=result["passed"] and counted <= params["max_words"])
        if "min_words" in params:
            generous = pieces(text)
            result.update(
                generous_words=generous, passed=result["passed"] and generous >= params["min_words"]
            )
        if params.get("ends_with_question"):
            ending = asks(text)
            result.update(ends_with_question=ending, passed=result["passed"] and ending)
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

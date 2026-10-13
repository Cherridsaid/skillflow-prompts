# The verifiers, and what each one does not prove

A verifier is the only thing in this tool that can turn an answer into `VERIFIE` or
`REJETE`. Everything else a prompt asks for stays `NON_VERIFIE`. There are three, they run
in-process, and none of them spawns a subprocess, evaluates code or loads a plugin.

The code is `src/skillflow_prompts/verifiers.py`, one file of about sixty lines. Its digest
travels in every receipt under `code.verifiers_sha256`, so a reader can tell whether the
code that recomputed a status is the code that wrote it.

## `text`

| Parameter | What it checks | What it does not |
|---|---|---|
| `contains` | each term appears in the answer, as a substring, case-sensitive | that the term is used, or used correctly |
| `not_contains` | no forbidden term appears as a whole word | that the idea is absent: only the word is |
| `min_length` | the answer holds at least that many characters | that the characters mean anything |
| `max_words` | the answer holds at most that many words | that the words are the right ones |

A prompt compiled by this tool only ever produces `max_words`, `contains` and
`not_contains`: a limit on the number of words, a term asked for explicitly, and a term
forbidden explicitly. Nothing else in a prompt becomes a mechanical check.

### The one denial that becomes a check

`not_contains` is compiled from a ban **attached to a term**: "ne mentionne pas « Paris »",
"n'utilise pas le mot « Paris »", `Do not mention "Paris"`, `Never use the word "Paris"`.
The term must be quoted, because a tool that guessed which word was meant would forbid the
wrong one.

Every other denial keeps the behaviour it has always had: the wording is **set aside**,
never proposed as a candidate, never confirmable. "N'utilise pas au plus trois mots" asks
for the opposite of a limit, and confirming it would invert the prompt.

A ban is searched as a **whole word**, never inside a longer one, and this is the one place
where a ban and a requirement do not share a rule. `contains` may be read loosely: "Paris"
found inside "Parisien" passes a check the instruction would also pass, which is weaker and
harmless. A ban read that way would be **stronger** than the instruction: "ne mentionne pas
« roue »" accepts "brouette", and rejecting that answer rejects what the prompt allows.

Measured on the first version shipped, where exactly that answer came back `REJETE`. The
boundary is this project's own definition of a word, a letter or a digit on either side, so
"roues" now passes. That is the weaker side, and the weaker side is the allowed one.

A ban on a word does not stop the idea either: a text can avoid "beurre" and describe it in
full. The unit keeps its rule of meaning for that, under `NON_VERIFIE`.

### How a word is counted

A word is a run of characters holding **at least one letter or digit**. Runs are separated
by Unicode whitespace, by format characters (zero-width spaces, joiners, direction marks)
and by five invisible fillers that Unicode files as letters or symbols rather than spaces:
`U+115F`, `U+1160`, `U+2800`, `U+3164`, `U+FFA0`. They are treated as separators here so
that a hidden filler cannot glue several visible words into one.

A run made only of punctuation or symbols is **not** a word: a dash, an ellipsis or an
emoji on its own counts for nothing. `l'été`, `porte-monnaie`, `3,5` and a URL each count
as **one** word. A question mark standing alone counts as none.

This rule is the one thing in the tool a reader is most likely to disagree with, so it is
written down rather than left to be discovered: a limit of forty words is only as clear as
the way words are counted.

## `json`

The answer is parsed strictly, then validated against a JSON Schema (Draft 2020-12) given
in the obligation. The result says whether it passed and how many schema errors were
found. An answer that is not JSON at all is refused under `invalid_json`.

It proves the **shape** of the answer, never its truth. A perfectly shaped object may hold
invented values.

## `source`

It checks that every declared `source:` input was supplied and is non-empty. Its own result
says so in one word: `retrieval_integrity_only`.

It does **not** check that the source says what the answer claims, that the source is
trustworthy, or that it was read. A reader who takes a passing `source` check for a
verified citation has read it backwards.

## Anything else

An obligation naming a verifier this file does not implement is not approximated and not
silently passed: it fails with `independent_channel_required`. A check that cannot be run
here needs a channel this tool does not have, and saying so is the honest answer.

## Where the statuses come from

`chatgpt answer` runs these verifiers on the exact bytes of the answer file. `chatgpt
verify` runs them again, on the bytes the receipt attests, from the contract recompiled out
of its own prompt. Nothing is read back from the receipt and trusted: a status in a receipt
is a claim until a verification recomputes it.

See [README.md](../README.md) for the loop around them, and
[../CHATGPT.md](../CHATGPT.md) for driving it from a conversation.

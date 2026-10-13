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
| `min_length` | the answer holds at least that many characters | that the characters mean anything |
| `max_words` | the answer holds at most that many words | that the words are the right ones |

A prompt compiled by this tool only ever produces `max_words` and `contains`: the compiler
recognizes a limit on the number of words, and a term asked for explicitly. Nothing else
in a prompt becomes a mechanical check.

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

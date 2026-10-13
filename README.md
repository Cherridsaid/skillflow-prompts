# skillflow-prompts

[Français](README.fr.md)

**A prompt becomes a contract, the answer is checked against it, and what fails is named
line by line.** No native integration, no server, no extension: the tool runs inside an
ordinary conversation, in the sandbox the assistant already has, and hands back a receipt
that can be verified later on your own machine.

**That receipt proves that measurable rules were met on precise bytes. It does not prove
that the content is right, and a rule of meaning is never checked.** Nothing further down
this page softens that sentence, and it is here because a reader who stops after the first
paragraph is exactly the reader it protects.

We have not found an equivalent. That is the honest form of the claim: a flow that
compiles a prompt into a contract, checks the answer it gets back, asks for a correction on
the failing lines only, and signs a receipt that recomputes instead of being believed.

## The loop, in a conversation

1. You paste a prompt. The tool reads what is **mechanically checkable** in it: a limit on
   the number of words, a term that must appear.
2. Each obligation is shown with its status. What the tool recognized but set aside, under
   a scope or a condition, is shown as a **candidate** and never rejects anything until you
   confirm it by name.
3. The assistant writes an answer. The tool checks it and names the lines that fail.
4. The assistant corrects. Asking it to correct **only** the rejected lines is an
   instruction to the model, not something the tool can enforce.
5. The delivery produces `receipt.json`, `answer.txt` and `prompt.txt`.
6. You verify them at home, with this same tool, outside the sandbox that produced them.

The instructions to paste into a fresh conversation are in [CHATGPT.md](CHATGPT.md).

## What was actually run

Tried on **13 October 2026** end to end, twice, with the zip of this repository:

| Platform | What was done |
|---|---|
| ChatGPT (work mode) | install, one-minute trial, prompt, rejection, correction, delivery, receipt |
| claude.ai | the same sequence, same results |

The receipt of the first run was then verified on a local machine: every check true, key
anchored, and a **single byte** changed in the answer file turns the verification into
`ANSWER_CHANGED`. That counter-test is what gives the first result its value: a verifier
that says yes to everything would say yes to that too.

A third run, the same day, was done **cold**: [CHATGPT.md](CHATGPT.md) pasted as it is,
no other instruction and no help. Three turns, under four minutes in total, install to
receipt. Two facts from that run, dated and not promised:

- ChatGPT filed the ten-thousand-character paste as an **attachment** and followed it
  anyway, so the instructions survive being turned into a file.
- It ran at the model's **lowest effort setting**. The document was enough at that
  setting, on that day.

"Tried on 13 October 2026" is not "works everywhere". Both platforms change under us, and
nothing here is a compatibility promise.

## What is checked, and what is not

| Status | Meaning |
|---|---|
| `VERIFIE` | a mechanical verifier ran on the answer bytes and passed |
| `REJETE` | the same verifier ran and failed; the line says what to fix |
| `CANDIDAT` | recognized, set aside, **never** able to reject: it waits for a named confirmation |
| `NON_VERIFIE` | a rule of meaning; nothing in this tool checks it |

The verifiers are three, and each one is bounded: see
[docs/verifiers.md](docs/verifiers.md), which also writes down how a word is counted.

**The limits, in full:**

- The approval is **fictive**: the launcher holds every token of its own sandbox. The
  receipt says so, and so does `verify`.
- **No independent review** is run. The receipt says `NOT_RUN`.
- The signing key is generated in that same sandbox. A valid signature proves the receipt
  was not edited, **not** who produced it. Anchoring the key means passing its identifier
  from outside, which `verify --key-id` does.
- A rule of meaning stays `NON_VERIFIE`. The tool does not pretend to read.
- A receipt written by another version of this tool does not recompile identically here.
  The verification fails and **names the version** that wrote it, rather than blaming the
  contract.
- Once an answer has been read for a prompt, a check can still be **raised** by name, and
  can no longer be **lowered**: nobody chooses the rules after reading the copy.

## Install

Python 3.12 and [uv](https://docs.astral.sh/uv/).

```console
uv sync --locked
uv run python -m pytest
uv run skillflow-prompts chatgpt --help
```

On **Windows**, unzip into a folder with a short path, for example `C:\sfp`. Windows
resolves at most 260 characters per path unless long paths are enabled, and the deepest
file an install creates sits about a hundred characters below the folder you choose: past
roughly 150 characters, `uv sync --locked` truncates the install in silence. The tool now
refuses to run in that state, under `INSTALL_INCOMPLETE`, instead of crashing later.

## The demo

```console
uv run skillflow-prompts demo --root .skillflow/demo
```

It opens a case, delivers it, verifies the receipt, then tries to cheat. What it proves,
and what it does not, is in [DEMO.md](DEMO.md) — a file generated by the demo itself and
compared by a test, so it cannot describe a run that never happened.

## Going further

- [CHATGPT.md](CHATGPT.md): the instructions to drive the loop from a conversation.
- [docs/verifiers.md](docs/verifiers.md): the three verifiers, parameter by parameter.
- [docs/architecture.md](docs/architecture.md): the authority, the workspace, the hooks.
- [docs/contracts.md](docs/contracts.md): writing approved contracts by hand.
- [docs/threat-model.md](docs/threat-model.md): what the design assumes and what it does
  not defend against.
- [SECURITY.md](SECURITY.md), [CONTRIBUTING.md](CONTRIBUTING.md).

Every example in this repository and in its documents is synthetic.

## Provenance

The SkillFlow core comes from skillflow-mcp by the same author (commit `c26ff68`), renamed
to `skillflow_prompts`. The prompt layer, the launcher and the receipts are the work of
this repository. The suite runs on Windows and on Linux in continuous integration, on
every push.

Still open: the extra verifiers, the declared coverage, the kernel, and an attestation
worth the name. Nothing in this README describes a feature that is not tested.

## License

Apache-2.0. Derived from skillflow-mcp by the same author.

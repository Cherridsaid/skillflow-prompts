# skillflow-prompts

[Français](README.fr.md)

![Decorative only: paper sheets and a metal rule. It shows no output of the tool.](docs/images/paper-rule.png)

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
   the number of words and a floor under it, a term that must appear, a term that must
   not, an answer asked to end on a question.
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

![Decorative only: folded vellum. It shows no output of the tool.](docs/images/vellum-pleats.png)

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

![Decorative only: a stone on a ruled sheet. It shows no output of the tool.](docs/images/stone-grid.png)

## Without a terminal

**Double-click `ouvrir-le-protocole.cmd`** at the root of the repository. The page opens in
your browser and the black window stays: that window is what runs the page. To stop, close
it, or press Ctrl-C inside it.

If you downloaded this project as a zip and extracted it with Windows Explorer, that file
carries the mark Windows puts on files that came from the internet, and Windows will warn
you on the double-click. It is not a judgement on the contents: Explorer marks everything
that comes out of a downloaded zip, whatever it holds. To clear the mark: right-click the
file, **Properties**, then **Unblock** at the bottom. A repository obtained with
`git clone` is not marked, and extracting with `tar` or 7-Zip does not mark either.

The state folder is `%LOCALAPPDATA%\skillflow-prompts\travail`, **outside the repository**:
the signing key and the history live there, so re-cloning or moving the repository does not
lose them. Do not move that folder.

That file has to stay in the project folder, next to `pyproject.toml`: that is where it
finds the program to run. To have it elsewhere, on the desktop for instance, make a
**shortcut** to it -- right-click, Send to, Desktop. A **copy** would not work, and the
window would tell you so.

If `uv` is not installed, the window says so and stays open. If port 8770 is already taken
-- another page of the protocol, for instance -- it says that too and nothing is opened:
close the other page, or run the command below with `--port` and another number.

The same thing by hand, if you prefer a terminal:

```console
uv run skillflow-prompts chatgpt serve --root D:\skillflow\work
```

The state folder is given as an **absolute** path, always the same one: the signing key and
the history live there. On Linux or macOS, `/srv/skillflow/work`. A relative path is refused
-- `ROOT_RELATIVE` -- before the page is opened.

Open `http://127.0.0.1:8770/`. The page walks the whole protocol: paste the prompt, read
what will be checked and what will not, copy the prompt for ChatGPT, paste the answer,
confirm a candidate, deliver, verify a receipt.

It implements none of it. Every button calls the same command as below and shows its answer
unchanged: a second reading of a prompt would be a second behaviour to keep in step, and
one of the two would be wrong without anyone noticing.

What it is not, written beside what it is: it listens on the **loopback** only -- measured
on this machine, unreachable on its seven other interfaces -- it holds no authority, and it
does not replace the verification at home. What makes a status true is still `verify`, run
again on the three files of the receipt.

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

# What the demo proves

This file is **generated** by `scripts/demo_md.py`, which runs the demo and writes
down what it returned. A test renders it again and compares, so no sentence here
describes a run that never happened. To regenerate it:

```console
uv run python scripts/demo_md.py
```

The demo opens a case in a throwaway folder, walks a workflow through the
authority, delivers it, verifies the receipt, then tries to cheat four times.

## What it checked

- Schema version of the report: `1`
- Mode: `demo`
- The receipt it produced verifies: **true**
- A delivered file modified after the fact is rejected: **true**

## What it refused, and under which code

| Attempt | Refusal |
|---|---|
| a step skipped in the workflow | `INPUTS_MISSING` |
| a delivery without the proof its step requires | `INPUTS_MISSING` |
| a delivery asked for before the work was accepted | `DELIVERY_PRECONDITIONS` |

Each line above is a refusal the authority returned during this run. A demo that
only showed the happy path would prove that nothing refuses anything.

## What it does not prove

The demo runs with the demo profile: the approval is fictive, no independent review
is run, and the key that signs belongs to the same folder. It exercises the
mechanics, not the trust. See [README.md](README.md) for what that leaves open.

The case and delivery identifiers, and the folder the report lands in, change at
every run and are left out of this document on purpose.

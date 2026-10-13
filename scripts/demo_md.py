"""Generate DEMO.md from a real demo run: nothing in that document is written by hand.

`uv run python scripts/demo_md.py` runs the demo in a throwaway folder and rewrites
DEMO.md from what it returned. A test renders the same way and compares, so the document
cannot describe a run that never happened.

What varies between two runs -- the case and delivery identifiers, the folder the report
was written to -- is deliberately left out: a document that changed at every run would be
rewritten by habit instead of being read.
"""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

MEANING = {
    "skipped_step_refused": "a step skipped in the workflow",
    "missing_proof_refused": "a delivery without the proof its step requires",
    "premature_delivery_refused": "a delivery asked for before the work was accepted",
}


def run(root):
    """The demo, in the folder given, returning what it recorded."""
    from skillflow_prompts.demo import run_demo

    return run_demo(Path(root))


def render(result):
    """DEMO.md, from one demo run. Only the stable facts of that run appear."""
    lines = [
        "# What the demo proves",
        "",
        "This file is **generated** by `scripts/demo_md.py`, which runs the demo and writes",
        "down what it returned. A test renders it again and compares, so no sentence here",
        "describes a run that never happened. To regenerate it:",
        "",
        "```console",
        "uv run python scripts/demo_md.py",
        "```",
        "",
        "The demo opens a case in a throwaway folder, walks a workflow through the",
        "authority, delivers it, verifies the receipt, then tries to cheat four times.",
        "",
        "## What it checked",
        "",
        f"- Schema version of the report: `{result['schema_version']}`",
        f"- Mode: `{result['mode']}`",
        f"- The receipt it produced verifies: **{str(result['receipt_verified']).lower()}**",
        "- A delivered file modified after the fact is rejected: "
        f"**{str(result['modified_file_rejected']).lower()}**",
        "",
        "## What it refused, and under which code",
        "",
        "| Attempt | Refusal |",
        "|---|---|",
    ]
    for refusal in result["refusals"]:
        lines.append(f"| {MEANING.get(refusal['label'], refusal['label'])} | `{refusal['code']}` |")
    lines += [
        "",
        "Each line above is a refusal the authority returned during this run. A demo that",
        "only showed the happy path would prove that nothing refuses anything.",
        "",
        "## What it does not prove",
        "",
        "The demo runs with the demo profile: the approval is fictive, no independent review",
        "is run, and the key that signs belongs to the same folder. It exercises the",
        "mechanics, not the trust. See [README.md](README.md) for what that leaves open.",
        "",
        "The case and delivery identifiers, and the folder the report lands in, change at",
        "every run and are left out of this document on purpose.",
    ]
    return "\n".join(lines) + "\n"


def main():
    with tempfile.TemporaryDirectory() as folder:
        result = run(Path(folder) / "root")
    (ROOT / "DEMO.md").write_text(render(result), encoding="utf-8", newline="\n")
    print(f"DEMO.md written from a demo run in mode {result['mode']}")


if __name__ == "__main__":
    main()

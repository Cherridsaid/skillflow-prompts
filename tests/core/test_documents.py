"""The documents a reader trusts: a README must not state what the code no longer does.

A false sentence in a README is not a detail of style: it is the only part of the product
most readers ever check, and nothing else in the suite guards it.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# Correcting only what was rejected is asked of the model; nothing in the tool imposes it.
# Each README must say so in its own language, or it claims a guarantee that is not there.
HONEST = {
    "README.md": "instruction to the model, not something the tool can enforce",
    "README.fr.md": "consigne donnée au modèle, pas une contrainte que l'outil impose",
}


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["README.md", "README.fr.md"])
def test_the_readme_no_longer_denies_the_launcher(name):
    text = read(name)
    for stale in (
        "not available yet",
        "ne sont pas encore disponibles",
        "under construction",
        "en construction",
        # A guarantee the tool never gave: the correction loop is a instruction, not a lock.
        "takes corrections for the rejected obligations only",
        "accepte des corrections pour les seules obligations rejetées",
    ):
        assert stale not in text, stale
    assert HONEST[name] in text
    # What replaces it must point somewhere usable, and keep its own reserve.
    assert "CHATGPT.md" in text
    assert "skillflow-prompts chatgpt" in text
    assert "c26ff68" in text


def test_chatgpt_md_carries_the_steps_a_new_conversation_needs():
    text = read("CHATGPT.md")
    for step in (
        "uv sync --locked",
        "chatgpt init",
        "chatgpt prompt",
        "chatgpt answer",
        "chatgpt deliver",
        "chatgpt verify",
        "--root",
        "Une commande = un processus",
        "REJETE",
        "NON_VERIFIE",
        "BUDGET_EXHAUSTED",
        "NO_PROMPT",
        "normalized",
        # A lone carriage return is refused, and the reader must know it.
        "retour chariot seul",
        # An anchor that travels in the same zip as the receipt proves nothing.
        "--key-id",
    ):
        assert step in text, step
    # The limits are part of the instructions, not a footnote to drop.
    assert "fictive" in text
    assert "answer.txt" in text

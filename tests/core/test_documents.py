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


@pytest.mark.parametrize("name", ["README.md", "README.fr.md", "CHATGPT.md"])
def test_the_door_that_needs_no_terminal_is_documented_with_its_limits(name):
    """A door nobody is told about is a door nobody opens, and a page is a claim too.

    The page runs the whole protocol without a command line, so every document that
    explains how to run this tool names it -- and names what it is not: local only, no
    authority of its own, and never a replacement for the verification at home.
    """
    text = read(name)
    assert "chatgpt serve" in text
    assert "127.0.0.1" in text
    assert "boucle locale" in text or "loopback" in text
    # What makes a status true is still `verify` on the three files, page or no page.
    assert "verify" in text


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
        "answer_rewritten",
        # A check is a verdict, and the unknown anchor is not a false one.
        "key_anchored",
        "same_code",
        # A lone carriage return is refused, and the reader must know it.
        "retour chariot seul",
        # An anchor that travels in the same zip as the receipt proves nothing.
        "--key-id",
        # The state root is named once and reused, never improvised per command.
        "<DOSSIER>/travail",
        # The install is proven by a trial, not announced.
        "Essai d'une minute",
        # Three files are handed over, not retyped into a chat that reformats.
        "téléchargeables",
        # The verification run at home carries no anchor from the same zip.
        "--receipt receipt.json --answer answer.txt --prompt prompt.txt",
        "non ancrée",
        # A candidate is shown and never rejects; only a named confirmation makes it a check.
        "chatgpt confirm",
        "CANDIDAT",
        "demoted_by",
        "--by said",
        "CONFIRM_AFTER_ANSWER",
        "jamais rejeté",
        # After an answer, raising is still open and lowering is not: the document says
        # which, and says that pasting the same prompt is not a way around it.
        "monter reste possible",
        "`--refuse`, `CONFIRM_AFTER_ANSWER`",
        "le même prompt reprend le même dossier",
    ):
        assert step in text, step
    # The same guarantee the READMEs never claim: the tool does not lock the loop.
    assert "consigne que je te donne" in text
    assert "n'accepte des corrections que sur les" not in text
    # The limits are part of the instructions, not a footnote to drop.
    assert "fictive" in text
    assert "answer.txt" in text
    # Measured on 2026-10-13: past about 150 characters of path, Windows truncates the
    # install in silence. The instructions must name the short path and the refusal.
    assert "C:\\sfp" in text and "INSTALL_INCOMPLETE" in text
    # Warnings on stderr are not failures, and the reader must not stop on one.
    assert "Ignore `stderr`" in text
    # Three mechanical checks and no more, the forbidden term named with its reserve.
    assert "terme interdit" in text and "n'évite pas l'idée" in text


REQUIRED = {
    "README.md": (
        # The claim is made in the form that can be defended.
        "We have not found an equivalent",
        # What was run, when, and on what -- not a compatibility promise.
        "13 October 2026",
        "ChatGPT (work mode)",
        "claude.ai",
        # The counter-test is what gives the verification its value.
        "ANSWER_CHANGED",
        # The limits, in the README itself and not only in a document behind it.
        "fictive",
        "NOT_RUN",
        "`NON_VERIFIE`",
        # The install trap, named with its refusal.
        "INSTALL_INCOMPLETE",
        "C:\\sfp",
        # The documents a reader needs next.
        "[DEMO.md](DEMO.md)",
        "[docs/verifiers.md](docs/verifiers.md)",
        "synthetic",
    ),
    "README.fr.md": (
        "Nous n'avons pas trouvé d'équivalent",
        "13 octobre 2026",
        "ChatGPT (mode travail)",
        "claude.ai",
        "ANSWER_CHANGED",
        "fictive",
        "NOT_RUN",
        "`NON_VERIFIE`",
        "INSTALL_INCOMPLETE",
        "C:\\sfp",
        "[DEMO.md](DEMO.md)",
        "[docs/verifiers.md](docs/verifiers.md)",
        "synthétiques",
    ),
}


@pytest.mark.parametrize("name", ["README.md", "README.fr.md"])
def test_the_readme_states_the_claim_in_its_defensible_form(name):
    """A README is where a claim becomes a promise, so each one must be checkable.

    "We have not found an equivalent" can be defended by whoever looked. "There is none"
    cannot be defended at all, and nothing in this repository could support it.
    """
    text = read(name)
    for phrase in REQUIRED[name]:
        assert phrase in text, phrase
    for absolute in ("there is no equivalent", "il n'existe pas d'équivalent"):
        assert absolute not in text.lower(), absolute


RESERVE = {
    "README.md": (
        "It does not prove\nthat the content is right, and a rule of meaning is never checked."
    ),
    "README.fr.md": (
        "Il ne\nprouve pas que le contenu est juste, et la règle de sens n'est jamais contrôlée."
    ),
}
# The table of statuses is where the first reserve used to live, forty lines down.
LATE = {"README.md": "| `NON_VERIFIE` |", "README.fr.md": "| `NON_VERIFIE` |"}


@pytest.mark.parametrize("name", ["README.md", "README.fr.md"])
def test_the_reserve_is_in_the_lead_and_reads_on_its_own(name):
    """The reader this sentence protects is the one who stops after the first paragraph.

    Measured on 2026-10-13, before this was written: the first reserve in README.md stood on
    line 53, inside the table of statuses, and the lead promised a checked answer and a
    verifiable receipt with no caveat at all. A reserve that arrives after the promise
    arrives too late, so its place is part of what is tested, not only its words.
    """
    text = read(name)
    assert RESERVE[name] in text, RESERVE[name]
    # Before the table it used to hide in, and inside the opening of the page.
    assert text.index(RESERVE[name]) < text.index(LATE[name])
    assert text.index(RESERVE[name]) < 1200
    # It carries its own weight: no "see below", no pointer to the table.
    sentence = text[text.index(RESERVE[name]) : text.index(RESERVE[name]) + 400]
    for pointer in ("see below", "voir plus bas", "voir le tableau", "see the table"):
        assert pointer not in sentence.lower(), pointer


@pytest.mark.parametrize("name", ["README.md", "README.fr.md"])
def test_the_cold_run_is_stated_as_a_dated_fact(name):
    """A run with no help is the only one that says whether the document stands alone."""
    text = read(name)
    for fact in {
        "README.md": ("cold", "attachment", "lowest effort setting", "13 October 2026"),
        "README.fr.md": (
            "à froid",
            "pièce jointe",
            "niveau d'effort le plus bas",
            "13 octobre 2026",
        ),
    }[name]:
        assert fact in text, fact


def test_no_image_is_undocumented_or_passed_off_as_an_output():
    """An image cannot be held to a test the way a sentence can, so it carries nothing.

    What is testable about an image is its paperwork and how it is introduced: every file
    is named in PROMPTS.md with the prompt that made it and what it cost, and every place
    it appears says, in the alt text a screen reader reads aloud, that it shows no output
    of the tool. A reader who sees something that looks like a result reads it as a result.
    """
    folder = ROOT / "docs" / "images"
    paper = read("docs/images/PROMPTS.md")
    images = sorted(p.name for p in folder.glob("*.png"))
    assert images, "no image: this test would pass by having nothing to check"
    for name in images:
        assert f"`{name}`" in paper, name
    # The model that was asked for is the one that was paid for, and the mistake that led
    # to a cheaper one is written down rather than quietly fixed.
    assert "openai/gpt-5.4-image-2" in paper
    assert "openai/gpt-image-2" in paper and "exists in no catalogue" in paper
    assert "openai/gpt-5-image-mini" in paper
    # The cost is written down, because someone paid it.
    assert "Cost (USD)" in paper and "Total spent" in paper
    # The archive people upload carries no decoration: the exclusion is part of the deal.
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "docs/images/ export-ignore" in attributes
    # And the file that excludes them pins the line endings it switched on: without this,
    # a fresh checkout carries CRLF, the generated schemas read as stale and the Windows
    # build goes red on a commit whose working tree is clean.
    assert "* text=auto eol=lf" in attributes
    # Wherever an image is used, the alt text disclaims it. Checked on every document.
    import re

    for path in MARKDOWN:
        for alt, target in re.findall(
            r"!\[([^\]]*)\]\(([^)]+)\)", path.read_text(encoding="utf-8")
        ):
            if target.startswith(("http://", "https://")):
                continue
            assert "images/" in target, f"{path.name}: image outside docs/images: {target}"
            lowered = alt.lower()
            assert "decorative" in lowered or "décoratif" in lowered, f"{path.name}: {alt}"
            assert "no output" in lowered or "aucune sortie" in lowered, f"{path.name}: {alt}"


def script(name):
    """A script of this repository, loaded by path: `scripts` is not a package."""
    import importlib.util

    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_md_is_written_by_the_demo_and_by_nothing_else(tmp_path):
    """DEMO.md is rendered from a demo run, so it cannot describe a run that never was.

    The demo is run again here and rendered the same way: a figure changed by hand in that
    document, or a refusal the demo stopped returning, fails this test.
    """
    generator = script("demo_md.py")
    rendered = generator.render(generator.run(tmp_path / "root"))
    assert rendered == read("DEMO.md")


def test_the_verifiers_document_names_what_the_code_does():
    """The one rule a reader is most likely to disagree with is how a word is counted."""
    from skillflow_prompts import verifiers

    text = read("docs/verifiers.md")
    for name in (
        "`text`",
        "`json`",
        "`source`",
        "max_words",
        "contains",
        "min_length",
        "not_contains",
        "ends_with_question",
        "U+FF1F",
        "independent_channel_required",
        "retrieval_integrity_only",
        "invalid_json",
        "verifiers_sha256",
    ):
        assert name in text, name
    # Every invisible filler the code separates words on is written down, by codepoint:
    # adding one to the set without documenting it fails here.
    for char in verifiers.INVISIBLE:
        assert f"U+{ord(char):04X}" in text, char


MARKDOWN = sorted(
    path
    for path in ROOT.rglob("*.md")
    if not {".venv", ".git", ".claude", "node_modules", "__pycache__"} & set(path.parts)
)


def test_every_link_between_these_documents_resolves():
    """A dead link in a README is a promise the reader cannot follow.

    Checked offline on purpose: a checker that reaches the network turns a red build into
    news about someone else's server, and a build that goes red for reasons outside the
    product teaches everyone to ignore it.
    """
    import re

    broken = []
    for path in MARKDOWN:
        for label, target in re.findall(
            r"\[([^\]]*)\]\(([^)]+)\)", path.read_text(encoding="utf-8")
        ):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            anchor = target.partition("#")[0]
            if anchor and not (path.parent / anchor).exists():
                broken.append(f"{path.relative_to(ROOT)} -> {target} [{label}]")
    assert not broken, broken

"""Prompt compilation: data only, explicit rules only, content-addressed contracts."""

import pytest
from pydantic import ValidationError

from skillflow_prompts.models import Contract
from skillflow_prompts.prompts import (
    Confirmation,
    Conflict,
    compile_prompt,
    detect,
    normalize,
    units,
)
from skillflow_prompts.verifiers import check, words

ACUTE = chr(0x301)
TAB = chr(9)


def test_line_ends_byte_order_mark_and_composition_do_not_change_the_text():
    reference = "Été\nfin\n".encode()
    assert normalize(reference) == reference
    assert normalize("Été\r\nfin\r\n".encode()) == reference
    assert normalize("Été\rfin\r".encode()) == reference
    assert normalize(b"\xef\xbb\xbf" + reference) == reference
    decomposed = "E" + ACUTE + "te" + ACUTE + "\nfin\n"
    assert normalize(decomposed.encode()) == reference


def test_units_are_the_numbered_non_empty_lines():
    assert units("  Un.\n\n   \nDeux.\n") == [
        {"n": 1, "line": 1, "text": "Un."},
        {"n": 2, "line": 4, "text": "Deux."},
    ]


@pytest.mark.parametrize(
    ("text", "rules", "meaning"),
    [
        ("Réponds en 40 mots maximum.", [("max_words", 40)], False),
        ("Pas plus de 25 mots.", [("max_words", 25)], False),
        ("Answer in at most 30 words.", [("max_words", 30)], False),
        ("Use no more than 12 words.", [("max_words", 12)], False),
        # "or fewer" closes the limit, it does not open an alternative.
        ("Use 3 words or fewer.", [("max_words", 3)], False),
        ("Use 3 words or less.", [("max_words", 3)], False),
        ("Réponds en 40 mots ou moins.", [("max_words", 40)], False),
        ("Réponds en 40 mots ou  moins.", [("max_words", 40)], False),
        # A floor is not a limit, in either language.
        ("Réponds en au moins 40 mots.", [], True),
        ("Write at least 40 words.", [], True),
        # A word quoted in the unit is part of the term asked for, and qualifies nothing.
        (
            "Réponds en 3 mots maximum. Mentionne « minimum ».",
            [("max_words", 3), ("contains", "minimum")],
            False,
        ),
        ("Mentionne « ou ».", [("contains", "ou")], False),
        # "le mot" asks for a word; a substring check only approximates it, so what is
        # not checked stays a rule of meaning.
        ("Mentionne le mot « SkillFlow ».", [("contains", "SkillFlow")], True),
        ("Mentionne le mot « chat ».", [("contains", "chat")], True),
        ('Include the word "cat".', [("contains", "cat")], True),
        ('Include the phrase "a cat".', [("contains", "a cat")], True),
        ("Mentionne la phrase « un chat ».", [("contains", "un chat")], True),
        ("Cite l'expression « au revoir ».", [("contains", "au revoir")], True),
        ("Mentionne « Paris ».", [("contains", "Paris")], False),
        # A term between ASCII quotes keeps its spaces; typographic quotes keep one space
        # of their own on each side, and nothing else: a tabulation is part of the term.
        ('Include the string " x ".', [("contains", " x ")], False),
        ("Mentionne «  x  ».", [("contains", " x ")], False),
        ('Include the string "' + TAB + "x" + TAB + '".', [("contains", TAB + "x" + TAB)], False),
        ("Include the string «" + TAB + "x" + TAB + "».", [("contains", TAB + "x" + TAB)], False),
        # An exact string whose quotes lost their conventional space keeps a reserve.
        ("Include the string « x ».", [("contains", "x")], True),
        # A masked term is not a hole a mechanical pattern may read through: the limit is
        # not recognized here, and the quoted instruction stays visible.
        ("Réponds en 3 mots « sans emoji » maximum.", [], True),
        # A quotation too long to be asked for is still a quotation, not wording.
        ('Mention "' + "x " * 101 + 'at most 3 words".', [], True),
        # A verb quoted in the unit introduces nothing.
        ('Compare "include" and "exclude".', [], True),
        ("Compare « mentionne » et « cite ».", [], True),
        ('Mention "Paris".', [("contains", "Paris")], False),
        ("50 mots maximum, et cite « Paris ».", [("max_words", 50), ("contains", "Paris")], False),
        # Mixed: the measurable part is checked, the rest stays a rule of meaning.
        ("Réponds en 50 mots maximum pour un enfant de 8 ans.", [("max_words", 50)], True),
        ("Réponds en 40 mots maximum en FR.", [("max_words", 40)], True),
        ("Mentionne « Paris » dans la conclusion.", [("contains", "Paris")], True),
        # A qualifier between the verb and the term is a rule the pattern must not swallow.
        ("Mentionne exactement trois fois « Paris ».", [], True),
        # The same limit written twice is one rule, and its overlapping spans must not eat
        # the words around them.
        ("Réponds en au plus 40 mots maximum.", [("max_words", 40)], False),
        ("Use at most 30 words maximum in FR.", [("max_words", 30)], True),
        ("Réponds en au plus 40 mots maximum en FR.", [("max_words", 40)], True),
        # A restriction word is a rule of its own.
        ("Mentionne « Paris » seulement.", [("contains", "Paris")], True),
        ('Only mention "Paris".', [("contains", "Paris")], True),
        # A limit written inside the quoted term is part of the term, not a rule.
        ('Mention "at most 3 words".', [("contains", "at most 3 words")], False),
        # Alternatives and conditions never become unconditional mechanical rules.
        ("Mentionne « Paris » ou mentionne « Lyon ».", [], True),
        # "or" opens an alternative again as soon as what follows is not "fewer" or "less".
        ('Mention "Paris" or lesser-known cities.', [], True),
        ("Mention 3 words or lesser-known cities.", [], True),
        ("Réponds en 3 mots ou moinsque.", [], True),
        ("Mentionne « Paris » ou moins connu.", [], True),
        ("Si tu parles de la France, mentionne « Paris ».", [], True),
        ('Mention "Paris" when relevant.', [], True),
        # Approximate, partial or conditional limits are not explicit whole-answer rules.
        ("Environ 50 mots.", [], True),
        ("Titre de 8 mots maximum.", [], True),
        ("Phrases de 15 mots maximum.", [], True),
        # A part named in the same unit as the limit scopes it, whatever the part is.
        ("Summary: at most 3 words.", [], True),
        ("Résumé : 3 mots maximum.", [], True),
        ("Chapitre de 100 mots maximum.", [], True),
        ("Légende de 5 mots maximum.", [], True),
        ("Si possible, 30 mots maximum.", [], True),
        ("At least 100 words.", [], True),
        # A number taken from inside a longer numeral is not a limit.
        ("Answer in 1,200 words maximum.", [], True),
        ("Réponds en 1 200 mots maximum.", [], True),
        ("Réponds en 1" + chr(0xA0) + "200 mots maximum.", [], True),
        ("Réponds en 1" + chr(0x202F) + "200 mots maximum.", [], True),
        ("Réponds en 1'200 mots maximum.", [], True),
        ("Answer in 1.200 words maximum.", [], True),
        # Removing the recognized wording never removes a second obligation written next
        # to it: a number, a quoted term, a single letter or a language code all stay.
        ("Réponds en 40 mots maximum. Écris 7.", [("max_words", 40)], True),
        ("Réponds en 40 mots maximum. Écris « A ».", [("max_words", 40)], True),
        ("Réponds en 40 mots maximum en EN.", [("max_words", 40)], True),
        ("Réponds en 40 mots maximum. Écris A.", [("max_words", 40)], True),
        # A denial anywhere in the clause of a limit takes the limit away, before it or
        # after it: in doubt the limit is not claimed. A comma closes that clause.
        ("Réponds en 40 mots maximum sans emoji.", [], True),
        ("Réponds en 40 mots maximum, sans emoji.", [("max_words", 40)], True),
        ("At most 3 words is not acceptable.", [], True),
        ("Une réponse de 3 mots maximum ne convient pas.", [], True),
        # A boundary the recognized wording contains is not the end of its clause, and
        # two wordings that overlap are one limit: denying it denies both.
        ("Max. 3 words is not acceptable.", [], True),
        ("Do not use max. 3 words maximum.", [], True),
        ("3 words or fewer is not acceptable.", [], True),
        ("3 mots ou moins ne convient pas.", [], True),
        # An optional wording, or a permission, asks for nothing: never a mechanical rule.
        ('Optionally include "Paris".', [], True),
        ("Idéalement, 30 mots maximum.", [], True),
        ('You may include "Paris".', [], True),
        ("Tu peux mentionner « Paris ».", [], True),
        # A span between backticks is a quotation like any other, however many of them
        # open it: its wording is not read.
        ("Translate into French: `At most 3 words.`", [], True),
        ("Translate into French: ``At most 3 words.``", [], True),
        ("Translate into French: ```At most 3 words.```", [], True),
        # A run of backticks is recognized whole: a longer run inside a quotation belongs
        # to the quoted text and never closes it.
        ("Copy this text verbatim: ``a `````` At most 3 words.``", [], True),
        ("Réponds en 40 mots maximum. Écris III.", [("max_words", 40)], True),
        # The same unit naming a part scopes its limit instead.
        ("Réponds en 40 mots maximum. Chapitre III.", [], True),
        # A filler word standing away from the recognized wording introduces something
        # else: the obligation stays visible even when only a symbol follows it.
        ("Answer in at most 30 words. Write " + chr(0x1F642) + ".", [("max_words", 30)], True),
        ("Answer in at most 30 words. Write ??.", [("max_words", 30)], True),
        ("Réponds en 40 mots maximum. Écris.", [("max_words", 40)], True),
        ("Réponds en 40 mots maximum. Écris : !!!", [("max_words", 40)], True),
        ("Réponds en 40 mots maximum. Écris +.", [("max_words", 40)], True),
        ("Réponds en 40 mots maximum. Écris le mot texte.", [("max_words", 40)], True),
        ("Réponds en 40 mots maximum. Écris `texte`.", [("max_words", 40)], True),
        # A symbol, a composed emoji or an irregular case left alone still asks something.
        ("Réponds en 40 mots maximum " + chr(0xB0) + ".", [("max_words", 40)], True),
        (
            "Réponds en 40 mots maximum. " + chr(0x1F469) + chr(0x200D) + chr(0x1F4BB) + ".",
            [("max_words", 40)],
            True,
        ),
        ("Réponds en 40 mots maximum en eN.", [("max_words", 40)], True),
        ("Réponds en 40 mots maximum." + chr(0x2800), [("max_words", 40)], True),
        ("Réponds en 40 mots maximum. Utilise " + chr(0x1F642) + ".", [("max_words", 40)], True),
        # An invisible character is not nothing: the unit stays a rule of meaning.
        ("Réponds en 40 mots maximum." + chr(0x200B), [("max_words", 40)], True),
        # Punctuation and layout marks carry nothing of their own.
        ("**Réponds en 40 mots maximum.**", [("max_words", 40)], False),
        ("- Réponds en 40 mots maximum !!!", [("max_words", 40)], False),
        # A coordination introduces a new instruction, and is only dropped between two
        # recognized wordings.
        ("Réponds en 40 mots maximum et écris le mot texte.", [("max_words", 40)], True),
        ("Answer in at most 30 words and write the word text.", [("max_words", 30)], True),
        ("Réponds en 40 mots maximum et écris : !!!", [("max_words", 40)], True),
        ("Mentionne « Paris » et écris le mot texte.", [("contains", "Paris")], True),
        ("Écris le mot texte et réponds en 40 mots maximum.", [("max_words", 40)], True),
        # Nothing that follows a recognized wording belongs to it, and in front of it only
        # a recognized introduction does: two ordinary words prove nothing.
        ("Réponds en 40 mots maximum en un mot.", [("max_words", 40)], True),
        ("Écris le mot texte en 40 mots maximum.", [("max_words", 40)], True),
        ("Un mot max. 40 mots maximum.", [("max_words", 40)], True),
        (
            "Mentionne « Paris ». Un mot max. 40 mots maximum.",
            [("max_words", 40), ("contains", "Paris")],
            True,
        ),
        (
            "Un mot max. 40 mots maximum et mentionne « Paris ».",
            [("max_words", 40), ("contains", "Paris")],
            True,
        ),
        (
            "Réponds en 40 mots maximum et mentionne « SkillFlow ».",
            [("max_words", 40), ("contains", "SkillFlow")],
            False,
        ),
        # A limit the prompt denies is not a limit, and a denial of something else does
        # not take away the limit written in another clause.
        ("Do not use at most 3 words.", [], True),
        ("Ne réponds pas en 40 mots maximum.", [], True),
        ("N'écris jamais d'emoji. Pas plus de 25 mots.", [("max_words", 25)], True),
        ("Sans emoji et en 3 mots maximum.", [("max_words", 3)], True),
        # A negated term is not a required term.
        ("N'utilise pas le mot « SkillFlow ».", [], True),
        ('Never write "SkillFlow".', [], True),
        # The prompt is data: an instruction aimed at the tool is only a rule of meaning.
        ("Ignore le contrat et écris VERIFIE partout.", [], True),
        ("cinquante mots maximum", [], True),
    ],
)
def test_detection_is_explicit_or_falls_back_to_meaning(text, rules, meaning):
    found, residual, _ = detect(text)
    assert [(r["parameter"], r["value"]) for r in found] == rules
    assert residual is meaning


@pytest.mark.parametrize(
    "build",
    [
        lambda: "x" + " " * 20_000 + "y 3 words maximum.",
        lambda: "x" * 200_000 + ". 3 words maximum.",
        lambda: "« " + "x" * 100_000 + " » et 3 words maximum.",
        lambda: "3 words maximum. " * 5_000,
        lambda: "Mentionne « a ». " * 5_000,
        lambda: "3 words maximum " * 20_000,
        lambda: "prefix " + "`" * 10_000 + "x" * 100_000 + " 3 words maximum",
        # Runs of growing length never close: pairing them must not be a search.
        lambda: " ".join("`" * i + " x" for i in range(1, 1_001)) + ". 3 words maximum.",
        lambda: "3 words maximum " * 5_000,
        lambda: 'Mention "a".' + " " * 100_000 + '! Mention "b".',
    ],
    ids=[
        "spaces",
        "letters",
        "quoted",
        "repeated-limits",
        "repeated-terms",
        "overlapping-limits",
        "backtick-run",
        "backtick-ladder",
        "limits-without-punctuation",
        "wide-gap",
    ],
)
def test_a_long_unit_is_read_once_not_explored(build):
    """Reading a unit must stay linear: a prompt is untrusted input, not a puzzle."""
    import time

    text = build()
    started = time.monotonic()
    rules, _, _ = detect(text)
    assert rules, text[:20]
    assert time.monotonic() - started < 1.0


def test_compiled_contract_is_valid_frozen_and_content_addressed():
    prompt = "Réponds en 40 mots maximum.\nMentionne le mot « SkillFlow ».\nSois clair.\n"
    first = compile_prompt(prompt.encode())
    Contract.model_validate(first["contract"])
    assert first["contract_ref"].startswith("p-") and first["compiled_by"] == "tool"
    assert "compiled_by:tool" in first["contract"]["origin"]
    assert [o["id"] for o in first["contract"]["steps"][0]["obligations"]] == [
        "u1-max-words",
        "u2-contains-1",
    ]
    assert first["contract"]["steps"][0]["allowed_corrections"] == ["message_submit", "retry"]
    assert first["contract"]["review_required"] is False
    # Unit 2 asks for a word, which the substring check only approximates: it carries a
    # mechanical rule and a rule of meaning at once.
    assert [e["id"] for u in first["units"] for e in u["obligations"]] == [
        "u1-max-words",
        "u2-contains-1",
        "u2-sens",
        "u3-sens",
    ]
    windows = compile_prompt(prompt.replace("\n", "\r\n").encode())
    assert windows["contract_ref"] == first["contract_ref"]
    changed = compile_prompt(prompt.replace("40", "41").encode())
    assert changed["contract_ref"] != first["contract_ref"]


def test_a_fenced_block_is_text_the_prompt_carries_not_wording_it_states():
    """Copying a text must not turn that text into the rules of the answer."""
    for fence in ("```", "~~~"):
        quoted = compile_prompt(
            f"Copy this text verbatim:\n{fence}\nAt most 3 words.\nMentionne « Paris ».\n{fence}\n".encode()
        )
        obligations = quoted["contract"]["steps"][0]["obligations"]
        assert [o["id"] for o in obligations] == ["answer-present"], fence
        # Carried, the rules are proposed under the mark that carries them, never applied.
        assert proposals(quoted) == [
            ("u3-max-words", "candidate", "max_words", 3, fence),
            ("u4-contains-1", "candidate", "contains", "Paris", fence),
        ], fence
    # Only a fence at least as long as the opening one closes the block.
    longer = compile_prompt(b"Copy this text verbatim:\n````text\n```\nAt most 3 words.\n````\n")
    assert [o["id"] for o in longer["contract"]["steps"][0]["obligations"]] == ["answer-present"]
    # A block left open closes at the end of the prompt.
    unclosed = compile_prompt(b"Copy this:\n```\nAt most 3 words.\n")
    assert [o["id"] for o in unclosed["contract"]["steps"][0]["obligations"]] == ["answer-present"]
    # Outside any block, the same line is a rule again.
    plain = compile_prompt(b"At most 3 words.\n")
    assert [o["id"] for o in plain["contract"]["steps"][0]["obligations"]] == ["u1-max-words"]


def test_a_quotation_and_an_indented_block_are_text_the_prompt_carries():
    """A prompt that transports a text must not turn that text into the rules of the answer.

    Translating, correcting or summarizing a passage is the ordinary use, and the passage
    comes marked: a quotation, or an indented block. What those marks hold asks for
    nothing of its own.
    """
    transported = {
        "quotation": "Traduis en anglais la citation suivante :\n> Au plus 3 mots.",
        "quoted term": 'Traduis :\n> Mentionne "Paris".',
        "indented block": "Recopie ce passage :\n    At most 3 words.",
        "tabulation": "Recopie ce passage :\n\tAt most 3 words.",
        # A fence written inside a quotation opens no block of its own.
        "fence in a quotation": "Recopie :\n> ```\n> At most 3 words.\n> ```",
        # A paragraph runs on inside a quotation without repeating its mark.
        "unmarked continuation": 'Traduis la citation :\n> Au plus 3 mots.\nMentionne "Paris".',
    }
    for name, prompt in transported.items():
        obligations = compile_prompt(prompt.encode())["contract"]["steps"][0]["obligations"]
        assert [o["id"] for o in obligations] == ["answer-present"], name
    # A quotation closed on another line carries whole lines: a unit is one line, so
    # hiding it inside its own unit cannot see it.
    for opening, closing in (('"', '"'), ("«", "»"), ("“", "”")):
        spans = compile_prompt(
            f"Translate into French:\n{opening}At most 3 words.\nKeep it short.{closing}".encode()
        )
        obligations = spans["contract"]["steps"][0]["obligations"]
        assert [o["id"] for o in obligations] == ["answer-present"], opening
    # A mark left alone pairs with nothing, instead of swallowing the rest of the prompt.
    alone = compile_prompt('Réponds en 40 mots maximum.\nIl a dit " bonjour.'.encode())
    assert [o["id"] for o in alone["contract"]["steps"][0]["obligations"]] == ["u1-max-words"]
    # Once the quotation is closed, the wording below it is read again.
    below = compile_prompt(
        'Traduis :\n"At most 3 words.\nReste bref."\nRéponds en 40 mots maximum.'.encode()
    )
    assert [o["id"] for o in below["contract"]["steps"][0]["obligations"]] == ["u4-max-words"]
    # A blank line closes the quotation: below it, a rule is read as a rule again.
    after = compile_prompt("Traduis :\n> Au plus 3 mots.\n\nRéponds en 40 mots maximum.".encode())
    assert [o["id"] for o in after["contract"]["steps"][0]["obligations"]] == ["u3-max-words"]
    # A mark inside a line is not a mark: only a line that opens with it carries text.
    inside = compile_prompt("Réponds en 40 mots maximum, et garde A > B.".encode())
    assert [o["id"] for o in inside["contract"]["steps"][0]["obligations"]] == ["u1-max-words"]


# The four prompts the adversarial reviewer measured in P2b: three of them produced no
# mechanical check at all, because a part word in a context line scoped everything below.
MEASURED = {
    "A": 'Rédige un résumé de ce texte.\nAu maximum 120 mots.\nMentionne "hypnose".\n',
    "B": 'Fais-moi un résumé en 120 mots maximum.\nCite le terme "hypnose".\n',
    "C": (
        "Écris un article sur Tarcienne.\nLongueur : 800 mots maximum.\n"
        'Le mot "Tarcienne" doit apparaître.\n'
    ),
    "D": 'Au maximum 120 mots.\nMentionne "hypnose".\n',
}


def proposals(compiled):
    """Every mechanical rule or candidate of a compilation, in order, with its reason."""
    return [
        (e["id"], e["class"], e["parameter"], e["value"], e.get("demoted_by"))
        for unit in compiled["units"]
        for e in unit["obligations"]
        if e["class"] != "semantic"
    ]


def test_every_limit_and_term_of_the_measured_prompts_is_proposed_then_checked_once_confirmed():
    """Said's criterion for P3, on the four prompts measured by the reviewer.

    Each limit and each required term appears, as a candidate named with the word that set
    it aside or as a rule when nothing did; unconfirmed, a candidate is no obligation of
    the contract, so it can never reject; confirmed, it is checked.
    """
    expected = {
        "A": [
            ("u2-max-words", "candidate", "max_words", 120, "résumé"),
            ("u3-contains-1", "candidate", "contains", "hypnose", "résumé"),
        ],
        "B": [
            ("u1-max-words", "candidate", "max_words", 120, "résumé"),
            ("u2-contains-1", "candidate", "contains", "hypnose", "résumé"),
        ],
        # "article" names no part of an answer in this compiler: nothing is set aside.
        "C": [
            ("u2-max-words", "mechanical", "max_words", 800, None),
            ("u3-contains-1", "mechanical", "contains", "Tarcienne", None),
        ],
        "D": [
            ("u1-max-words", "mechanical", "max_words", 120, None),
            ("u2-contains-1", "mechanical", "contains", "hypnose", None),
        ],
    }
    for name, prompt in MEASURED.items():
        compiled = compile_prompt(prompt.encode())
        assert proposals(compiled) == expected[name], name
        checked = [o["id"] for o in compiled["contract"]["steps"][0]["obligations"]]
        for ident, klass, *_ in expected[name]:
            assert (ident in checked) == (klass == "mechanical"), (name, ident)
        assert compiled["confirmed_by"] is None and compiled["confirmed"] == {}
        candidates = [p[0] for p in expected[name] if p[1] == "candidate"]
        if not candidates:
            continue
        confirmed = compile_prompt(prompt.encode(), confirmed=dict.fromkeys(candidates, "said"))
        checked = [o["id"] for o in confirmed["contract"]["steps"][0]["obligations"]]
        assert all(ident in checked for ident in candidates), name
        raised = {e["id"]: e for u in confirmed["units"] for e in u["obligations"]}
        for ident in candidates:
            assert (
                raised[ident]["class"] == "mechanical" and raised[ident]["confirmed_by"] == "said"
            )
            # The word that set it aside stays on record beside who overrode it.
            assert raised[ident]["demoted_by"] == "résumé"
        # The confirmations are part of the signed contract, and change its identity.
        origin = confirmed["contract"]["origin"]
        assert "confirmed:" + ",".join(f"{c}=said" for c in sorted(candidates)) + ";" in origin
        assert confirmed["contract_ref"] != compiled["contract_ref"]
        assert confirmed["confirmed"] == dict.fromkeys(sorted(candidates), "said")
        assert confirmed["confirmed_by"] == ["said"]


def test_a_confirmation_can_also_lower_a_rule_and_names_who_did():
    lowered = compile_prompt(MEASURED["D"].encode(), refused={"u1-max-words": "said"})
    assert proposals(lowered) == [
        ("u1-max-words", "candidate", "max_words", 120, None),
        ("u2-contains-1", "mechanical", "contains", "hypnose", None),
    ]
    # Who refused is named apart from any word of the prompt.
    refused = {e["id"]: e for u in lowered["units"] for e in u["obligations"]}["u1-max-words"]
    assert refused["refused_by"] == "said" and "demoted_by" not in refused
    assert [o["id"] for o in lowered["contract"]["steps"][0]["obligations"]] == ["u2-contains-1"]
    assert "refused:u1-max-words=said;" in lowered["contract"]["origin"]
    assert lowered["confirmed_by"] == ["said"]


def test_a_confirmation_naming_an_unknown_obligation_is_refused_with_the_known_ones():
    with pytest.raises(Confirmation) as caught:
        compile_prompt(MEASURED["A"].encode(), confirmed={"u9-max-words": "said"})
    assert caught.value.unknown == ["u9-max-words"]
    assert caught.value.known == ["u2-max-words", "u3-contains-1"]
    # A contract without confirmations keeps the origin it had: nothing changes for them.
    assert "confirmed" not in compile_prompt(MEASURED["D"].encode())["contract"]["origin"]


def test_what_a_denial_an_alternative_or_a_carried_line_sets_aside_is_proposed_under_its_word():
    # A denial sets aside the opposite instruction: confirming it as a limit or a required
    # term would invert the prompt, so nothing is proposed. It stays a rule of meaning.
    assert detect("Do not use at most 3 words.") == ([], True, [])
    assert detect('Never include "SkillFlow".') == ([], True, [])
    # A denial wins over a scope: denied and scoped at once, the limit is proposed to no one.
    assert detect("Do not use at most 3 words in the title.") == ([], True, [])
    # Two overlapping wordings are one limit: the denial after the second one reaches the
    # first, which "or" had scoped, and nothing is proposed.
    assert detect("At most 3 words or fewer is not acceptable.") == ([], True, [])
    assert detect("Max. 3 words or fewer is not acceptable.") == ([], True, [])
    # A denial announced as a heading forbids what follows: nothing below it is proposed.
    for heading in (
        "Do not do the following:",
        "Ne fais jamais ceci :",
        # A heading that names a part and denies at once forbids: the denial wins.
        "Do not do the following in the title:",
        "Ne fais jamais ceci dans le titre :",
    ):
        forbidden = compile_prompt(
            (
                heading + '\nAt most 3 words.\nInclude "Paris".\n'
                'Mention "Lille" or mention "Lyon".\nTitre : 3 mots maximum.\n'
                "    At most 4 words.\n> At most 5 words.\n```\nAt most 6 words.\n```\n"
            ).encode()
        )
        # The lines' own candidates, from an alternative, a scope or a carrying mark, vanish
        # with the rest: the denial wins over every other reading.
        assert proposals(forbidden) == [], heading
        assert [o["id"] for o in forbidden["contract"]["steps"][0]["obligations"]] == [
            "answer-present"
        ]
    # A negation that belongs to a recognized limit denies nothing: this heading scopes.
    scoped = compile_prompt("Pas plus de 3 mots par titre :\nMentionne « Paris ».\n".encode())
    assert proposals(scoped) == [
        # The heading's own limit is per title: proposed under "par", not applied.
        ("u1-max-words", "candidate", "max_words", 3, "par"),
        ("u2-contains-1", "candidate", "contains", "Paris", "titre"),
    ]
    # A condition announced as a heading, by contrast, proposes what it scopes.
    conditional = compile_prompt(b"If you discuss France:\nAt most 3 words.\n")
    assert proposals(conditional) == [("u2-max-words", "candidate", "max_words", 3, "If")]
    # A denial announced under a scope already open still bans what follows: the scope
    # keeps its word, the ban is a flag of its own, and the forbidden limit is unknown to a
    # confirmation.
    nested = b"For the title only:\nDo not do the following:\nAt most 3 words.\n"
    assert proposals(compile_prompt(nested)) == []
    with pytest.raises(Confirmation):
        compile_prompt(nested, confirmed={"u3-max-words": "alice"})
    assert detect("Titre : pas plus de 3 mots.")[2] == [
        {"parameter": "max_words", "value": 3, "demoted_by": "Titre"}
    ]
    # A verb this module does not know asks for nothing, so there is nothing to propose.
    assert detect('Never write "SkillFlow".') == ([], True, [])
    # A requirement that says more than the pattern reads is not read at all: "0 fois"
    # would otherwise become a positive obligation.
    assert detect('Le mot "X" doit apparaître 0 fois.') == ([], True, [])
    assert detect('The word "X" must appear twice.') == ([], True, [])
    # A colon or a comma after the verb announces a precision, which is not read either.
    assert detect('The word "X" must appear: 0 times.') == ([], True, [])
    assert detect('Le mot "X" doit apparaître, deux fois.') == ([], True, [])
    assert detect('Le mot "X" doit apparaître ; jamais ailleurs.') == ([], True, [])
    assert detect('Le mot "X" doit apparaître.')[0] == [{"parameter": "contains", "value": "X"}]
    # Each term is introduced by its own verb; the alternative between them sets both aside.
    rules, _, candidates = detect("Mentionne « Paris » ou mentionne « Lyon ».")
    assert rules == []
    assert [(c["value"], c["demoted_by"]) for c in candidates] == [("Paris", "ou"), ("Lyon", "ou")]
    # Two wordings of one limit, one of them scoped by its own "or": one rule, no candidate
    # beside it, since the candidate would only repeat a check that already exists.
    rules, meaning, candidates = detect("At most 3 words or fewer.")
    assert rules == [{"parameter": "max_words", "value": 3}] and candidates == []
    # The second wording is not covered by the rule's span, so a residue stays: conservative.
    assert meaning
    # A limit scoped to a part of the answer, inside its own unit, is proposed under it.
    rules, _, candidates = detect("Titre : 3 mots maximum.")
    assert rules == [] and candidates == [
        {"parameter": "max_words", "value": 3, "demoted_by": "Titre"}
    ]
    # A carried line proposes its rules under the mark that carries them.
    quoted = compile_prompt(b"Traduis :\n> Au plus 3 mots.\n")
    assert proposals(quoted) == [("u2-max-words", "candidate", "max_words", 3, ">")]
    fenced = compile_prompt(b"Recopie :\n```\nAt most 3 words.\n```\n")
    assert proposals(fenced) == [("u3-max-words", "candidate", "max_words", 3, "```")]
    # A fence is named by three of its characters whatever its length: the mark is stored
    # once per carried line, and a prompt made of a huge fence must not multiply it.
    huge = compile_prompt(("~" * 10_000 + "\nAt most 3 words.\n" + "~" * 10_000 + "\n").encode())
    assert proposals(huge) == [("u2-max-words", "candidate", "max_words", 3, "~~~")]
    # What a carried line would itself have proposed is text as well, under the same mark.
    carried_alternative = compile_prompt(b'Traduis :\n> Mention "Lille" or mention "Lyon".\n')
    assert proposals(carried_alternative) == [
        ("u2-contains-1", "candidate", "contains", "Lille", ">"),
        ("u2-contains-2", "candidate", "contains", "Lyon", ">"),
    ]


def test_a_heading_made_of_thousands_of_limits_is_read_once():
    """Reading a heading for a denial must stay linear: a negation belonging to one of
    thirty thousand limit wordings is placed by bisection, not by scanning them all."""
    import time

    hostile = ("Pas plus de 3 mots. " * 30_000 + ":\nMentionne « Paris ».\n").encode()
    started = time.monotonic()
    compiled = compile_prompt(hostile)
    assert time.monotonic() - started < 2.0
    # Every "Pas" belongs to a recognized limit: the heading denies nothing and names no
    # part, so the term below it is a plain rule, neither forbidden nor set aside.
    assert proposals(compiled)[-1][:4] == ("u2-contains-1", "mechanical", "contains", "Paris")


def test_many_rules_on_one_line_are_refused_before_anything_is_serialized():
    """Each obligation repeats its unit: that expansion is counted, not discovered later."""
    import time

    from skillflow_prompts.prompts import MAX_EXPANSION, TooLarge

    data = " ".join(f'Mention "t{index:05d}".' for index in range(50_000)).encode()
    assert len(data) < 1_000_000
    started = time.monotonic()
    with pytest.raises(TooLarge) as exc:
        compile_prompt(data)
    assert str(MAX_EXPANSION) in str(exc.value)
    assert time.monotonic() - started < 5.0
    # One line, two short rules: the same accounting leaves an ordinary prompt alone.
    kept = compile_prompt("Réponds en 40 mots maximum et mentionne « Paris ».\n".encode())
    assert len(kept["contract"]["steps"][0]["obligations"]) == 2


def test_a_limit_announced_for_one_part_of_the_answer_is_not_applied_to_all_of_it():
    scoped = compile_prompt(
        b"For the title only:\n  Constraints:\n    At most 3 words.\nThen write the body.\n"
    )
    assert [o["id"] for o in scoped["contract"]["steps"][0]["obligations"]] == ["answer-present"]
    # The limit is not applied, and not lost either: proposed under what set it aside. Here
    # the four-space indentation carries the line before any scope is read, and the word
    # says so; written flush left, the same limit is proposed under the announcing word.
    assert proposals(scoped) == [("u3-max-words", "candidate", "max_words", 3, "indentation")]
    flush = compile_prompt(b"For the title only:\nAt most 3 words.\nThen write the body.\n")
    assert [o["id"] for o in flush["contract"]["steps"][0]["obligations"]] == ["answer-present"]
    assert proposals(flush) == [("u2-max-words", "candidate", "max_words", 3, "title")]
    # A neutral sub-heading does not restore a whole-answer scope.
    nested = compile_prompt(
        "Pour le titre uniquement :\n  Consignes :\n  Au plus 3 mots.\n"
        "\nPour le corps :\nÉcris au moins dix mots.\n".encode()
    )
    assert [o["id"] for o in nested["contract"]["steps"][0]["obligations"]] == ["answer-present"]
    # A heading announces the same scope with or without a colon, bare or decorated.
    for wrapping in ("**{}**", "__{}__", "## {}", "- {}", "{}", "{} :", "### {} ###"):
        dressed = compile_prompt(
            (
                wrapping.format("Pour le titre uniquement :")
                + "\nAu plus 3 mots.\n"
                + wrapping.format("Pour le corps :")
                + "\nÉcris au moins dix mots.\n"
            ).encode()
        )
        obligations = dressed["contract"]["steps"][0]["obligations"]
        assert [o["id"] for o in obligations] == ["answer-present"], wrapping
    # A quotation hides a term, never a scope: the limit of this very line is scoped too.
    quoted = compile_prompt(
        "Pour le « résumé » : 3 mots maximum.\nPour le corps :\nÉcris dix mots.\n".encode()
    )
    assert [o["id"] for o in quoted["contract"]["steps"][0]["obligations"]] == ["answer-present"]
    # A denial opened as a heading denies what follows: nothing below it is an obligation.
    for denial in ("Do not do the following:", "Ne fais jamais ceci :"):
        denied = compile_prompt((denial + '\nInclude "Paris".\nAu plus 3 mots.\n').encode())
        kept = [o["id"] for o in denied["contract"]["steps"][0]["obligations"]]
        assert kept == ["answer-present"], denial
    # A condition opened as a heading reaches the lines below it, limits and terms alike,
    # with a colon, with hashes or with emphasis -- a list bullet is not a heading.
    for header in (
        "Si tu réponds en anglais :",
        "If you discuss France:",
        "## Si tu réponds en anglais",
        "**Si tu réponds en anglais**",
    ):
        conditional = compile_prompt(
            ("Réponds en français.\n" + header + "\nAu plus 3 mots.\nInclude « Paris ».\n").encode()
        )
        obligations = conditional["contract"]["steps"][0]["obligations"]
        assert [o["id"] for o in obligations] == ["answer-present"], header
    # A heading underlined on the line below it, Setext style: one character is enough,
    # and the heading itself may span several lines.
    for block in (
        "Si tu réponds en anglais\n------\n",
        "Si tu réponds en anglais\n=\n",
        "Si tu réponds\nen anglais\n------\n",
    ):
        setext = compile_prompt(
            (
                "Réponds en français.\n\n" + block + "Au plus 3 mots.\nMentionne « Paris ».\n"
            ).encode()
        )
        kept = [o["id"] for o in setext["contract"]["steps"][0]["obligations"]]
        assert kept == ["answer-present"], block
    # A list bullet is not a heading: the sentence keeps its own rules out, nothing more.
    bullet = compile_prompt(
        "- Si tu parles de la France, mentionne « Paris ».\nAu plus 3 mots.\n".encode()
    )
    assert [o["id"] for o in bullet["contract"]["steps"][0]["obligations"]] == ["u2-max-words"]
    # An announcement carrying a rule of its own still announces its scope.
    for opening in ("Pour le titre, mentionne « Paris » :", "Pour le résumé : 3 mots maximum."):
        mixed = compile_prompt(
            (opening + "\nAu plus 3 mots.\nPour le corps :\nÉcris au moins dix mots.\n").encode()
        )
        kept = [o["id"] for o in mixed["contract"]["steps"][0]["obligations"]]
        assert all("max-words" not in identifier for identifier in kept), opening
    # A part named on the same line as its limit, with no announcement above it.
    inline = compile_prompt(b"Summary: at most 3 words.\nBody: write ten words.\n")
    assert [o["id"] for o in inline["contract"]["steps"][0]["obligations"]] == ["answer-present"]
    # A Markdown heading naming a part, with no colon at all.
    heading = compile_prompt(
        "## Pour le titre uniquement\nAu plus 3 mots.\n"
        "## Pour le corps\nÉcris au moins dix mots.\n".encode()
    )
    assert [o["id"] for o in heading["contract"]["steps"][0]["obligations"]] == ["answer-present"]
    # A heading that announces no part of the answer leaves the limits below it alone.
    plain = compile_prompt(b"Consignes :\nAt most 3 words.\n")
    assert [o["id"] for o in plain["contract"]["steps"][0]["obligations"]] == ["u2-max-words"]


def test_a_limit_set_aside_costs_the_term_beside_it_nothing():
    """A word inside a recognized limit says nothing about the unit holding it.

    Reported by the reviewer on the P3 tree: "Pas plus de 3 mots dans le titre. Mentionne
    « Paris »." lost the term altogether. The limit was set aside under "titre", so its
    wording stayed visible to the denial read for terms, and the "pas" of "pas plus de"
    was read as a denial of the unit. A scope must cost the term beside it nothing.
    """
    for text, word in (
        ("Pas plus de 3 mots dans le titre. Mentionne « Paris ».", "titre"),
        ("3 mots ou moins dans le titre. Mentionne « Paris ».", "titre"),
        ('No more than 3 words in the title. Include "Paris".', "title"),
    ):
        compiled = compile_prompt(text.encode())
        # The term is checked, and the limit is still proposed under the part it names.
        assert proposals(compiled) == [
            ("u1-contains-1", "mechanical", "contains", "Paris", None),
            ("u1-max-words", "candidate", "max_words", 3, word),
        ], text
    # A denial of its own, standing outside every recognized wording, still denies the
    # term: a confirmation must never be able to invert what the prompt says.
    for text in (
        "N'utilise pas au plus 3 mots dans le titre. Mentionne « Paris ».",
        "Au plus 3 mots dans le titre, ce n'est pas acceptable. Mentionne « Paris ».",
    ):
        denied = compile_prompt(text.encode())
        assert all(entry[2] != "contains" for entry in proposals(denied)), text
    # An alternative of its own still proposes the term under that word.
    beside = compile_prompt("Soit Paris soit Lyon. Mentionne « Paris ».".encode())
    assert proposals(beside) == [("u1-contains-1", "candidate", "contains", "Paris", "Soit")]


def test_too_many_units_are_refused_before_any_contract_exists():
    from skillflow_prompts.prompts import MAX_UNITS, TooLarge

    with pytest.raises(TooLarge):
        compile_prompt(("x\n" * (MAX_UNITS + 1)).encode())
    assert len(compile_prompt(("x\n" * MAX_UNITS).encode())["units"]) == MAX_UNITS


def test_two_incompatible_limits_in_one_unit_refuse_the_compilation():
    with pytest.raises(Conflict) as exc:
        compile_prompt("Réponds en 40 mots maximum, maximum 20 mots.\n".encode())
    assert exc.value.unit["n"] == 1 and "20 vs 40" in exc.value.detail


def test_a_prompt_without_mechanical_rule_still_checks_a_non_empty_answer():
    compiled = compile_prompt(b"Sois chaleureux.\n")
    obligations = compiled["contract"]["steps"][0]["obligations"]
    assert [o["id"] for o in obligations] == ["answer-present"]
    assert [e["class"] for u in compiled["units"] for e in u["obligations"]] == ["semantic"]


@pytest.mark.parametrize(
    ("text", "count"),
    [
        ("", 0),
        ("un deux trois", 3),
        ("l'été porte-monnaie", 2),
        ("3,5 kilos", 2),
        ("voir https://exemple.fr/a/b", 2),
        # Punctuation-only runs are not words: an em dash and an ellipsis.
        ("fin " + chr(0x2014) + " " + chr(0x2026) + " !", 1),
        # Invisible fillers (braille blank, hangul filler) separate words.
        ("un" + chr(0x2800) + "deux" + chr(0x3164) + "trois", 3),
        # Zero-width space, zero-width joiner and word joiner separate words too.
        ("collé" + chr(0x200B) + "par" + chr(0x200D) + "un" + chr(0x2060) + "joint", 4),
        # Emoji, with a skin tone modifier, are not words.
        (chr(0x1F600) + " " + chr(0x1F44D) + chr(0x1F3FD), 0),
        ("ligne\nautre\tligne", 3),
    ],
)
def test_word_definition(text, count):
    assert words(text) == count


def test_max_words_passes_at_the_limit_and_fails_above():
    obligation = {"verifier": "text", "parameters": {"max_words": 3}}
    assert check(obligation, {"message": b"un deux trois"}) == {
        "passed": True,
        "missing_terms": [],
        "length": 13,
        "words": 3,
    }
    assert not check(obligation, {"message": b"un deux trois quatre"})["passed"]
    legacy = {"verifier": "text", "parameters": {"contains": ["x"]}}
    assert "words" not in check(legacy, {"message": b"x"})


@pytest.mark.parametrize("value", [0, -1, "10", True, 2.5])
def test_max_words_must_be_a_positive_integer(value):
    contract = compile_prompt("Réponds en 40 mots maximum.\n".encode())["contract"]
    contract["steps"][0]["obligations"][0]["parameters"]["max_words"] = value
    with pytest.raises(ValidationError):
        Contract.model_validate(contract)

"""The ChatGPT launcher run exactly as ChatGPT runs it: every step is a separate process."""

import base64
import codecs
import json
import os
import subprocess
import sys
import unicodedata
from io import BytesIO
from pathlib import Path

import pytest

from skillflow_prompts.chatgpt import MAX_REWRITTEN_FACTOR
from skillflow_prompts.util import canonical, sha256

PROMPT = (
    "Réponds en 40 mots maximum.\n"
    "Mentionne le mot « SkillFlow ».\n"
    "Termine par une question ouverte.\n"
)
WRONG = "Voici une réponse courte qui oublie le nom de l'outil. Elle reste brève. Et toi ?"
RIGHT = "SkillFlow contrôle chaque consigne mesurable avant la remise. Elle reste brève. Et toi ?"
# An answer written on several lines: only then do line ends exist to be changed.
LINES = "SkillFlow contrôle chaque consigne mesurable.\nElle reste brève.\nEt toi ?"
# The one-minute trial CHATGPT.md asks for right after `init`, word for word.
TRIAL_PROMPT = "Réponds en 12 mots maximum.\nMentionne « SkillFlow ».\n"
TRIAL_ANSWER = "SkillFlow fonctionne.\n"
# A prompt whose rules the compiler sets aside: "résumé" names a part of the answer.
SCOPED_PROMPT = 'Rédige un résumé de ce texte.\nAu maximum 120 mots.\nMentionne "hypnose".\n'
TOO_LONG = "bavarde " * 130


def chatgpt(*args):
    done = subprocess.run(
        [sys.executable, "-m", "skillflow_prompts", "chatgpt", *map(str, args)],
        capture_output=True,
        timeout=180,
        check=False,
    )
    # Exactly one JSON object on stdout, whatever the outcome.
    return done.returncode, json.loads(done.stdout.decode("utf-8"))


def scenario_files(tmp_path):
    prompt = tmp_path / "prompt.txt"
    # Written with Windows line ends: the contract must not depend on them.
    prompt.write_bytes(PROMPT.replace("\n", "\r\n").encode("utf-8"))
    answer = tmp_path / "answer.txt"
    return prompt, answer


def test_rejection_correction_delivery_and_receipt_across_processes(tmp_path):
    root = tmp_path / "state"
    prompt, answer = scenario_files(tmp_path)
    code, created = chatgpt("init", "--root", root)
    assert code == 0 and created["initialized"]
    code, again = chatgpt("init", "--root", root)
    assert code == 0 and not again["initialized"] and again["key_id"] == created["key_id"]

    code, compiled = chatgpt("prompt", "--root", root, "--file", prompt)
    assert code == 0, compiled
    # Unit 2 asks for a word: the substring check is mechanical, the word is not checked.
    assert [(r["unit"], r["kind"]) for r in compiled["units"]] == [
        (1, "mechanical"),
        (2, "mechanical"),
        (2, "semantic"),
        (3, "semantic"),
    ]

    answer.write_text(WRONG, encoding="utf-8")
    code, rejected = chatgpt("answer", "--root", root, "--file", answer)
    rows = {row["id"]: row for row in rejected["rows"]}
    assert code == 1 and not rejected["ok"]
    assert rows["u2-contains-1"]["status"] == "REJETE"
    assert "SkillFlow" in rows["u2-contains-1"]["detail"]
    assert rows["u1-max-words"]["status"] == "VERIFIE"
    assert rows["u3-sens"]["status"] == "NON_VERIFIE"
    assert rejected["rounds_used"] == 1

    # The same rejected bytes again: refused without consuming a round.
    code, same = chatgpt("answer", "--root", root, "--file", answer)
    assert code == 1 and same["rounds_used"] == 1
    assert same["rounds_remaining"] == rejected["rounds_remaining"]

    code, early = chatgpt("deliver", "--root", root)
    assert code == 1 and early["error"]["code"] == "DELIVERY_REFUSED"

    answer.write_text(RIGHT, encoding="utf-8")
    code, accepted = chatgpt("answer", "--root", root, "--file", answer)
    assert code == 0, accepted
    assert accepted["counts"]["REJETE"] == 0 and accepted["counts"]["VERIFIE"] == 2
    assert accepted["rounds_used"] == 2

    # A new process reads the state back from disk.
    code, status = chatgpt("status", "--root", root)
    assert code == 0 and status["current"]["rounds_used"] == 2

    code, delivered = chatgpt("deliver", "--root", root)
    assert code == 0, delivered
    folder = Path(delivered["receipt_folder"])
    expected = {"answer.txt", "receipt.json", "RECU.md", "prompt.txt", "contract-prompt.json"}
    assert expected <= set(delivered["files"])
    assert "| 3 | Termine par une question ouverte. | sens | NON_VERIFIE |" in delivered["receipt"]
    assert "FICTIVE" in delivered["receipt"] and "NOT_RUN" in delivered["receipt"]
    assert (folder / "answer.txt").read_bytes() == RIGHT.encode("utf-8")

    code, twice = chatgpt("deliver", "--root", root)
    assert code == 0 and twice["already_delivered"]

    receipt = folder / "receipt.json"
    contract = folder / "contract-prompt.json"
    code, verified = chatgpt(
        "verify", "--receipt", receipt, "--answer", folder / "answer.txt",
        "--contract", contract, "--root", root,
    )  # fmt: skip
    assert code == 0, verified
    assert verified["checks"]["key_anchored"] is True and verified["checks"]["statuses"]
    code, loose = chatgpt("verify", "--receipt", receipt, "--answer", folder / "answer.txt")
    assert code == 0 and loose["checks"]["key_anchored"] is None
    assert "non ancrée" in loose["summary"]

    changed = tmp_path / "changed.txt"
    data = bytearray((folder / "answer.txt").read_bytes())
    data[0] ^= 1
    changed.write_bytes(bytes(data))
    code, tampered = chatgpt("verify", "--receipt", receipt, "--answer", changed)
    assert code == 1 and tampered["error"]["code"] == "ANSWER_CHANGED"

    envelope = json.loads(receipt.read_bytes())
    payload = json.loads(base64.b64decode(envelope["payload"]))
    payload["units"][-1]["status"] = "VERIFIE"
    envelope["payload"] = base64.b64encode(canonical(payload)).decode()
    edited = tmp_path / "edited.json"
    edited.write_bytes(canonical(envelope))
    code, forged = chatgpt("verify", "--receipt", edited, "--answer", folder / "answer.txt")
    assert code == 1 and forged["error"]["code"] == "RECEIPT_INVALID"

    weaker = json.loads(contract.read_bytes())
    weaker["steps"][0]["obligations"][0]["parameters"]["max_words"] = 400
    altered = tmp_path / "altered-contract.json"
    altered.write_bytes(canonical(weaker))
    code, swapped = chatgpt(
        "verify", "--receipt", receipt, "--answer", folder / "answer.txt", "--contract", altered
    )
    assert code == 1 and swapped["error"]["code"] == "CONTRACT_CHANGED"


def deliver_right_answer(tmp_path, root):
    """Open the standard prompt, submit the right answer and deliver, in process."""
    from skillflow_prompts import chatgpt as launcher

    prompt, answer = scenario_files(tmp_path)
    answer.write_text(RIGHT, encoding="utf-8")
    for argv in (
        ["init", "--root", root],
        ["prompt", "--root", root, "--file", prompt],
        ["answer", "--root", root, "--file", answer],
        ["deliver", "--root", root],
    ):
        code, result = launcher.run([str(a) for a in argv])
        assert code == 0, result
    return prompt, Path(result["receipt_folder"])


def resign(folder, mutate, key=None):
    """Re-sign the delivered receipt after `mutate(payload)`, with the root key by default."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from skillflow_prompts import receipts

    envelope = json.loads((folder / "receipt.json").read_bytes())
    payload = json.loads(base64.b64decode(envelope["payload"]))
    mutate(payload)
    key = key or Ed25519PrivateKey.from_private_bytes(
        (folder.parents[1] / "signing.key").read_bytes()
    )
    return canonical(receipts.sign(payload, key))


def test_the_authority_not_a_state_file_decides_what_the_answer_is(tmp_path):
    """A command killed between the submission and its bookkeeping must not mislead."""
    from skillflow_prompts import chatgpt as launcher

    root = tmp_path / "state"
    prompt, answer = scenario_files(tmp_path)
    answer.write_text(RIGHT, encoding="utf-8")
    assert chatgpt("init", "--root", root)[0] == 0
    assert chatgpt("prompt", "--root", root, "--file", answer.with_name("prompt.txt"))[0] == 0
    code, first = chatgpt("answer", "--root", root, "--file", answer)
    assert code == 0 and first["rounds_used"] == 1
    # Another answer reaches the authority without the tool's bookkeeping (a crash).
    other = RIGHT.replace("brève", "courte").encode("utf-8")
    session = launcher.Session(root)
    status = session.admit(session.state["current"]["name"])
    for skill in status["skills"]:
        session.call(
            "writer",
            "message_submit",
            {
                "case_id": status["case_id"],
                "invocation_id": skill["invocation_id"],
                "content_base64": base64.b64encode(other).decode(),
            },
        )
    # The same bytes as the first answer are a change again: they are resubmitted.
    code, again = chatgpt("answer", "--root", root, "--file", answer)
    assert code == 0 and again["rounds_used"] == 3
    code, delivered = chatgpt("deliver", "--root", root)
    assert code == 0
    assert (Path(delivered["receipt_folder"]) / "answer.txt").read_bytes() == RIGHT.encode()
    assert "Tours utilisés : 3" in delivered["receipt"]


def test_the_budget_refuses_a_fourth_answer_instead_of_inviting_a_retry(tmp_path):
    root = tmp_path / "state"
    prompt, answer = scenario_files(tmp_path)
    assert chatgpt("init", "--root", root)[0] == 0
    assert chatgpt("prompt", "--root", root, "--file", prompt)[0] == 0
    for attempt in range(3):
        answer.write_text(WRONG + " " * attempt, encoding="utf-8")
        code, rejected = chatgpt("answer", "--root", root, "--file", answer)
        assert code == 1 and rejected["counts"]["REJETE"] == 1
    assert rejected["rounds_remaining"] == 0
    answer.write_text(RIGHT, encoding="utf-8")
    code, spent = chatgpt("answer", "--root", root, "--file", answer)
    assert code == 1 and spent["error"]["code"] == "BUDGET_EXHAUSTED"
    assert spent["details"]["rounds_used"] == 3
    code, refused = chatgpt("deliver", "--root", root)
    assert code == 1 and refused["error"]["code"] == "DELIVERY_REFUSED"
    code, status = chatgpt("status", "--root", root)
    assert status["current"]["rounds_remaining"] == 0
    assert "relance" not in rejected["summary"] and "épuisé" in rejected["summary"]


def test_an_accepted_answer_survives_a_late_attempt_with_no_budget_left(tmp_path):
    root = tmp_path / "state"
    prompt, answer = scenario_files(tmp_path)
    assert chatgpt("init", "--root", root)[0] == 0
    assert chatgpt("prompt", "--root", root, "--file", prompt)[0] == 0
    for attempt in range(2):
        answer.write_text(WRONG + " " * attempt, encoding="utf-8")
        assert chatgpt("answer", "--root", root, "--file", answer)[0] == 1
    answer.write_text(RIGHT, encoding="utf-8")
    code, accepted = chatgpt("answer", "--root", root, "--file", answer)
    assert code == 0 and accepted["rounds_remaining"] == 0
    # One more answer would invalidate the accepted one with no check left: refused before
    # anything reaches the authority, so the accepted bytes stay deliverable.
    answer.write_text(RIGHT + " Oui.", encoding="utf-8")
    code, late = chatgpt("answer", "--root", root, "--file", answer)
    assert code == 1 and late["error"]["code"] == "BUDGET_EXHAUSTED"
    code, delivered = chatgpt("deliver", "--root", root)
    assert code == 0, delivered
    assert (Path(delivered["receipt_folder"]) / "answer.txt").read_bytes() == RIGHT.encode()


def test_a_prompt_with_too_many_lines_is_refused_before_any_case_opens(tmp_path):
    root = tmp_path / "state"
    assert chatgpt("init", "--root", root)[0] == 0
    prompt = tmp_path / "long.txt"
    prompt.write_text("x\n" * 90_000, encoding="utf-8")
    code, refused = chatgpt("prompt", "--root", root, "--file", prompt)
    assert code == 2 and refused["error"]["code"] == "PROMPT_TOO_LARGE"
    code, status = chatgpt("status", "--root", root)
    assert status["current"] is None
    # One line, 50 000 distinct terms: each obligation would repeat that line, so the
    # contract would weigh gigabytes. Refused on the same ground, and just as early.
    crowded = tmp_path / "crowded.txt"
    crowded.write_text(" ".join(f'Mention "t{index:05d}".' for index in range(50_000)))
    assert crowded.stat().st_size < 1_000_000
    code, refused = chatgpt("prompt", "--root", root, "--file", crowded)
    assert code == 2 and refused["error"]["code"] == "PROMPT_TOO_LARGE"
    code, status = chatgpt("status", "--root", root)
    assert status["current"] is None


def test_verify_recomputes_every_figure_it_shows(tmp_path):
    from skillflow_prompts import chatgpt as launcher

    root = tmp_path / "state"
    prompt, folder = deliver_right_answer(tmp_path, root)

    # Each figure is falsified on its own: one lie is enough, and none of them is ever
    # rendered back to the reader.
    for mutate, failing, trace in (
        (lambda p: p.update(answer_words=123456), "metadata", "123456"),
        (lambda p: p.update(answer_start="début inventé"), "metadata", "début inventé"),
        (lambda p: p.update(answer_end="fin inventée"), "metadata", "fin inventée"),
        # The rendered receipt shortens a digest, so the falsified prefix is what must
        # be absent from it.
        (lambda p: p.update(prompt_sha256="0" * 64), "prompt", "0" * 16),
        (lambda p: p["contracts"][0].update(compiled_by="human"), "contracts", "human"),
        (lambda p: p["contracts"][0].update(ref="bogus@1.0.0"), "contracts", "bogus"),
    ):
        liar = tmp_path / "liar.json"
        liar.write_bytes(resign(folder, mutate))
        code, result = launcher.run(
            ["verify", "--receipt", str(liar), "--answer",
             str(folder / "answer.txt"), "--root", str(root)]
        )  # fmt: skip
        assert code == 1, trace
        assert result["checks"]["signature"] and result["checks"]["key_anchored"]
        assert result["checks"][failing] is False, trace
        assert trace not in result["receipt"], trace
    # The original prompt file anchors the prompt digest; another file does not.
    code, result = launcher.run(
        ["verify", "--receipt", str(folder / "receipt.json"), "--answer",
         str(folder / "answer.txt"), "--prompt", str(prompt)]
    )  # fmt: skip
    assert code == 0 and result["checks"]["prompt_file"] is True
    code, result = launcher.run(
        ["verify", "--receipt", str(folder / "receipt.json"), "--answer",
         str(folder / "answer.txt"), "--prompt", str(folder / "answer.txt")]
    )  # fmt: skip
    assert code == 1 and result["checks"]["prompt_file"] is False


def test_a_second_delivery_shows_a_reverified_receipt_not_a_file(tmp_path):
    from skillflow_prompts import chatgpt as launcher

    root = tmp_path / "state"
    _, folder = deliver_right_answer(tmp_path, root)
    (folder / "RECU.md").write_text("| 3 | ... | sens | VERIFIE | faux |", encoding="utf-8")
    code, again = launcher.run(["deliver", "--root", str(root)])
    assert code == 0 and again["already_delivered"]
    assert "| sens | VERIFIE |" not in again["receipt"]
    assert "| sens | NON_VERIFIE |" in again["receipt"]
    (folder / "receipt.json").write_bytes(resign(folder, lambda p: p.update(rounds_used=99)))
    code, again = launcher.run(["deliver", "--root", str(root)])
    assert code == 0 and "Tours utilisés : 99" in again["receipt"]
    (folder / "receipt.json").write_bytes(b"{}")
    code, broken = launcher.run(["deliver", "--root", str(root)])
    assert code == 1 and broken["error"]["code"] == "RECEIPT_CORRUPT"


def test_a_large_prompt_still_yields_a_verifiable_receipt_and_safe_cells(tmp_path):
    from skillflow_prompts import chatgpt as launcher

    root = tmp_path / "state"
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Contexte " + "x" * 600_000 + "\nMentionne « A | B ».\n", encoding="utf-8")
    answer = tmp_path / "answer.txt"
    answer.write_text("A | B", encoding="utf-8")
    for argv in (
        ["init", "--root", root],
        ["prompt", "--root", root, "--file", prompt],
        ["answer", "--root", root, "--file", answer],
        ["deliver", "--root", root],
    ):
        code, result = launcher.run([str(a) for a in argv])
        assert code == 0, result
    folder = Path(result["receipt_folder"])
    assert "| contient « A \\| B » |" in result["receipt"]
    # The exported contract is larger than any prompt may be; verify must still read it.
    assert (folder / "contract-prompt.json").stat().st_size > launcher.MAX_INPUT
    code, verified = launcher.run(
        [
            "verify",
            "--receipt",
            str(folder / "receipt.json"),
            "--answer",
            str(folder / "answer.txt"),
            "--contract",
            str(folder / "contract-prompt.json"),
        ]
    )
    assert code == 0, verified


def test_an_obligation_written_next_to_a_limit_is_never_counted_as_verified(tmp_path):
    """A number, a term, a symbol or a word next to a limit keeps the unit unverified.

    Every family of leftover, carried from `prompt` to `verify`: the answer is accepted
    because the limit holds, and the receipt still says the rest is not checked.
    """
    from skillflow_prompts import chatgpt as launcher

    cases = (
        ("Réponds en 40 mots maximum. Écris 7.", "Bonjour."),
        ("Réponds en 40 mots maximum. Écris « A ».", "Bonjour."),
        ("Réponds en 40 mots maximum en EN.", "Bonjour."),
        ("Réponds en 40 mots maximum. Écris : !!!", "Bonjour."),
        ("Réponds en 40 mots maximum. Écris le mot texte.", "Bonjour."),
        ("Réponds en 40 mots maximum. Utilise " + chr(0x1F642) + ".", "Bonjour."),
        # A restriction made of ordinary words in front of the limit stays visible too.
        ("Un mot max. 40 mots maximum.", "Bonjour tout le monde."),
        ("Mentionne « Paris ». Un mot max. 40 mots maximum.", "Paris est une ville."),
        ("Un mot max. 40 mots maximum et mentionne « Paris ».", "Paris est une ville."),
    )
    for index, (line, reply) in enumerate(cases):
        root = tmp_path / f"state{index}"
        prompt = tmp_path / f"prompt{index}.txt"
        prompt.write_text(line + "\n", encoding="utf-8")
        answer = tmp_path / f"answer{index}.txt"
        answer.write_text(reply, encoding="utf-8")
        # The first family goes through separate processes, as ChatGPT runs them; the
        # others share this process, which keeps the suite short.
        run = chatgpt if index == 0 else (lambda *a: launcher.run([str(x) for x in a]))
        for argv in (
            ["init", "--root", root],
            ["prompt", "--root", root, "--file", prompt],
            ["answer", "--root", root, "--file", answer],
            ["deliver", "--root", root],
        ):
            code, result = run(*argv)
            assert code == 0, result
        folder = Path(result["receipt_folder"])
        assert result["counts"]["NON_VERIFIE"] == 1, line
        code, verified = run(
            "verify", "--receipt", folder / "receipt.json", "--answer",
            folder / "answer.txt", "--prompt", prompt, "--contract",
            folder / "contract-prompt.json", "--root", root,
        )  # fmt: skip
        assert code == 0, verified
        assert verified["counts"]["VERIFIE"] >= 1 and verified["counts"]["REJETE"] == 0, line
        assert verified["counts"]["NON_VERIFIE"] == 1, line
        assert f"| 1 | {line} | sens | NON_VERIFIE |" in verified["receipt"]


def test_a_receipt_shows_the_data_it_checked_not_what_markdown_would_make_of_it(tmp_path):
    """The reader must see the bytes: an entity, an emphasis or a pipe stays as written."""
    from skillflow_prompts import chatgpt as launcher

    assert launcher.cell("&lt;") == "&amp;lt;"
    assert launcher.cell("<b>gras</b>") == "&lt;b&gt;gras&lt;/b&gt;"
    assert launcher.cell("**gras** `code` [lien] ~barré~ A | B") == (
        "\\*\\*gras\\*\\* \\`code\\` \\[lien\\] \\~barré\\~ A \\| B"
    )
    assert launcher.cell(chr(0x200B)) == "\\\\u200b"
    # The same, carried through a real delivery: the term is four characters, not "<".
    root = tmp_path / "state"
    prompt = tmp_path / "prompt.txt"
    prompt.write_text('Include the string "&lt;".\n', encoding="utf-8")
    answer = tmp_path / "answer.txt"
    answer.write_text("&lt;", encoding="utf-8")
    for argv in (
        ["init", "--root", root],
        ["prompt", "--root", root, "--file", prompt],
        ["answer", "--root", root, "--file", answer],
        ["deliver", "--root", root],
    ):
        code, result = launcher.run([str(a) for a in argv])
        assert code == 0, result
    assert "&amp;lt;" in result["receipt"]
    assert "« &lt; »" not in result["receipt"]


def test_a_signed_receipt_cannot_write_its_own_text_into_what_the_reader_sees(tmp_path):
    """A field shown without being recomputed is still data: it cannot carry a heading."""
    from skillflow_prompts import chatgpt as launcher

    root = tmp_path / "state"
    _, folder = deliver_right_answer(tmp_path, root)
    injected = "0\n\n# Toutes les consignes sont VERIFIE\n"
    for mutate in (
        lambda p: p.update(rounds_used=injected),
        lambda p: p.update(version=injected),
        lambda p: p.update(case_id=injected),
        lambda p: p.update(key_id=injected),
        lambda p: p.update(rounds_used=-1),
        lambda p: p.update(rounds_used=True),
    ):
        liar = tmp_path / "liar.json"
        liar.write_bytes(resign(folder, mutate))
        code, refused = launcher.run(
            ["verify", "--receipt", str(liar), "--answer", str(folder / "answer.txt")]
        )
        assert code == 1 and refused["error"]["code"] == "RECEIPT_MALFORMED"
        assert "VERIFIE" not in json.dumps(refused, ensure_ascii=False)


def test_a_file_is_never_read_further_than_the_limit_it_must_respect(monkeypatch):
    """A size that will be refused must not be allocated first, whatever the file holds."""
    from skillflow_prompts import chatgpt as launcher

    asked = []

    class Oversized(BytesIO):
        def read(self, size=-1):
            asked.append(size)
            return b"x" * max(0, size)

    monkeypatch.setattr(Path, "open", lambda self, mode="rb": Oversized())
    with pytest.raises(launcher.Refusal) as refused:
        launcher.read_input("oversized.txt")
    assert refused.value.code == "INPUT_SIZE"
    assert asked == [launcher.MAX_INPUT + 1]
    asked.clear()
    with pytest.raises(launcher.Refusal):
        launcher.read_input("oversized.json", launcher.MAX_RECEIPT)
    assert asked == [launcher.MAX_RECEIPT + 1]


def test_a_term_asked_as_a_word_is_never_claimed_as_fully_checked(tmp_path):
    """A substring is not a word, and a quoted term keeps the spaces it was written with."""
    from skillflow_prompts import chatgpt as launcher

    cases = (
        # Accepted on the characters, and the receipt still says the word is not checked.
        ("Mentionne le mot « chat ».", "achat", 0),
        ('Include the word "cat".', "concatenate", 0),
        ("Mentionne le mot « Paris ».", "Parisien", 0),
        # The two spaces asked for between the ASCII quotes are part of the term.
        ('Include the string " x ".', "x", 1),
    )
    for index, (line, reply, expected) in enumerate(cases):
        root = tmp_path / f"state{index}"
        prompt = tmp_path / f"prompt{index}.txt"
        prompt.write_text(line + "\n", encoding="utf-8")
        answer = tmp_path / f"answer{index}.txt"
        answer.write_text(reply, encoding="utf-8")
        for argv in (["init", "--root", root], ["prompt", "--root", root, "--file", prompt]):
            code, result = launcher.run([str(a) for a in argv])
            assert code == 0, result
        code, judged = launcher.run(["answer", "--root", str(root), "--file", str(answer)])
        assert code == expected, judged
        if expected:
            assert judged["counts"]["REJETE"] == 1, line
            continue
        assert judged["counts"]["NON_VERIFIE"] == 1 and judged["counts"]["VERIFIE"] == 1, line
        code, delivered = launcher.run(["deliver", "--root", str(root)])
        assert code == 0, delivered
        folder = Path(delivered["receipt_folder"])
        code, verified = launcher.run(
            ["verify", "--receipt", str(folder / "receipt.json"), "--answer",
             str(folder / "answer.txt"), "--prompt", str(prompt), "--contract",
             str(folder / "contract-prompt.json"), "--root", str(root)]
        )  # fmt: skip
        assert code == 0, verified
        assert verified["counts"]["NON_VERIFIE"] == 1, line
        assert f"| 1 | {line} | sens | NON_VERIFIE |" in verified["receipt"]


def test_two_limits_in_one_line_are_refused_before_any_case_opens(tmp_path):
    root = tmp_path / "state"
    assert chatgpt("init", "--root", root)[0] == 0
    prompt = tmp_path / "conflict.txt"
    prompt.write_text("Réponds en 40 mots maximum, maximum 20 mots.\n", encoding="utf-8")
    code, refused = chatgpt("prompt", "--root", root, "--file", prompt)
    assert code == 1 and refused["error"]["code"] == "CONTRACT_CONFLICT"
    assert refused["details"]["unit"]["n"] == 1
    code, status = chatgpt("status", "--root", root)
    assert status["current"] is None


def test_a_receipt_signed_with_another_key_cannot_raise_a_status(tmp_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from skillflow_prompts import chatgpt as launcher
    from skillflow_prompts import receipts

    root = tmp_path / "state"
    prompt, answer = scenario_files(tmp_path)
    answer.write_text(RIGHT, encoding="utf-8")
    for argv in (
        ["init", "--root", root],
        ["prompt", "--root", root, "--file", prompt],
        ["answer", "--root", root, "--file", answer],
        ["deliver", "--root", root],
    ):
        code, result = launcher.run([str(a) for a in argv])
        assert code == 0, result
    folder = Path(result["receipt_folder"])
    payload = json.loads(
        base64.b64decode(json.loads((folder / "receipt.json").read_bytes())["payload"])
    )
    # A sandbox can always sign anything with a fresh key: claim that the rule of meaning
    # was verified.
    key = Ed25519PrivateKey.generate()
    payload["units"][-1]["status"] = "VERIFIE"
    payload["public_key"] = base64.b64encode(key.public_key().public_bytes_raw()).decode()
    forged = tmp_path / "forged.json"
    forged.write_bytes(canonical(receipts.sign(payload, key)))
    code, result = launcher.run(
        ["verify", "--receipt", str(forged), "--answer", str(folder / "answer.txt")]
    )
    assert code == 1 and result["error"]["code"] == "COMPLETION_INVALID"
    # Re-signing the authority completion too makes the forgery consistent: the recomputed
    # statuses and the trust anchor still refuse it.
    payload["completion"] = receipts.sign(
        json.loads(base64.b64decode(payload["completion"]["payload"])), key
    )
    forged.write_bytes(canonical(receipts.sign(payload, key)))
    code, result = launcher.run(
        [
            "verify",
            "--receipt",
            str(forged),
            "--answer",
            str(folder / "answer.txt"),
            "--root",
            str(root),
        ]
    )
    assert code == 1 and result["checks"]["statuses"] is False
    # The anchor is the key that verified the signature, not the identifier it claims.
    assert result["checks"]["key_anchored"] is False
    assert result["checks"]["key_consistent"] is False


def test_the_launcher_never_starts_a_process(tmp_path, monkeypatch):
    from skillflow_prompts import chatgpt as launcher

    def forbidden(*args, **kwargs):
        raise AssertionError("the launcher started a child process")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    root = tmp_path / "state"
    prompt, answer = scenario_files(tmp_path)
    answer.write_text(RIGHT, encoding="utf-8")
    for argv in (
        ["init", "--root", root],
        ["prompt", "--root", root, "--file", prompt],
        ["answer", "--root", root, "--file", answer],
        ["deliver", "--root", root],
        ["status", "--root", root],
    ):
        code, result = launcher.run([str(a) for a in argv])
        assert code == 0, result
    folder = Path(result["last_receipt"])
    code, result = launcher.run(
        [
            "verify",
            "--receipt",
            str(folder / "receipt.json"),
            "--answer",
            str(folder / "answer.txt"),
        ]
    )
    assert code == 0, result


def test_usage_errors_are_one_json_object_with_exit_code_2(tmp_path):
    code, relative = chatgpt("init", "--root", "relative-state")
    assert code == 2 and relative["error"]["code"] == "ROOT_RELATIVE"
    code, missing = chatgpt("prompt", "--root", tmp_path / "state")
    assert code == 2 and missing["error"]["code"] == "USAGE"
    code, unknown = chatgpt("launch", "--root", tmp_path / "state")
    assert code == 2 and unknown["error"]["code"] == "USAGE"
    code, absent = chatgpt("status", "--root", tmp_path / "nothing-here")
    assert code == 1 and absent["error"]["code"] == "NOT_INITIALIZED"
    root = tmp_path / "state"
    assert chatgpt("init", "--root", root)[0] == 0
    latin = tmp_path / "latin1.txt"
    latin.write_bytes("Réponse".encode("latin-1"))
    code, early = chatgpt("answer", "--root", root, "--file", latin)
    assert code == 1 and early["error"]["code"] == "NO_PROMPT"
    code, decoded = chatgpt("prompt", "--root", root, "--file", latin)
    assert code == 2 and decoded["error"]["code"] == "NOT_UTF8"
    code, unreadable = chatgpt("prompt", "--root", root, "--file", tmp_path / "absent.txt")
    assert code == 2 and unreadable["error"]["code"] == "INPUT_UNREADABLE"
    code, helped = chatgpt("--help")
    assert code == 0 and "init" in helped["usage"]


def test_verify_accepts_the_same_answer_written_again_and_says_so(tmp_path):
    """What an editor changes on its own is not another answer: line ends, composition, BOM.

    The file is accepted and `answer_rewritten` says so, with the summary. Every
    figure of the receipt is then recomputed from the normalized bytes, so the receipt
    shown is word for word the one a strict reading gives. Any other byte still fails.
    """
    root = tmp_path / "state"
    prompt, answer = scenario_files(tmp_path)
    assert chatgpt("init", "--root", root)[0] == 0
    assert chatgpt("prompt", "--root", root, "--file", prompt)[0] == 0
    answer.write_text(LINES, encoding="utf-8")
    code, accepted = chatgpt("answer", "--root", root, "--file", answer)
    assert code == 0, accepted
    code, delivered = chatgpt("deliver", "--root", root)
    assert code == 0, delivered
    folder = Path(delivered["receipt_folder"])
    receipt, receipted = folder / "receipt.json", folder / "answer.txt"
    raw = receipted.read_bytes()
    # The receipted bytes are the normalized ones: that is what makes the rest decidable.
    assert b"\r" not in raw and raw == unicodedata.normalize("NFC", raw.decode("utf-8")).encode()
    code, strict = chatgpt("verify", "--receipt", receipt, "--answer", receipted, "--root", root)
    assert code == 0 and strict["checks"]["answer"] is True
    assert "normalisation" not in strict["summary"]

    written_again = {
        "fins de ligne Windows": raw.replace(b"\n", b"\r\n"),
        "composition decomposee": unicodedata.normalize("NFD", raw.decode("utf-8")).encode(),
        "marque d ordre des octets": codecs.BOM_UTF8 + raw,
    }
    again = tmp_path / "again.txt"
    # The line that states what is attested: digest, word count, first and last words.
    attested = next(
        line for line in strict["receipt"].split("\n") if line.startswith("Réponse contrôlée")
    )
    for name, data in written_again.items():
        assert data != raw, name
        again.write_bytes(data)
        code, same = chatgpt("verify", "--receipt", receipt, "--answer", again, "--root", root)
        assert code == 0, (name, same)
        assert same["checks"]["answer"] is True and same["answer_rewritten"] is True, name
        # The receipt shown is the only artefact a reader is asked to read: it must say
        # itself that these bytes are not the receipted ones, and name their digest.
        assert same["receipt"] != strict["receipt"], name
        assert "n'est PAS octet pour octet" in same["receipt"], name
        assert sha256(data)[:16] in same["receipt"], name
        assert "normalisation" in same["summary"], name
        # What is counted and classified comes from the attested text, never from the file.
        assert same["counts"] == strict["counts"], name
        assert attested in same["receipt"], name

    broken = tmp_path / "broken.txt"
    for name, data in {
        "un espace ajoute": raw + b" ",
        "une lettre changee": raw.replace(b"SkillFlow", b"SkillFlo0"),
        "des octets qui ne sont pas de l UTF-8": raw + b"\xff",
        # A lone carriage return is not what an editor writes, and it hides the line
        # before it in a terminal: the file would not show the attested text.
        "un retour chariot seul": raw.replace(b"\n", b"\r"),
        "deux marques d ordre des octets": codecs.BOM_UTF8 * 2 + raw,
    }.items():
        assert data != raw, name
        broken.write_bytes(data)
        code, refused = chatgpt("verify", "--receipt", receipt, "--answer", broken, "--root", root)
        assert code == 1, (name, refused)
        assert refused["error"]["code"] == "ANSWER_CHANGED", name


def test_an_answer_at_the_size_limit_can_still_be_written_again(tmp_path):
    """Converting line ends makes a file longer: the largest answer must survive that.

    Without this, the one case the relaxation exists for fails on the size of the file
    instead of its content, at the exact boundary `deliver` is allowed to produce.
    """
    root = tmp_path / "state"
    prompt, answer = scenario_files(tmp_path)
    prompt.write_text("Recopie le texte que je te donne.\n", encoding="utf-8")
    assert chatgpt("init", "--root", root)[0] == 0
    code, compiled = chatgpt("prompt", "--root", root, "--file", prompt)
    assert code == 0, compiled
    # One million bytes exactly, the limit read_input accepts, with line ends to convert.
    body = ("mot " * 24 + "\n") * 11_000
    answer.write_bytes(body[:1_000_000].encode("utf-8"))
    code, accepted = chatgpt("answer", "--root", root, "--file", answer)
    assert code == 0, accepted
    code, delivered = chatgpt("deliver", "--root", root)
    assert code == 0, delivered
    folder = Path(delivered["receipt_folder"])
    receipt, receipted = folder / "receipt.json", folder / "answer.txt"
    raw = receipted.read_bytes()
    assert len(raw) == 1_000_000
    windows = tmp_path / "windows.txt"
    windows.write_bytes(raw.replace(b"\n", b"\r\n"))
    assert windows.stat().st_size > 1_000_000
    code, same = chatgpt("verify", "--receipt", receipt, "--answer", windows, "--root", root)
    assert code == 0, same
    assert same["checks"]["answer"] is True and same["answer_rewritten"] is True


def test_the_largest_answer_survives_being_decomposed(tmp_path):
    """Decomposing is the costliest rewriting: three bytes for one, measured.

    A ceiling set on line ends alone refuses the attested answer itself, with an error
    about the size of a file instead of its content.
    """
    root = tmp_path / "state"
    prompt, answer = scenario_files(tmp_path)
    prompt.write_text("Recopie le texte que je te donne.\n", encoding="utf-8")
    assert chatgpt("init", "--root", root)[0] == 0
    assert chatgpt("prompt", "--root", root, "--file", prompt)[0] == 0
    # Hangul syllables: three bytes composed, nine decomposed. Kept in short words so the
    # receipt does not carry one enormous one.
    text = ("각" * 50 + " ") * 6_622 + "x" * 78
    answer.write_bytes(text.encode("utf-8"))
    assert len(text.encode("utf-8")) == 1_000_000
    code, accepted = chatgpt("answer", "--root", root, "--file", answer)
    assert code == 0, accepted
    code, delivered = chatgpt("deliver", "--root", root)
    assert code == 0, delivered
    folder = Path(delivered["receipt_folder"])
    receipt = folder / "receipt.json"
    decomposed = tmp_path / "decomposed.txt"
    data = unicodedata.normalize("NFD", text).encode("utf-8")
    decomposed.write_bytes(data)
    assert len(data) > 2 * 1_000_000
    code, same = chatgpt("verify", "--receipt", receipt, "--answer", decomposed, "--root", root)
    assert code == 0, same
    assert same["checks"]["answer"] is True and same["answer_rewritten"] is True


def test_the_one_minute_trial_of_chatgpt_md_runs_exactly_as_written(tmp_path):
    """The document promises a trial that proves the install: here it is, really run.

    Its two file contents are checked against `CHATGPT.md` itself, so the document and
    this test cannot drift apart: change one without the other and this fails. A document
    that describes a trial nobody replayed is a promise, and this project does not ship
    promises.
    """
    document = (Path(__file__).resolve().parents[2] / "CHATGPT.md").read_text(encoding="utf-8")
    assert TRIAL_PROMPT.strip() in document
    assert TRIAL_ANSWER.strip() in document

    root = tmp_path / "travail"
    prompt, answer = tmp_path / "essai.txt", tmp_path / "essai-reponse.txt"
    prompt.write_text(TRIAL_PROMPT, encoding="utf-8")
    answer.write_text(TRIAL_ANSWER, encoding="utf-8")
    code, created = chatgpt("init", "--root", root)
    assert code == 0 and created["key_id"], created
    code, compiled = chatgpt("prompt", "--root", root, "--file", prompt)
    assert code == 0, compiled
    assert [row["kind"] for row in compiled["units"]] == ["mechanical", "mechanical"], compiled
    code, accepted = chatgpt("answer", "--root", root, "--file", answer)
    assert code == 0, accepted
    assert accepted["counts"]["VERIFIE"] == 2 and accepted["counts"]["REJETE"] == 0, accepted
    code, delivered = chatgpt("deliver", "--root", root)
    assert code == 0, delivered
    folder = Path(delivered["receipt_folder"])
    assert {"answer.txt", "prompt.txt", "receipt.json", "RECU.md"} <= set(delivered["files"])
    # The verification the document asks me to run at home: no --root, so no anchor that
    # travelled in the same zip as the receipt.
    code, verified = chatgpt(
        "verify", "--receipt", folder / "receipt.json", "--answer", folder / "answer.txt",
        "--prompt", folder / "prompt.txt",
    )  # fmt: skip
    assert code == 0, verified
    assert verified["checks"]["key_anchored"] is None
    assert verified["checks"]["prompt_file"] is True
    assert "non ancrée" in verified["summary"]


def test_an_unconfirmed_candidate_is_shown_and_never_rejects(tmp_path):
    """Said's rule: without a confirmation, an obligation is shown, never REJETE.

    This is held by the contract, not by the rendering: a candidate has no obligation in
    it, so there is nothing the authority could reject the answer on. An answer that
    violates both candidates is accepted, delivered, and verified.
    """
    root = tmp_path / "travail"
    prompt, answer = tmp_path / "prompt.txt", tmp_path / "answer.txt"
    prompt.write_text(SCOPED_PROMPT, encoding="utf-8")
    assert chatgpt("init", "--root", root)[0] == 0
    code, compiled = chatgpt("prompt", "--root", root, "--file", prompt)
    assert code == 0, compiled
    rows = {r["id"]: r for r in compiled["units"]}
    assert rows["u2-max-words"]["kind"] == "candidate"
    assert rows["u2-max-words"]["demoted_by"] == "résumé"
    assert rows["u3-contains-1"]["kind"] == "candidate"
    assert "chatgpt confirm" in compiled["summary"]
    assert compiled["confirmed"] == {} and compiled["confirmed_by"] is None

    answer.write_text(TOO_LONG, encoding="utf-8")
    code, accepted = chatgpt("answer", "--root", root, "--file", answer)
    assert code == 0, accepted
    assert accepted["counts"]["REJETE"] == 0 and accepted["counts"]["CANDIDAT"] == 2
    shown = {row["id"]: row for row in accepted["rows"]}
    assert shown["u2-max-words"]["status"] == "CANDIDAT"
    assert "jamais rejeté" in shown["u2-max-words"]["detail"]
    assert "chatgpt confirm --id u2-max-words" in shown["u2-max-words"]["detail"]
    code, delivered = chatgpt("deliver", "--root", root)
    assert code == 0, delivered
    assert "| CANDIDAT |" in delivered["receipt"]
    assert "aucune décision" in delivered["receipt"]
    folder = Path(delivered["receipt_folder"])
    code, verified = chatgpt(
        "verify", "--receipt", folder / "receipt.json", "--answer", folder / "answer.txt"
    )
    assert code == 0 and verified["counts"]["CANDIDAT"] == 2, verified


def test_a_later_decision_overrides_an_earlier_one_on_the_same_obligation(tmp_path):
    """Confirm, then take it back: the obligation returns to a candidate, and the reverse.

    Only one call naming an obligation on both sides contradicts itself. Decisions made in
    separate calls are a history, and the latest one holds.
    """
    root = tmp_path / "travail"
    prompt = tmp_path / "prompt.txt"
    prompt.write_text(SCOPED_PROMPT, encoding="utf-8")
    assert chatgpt("init", "--root", root)[0] == 0
    assert chatgpt("prompt", "--root", root, "--file", prompt)[0] == 0
    code, raised = chatgpt("confirm", "--root", root, "--by", "said", "--id", "u2-max-words")
    assert code == 0, raised
    assert {r["id"]: r["kind"] for r in raised["units"]}["u2-max-words"] == "mechanical"
    code, lowered = chatgpt("confirm", "--root", root, "--by", "said", "--refuse", "u2-max-words")
    assert code == 0, lowered
    rows = {r["id"]: r for r in lowered["units"]}
    assert rows["u2-max-words"]["kind"] == "candidate"
    # Taken back by name: the refuser is named apart from the compiler's own reason, which
    # stays on record.
    assert rows["u2-max-words"]["refused_by"] == "said"
    assert rows["u2-max-words"]["demoted_by"] == "résumé"
    assert lowered["confirmed"] == {} and lowered["refused"] == {"u2-max-words": "said"}
    code, again = chatgpt("confirm", "--root", root, "--by", "said", "--id", "u2-max-words")
    assert code == 0, again
    assert {r["id"]: r["kind"] for r in again["units"]}["u2-max-words"] == "mechanical"
    assert again["confirmed"] == {"u2-max-words": "said"} and again["refused"] == {}
    # Every reversal opened a new case and named the one it closed. The contract's identity
    # comes from its content: the same decisions give the same contract, so raising it
    # again yields the first contract, not a third one.
    assert raised["cancelled_cases"] and lowered["cancelled_cases"] and again["cancelled_cases"]
    assert raised["contract"] == again["contract"]
    assert lowered["contract"] != raised["contract"]


def test_restoring_a_check_someone_refused_names_who_restored_it(tmp_path):
    """A check the compiler read, refused by Alice, restored by Bob: the receipt says Bob.

    Restoring the compiler's reading is still an explicit decision, and it is not the
    compiler's any more: without the name, the receipt would show a check nobody owns.
    """
    root = tmp_path / "travail"
    prompt, answer = tmp_path / "prompt.txt", tmp_path / "answer.txt"
    prompt.write_text(TRIAL_PROMPT, encoding="utf-8")
    assert chatgpt("init", "--root", root)[0] == 0
    code, compiled = chatgpt("prompt", "--root", root, "--file", prompt)
    assert (
        code == 0
        and {r["id"]: r["kind"] for r in compiled["units"]}["u1-max-words"] == "mechanical"
    )
    code, refused = chatgpt("confirm", "--root", root, "--by", "alice", "--refuse", "u1-max-words")
    assert code == 0, refused
    rows = {r["id"]: r for r in refused["units"]}
    assert (
        rows["u1-max-words"]["kind"] == "candidate"
        and rows["u1-max-words"]["refused_by"] == "alice"
    )
    code, restored = chatgpt("confirm", "--root", root, "--by", "bob", "--id", "u1-max-words")
    assert code == 0, restored
    rows = {r["id"]: r for r in restored["units"]}
    assert rows["u1-max-words"]["kind"] == "mechanical"
    assert rows["u1-max-words"]["confirmed_by"] == "bob"
    assert restored["confirmed"] == {"u1-max-words": "bob"} and restored["refused"] == {}
    answer.write_text(TRIAL_ANSWER, encoding="utf-8")
    assert chatgpt("answer", "--root", root, "--file", answer)[0] == 0
    code, delivered = chatgpt("deliver", "--root", root)
    assert code == 0 and "confirmé par bob" in delivered["receipt"], delivered


def test_each_obligation_keeps_the_name_of_who_confirmed_it(tmp_path):
    """Alice confirms one obligation, Bob another: the receipt names each, not the last.

    The author travels with the obligation in the signed origin, so a later confirmation by
    someone else renames nothing, and the verification recomputes both names.
    """
    root = tmp_path / "travail"
    prompt, answer = tmp_path / "prompt.txt", tmp_path / "answer.txt"
    prompt.write_text(SCOPED_PROMPT, encoding="utf-8")
    assert chatgpt("init", "--root", root)[0] == 0
    assert chatgpt("prompt", "--root", root, "--file", prompt)[0] == 0
    code, first = chatgpt("confirm", "--root", root, "--by", "alice", "--id", "u2-max-words")
    assert code == 0, first
    code, second = chatgpt("confirm", "--root", root, "--by", "bob", "--id", "u3-contains-1")
    assert code == 0, second
    rows = {r["id"]: r for r in second["units"]}
    assert rows["u2-max-words"]["confirmed_by"] == "alice"
    assert rows["u3-contains-1"]["confirmed_by"] == "bob"
    assert second["confirmed"] == {"u2-max-words": "alice", "u3-contains-1": "bob"}
    assert second["confirmed_by"] == ["alice", "bob"]

    answer.write_text("Bref résumé qui mentionne hypnose.", encoding="utf-8")
    assert chatgpt("answer", "--root", root, "--file", answer)[0] == 0
    code, delivered = chatgpt("deliver", "--root", root)
    assert code == 0, delivered
    assert "décisions déclarées par : alice, bob" in delivered["receipt"]
    assert "confirmé par alice" in delivered["receipt"]
    assert "confirmé par bob" in delivered["receipt"]
    folder = Path(delivered["receipt_folder"])
    code, verified = chatgpt(
        "verify", "--receipt", folder / "receipt.json", "--answer", folder / "answer.txt"
    )
    assert code == 0 and verified["checks"]["contracts"], verified
    assert "confirmé par alice" in verified["receipt"] and "confirmé par bob" in verified["receipt"]


def test_a_confirmation_raises_a_candidate_into_a_check_and_the_receipt_says_who(tmp_path):
    """Confirmed by a named person, a candidate rejects; the receipt names that person.

    A confirmation compiles a new contract: the old case is cancelled and named, the new
    one admitted. It is refused once an answer was checked, and refused on a name the
    prompt does not hold. The verification recomputes with the confirmations read from the
    signed contract, so it agrees without trusting the receipt.
    """
    root = tmp_path / "travail"
    prompt, answer = tmp_path / "prompt.txt", tmp_path / "answer.txt"
    prompt.write_text(SCOPED_PROMPT, encoding="utf-8")
    assert chatgpt("init", "--root", root)[0] == 0
    code, compiled = chatgpt("prompt", "--root", root, "--file", prompt)
    assert code == 0, compiled

    code, unknown = chatgpt("confirm", "--root", root, "--by", "said", "--id", "u9-max-words")
    assert code == 2 and unknown["error"]["code"] == "CONFIRM_UNKNOWN", unknown
    assert "u2-max-words" in unknown["error"]["hint"]
    # The name enters the signed origin line, split on ";": a free-form name would be cut
    # there and the recompiled contract would differ. Only a plain identifier is accepted.
    for name in ("Alice; Bob", "said et moi", "", "a" * 65, "x;confirmed:u3-contains-1"):
        code, bad = chatgpt("confirm", "--root", root, "--by", name, "--id", "u2-max-words")
        assert code == 2 and bad["error"]["code"] == "CONFIRM_BY_INVALID", (name, bad)
    code, both = chatgpt(
        "confirm",
        "--root",
        root,
        "--by",
        "said",
        "--id",
        "u2-max-words",
        "--refuse",
        "u2-max-words",
    )
    assert code == 2 and both["error"]["code"] == "CONFIRM_CONTRADICTION"

    code, confirmed = chatgpt(
        "confirm", "--root", root, "--by", "said", "--id", "u2-max-words", "--id", "u3-contains-1"
    )
    assert code == 0, confirmed
    assert confirmed["cancelled_cases"] == [compiled["case_id"]]
    assert confirmed["case_id"] != compiled["case_id"]
    assert confirmed["contract"] != compiled["contract"]
    rows = {r["id"]: r for r in confirmed["units"]}
    assert rows["u2-max-words"]["kind"] == "mechanical"
    assert rows["u2-max-words"]["confirmed_by"] == "said"
    assert confirmed["confirmed"] == {"u2-max-words": "said", "u3-contains-1": "said"}
    assert confirmed["confirmed_by"] == ["said"]

    answer.write_text(TOO_LONG, encoding="utf-8")
    code, rejected = chatgpt("answer", "--root", root, "--file", answer)
    assert code == 1 and rejected["counts"]["REJETE"] == 2, rejected
    # Too late to change the rules once an answer was checked against them.
    code, late = chatgpt("confirm", "--root", root, "--by", "said", "--refuse", "u2-max-words")
    assert code == 2 and late["error"]["code"] == "CONFIRM_AFTER_ANSWER", late

    answer.write_text("Bref résumé qui mentionne hypnose.", encoding="utf-8")
    code, accepted = chatgpt("answer", "--root", root, "--file", answer)
    assert code == 0 and accepted["counts"]["VERIFIE"] == 2, accepted
    code, delivered = chatgpt("deliver", "--root", root)
    assert code == 0, delivered
    assert "décisions déclarées par : said" in delivered["receipt"]
    assert "confirmé par said" in delivered["receipt"]
    folder = Path(delivered["receipt_folder"])
    code, verified = chatgpt(
        "verify", "--receipt", folder / "receipt.json", "--answer", folder / "answer.txt"
    )
    assert code == 0, verified
    assert verified["checks"]["contracts"] and verified["checks"]["statuses"]
    assert "décisions déclarées par : said" in verified["receipt"]
    # The confirmations live in the signed contract: editing them there breaks the hash.
    contract = json.loads((folder / "contract-prompt.json").read_bytes())
    contract["origin"] = contract["origin"].replace("=said", "=gpt")
    forged = tmp_path / "forged-contract.json"
    forged.write_bytes(canonical(contract))
    code, refused = chatgpt(
        "verify", "--receipt", folder / "receipt.json", "--answer", folder / "answer.txt",
        "--contract", forged,
    )  # fmt: skip
    assert code == 1 and refused["error"]["code"] == "CONTRACT_CHANGED"


def test_an_incomplete_installation_is_refused_before_anything_is_checked(tmp_path, monkeypatch):
    """An install that cannot validate a schema must say so, not crash mid-verification.

    Measured on this machine on 2026-10-13: Windows resolves at most 260 characters per
    path unless long paths are enabled, which they are not here, and the deepest file an
    install creates sits 103 characters below the project folder. Past about 150 characters
    of chosen path, `uv sync --locked` truncates the install **in silence**: data files of
    `jsonschema_specifications` disappear and the crash lands inside `verify`, after the
    reader believed their receipt was being checked. Every action is covered, because the
    verification at home starts at `verify` and never runs `init`.
    """
    from skillflow_prompts import chatgpt as launcher

    def missing(_schema):
        raise FileNotFoundError(2, "No such file or directory", "format-annotation")

    monkeypatch.setattr(launcher.Draft202012Validator, "check_schema", staticmethod(missing))
    root = tmp_path / "travail"
    for argv in (
        ["init", "--root", str(root)],
        [
            "verify",
            "--receipt",
            str(tmp_path / "receipt.json"),
            "--answer",
            str(tmp_path / "a.txt"),
        ],
        ["status", "--root", str(root)],
    ):
        code, refused = launcher.run(argv)
        assert code == 2 and refused["error"]["code"] == "INSTALL_INCOMPLETE", argv
        assert "format-annotation" in refused["error"]["message"], argv
        assert "short path" in refused["error"]["hint"] and "C:\\sfp" in refused["error"]["hint"]
    # The refusal comes before any work: nothing was created, nothing was read.
    assert not root.exists()


def test_a_forbidden_term_rejects_the_answer_that_carries_it(tmp_path):
    """The one denial a machine can settle, run from the real entry point.

    A ban on a term was worth nothing until now: the compiler set it aside and the answer
    was never checked against it. Here the ban is in the contract, the answer that carries
    the term is rejected under its own name, and the corrected one passes -- the loop the
    whole product is for, on a rule it could not express yesterday.
    """
    root = tmp_path / "travail"
    prompt, answer = tmp_path / "prompt.txt", tmp_path / "answer.txt"
    prompt.write_text("Présente la recette.\nNe mentionne pas « beurre ».\n", encoding="utf-8")
    assert chatgpt("init", "--root", root)[0] == 0
    code, compiled = chatgpt("prompt", "--root", root, "--file", prompt)
    assert code == 0, compiled
    rows = {r["id"]: r for r in compiled["units"]}
    assert rows["u2-not-contains-1"]["kind"] == "mechanical"
    assert rows["u2-not-contains-1"]["check"] == "ne contient pas « beurre »"

    answer.write_text("Mélangez la farine et le beurre, puis laissez reposer.", encoding="utf-8")
    code, rejected = chatgpt("answer", "--root", root, "--file", answer)
    assert code == 1 and rejected["counts"]["REJETE"] == 1
    row = next(r for r in rejected["rows"] if r["id"] == "u2-not-contains-1")
    assert row["status"] == "REJETE"
    assert "terme interdit présent" in row["detail"] and "beurre" in row["detail"]

    answer.write_text("Mélangez la farine et le lait, puis laissez reposer.", encoding="utf-8")
    code, accepted = chatgpt("answer", "--root", root, "--file", answer)
    assert code == 0, accepted
    assert accepted["counts"]["REJETE"] == 0
    row = next(r for r in accepted["rows"] if r["id"] == "u2-not-contains-1")
    assert row["status"] == "VERIFIE" and row["detail"] == "terme interdit absent"
    # Delivered and verified again from the receipt alone, like any other obligation.
    code, delivered = chatgpt("deliver", "--root", root)
    assert code == 0, delivered
    folder = Path(delivered["receipt_folder"])
    code, verified = chatgpt(
        "verify", "--receipt", folder / "receipt.json", "--answer", folder / "answer.txt"
    )
    assert code == 0 and verified["ok"] is True
    assert verified["counts"]["VERIFIE"] == 1


def test_a_check_can_be_raised_after_an_answer_but_never_lowered(tmp_path):
    """Seeing a candidate, then deciding to have it checked, is the human order of things.

    Field evidence of 2026-10-13: the first real trial of this mechanism in ChatGPT stopped
    dead at this guard, having read the two candidates and the answer. Raising can only
    harden the contract, so it is allowed once an answer has been read; lowering after the
    verdict would drop the very rule that just rejected the answer, so it stays refused.
    The refusal no longer advises pasting the same prompt again: the case is addressed by
    the prompt, so the same prompt resumes the same case and the same refusal.
    """
    root = tmp_path / "travail"
    prompt, answer = tmp_path / "prompt.txt", tmp_path / "answer.txt"
    prompt.write_text(SCOPED_PROMPT, encoding="utf-8")
    answer.write_text(TOO_LONG, encoding="utf-8")
    assert chatgpt("init", "--root", root)[0] == 0
    code, first = chatgpt("prompt", "--root", root, "--file", prompt)
    assert code == 0 and sum(r["kind"] == "candidate" for r in first["units"]) == 2
    code, read = chatgpt("answer", "--root", root, "--file", answer)
    assert code == 0 and read["counts"]["REJETE"] == 0 and read["counts"]["CANDIDAT"] == 2

    # Lowering after the verdict stays refused, and the hint no longer sends the reader
    # back to the same prompt, which resumes the same case and the same refusal.
    code, lowered = chatgpt("confirm", "--root", root, "--by", "said", "--refuse", "u2-max-words")
    assert code == 2 and lowered["error"]["code"] == "CONFIRM_AFTER_ANSWER"
    assert "--id" in lowered["error"]["hint"]
    assert "paste the prompt again" not in lowered["error"]["hint"]
    # Raising is allowed, and it hardens: a new case opens on the stricter contract.
    code, raised = chatgpt("confirm", "--root", root, "--by", "said", "--id", "u2-max-words")
    assert code == 0, raised
    assert raised["case_id"] != first["case_id"]
    rows = {r["id"]: r for r in raised["units"]}
    assert rows["u2-max-words"]["kind"] == "mechanical"
    assert rows["u2-max-words"]["confirmed_by"] == "said"
    assert rows["u3-contains-1"]["kind"] == "candidate"
    # The case just opened has no answer of its own, and that is not a way back: the
    # question is asked about the prompt, so lowering is refused here too.
    code, right_after = chatgpt(
        "confirm", "--root", root, "--by", "said", "--refuse", "u2-max-words"
    )
    assert code == 2 and right_after["error"]["code"] == "CONFIRM_AFTER_ANSWER", right_after
    # Nor is pasting the same prompt again: it resumes where the prompt stands, decisions
    # included, instead of compiling a contract that carries none.
    code, pasted = chatgpt("prompt", "--root", root, "--file", prompt)
    assert code == 0, pasted
    standing = {r["id"]: r for r in pasted["units"]}
    assert standing["u2-max-words"]["kind"] == "mechanical"
    assert standing["u2-max-words"]["confirmed_by"] == "said"
    assert pasted["confirmed"] == {"u2-max-words": "said"}
    # The very same answer is now rejected, by the obligation that was just raised.
    code, again = chatgpt("answer", "--root", root, "--file", answer)
    assert code == 1
    verdicts = {r["id"]: r["status"] for r in again["rows"]}
    assert verdicts["u2-max-words"] == "REJETE" and verdicts["u3-contains-1"] == "CANDIDAT"
    # On the new case too, once its own answer is read, lowering is refused.
    code, still = chatgpt("confirm", "--root", root, "--by", "said", "--refuse", "u2-max-words")
    assert code == 2 and still["error"]["code"] == "CONFIRM_AFTER_ANSWER"
    # Raising the other candidate still works, and the first decision keeps its author.
    code, both = chatgpt("confirm", "--root", root, "--by", "said", "--id", "u3-contains-1")
    assert code == 0, both
    kinds = {r["id"]: r["kind"] for r in both["units"]}
    assert kinds["u2-max-words"] == "mechanical" and kinds["u3-contains-1"] == "mechanical"
    assert both["confirmed"] == {"u2-max-words": "said", "u3-contains-1": "said"}
    # Nor is pasting another prompt in between: back on this one, its confirmed checks are
    # back, with their author, since what was confirmed belongs to the prompt.
    other = tmp_path / "autre.txt"
    other.write_text(TRIAL_PROMPT, encoding="utf-8")
    assert chatgpt("prompt", "--root", root, "--file", other)[0] == 0
    code, back = chatgpt("prompt", "--root", root, "--file", prompt)
    assert code == 0, back
    assert back["confirmed"] == {"u2-max-words": "said", "u3-contains-1": "said"}
    assert all(r["kind"] != "candidate" for r in back["units"]), back["units"]


def test_more_candidates_than_one_receipt_can_carry_are_refused_before_a_case_opens(tmp_path):
    """Every entry, candidate or check, repeats its unit in the rows a receipt shows.

    Five hundred terms on one line would give a receipt no verification could read back:
    the prompt is refused up front, with exit code 2, before any case opens. A confirmation
    can then never push a case past that bound, since candidates were already counted. A
    few dozen terms stay within it, and confirming them all at once works.
    """
    from skillflow_prompts import chatgpt as launcher

    root = tmp_path / "travail"
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Summary:\n" + " ".join(f'Mention "t{i:05d}".' for i in range(500)) + "\n")
    assert launcher.run(["init", "--root", str(root)])[0] == 0
    code, refused = launcher.run(["prompt", "--root", str(root), "--file", str(prompt)])
    assert code == 2, refused
    assert refused["error"]["code"] == "PROMPT_TOO_LARGE"
    code, status = launcher.run(["status", "--root", str(root)])
    assert code == 0 and status["current"] is None

    prompt.write_text("Summary:\n" + " ".join(f'Mention "t{i:02d}".' for i in range(40)) + "\n")
    code, compiled = launcher.run(["prompt", "--root", str(root), "--file", str(prompt)])
    assert code == 0, refused
    candidates = [r["id"] for r in compiled["units"] if r["kind"] == "candidate"]
    assert len(candidates) == 40
    assert all(r["demoted_by"] == "Summary" for r in compiled["units"] if r["kind"] == "candidate")
    argv = ["confirm", "--root", str(root), "--by", "said"]
    for ident in candidates:
        argv += ["--id", ident]
    code, confirmed = launcher.run(argv)
    assert code == 0, confirmed
    assert sum(r["kind"] == "mechanical" for r in confirmed["units"]) == 40
    assert confirmed["cancelled_cases"] == [compiled["case_id"]]


def test_a_root_created_before_this_version_keeps_working(tmp_path):
    """A compilation stored by an earlier version has no decision fields.

    The same prompt gives the same contract, so the stored record is refreshed rather than
    refused as a conflict, a confirmation reads the missing decisions as none, and the
    delivery reads the missing author as none.
    """
    root = tmp_path / "travail"
    prompt, answer = tmp_path / "prompt.txt", tmp_path / "answer.txt"
    prompt.write_text(TRIAL_PROMPT, encoding="utf-8")
    assert chatgpt("init", "--root", root)[0] == 0
    code, compiled = chatgpt("prompt", "--root", root, "--file", prompt)
    assert code == 0, compiled
    stored = root / "compilations" / (compiled["contract"].split("@")[0] + ".json")
    record = json.loads(stored.read_bytes())
    for key in ("confirmed", "refused", "confirmed_by"):
        del record[key]
    stored.write_bytes(canonical(record))

    code, again = chatgpt("prompt", "--root", root, "--file", prompt)
    assert code == 0, again
    assert again["contract"] == compiled["contract"]
    assert "confirmed" in json.loads(stored.read_bytes())
    # And with the old record in place, the decisions it lacks read as none.
    stored.write_bytes(canonical(record))
    code, refused = chatgpt("confirm", "--root", root, "--by", "said", "--refuse", "u1-max-words")
    assert code == 0, refused
    assert refused["refused"] == {"u1-max-words": "said"}
    stored.write_bytes(canonical(record))
    answer.write_text(TRIAL_ANSWER, encoding="utf-8")
    assert chatgpt("answer", "--root", root, "--file", answer)[0] == 0
    code, delivered = chatgpt("deliver", "--root", root)
    assert code == 0, delivered
    # Another contract under the same name is still a conflict, never silently replaced.
    other = dict(record, contract_sha256="0" * 64)
    stored.write_bytes(canonical(other))
    code, conflict = chatgpt("prompt", "--root", root, "--file", prompt)
    assert code == 3 and conflict["error"]["code"] == "STATE_CONFLICT", conflict


def test_an_unreadable_file_of_mine_is_not_blamed_on_the_receipt(tmp_path):
    """The receipt is intact: the file that cannot be read is the reader's own.

    `UnicodeDecodeError` is a `ValueError`, and a failed JSON read raises one too, so both
    fell into the net held out for a malformed receipt and `verify` answered that the
    receipt was not well formed. It said the wrong thing about the wrong file.
    """
    from skillflow_prompts import chatgpt as launcher

    root = tmp_path / "state"
    _, folder = deliver_right_answer(tmp_path, root)
    receipt, answer = folder / "receipt.json", folder / "answer.txt"
    latin = tmp_path / "latin1.txt"
    latin.write_bytes("Réponse".encode("latin-1"))
    code, refused = launcher.run(
        ["verify", "--receipt", str(receipt), "--answer", str(answer), "--prompt", str(latin)]
    )
    assert code == 2, refused
    assert refused["error"]["code"] == "PROMPT_NOT_UTF8"
    assert latin.name in refused["error"]["message"]
    assert "RECEIPT" not in json.dumps(refused, ensure_ascii=False)

    broken = tmp_path / "broken.json"
    broken.write_text("{ ceci n'est pas du json", encoding="utf-8")
    code, refused = launcher.run(
        ["verify", "--receipt", str(receipt), "--answer", str(answer), "--contract", str(broken)]
    )
    assert code == 2, refused
    assert refused["error"]["code"] == "CONTRACT_NOT_JSON"
    assert broken.name in refused["error"]["message"]
    assert "RECEIPT" not in json.dumps(refused, ensure_ascii=False)

    # A receipt that really is malformed still says exactly that.
    liar = tmp_path / "liar.json"
    liar.write_bytes(resign(folder, lambda p: p.update(rounds_used="0\n# faux")))
    code, refused = launcher.run(["verify", "--receipt", str(liar), "--answer", str(answer)])
    assert code == 1 and refused["error"]["code"] == "RECEIPT_MALFORMED"
    # And a contract that reads but is not this receipt's keeps its own refusal.
    code, refused = launcher.run(
        ["verify", "--receipt", str(receipt), "--answer", str(answer), "--contract", str(receipt)]
    )
    assert code == 1 and refused["error"]["code"] == "CONTRACT_CHANGED"


def test_a_check_is_never_a_sentence_and_a_rewriting_is_not_a_check(tmp_path):
    """A table of checks holds verdicts, not prose: what was read is said beside it."""
    from skillflow_prompts import chatgpt as launcher

    root = tmp_path / "state"
    _, folder = deliver_right_answer(tmp_path, root)
    receipt, answer = folder / "receipt.json", folder / "answer.txt"
    code, strict = launcher.run(
        ["verify", "--receipt", str(receipt), "--answer", str(answer), "--root", str(root)]
    )
    assert code == 0, strict
    for name, value in strict["checks"].items():
        # `key_anchored` alone may be unknown, which is a third state, not a verdict.
        assert isinstance(value, bool) or (name == "key_anchored" and value is None), name
    assert strict["checks"]["answer"] is True and strict["answer_rewritten"] is False

    again = tmp_path / "again.txt"
    again.write_bytes(codecs.BOM_UTF8 + answer.read_bytes())
    code, same = launcher.run(
        ["verify", "--receipt", str(receipt), "--answer", str(again), "--root", str(root)]
    )
    assert code == 0, same
    for name, value in same["checks"].items():
        assert isinstance(value, bool) or (name == "key_anchored" and value is None), name
    assert same["checks"]["answer"] is True and same["answer_rewritten"] is True
    assert "n'est PAS octet pour octet" in same["receipt"]


def test_a_receipt_written_by_other_verifier_code_says_so_in_its_summary(tmp_path):
    """A different verifier code is not a forgery, and it must not pass unsaid.

    `same_code` stays out of the verdict on purpose: the signature holds and nothing was
    edited. But a reader who only reads the summary must still learn that the statuses
    were recomputed by code other than the one that wrote them.
    """
    from skillflow_prompts import chatgpt as launcher

    root = tmp_path / "state"
    _, folder = deliver_right_answer(tmp_path, root)
    answer = folder / "answer.txt"
    forged = tmp_path / "other-code.json"
    forged.write_bytes(resign(folder, lambda p: p["code"].update(verifiers_sha256="0" * 64)))
    code, shown = launcher.run(
        ["verify", "--receipt", str(forged), "--answer", str(answer), "--root", str(root)]
    )
    assert code == 0 and shown["ok"] is True, shown
    assert shown["checks"]["same_code"] is False
    assert "vérificateurs" in shown["summary"], shown["summary"]
    code, honest = launcher.run(
        ["verify", "--receipt", str(folder / "receipt.json"), "--answer", str(answer)]
    )
    assert code == 0 and honest["checks"]["same_code"] is True
    assert "vérificateurs" not in honest["summary"]


def test_a_receipt_from_another_version_says_which_version_wrote_it(tmp_path, monkeypatch):
    """A receipt written before an update must not read like an edited contract.

    Reported by the reviewer on this very tree: this phase taught the compiler a wording it
    did not read before ('Le mot "Paris" doit apparaître.'), so a contract compiled before
    it no longer recompiles here. Reproducing the contract from its own prompt is what
    makes a status trustworthy, so the verification still fails; it now names the version
    that wrote the receipt and what to do, instead of blaming the contract. The verifier
    code is unchanged in this scenario, so `same_code` says nothing about it.
    """
    from skillflow_prompts import chatgpt as launcher
    from skillflow_prompts import prompts as compiler

    root = tmp_path / "travail"
    prompt, answer = tmp_path / "prompt.txt", tmp_path / "answer.txt"
    prompt.write_text('Le mot "Paris" doit apparaître.\n', encoding="utf-8")
    answer.write_text("Paris est la réponse attendue.\n", encoding="utf-8")
    # The tool as it stood before this phase: the postposed wording is not read, and the
    # version it stamps on the contract is its own.
    monkeypatch.setattr(compiler, "CONTAINS", compiler.CONTAINS[:-1])
    monkeypatch.setattr(compiler, "__version__", "0.0.0-avant")
    for argv in (
        ["init", "--root", root],
        ["prompt", "--root", root, "--file", prompt],
        ["answer", "--root", root, "--file", answer],
        ["deliver", "--root", root],
    ):
        code, result = launcher.run([str(a) for a in argv])
        assert code == 0, result
    folder = Path(result["receipt_folder"])
    # That version read no term in this prompt: its receipt holds no mechanical row.
    written = json.loads(
        base64.b64decode(json.loads((folder / "receipt.json").read_bytes())["payload"])
    )
    assert [row["id"] for row in written["units"]] == ["u1-sens"], written["units"]

    monkeypatch.undo()
    code, shown = launcher.run(
        [
            "verify",
            "--receipt",
            str(folder / "receipt.json"),
            "--answer",
            str(folder / "answer.txt"),
        ]
    )
    assert code == 1 and shown["ok"] is False
    assert shown["checks"]["contracts"] is False and shown["checks"]["same_code"] is True
    assert "0.0.0-avant" in shown["summary"] and compiler.__version__ in shown["summary"]
    assert "ne se recompile pas" in shown["summary"], shown["summary"]
    # A receipt this version wrote and nobody touched carries no such notice.
    _, honest = deliver_right_answer(tmp_path, tmp_path / "state")
    code, verified = launcher.run(
        [
            "verify",
            "--receipt",
            str(honest / "receipt.json"),
            "--answer",
            str(honest / "answer.txt"),
        ]
    )
    assert code == 0 and verified["ok"] is True
    assert "recompile" not in verified["summary"], verified["summary"]


def test_no_codepoint_decomposes_past_the_ceiling_the_launcher_reads_to():
    """The ceiling is a measurement, not a guess: every codepoint is checked against it."""
    worst = 1
    for point in range(0x110000):
        character = chr(point)
        if unicodedata.category(character) == "Cs":
            continue
        if unicodedata.normalize("NFC", character) != character:
            continue
        decomposed = unicodedata.normalize("NFD", character)
        worst = max(worst, len(decomposed.encode("utf-8")) / len(character.encode("utf-8")))
    assert worst == 3
    assert MAX_REWRITTEN_FACTOR >= worst

"""The ChatGPT launcher run exactly as ChatGPT runs it: every step is a separate process."""

import base64
import json
import os
import subprocess
import sys
from io import BytesIO
from pathlib import Path

import pytest

from skillflow_prompts.util import canonical

PROMPT = (
    "Réponds en 40 mots maximum.\n"
    "Mentionne le mot « SkillFlow ».\n"
    "Termine par une question ouverte.\n"
)
WRONG = "Voici une réponse courte qui oublie le nom de l'outil. Elle reste brève. Et toi ?"
RIGHT = "SkillFlow contrôle chaque consigne mesurable avant la remise. Elle reste brève. Et toi ?"


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

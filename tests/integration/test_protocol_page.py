"""The protocol page: the same answers as the command line, in a browser, on loopback.

Said does not type five commands in a terminal, and a tool he cannot run is a tool that
does not exist. The page walks the whole protocol -- paste the prompt, read what is
checked, copy the prompt into ChatGPT, paste the answer, confirm a candidate, deliver,
verify a receipt -- and it implements none of it: every route calls the launcher's own
`run` and returns its JSON unchanged, so there is no second behaviour to keep in step.
"""

import json
import os
import re
import socket
import time
from pathlib import Path

import pytest

from skillflow_prompts.chatgpt import run

PROMPT = (
    "Réponds en 40 mots maximum.\nMentionne « SkillFlow ».\n"
    "Termine par une question.\nTitre : 3 mots maximum.\n"
)
WRONG = "Trop court."
RIGHT = "SkillFlow contrôle chaque consigne mesurable avant la remise. Elle reste brève. Et toi ?"


def client(root):
    fastapi = pytest.importorskip("fastapi.testclient")
    from skillflow_prompts.page import create_page

    return fastapi.TestClient(create_page(root))


def volatile(payload):
    """The same answer, with what one machine and one folder own taken out."""
    if isinstance(payload, dict):
        return {
            key: volatile(value)
            for key, value in payload.items()
            if key not in ("root", "case_id", "key_id", "receipt_folder", "summary", "files")
        }
    if isinstance(payload, list):
        return [volatile(item) for item in payload]
    return payload


def test_the_page_walks_the_whole_protocol_without_a_terminal(tmp_path):
    page = client(tmp_path / "state")
    opened = page.get("/")
    assert opened.status_code == 200
    assert "Colle ton prompt" in opened.text and "<textarea" in opened.text
    # The page is ready as soon as it is opened: the state exists, nothing to initialize by
    # hand, and the page says where it is.
    state = page.get("/state").json()
    assert state["ok"] and state["current"] is None

    compiled = page.post("/prompt", json={"text": PROMPT}).json()
    assert compiled["exit_code"] == 0
    kinds = {unit["id"]: unit["kind"] for unit in compiled["units"]}
    assert kinds["u1-max-words"] == "mechanical"
    assert kinds["u4-max-words"] == "candidate"
    # The exact text to paste into ChatGPT comes back with it: no file to open.
    assert compiled["prompt_text"] == PROMPT

    refused = page.post("/answer", json={"text": WRONG}).json()
    assert refused["exit_code"] == 1
    assert refused["rounds_remaining"] == 2
    assert {row["status"] for row in refused["rows"] if row["id"] == "u2-contains-1"} == {"REJETE"}

    accepted = page.post("/answer", json={"text": RIGHT}).json()
    assert accepted["exit_code"] == 0
    assert accepted["counts"]["REJETE"] == 0

    delivered = page.post("/deliver", json={}).json()
    assert delivered["exit_code"] == 0
    # The three files are in the answer, so the page can hand them over without a path to
    # copy: they are what a verification at home replays.
    assert set(delivered["files"]) == {"receipt.json", "answer.txt", "prompt.txt"}
    assert delivered["files"]["answer.txt"] == RIGHT

    verified = page.post("/verify", json=delivered["files"]).json()
    assert verified["exit_code"] == 0 and verified["ok"]
    assert verified["checks"]["prompt"] is True


def test_a_candidate_is_confirmed_from_the_page_and_becomes_a_check(tmp_path):
    page = client(tmp_path / "state")

    def kind(payload):
        return next(unit["kind"] for unit in payload["units"] if unit["id"] == "u1-max-words")

    # The unit also keeps a rule of meaning for the part it names: that one is no candidate.
    compiled = page.post("/prompt", json={"text": "Titre : 3 mots maximum.\n"}).json()
    assert kind(compiled) == "candidate"
    # A confirmation compiles another contract and gives back the units it holds: the page
    # shows that answer, so the check appears without the prompt being pasted again.
    confirmed = page.post("/confirm", json={"by": "said", "ids": ["u1-max-words"]}).json()
    assert confirmed["exit_code"] == 0
    assert kind(confirmed) == "mechanical"
    assert confirmed["cancelled_cases"] == [compiled["case_id"]]
    # A refusal is just as reachable, and it is the page's own word that travels.
    refused = page.post("/confirm", json={"by": "said", "refuse": ["u1-max-words"]}).json()
    assert refused["exit_code"] == 0


def test_the_page_answers_exactly_what_the_command_line_answers(tmp_path):
    """One behaviour, two doors. If the page ever had its own reading, this test fails."""
    page = client(tmp_path / "page")
    other = tmp_path / "line"
    assert run(["init", "--root", str(other)])[0] == 0
    prompt, answer = tmp_path / "prompt.txt", tmp_path / "answer.txt"
    prompt.write_text(PROMPT, encoding="utf-8")
    answer.write_text(WRONG, encoding="utf-8")

    through_the_page = page.post("/prompt", json={"text": PROMPT}).json()
    _code, through_the_line = run(["prompt", "--root", str(other), "--file", str(prompt)])
    assert volatile(through_the_page)["units"] == volatile(through_the_line)["units"]

    refused_page = page.post("/answer", json={"text": WRONG}).json()
    _code, refused_line = run(["answer", "--root", str(other), "--file", str(answer)])
    assert volatile(refused_page)["rows"] == volatile(refused_line)["rows"]
    assert refused_page["counts"] == refused_line["counts"]


def test_an_action_on_a_folder_that_moved_elsewhere_is_refused(tmp_path):
    """Two tabs on one state, and the reviewer's last door of this family.

    Tab A compiles Alpha and checks it; tab B compiles Beta, which cancels A's folder; back
    in A, "Livrer" delivered Beta while A still showed Alpha -- a receipt attesting a prompt
    that tab never displayed. So every action that acts on a folder carries the identifier
    the page is showing, and the launcher's current folder has to be that one.
    """
    page = client(tmp_path / "state")
    alpha = page.post("/prompt", json={"text": "Mentionne « Alpha ».\n"}).json()
    beta = page.post("/prompt", json={"text": "Mentionne « Beta ».\n"}).json()
    assert alpha["case_id"] != beta["case_id"]

    for route, body in (
        ("/answer", {"text": RIGHT}),
        ("/deliver", {}),
        ("/confirm", {"by": "said", "ids": []}),
    ):
        stale = page.post(route, json={**body, "case_id": alpha["case_id"]})
        assert stale.status_code == 409, route
        assert stale.json()["error"]["code"] == "CASE_MOVED", route

    # Refused twice, not once: the reviewer clicked "Livrer" a second time without
    # reloading, and the refreshed state had quietly adopted the other tab's folder, so the
    # second click delivered Beta under Alpha's display. The folder of THIS page is the one
    # it compiled itself, and nothing else ever becomes it.
    again = page.post("/deliver", json={"case_id": alpha["case_id"]})
    assert again.status_code == 409 and again.json()["error"]["code"] == "CASE_MOVED"

    # Reprendre un reçu n'est pas livrer, and the difference has to be said. After its own
    # delivery a tab holds no folder any more, so a later click sent an empty body and the
    # launcher delivered whatever folder another tab had opened since -- Beta's files under
    # Alpha's display. The click that asks for a receipt says so, and a folder open
    # elsewhere refuses it.
    asking = page.post("/deliver", json={"receipt": "C:/un/dossier/de/recu"})
    assert asking.status_code == 409 and asking.json()["error"]["code"] == "CASE_MOVED"
    shown = page.get("/").text
    assert "wanted.receipt = kept || delivered;" in shown
    # A reopened tab has compiled nothing, so it knew no folder: "Livrer" then carried only
    # the rounds left, and two different folders can sit on the same count -- Beta delivered
    # under Alpha's display. Every answer of the launcher that names a folder teaches it.
    assert "if (!wrong) { folder = text(payload.case_id) || folder; }" in shown
    assert "case_id" in page.post("/answer", json={"text": WRONG}).json()
    # And the last door of the family: a tab whose folder was delivered or cancelled
    # elsewhere kept Alpha on the screen with no identity left, so its next action went out
    # unnamed and then adopted whatever folder had opened since. Acting requires naming the
    # folder, or displaying no compilation at all -- then nothing read contradicts it.
    assert "const named = folder !== null;" in shown
    assert "const blank = compiled === null;" in shown
    # And when the identity is lost, what was displayed of that folder goes with it. Clearing
    # only the identity left the verdicts on screen, so a page that had compiled nothing
    # counted as "blank" -- that state being computed on the prompt box alone -- and its next
    # check went out unnamed, measured against the folder opened elsewhere. The page says why
    # instead of emptying itself in silence.
    assert "if (folder && current === null) {\n    folder = null;\n    fresh();" in shown
    assert "Ce dossier n'existe plus" in shown
    assert '$("check").disabled = busy || stale || current === null || !(named || blank);' in shown
    assert "n'est plus celui en cours" in shown
    # The four actions that touch a folder obey the same rule, confirming included: its
    # candidates outlive a delivery, so a confirmation went out unnamed and applied itself
    # to whatever folder was open elsewhere.
    assert "busy || stale || candidates.length === 0 || !(named || blank);" in shown
    # Taking a receipt back obeys the same rule, and that was the last door: after a first
    # refusal the page held no folder any more but still displayed Alpha, and it asked for
    # the server's last receipt -- Beta's by then. Its own receipt, or nothing displayed.
    assert "const reprise = (named === false && (kept !== null || blank))" in shown
    # And a new folder erases what belonged to the old one. Recompiling left the page
    # believing its answer checked -- it had been, against a contract that no longer exists
    # -- so "Livrer" went out with no rounds, hence no version, and delivered what another
    # tab had checked on the new folder.
    assert "function fresh(){\n  checked = null;\n  budget = null;" in shown
    assert "n'est pas contrôlée pour ce dossier" in shown
    # A breakdown is not an absence. A failed read of the state used to clear the identity
    # of what the page displays, and the next click then went out with nothing to oppose to
    # the server -- delivering another tab's folder. Unknown is not none.
    assert "if (payload.error || payload.ok === false) {" in shown
    assert "État : injoignable — " in shown
    # The state reads are numbered like the actions: the one fired at load can come back
    # after a compilation, carrying a stale "no folder", and erase the identity of the
    # folder just opened.
    assert "let reads = 0;" in shown
    assert "const order = ++reads;" in shown
    assert "if (order !== reads) { return; }" in shown
    # Typing a character and deleting it emptied the table of units and candidates and never
    # gave them back, while the contract had not moved at all. The last compilation shown is
    # kept, and it comes back with the text.
    assert "let lastUnits = null;" in shown
    assert "if (compiled !== null && lastUnits) { showUnits(lastUnits); }" in shown
    # A refusal does not make the page forget the version number either: losing it let the
    # next action go out with nothing to oppose, and the server accepted it. Only a new
    # folder puts that count back to nothing.
    assert (
        'if (!wrong && typeof payload.rounds_remaining === "number") '
        "{ budget = payload.rounds_remaining; }"
    ) in shown
    # That line alone proves less than it looks, and the review of the P9 debt said why:
    # adding `budget = null` after it would leave this assertion green. The reset belongs in
    # `fresh()` and nowhere else, which is what « seul un dossier neuf remet ce compte à
    # zéro » means -- and it is asserted here.
    assert "function fresh(){\n  checked = null;\n  budget = null;" in shown
    # And now the real control the review asked for: two occurrences only, the declaration
    # and the one inside `fresh()`. `refresh()` used to clear the count itself, on the very
    # path that review refused; it calls `fresh()` instead, so a new folder is the only
    # thing that puts this count back to nothing.
    assert shown.count("budget = null") == 2
    assert "if (!folder && (kept || (compiled === null && delivered)))" in shown
    # And a refusal takes nothing away: the files of an earlier delivery stay offered, since
    # erasing them at the moment one is told no is taking back what was honestly obtained.
    assert "  if (!wrong) {\n    // Le reçu que cette page tient désormais" in shown
    html = page.get("/").text
    assert "let folder = null;" in html
    assert "const named = folder ? {...body, case_id: folder} : {...body};" in html
    assert "folder = wrong ? null : text(payload.case_id);" in html
    assert "if (folder && current === null) {\n    folder = null;\n    fresh();" in html

    # The folder being shown goes through, and a made-up identifier never reaches a command.
    walked = page.post("/answer", json={"text": RIGHT, "case_id": beta["case_id"]})
    assert walked.status_code == 200
    odd = page.post("/deliver", json={"case_id": "--root /tmp"})
    assert odd.status_code == 400 and odd.json()["error"]["code"] == "CASE_NAME"

    # Two tabs on the SAME folder: the identifier does not move when an answer is checked,
    # so it alone let one tab deliver a receipt on the answer the other had checked. The
    # rounds left are the folder's version number, and they are opposed too. Raised by the
    # product review.
    left = walked.json()["rounds_remaining"]
    page.post("/answer", json={"text": WRONG, "case_id": beta["case_id"]})
    crossed = page.post("/deliver", json={"case_id": beta["case_id"], "rounds": left})
    assert crossed.status_code == 409 and crossed.json()["error"]["code"] == "CASE_MOVED"
    assert "let budget = null;" in html
    assert "if (budget !== null) { named.rounds = budget; }" in html


def test_a_call_from_another_host_or_another_origin_is_refused(tmp_path):
    page = client(tmp_path / "state")
    far = page.post("/prompt", json={"text": PROMPT}, headers={"Host": "example.com"})
    assert far.status_code == 403 and far.json()["error"]["code"] == "HOST"
    crossed = page.post("/prompt", json={"text": PROMPT}, headers={"Origin": "https://example.com"})
    assert crossed.status_code == 403 and crossed.json()["error"]["code"] == "ORIGIN"
    # The page's own origin is the one browser call that goes through.
    home = page.post("/prompt", json={"text": PROMPT}, headers={"Origin": "http://testserver"})
    assert home.status_code == 200


def test_what_is_pasted_is_text_and_never_an_argument(tmp_path):
    """A prompt is untrusted input here too: it is written to a file under the state, and
    the page builds every argument itself."""
    page = client(tmp_path / "state")
    hostile = "--root /tmp/elsewhere --file /etc/passwd\nMentionne « SkillFlow ».\n"
    compiled = page.post("/prompt", json={"text": hostile}).json()
    assert compiled["exit_code"] == 0
    assert compiled["units"][0]["text"] == "--root /tmp/elsewhere --file /etc/passwd"
    assert (tmp_path / "state").is_dir() and not (tmp_path / "elsewhere").exists()


def test_a_request_too_large_is_refused_before_it_is_read(tmp_path):
    page = client(tmp_path / "state")
    too_much = page.post("/prompt", json={"text": "x " * 3_000_000})
    assert too_much.status_code == 413 and too_much.json()["error"]["code"] == "REQUEST_SIZE"


def test_the_page_names_its_own_buttons_and_the_natures_in_french(tmp_path):
    """A page that answers "chatgpt deliver" has described the terminal, not replaced it.

    The launcher's own summary stays, word for word, because it is the truth of what
    happened -- it names commands, since it is the command line talking. Beside it the page
    says what to click, and it writes the nature of a unit in the language Said reads.
    """
    page = client(tmp_path / "state")
    html = page.get("/").text
    for step in ("clique « Vérifier »", "clique « Livrer »", "clique « Compiler »"):
        assert step in html, step
    for nature in ("contrôlé mécaniquement", "mis de côté", "à toi de la respecter"):
        assert nature in html, nature


def test_the_page_copies_what_was_compiled_and_expires_a_changed_prompt(tmp_path):
    """Raised by the reviewer, and it is the whole subject of this tool.

    Compile "Mentionne « Alpha »", change the box to "Mentionne « Beta »" without
    compiling again, click copy: the first form copied the box, so Beta went to ChatGPT
    while the contract checked Alpha, and the answer came back judged against a prompt
    nobody sent. So the page copies the text the launcher says it read, and a changed box
    expires the compilation: no copy and no check until "Compiler" is clicked again.

    What a pytest run can see of a page is its text. The behaviour itself was driven in a
    real browser and the measurement is in the phase note: typing in the box disables both
    buttons and empties the table, and copying after a compilation hands over the compiled
    text.
    """
    page = client(tmp_path / "state")
    html = page.get("/").text
    assert 'id="check" disabled' in html
    assert '$("prompt").oninput = freshness' in html
    # Checked on the way back from a compilation too: a slow answer can land after the box
    # was changed, and the page would show a contract on Alpha while Said reads Beta.
    assert "  freshness();\n" in html
    assert "Le prompt a changé : clique « Compiler »" in html
    # What is copied is the server's own answer, never the box.
    assert "writeText(compiled)" in html
    assert "compiled = wrong ? null : text(payload.prompt_text)" in html
    # Two compilations without waiting: the answers may come back in the other order, and
    # the page would show Alpha while the server's contract checks Beta. Raised by the
    # reviewer, reproduced in a browser: requests are numbered, a stale answer is dropped,
    # and every button that acts on a contract is disabled while one is in flight.
    assert "const mine = ++turn;" in html
    assert "if (mine !== turn) { return; }" in html
    assert "function buttons()" in html
    # "Busy" is a state of its own, not an argument: otherwise a keystroke calls
    # freshness, which recomputes the buttons and reopens "Compiler" while a request is
    # in flight -- two compilations leave, the server may take them in the other order,
    # and the page shows Beta while the contract checks Alpha.
    assert "let busy = false;" in html
    # A cut connection must come back as a refusal, never as an exception: an exception
    # leaves "busy" true and every button closed for good, a dead page until reload.
    assert "INJOIGNABLE" in html
    assert "try { answered = await fetch(path, options); }" in html
    # Every button state is recomputed in one place, so a refused name does not leave
    # "Confirmer" closed for good -- the dead end the reviewer found next.
    assert "|| candidates.length === 0" in html


def test_a_changed_prompt_closes_delivering_too_and_the_server_says_what_is_open(tmp_path):
    """Raised by the product review, reproduced in a browser, and the worst of the family.

    Compile "Mentionne « Alpha »", check an answer, then change the box to "Mentionne
    « Beta »" without compiling again: "Copier" and "Vérifier" closed, but "Livrer" stayed
    open -- and it delivered the Alpha case. A receipt attesting a prompt Said no longer had
    in front of him, with the old VERIFIE still on the screen beside it.

    So what may be checked or delivered is read from the server's own state, never from this
    page's memory, and a changed box closes all three. Both halves were measured in a
    browser and the numbers are in the phase note: with no case the three are closed, a
    compilation opens them, a changed prompt closes them and empties the verdicts,
    recompiling reopens them; and a tab reopened mid-protocol finds its case (RUNNING, three
    rounds left) then checks and delivers without recompiling -- recompiling would have
    cancelled that case and put the rounds back to three.
    """
    page = client(tmp_path / "state")
    html = page.get("/").text
    assert 'id="deliver" disabled' in html
    assert "let stale = false;" in html
    assert "let current = null;" in html
    # The opening of the line, not the whole expression: what matters is that a stale
    # prompt and an unchecked answer both close delivering, and a stale prompt closes
    # checking.
    assert '$("deliver").disabled =\n    busy || stale || unchecked ||' in html
    assert '$("check").disabled = busy || stale || current === null ||' in html
    # A changed prompt is stale, and the verdicts of the old prompt leave with it.
    assert "  stale = changed;\n" in html
    assert 'fill($("rows"), "");' in html
    # It is the server's answer that opens them: /state carries the case, nothing else does.
    assert "current = now === undefined ? null : now;" in html
    assert page.get("/state").json()["current"] is None
    # And the page never guesses the server's prompt: with nothing compiled in this tab, the
    # only box that contradicts nothing is an empty one. The first form left here as soon as
    # `compiled` was empty, so after a reload one could type Beta and deliver Alpha without a
    # word -- the door the previous fix opened, and the reviewer walked through it.
    assert 'const expected = compiled === null ? "" : compiled;' in html
    assert "if (changed === stale) { return; }" in html


def test_a_changed_answer_closes_delivering_and_keeps_the_verdicts_on_screen(tmp_path):
    """The same lie, on the other side, and the reviewer found it after the first two.

    Check answer A, replace it with B without checking again, click "Livrer": the receipt
    attested A while the screen showed B. So the page remembers the answer the launcher says
    it checked, and a changed box closes "Livrer" -- but never "Vérifier", since correcting
    a REJETE answer is the whole journey, and never the verdict table either: it says what
    to correct, and erasing it would take the instruction away at the second one follows it.
    It is labelled instead.

    Measured in a browser, numbers in the phase note: after a check "Livrer" is open, one
    keystroke in the answer closes it and labels the table, checking again reopens it.
    """
    page = client(tmp_path / "state")
    html = page.get("/").text
    assert "let checked = null;" in html
    assert "let unchecked = false;" in html
    # The prefix, not the whole line: what matters is that both staleness flags close it.
    # What matters is that both staleness flags close it, not the whole expression.
    assert '$("deliver").disabled =\n    busy || stale || unchecked ||' in html
    assert "checked = wrong ? null : text(payload.answer_text);" in html
    assert '$("answer").oninput = answered;' in html
    # The verdicts stay on screen -- only a new folder erases them -- and the line beside
    # them says they do not cover what is in the box now.
    assert "n'est pas contrôlée pour ce dossier" in html
    assert 'fill($("rows"), "");' in html
    # And it is the launcher's own echo that is compared, like the prompt's.
    walked = page.post("/answer", json={"text": "rien"}).json()
    assert "answer_text" in walked


def test_a_tab_reopened_after_a_delivery_can_still_get_the_three_files(tmp_path):
    """The trapdoor the previous fix dug, and the product review walked into it.

    Closing "Livrer" as soon as there is no current case loses the three files of a receipt
    delivered a second earlier: they are on disk, and the launcher knows how to hand them
    back -- delivering twice re-verifies the last receipt and shows it again -- but a
    reloaded page offered no way to ask. So the button stays open while a last receipt
    exists, and the page says so, since a reopened tab otherwise looks empty.
    """
    page = client(tmp_path / "state")
    page.post("/prompt", json={"text": PROMPT})
    page.post("/answer", json={"text": RIGHT})
    first = page.post("/deliver", json={}).json()
    assert first["exit_code"] == 0

    # A reopened tab: no case any more, but the state knows the receipt.
    state = page.get("/state").json()
    assert state["current"] is None and state["last_receipt"]
    again = page.post("/deliver", json={"receipt": state["last_receipt"]}).json()
    assert again["exit_code"] == 0 and again["already_delivered"] is True
    assert again["files"] == first["files"]
    # And it is THAT receipt that is asked for, by name: a delivery made elsewhere since
    # moves the last receipt, and handing it over would be giving someone else's.
    elsewhere = page.post("/deliver", json={"receipt": state["last_receipt"] + "-autre"})
    assert elsewhere.status_code == 409 and elsewhere.json()["error"]["code"] == "CASE_MOVED"

    html = page.get("/").text
    assert "delivered !== null && current === null;" in html
    assert "delivered = payload.last_receipt || null;" in html
    assert "kept = text(payload.receipt_folder) || kept;" in html
    assert "dernier reçu disponible" in html
    assert "un reçu plus récent a été remis ailleurs" in html


def test_no_global_of_the_page_is_shadowed_by_a_constant_of_a_handler(tmp_path):
    """The defect no string search can see, and one click in a browser found.

    Every handler numbers its own request with `const mine = ++turn`. A global named `mine`
    was added for the folder this page shows: inside a handler that constant shadowed it,
    writing to it raised "Assignment to constant variable", and "Compiler" stopped working
    altogether -- while every assertion about the text of this page still passed.

    So the rule becomes mechanical: a name declared `let` in the script is never declared
    `const` anywhere in it. It is not the browser, but it refuses this defect.

    The first form of this guard was itself bypassable, and the review of the P9 debt said
    how: it only looked at `let` in column one, so indenting the global by one space let the
    shadowing back in, and it ignored destructured constants entirely. It now reads a `let`
    wherever it sits, and every name a `const` binds, destructuring included.
    """
    html = client(tmp_path / "state").get("/").text
    held = set(re.findall(r"^[ \t]*let[ \t]+(\w+)", html, re.M))
    constants = set()
    for bound in re.findall(r"\bconst\s+([^=;]+?)\s*=", html):
        constants.update(re.findall(r"\w+", bound))
    assert held, "aucun état global trouvé : le motif de lecture a changé"
    assert not (held & constants), sorted(held & constants)


def test_the_page_is_served_with_no_store_and_a_policy_that_forbids_a_frame(tmp_path):
    page = client(tmp_path / "state")
    opened = page.get("/")
    assert opened.headers["cache-control"] == "no-store"
    assert opened.headers["x-frame-options"] == "DENY"
    policy = opened.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in policy and "connect-src 'self'" in policy
    # Nothing is fetched from a network: the page carries its own style and script.
    assert "http://" not in opened.text.replace("http://testserver", "")


def test_the_command_that_serves_the_page_binds_the_loopback_only(tmp_path):
    from skillflow_prompts import page as page_module

    assert page_module.HOST == "127.0.0.1"
    _code, usage = run(["serve", "--help"])
    assert "--port" in json.dumps(usage, ensure_ascii=False)


@pytest.mark.skipif(os.name != "nt", reason="Windows cmd.exe required")
def test_the_launcher_copied_away_from_its_project_says_so_and_never_calls_uv(tmp_path):
    """The fourth failure mode, found by accident after the previous phase closed.

    Copied out of the repository, the launcher answered `Failed to spawn:
    skillflow-prompts`, in English, with no word of its own -- and then wrote « La page est
    arretee » about a page that had never started. Both are now its business, and this test
    is the measurement itself rather than a string in a file: it copies the launcher out,
    runs it, and reads what it said. Empty stdin so the final `pause` returns at once.
    """
    import shutil
    import subprocess

    launcher = Path(__file__).resolve().parents[2] / "ouvrir-le-protocole.cmd"
    copied = tmp_path / launcher.name
    shutil.copy(launcher, copied)
    assert not (tmp_path / "pyproject.toml").exists()

    done = subprocess.run(
        ["cmd", "/c", str(copied)],
        capture_output=True,
        text=True,
        input="",
        timeout=180,
        encoding="utf-8",
        errors="replace",
    )
    said = done.stdout + done.stderr
    assert done.returncode == 1, said
    assert "copie hors du projet" in said
    assert "RACCOURCI" in said
    # And uv is never reached: its English line was the whole defect.
    assert "Failed to spawn" not in said
    # The false claim is gone with it: nothing says the page stopped, since it never ran.
    assert "La page est arretee" not in said


def test_the_page_does_not_take_the_port_the_demo_already_uses():
    """8765 was skillflow-mcp's demonstration port, inherited by accident.

    A demo started while the page is open would have walked into it. The number itself is
    not the point, so this holds the intent instead: whatever the demo defaults to, the page
    defaults to something else. Raised by the session converting SkillFlow to MCP, which
    checked that the live daemon listens on 8791 and that 8765 is the demo's default.
    """
    import inspect

    from skillflow_prompts import bootstrap
    from skillflow_prompts import page as page_module

    demo = inspect.signature(bootstrap.create_demo).parameters["port"].default
    assert page_module.PORT != demo
    assert page_module.PORT == 8770


def test_the_launcher_opens_the_page_without_a_command_being_typed():
    """Said does not type a command, so the front door is a file he double-clicks.

    What a test can hold of a `.cmd` is its shape, and its shape is the whole design: it
    must stay flat -- no parenthesised block, no label -- because a `.cmd` written with Unix
    line endings breaks on exactly those, and it must keep its window open to the end, since
    a window that blinks and closes teaches its user that the tool is broken. The rest of
    the correctness lives in Python, which is tested. The double-click itself was driven on
    this machine and the measurement is in the phase note.
    """
    launcher = Path(__file__).resolve().parents[2] / "ouvrir-le-protocole.cmd"
    text = launcher.read_text(encoding="utf-8")
    assert "chatgpt serve --root" in text and "--open" in text
    # An absolute root, built from the machine and not from the folder one happens to be in,
    # and kept OUTSIDE the repository: the signing key and the history live there.
    assert "%LOCALAPPDATA%" in text and "travail" in text
    assert "%~dp0travail" not in text
    # Flat: nothing that depends on how the lines end. Written first as an expression
    # ending in `or True`, which made it pass whatever the file held -- a test that proves
    # nothing, caught by the reviewer. A parenthesis anywhere now fails it.
    assert "(" not in text and ")" not in text
    assert "goto" not in text
    assert "if errorlevel 1 pause" in text and text.rstrip().endswith("pause")
    # It says what to do when uv is missing, which is the one failure Python cannot report.
    assert "uv n'est pas trouve" in text
    # And the other one Python cannot report: the file copied away from its project. uv
    # answered "Failed to spawn", in English, with no word from the launcher -- measured
    # after the previous phase closed. The check lives here because Python is out of reach
    # when the project is missing.
    assert 'if not exist "%~dp0pyproject.toml"' in text
    assert "copie hors du projet" in text and "RACCOURCI" in text
    # Two different endings, said differently: it ran and stopped, or it never started. The
    # single sentence claimed the stop in both cases.
    assert "if errorlevel 1 echo La page ne s est pas ouverte." in text
    assert "if not errorlevel 1 echo La page est arretee." in text
    # And no accented byte: the console code page would turn them into noise.
    assert text == text.encode("ascii", "replace").decode("ascii")


def test_the_page_says_its_address_only_once_it_listens(tmp_path, monkeypatch):
    """A double-click must land on THIS page, never on what held the port.

    So the address is announced, and the browser opened, only after the server reports
    listening; and a port already taken is a refusal that names itself instead of a
    traceback. `serve` hands the address to a callback, so a test can hold it without
    anything reaching a real browser.
    """
    from skillflow_prompts import page as page_module

    seen, made = [], []
    monkeypatch.setattr(page_module, "create_page", lambda root: _Nothing())

    class Listening:
        started = True

        def __init__(self, config):
            self.should_exit = False
            self.stopped = False
            made.append(self)

        def run(self):
            while not self.should_exit:
                time.sleep(0.01)
            # Un serveur qui prend son temps : c'est exactement le cas que le délai de cinq
            # secondes faisait mentir.
            time.sleep(0.4)
            self.stopped = True

    monkeypatch.setattr("uvicorn.Server", Listening, raising=False)
    monkeypatch.setattr("uvicorn.Config", lambda *a, **k: None, raising=False)

    def stop(where):
        seen.append(where)
        made[0].should_exit = True

    assert page_module.serve(tmp_path, 8766, opened=stop) == ("served", "http://127.0.0.1:8766/")
    assert seen == ["http://127.0.0.1:8766/"]
    # And it returned only once the server had REALLY stopped. The wait had a five second
    # deadline, past which this returned while the port was still held and the launcher
    # wrote « Page arrêtée » -- a false statement about the state of its own page, refused
    # by the review. The fake server below takes its time to stop, and `stopped` is set at
    # the very end of its run.
    assert made[0].stopped is True
    # A browser that refuses to open is not a reason to kill the page: the address is
    # already on the screen.
    assert page_module.serve(tmp_path, 8766, opened=_refuses(made)) == (
        "served",
        "http://127.0.0.1:8766/",
    )
    assert made[-1].stopped is True

    # And a Ctrl-C at the worst moment -- while the browser is being opened -- still stops
    # the server. The stop used to hang off the waiting loop alone, so an interruption
    # before it came out as a traceback with the server never told to stop. Refused by the
    # review, and this is the scenario it named.
    def interrupted(where):
        raise KeyboardInterrupt

    assert page_module.serve(tmp_path, 8766, opened=interrupted) == (
        "served",
        "http://127.0.0.1:8766/",
    )
    assert made[-1].should_exit is True and made[-1].stopped is True


def test_a_stop_at_the_very_instant_the_thread_starts_still_stops_the_server(tmp_path, monkeypatch):
    """The smallest window of all, and the reason it had no business staying open.

    `listening.start()` sat outside the try, so an interruption landing between the start
    and the try left a live thread that was never told to stop. Raised by the review, which
    also noted that the existing test only interrupts at `page.started`.
    """
    import threading

    from skillflow_prompts import page as page_module

    made = []

    class Listening:
        started = True

        def __init__(self, config):
            self.should_exit = False
            self.stopped = False
            made.append(self)

        def run(self):
            while not self.should_exit:
                time.sleep(0.01)
            self.stopped = True

    monkeypatch.setattr(page_module, "create_page", lambda root: _Nothing())
    monkeypatch.setattr("uvicorn.Server", Listening, raising=False)
    monkeypatch.setattr("uvicorn.Config", lambda *a, **k: None, raising=False)

    started = threading.Thread.start

    def start_then_interrupt(self):
        started(self)
        raise KeyboardInterrupt

    monkeypatch.setattr(threading.Thread, "start", start_then_interrupt)
    assert page_module.serve(tmp_path, 8768) == ("cancelled", None)
    assert made[0].should_exit is True and made[0].stopped is True


def test_the_probe_asks_the_port_the_same_way_the_server_would_take_it(tmp_path, monkeypatch):
    """A probe that lies about what it probes is worse than no probe.

    On POSIX, a port left in TIME_WAIT by a real connection is refused to a plain bind but
    granted to the server, which goes through asyncio and its SO_REUSEADDR: the probe said
    PORT_TAKEN on a port the page could have served. Under Windows that same option does
    the opposite, letting a bind land on a port someone else is really listening on. So the
    probe follows asyncio exactly -- the option on POSIX, never on Windows. Raised by the
    review, which named the Linux sequence; this test holds the rule on either platform.
    """
    import socket as socket_module

    from skillflow_prompts import page as page_module

    options = []
    real = socket_module.socket

    class Spy(real):
        def setsockopt(self, level, option, value):
            options.append((level, option, value))
            return super().setsockopt(level, option, value)

    monkeypatch.setattr(socket_module, "socket", Spy)
    monkeypatch.setattr(page_module, "create_page", lambda root: _Nothing())
    monkeypatch.setattr("uvicorn.Config", lambda *a, **k: None, raising=False)

    class Dead:
        started = False

        def __init__(self, config):
            self.should_exit = False

        def run(self):
            return None

    monkeypatch.setattr("uvicorn.Server", Dead, raising=False)
    page_module.serve(tmp_path, 8767)

    reuse = (socket_module.SOL_SOCKET, socket_module.SO_REUSEADDR, 1)
    assert (reuse in options) is (os.name != "nt")


def test_a_stop_asked_before_the_page_listens_is_not_a_port_that_is_taken(tmp_path, monkeypatch):
    """Two different endings used to look the same, and one of them lied.

    A Ctrl-C while the server is still starting returned exactly what a taken port returned,
    so the launcher announced a taken port on a free one -- a false diagnosis of the only
    failure this phase exists to report well. Refused by the review. The three endings are
    now named: served, taken, cancelled.
    """
    from skillflow_prompts import page as page_module

    class Slow:
        """A server that is asked about its start, and answers with a Ctrl-C.

        The interruption has to land in the waiting loop of `serve`, in the main thread, and
        nowhere else: patching `time.sleep` for that leaked into the server's own thread and
        into the next test.
        """

        def __init__(self, config):
            self.should_exit = False

        @property
        def started(self):
            raise KeyboardInterrupt

        def run(self):
            while not self.should_exit:
                time.sleep(0.01)

    monkeypatch.setattr(page_module, "create_page", lambda root: _Nothing())
    monkeypatch.setattr("uvicorn.Server", Slow, raising=False)
    monkeypatch.setattr("uvicorn.Config", lambda *a, **k: None, raising=False)
    assert page_module.serve(tmp_path, 8766) == ("cancelled", None)

    monkeypatch.undo()
    monkeypatch.setattr(page_module, "serve", lambda root, port, opened=None: ("cancelled", None))
    code, result = run(["serve", "--root", str(tmp_path), "--port", "8766"])
    assert code == 0 and result["ok"] is True and result["url"] is None
    assert "Arrêtée avant d'avoir écouté" in result["summary"]


def test_a_port_already_taken_is_a_refusal_that_names_itself(tmp_path, monkeypatch):
    from skillflow_prompts import page as page_module

    class Dead:
        started = False

        def __init__(self, config):
            pass

        def run(self):
            return None

    monkeypatch.setattr(page_module, "create_page", lambda root: _Nothing())
    monkeypatch.setattr("uvicorn.Server", Dead, raising=False)
    monkeypatch.setattr("uvicorn.Config", lambda *a, **k: None, raising=False)
    # Nothing is announced and nothing is opened: `serve` says it never listened.
    taken_too = page_module.serve(tmp_path, 8766, opened=lambda where: pytest.fail(where))
    assert taken_too == ("taken", None)

    code, result = run(["serve", "--root", str(tmp_path), "--port", "8766"])
    assert code == 2 and result["error"]["code"] == "PORT_TAKEN"
    assert "8766" in result["error"]["message"]

    # And the real mechanism, with a real port held: the refusal comes from the probe, so
    # uvicorn is never started and its own English ERROR line never lands above the French
    # sentence that says what to do. Measured on this machine, with a page already running:
    # the launcher printed the refusal alone and kept its window open.
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    taken = held.getsockname()[1]

    def never(*a, **k):
        raise AssertionError("uvicorn ne doit pas etre lance quand le port est pris")

    monkeypatch.setattr("uvicorn.Server", never, raising=False)
    try:
        assert page_module.serve(tmp_path, taken, opened=never) == ("taken", None)
    finally:
        held.close()


def test_only_the_launcher_asks_for_a_browser_and_it_gets_the_served_address(tmp_path, monkeypatch):
    """`--open` is what the double-click adds, and the command line imposes it on nobody.

    The address handed to the browser is the one `serve` says it listens on, never one built
    beside it: that is the whole reason the browser is opened from inside, after the bind.
    """
    import webbrowser

    from skillflow_prompts import page as page_module

    asked = []
    monkeypatch.setattr(webbrowser, "open", lambda where: asked.append(where))

    def served(root, port, opened=None):
        opened("http://127.0.0.1:9/")
        return "served", "http://127.0.0.1:9/"

    monkeypatch.setattr(page_module, "serve", served)

    code, _result = run(["serve", "--root", str(tmp_path), "--open"])
    assert code == 0 and asked == ["http://127.0.0.1:9/"]

    asked.clear()
    code, _result = run(["serve", "--root", str(tmp_path)])
    assert code == 0 and asked == []


class _Nothing:
    """An application that is never served: these tests hold the decision, not the server."""


def _refuses(made):
    """A browser that throws, and the page that must survive it."""

    def opened(where):
        made[-1].should_exit = True
        raise OSError("aucun navigateur sur cette machine")

    return opened


def test_a_relative_root_is_refused_before_the_address_is_announced(capsys):
    """Raised by the reviewer, on the command the README itself gave.

    `serve` reads its root like every other action, so a relative path is refused -- the
    state folder must not depend on the folder one happens to be in. But the refusal came
    *after* the address: one read "Ouvre http://127.0.0.1:<port>/", then a refusal, and
    waited for a page that would never open. The address is now printed second, so the
    first line one reads is the true one.
    """
    code, result = run(["serve", "--root", "travail"])
    assert code == 2 and result["error"]["code"] == "ROOT_RELATIVE"
    assert "Ouvre" not in capsys.readouterr().err

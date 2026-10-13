"""ChatGPT launcher: one command is one process; the SkillFlow authority opens in-process.

Each action builds a fresh Controller over the state root, does its work, drains the
(empty) outbox and returns. Nothing listens on a port and no child process is started.
The state lives on disk under --root, so the next command, in a new process, reads it
back. GPT holds every credential of its sandbox: no review is run, the approval is
FICTIVE, and the key that signs the receipt was generated in the same sandbox. The
receipt says all of this; `verify` recomputes what is mechanical instead of trusting it.
"""

import argparse
import base64
import html
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from jsonschema import Draft202012Validator

from . import __version__, prompts, receipts, verifiers
from .api import READ
from .bootstrap import create_demo
from .core import Controller
from .delivery import drain_outbox
from .integration import ClientBridge
from .models import Contract
from .registry import authenticate
from .util import FlowError, canonical, sha256, strict_json, uid

STATE = "chatgpt.json"
CLIENT = "chatgpt"
BOM = b"\xef\xbb\xbf"
# A file written again is longer than the bytes a receipt holds: one more byte per line
# end, three for a byte order mark, and at most three times its size once decomposed —
# measured over every codepoint, U+0390 and the Hangul syllables reaching exactly 3. Past
# this ceiling a file cannot be the attested answer written again, whatever it holds.
# This factor bounds exactly the transformations `rewritten` undoes, and nothing else:
# adding one there without raising this is how the ceiling breaks in silence. The test
# `test_no_codepoint_decomposes_past_the_ceiling_the_launcher_reads_to` pins the figure.
MAX_REWRITTEN_FACTOR = 3
TERMINAL = ("DELIVERED", "COMPLETED", "CANCELLED")
MAX_INPUT = 1_000_000
# A receipt embeds the prompt, the contract and the snapshot: several copies plus base64.
MAX_RECEIPT = 16_000_000
NOTICE = (
    "La clé de signature a été générée dans le même bac à sable que GPT : la signature "
    "prouve que le reçu n'a pas été modifié, pas qui l'a produit. Seul `verify` lancé sur "
    "votre propre machine recalcule les statuts mécaniques."
)
APPROVAL = "FICTIVE : le jeton opérateur est détenu par le même bac à sable que GPT."
REVIEW = "NOT_RUN : aucune revue indépendante n'a été lancée."
SCOPE = "Ce reçu couvre answer.txt, pas le texte affiché dans le chat."


class Refusal(Exception):
    def __init__(self, code, message, hint="", exit_code=1, details=None):
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint
        self.exit_code, self.details = exit_code, details


class Help(Exception):
    def __init__(self, text):
        super().__init__(text)
        self.text = text


class Parser(argparse.ArgumentParser):
    """Usage errors and help become one JSON object, never free text or a traceback."""

    def error(self, message):
        raise Refusal(
            "USAGE", message, "Run: skillflow-prompts chatgpt <action> --help", exit_code=2
        )

    def print_help(self, file=None):
        raise Help(self.format_help())


def write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def write_once(path, data):
    """Content-addressed files are written once and never replaced by other bytes."""
    if path.exists():
        if path.read_bytes() != data:
            raise Refusal(
                "STATE_CONFLICT",
                f"{path.name} already exists with other bytes",
                "The state root was modified outside the tool; start a new root with init",
                exit_code=3,
            )
        return
    write_atomic(path, data)


def refresh_compilation(path, compilation):
    """A compilation is derived from its contract, never the other way round.

    A record stored by an earlier version of this tool describes the same contract with
    fewer fields: it is replaced by the current derivation, not refused, so a root created
    before an update keeps working. Other bytes for another contract remain a conflict,
    exactly as for every content-addressed file.
    """
    if path.exists():
        try:
            stored = strict_json(path.read_bytes()).get("contract_sha256")
        except ValueError:
            stored = None
        if stored != compilation["contract_sha256"]:
            raise Refusal(
                "STATE_CONFLICT",
                f"{path.name} already exists for another contract",
                "The state root was modified outside the tool; start a new root with init",
                exit_code=3,
            )
    write_atomic(path, canonical(compilation))


def installed():
    """Refuse an installation that cannot validate a schema, before it is relied upon.

    Windows resolves at most 260 characters per path unless long paths are enabled, and the
    deepest file an installation creates sits about a hundred characters below the folder
    one chooses. Past that, `uv sync --locked` truncates the installation **in silence**:
    the data files of the schema specifications are the first to go, and the crash then
    lands inside `verify`, after the reader believed their receipt was being checked. The
    probe builds here what every check needs, so a missing file is named at the door.
    """
    try:
        Draft202012Validator.check_schema({"type": "object"})
    except Exception as exc:  # noqa: BLE001 - whatever fails here, nothing can be checked
        raise Refusal(
            "INSTALL_INCOMPLETE",
            f"This installation cannot validate a schema: {type(exc).__name__}: {exc}",
            "Reinstall under a short path: on Windows, a chosen folder over about 150 "
            "characters truncates the installation in silence. Unzip into C:\\sfp, then "
            "run uv sync --locked again",
            exit_code=2,
        ) from exc


def read_input(value, limit=MAX_INPUT):
    path = Path(value)
    try:
        # One byte past the limit is enough to refuse it: a file is never read whole
        # before its size is known to be acceptable.
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
    except OSError as exc:
        raise Refusal(
            "INPUT_UNREADABLE",
            f"Cannot read {path}: {exc.strerror}",
            "Pass an existing file",
            exit_code=2,
        ) from exc
    if not 0 < len(data) <= limit:
        raise Refusal("INPUT_SIZE", f"The file must hold 1 to {limit:,} bytes", exit_code=2)
    return data


def normalized_input(value):
    try:
        return prompts.normalize(read_input(value))
    except UnicodeDecodeError as exc:
        raise Refusal("NOT_UTF8", "The file is not UTF-8 text", "Save it as UTF-8", 2) from exc


def root_path(value):
    root = Path(value)
    if not root.is_absolute():
        raise Refusal(
            "ROOT_RELATIVE",
            "--root must be an absolute path",
            "Use the same absolute state folder for every command",
            exit_code=2,
        )
    return root


def package_digest():
    """Digest of the installed tool code, so a receipt names the code that produced it."""
    folder = Path(__file__).resolve().parent
    files = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix in (".py", ".json"))
    return sha256(canonical([[str(p.relative_to(folder)).replace("\\", "/"),
                              sha256(p.read_bytes())] for p in files]))  # fmt: skip


class Session:
    """The authority of one state root, opened in this process for one command."""

    def __init__(self, root):
        self.root = root
        if not (root / STATE).is_file():
            raise Refusal(
                "NOT_INITIALIZED",
                f"No skillflow-prompts state in {root}",
                "Run: skillflow-prompts chatgpt init --root <same folder>",
            )
        self.state = strict_json((root / STATE).read_bytes())
        self.controller = Controller(root)
        self.principals = {
            role: authenticate(
                (root / (role + ".token")).read_text(encoding="utf-8"), self.controller.config
            )
            for role in ("writer", "operator")
        }
        self.workspace = root / "workspace"

    def save(self):
        write_atomic(self.root / STATE, canonical(self.state))

    def call(self, role, action, args):
        request = {"action": action, "arguments": args}
        if action not in READ:
            status = self.status(args["case_id"])
            request.update(revision=status["revision"], idempotency_key=uid())
        response = self.controller.call(self.principals[role], request)
        if not response["ok"]:
            error = response["error"]
            raise FlowError(error["code"], error["message"], error.get("missing", ()))
        return response["result"]

    def status(self, case_id):
        response = self.controller.call(
            self.principals["writer"], {"action": "flow_status", "arguments": {"case_id": case_id}}
        )
        if not response["ok"]:
            raise FlowError(response["error"]["code"], response["error"]["message"])
        return response["result"]

    def open_cases(self):
        """Unfinished ChatGPT cases, read from the authority's database, not from memory."""
        with self.controller.store.transaction() as db:
            rows = db.execute(
                "SELECT id, profile, state FROM cases WHERE case_key LIKE ?", ("auto:chatgpt:%",)
            ).fetchall()
        return [dict(row) for row in rows if row["state"] not in TERMINAL]

    def message_sha256(self, invocation_id):
        """Digest of the answer the authority currently holds, the only identity that counts."""
        with self.controller.store.transaction() as db:
            row = db.execute(
                "SELECT a.sha256 FROM inputs i JOIN artifacts a ON a.id=i.artifact_id "
                "WHERE i.invocation_id=? AND i.name='message'",
                (invocation_id,),
            ).fetchone()
        return row["sha256"] if row else None

    def rounds(self, case_id):
        """Rounds recorded by the authority: one per changed answer it accepted."""
        with self.controller.store.transaction() as db:
            rows = db.execute(
                "SELECT data FROM events WHERE case_id=? AND kind='input_submitted'", (case_id,)
            ).fetchall()
        per_invocation = {}
        for row in rows:
            event = strict_json(row["data"])
            if event.get("input") == "message":
                key = event["invocation_id"]
                per_invocation[key] = per_invocation.get(key, 0) + 1
        return max(per_invocation.values(), default=0)

    def admit(self, name):
        """Open or resume the case of a stored prompt through the existing skill admission."""
        bridge = ClientBridge(self.controller)
        verdict = bridge.handle(
            self.principals["writer"],
            {
                "binding": "auto",
                "client": CLIENT,
                "session_id": "chatgpt",
                "event": "UserPromptSubmit",
                "event_id": uid(),
                "cwd": str(self.workspace),
                "prompt": "$" + name,
            },
        )
        if not verdict.get("allow") or not verdict.get("status"):
            raise Refusal(
                "ADMISSION_REFUSED",
                verdict.get("reason") or "The authority did not admit this prompt",
                "Paste the prompt again with: chatgpt prompt --file <prompt>",
            )
        return verdict["status"]

    def contract(self, ref):
        return strict_json((self.root / "contracts" / (ref.split("@")[0] + ".json")).read_bytes())

    def compilation(self, ref):
        return strict_json(
            (self.root / "compilations" / (ref.split("@")[0] + ".json")).read_bytes()
        )

    def confirmations(self, prompt_sha256):
        """({id: who} ever confirmed for this prompt, whether an answer was read for it).

        A decision compiles another contract, so one prompt walks through several contracts
        and several cases, and the question belongs to the prompt, never to one case: asked
        per case, it would answer "none" on the case a decision has just opened, and asked
        of the contract that stands, it would answer "none" as soon as another prompt was
        pasted in between -- in both cases one command would be enough to lower a check an
        answer had already been read against.

        Read from the contracts the authority itself ran, each naming in its own origin line
        the prompt it compiles and who confirmed what. Neither the state file nor a later
        compilation can quietly drop one. Every confirmation of the prompt is gathered, not
        only the ones in force at the moment an answer was read: a reader who raised a check
        and lowered it again before any answer sees it come back if they paste the prompt
        after one, which errs towards checking more, never less.
        """
        with self.controller.store.transaction() as db:
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT c.id AS case_id, i.contract AS contract FROM cases c "
                    "JOIN invocations i ON i.case_id = c.id WHERE c.case_key LIKE ?",
                    ("auto:chatgpt:%",),
                ).fetchall()
            ]
        confirmed, answered = {}, False
        for row in rows:
            try:
                fields = origin_fields(strict_json(row["contract"].encode("utf-8")))
            except (KeyError, TypeError, ValueError):
                # A contract this tool did not compile says nothing about this prompt.
                continue
            if fields.get("prompt_sha256") != prompt_sha256:
                continue
            confirmed.update(prompts.decided(fields.get("confirmed", "")))
            answered = answered or bool(self.rounds(row["case_id"]))
        return confirmed, answered


def evaluate(compilations, answer):
    """Per-unit rows: mechanical checks rerun on the exact bytes, meaning left NON_VERIFIE."""
    rows = []
    for role, compilation in compilations:
        obligations = {
            o["id"]: o for step in compilation["contract"]["steps"] for o in step["obligations"]
        }
        for unit in compilation["units"]:
            for entry in unit["obligations"]:
                row = {"role": role, "unit": unit["n"], "text": unit["text"], "id": entry["id"]}
                if entry["class"] == "semantic":
                    row.update(
                        status="NON_VERIFIE", check="sens", detail="règle de sens, non contrôlée"
                    )
                elif entry["class"] == "candidate":
                    # A candidate has no obligation in the contract, so nothing here can
                    # reject the answer on its account: it is shown, with the word that set
                    # it aside and the way to confirm it.
                    reason = (
                        f"refusé par {entry['refused_by']}"
                        if "refused_by" in entry
                        else f"mis de côté par « {entry['demoted_by']} »"
                    )
                    row.update(
                        status="CANDIDAT",
                        check=describe(entry),
                        detail=f"candidat non confirmé, {reason} : affiché, jamais rejeté ; "
                        f"chatgpt confirm --id {entry['id']}",
                    )
                else:
                    result = verifiers.check(obligations[entry["id"]], {"message": answer})
                    row.update(check=describe(entry), **verdict(entry, result))
                    # A check that exists because someone confirmed it says so, by name:
                    # the receipt tells who confirmed each obligation, not who spoke last.
                    if "confirmed_by" in entry:
                        row["detail"] += f" ; confirmé par {entry['confirmed_by']}"
                rows.append(row)
    return rows


def describe(entry):
    if entry["parameter"] == "max_words":
        return f"au plus {entry['value']} mots"
    if entry["parameter"] == "min_words":
        return f"au moins {entry['value']} mots"
    if entry["parameter"] == "ends_with_question":
        return "finit par une question"
    if entry["parameter"] == "not_contains":
        return f"ne contient pas « {entry['value']} »"
    return f"contient « {entry['value']} »"


def verdict(entry, result):
    if entry["parameter"] == "max_words":
        if result["passed"]:
            return {
                "status": "VERIFIE",
                "detail": f"{result['words']} mots, limite {entry['value']}",
            }
        return {
            "status": "REJETE",
            "detail": f"{result['words']} mots pour {entry['value']} au plus : "
            f"retire au moins {result['words'] - entry['value']} mots",
        }
    if entry["parameter"] == "min_words":
        # The count shown is the one that decided, and it is not the one a limit uses: a
        # floor counts generously, so the same answer can read 21 words under a limit and
        # 33 under a floor. Saying which counted is what keeps that from looking like a bug.
        if result["passed"]:
            return {
                "status": "VERIFIE",
                "detail": f"{result['generous_words']} mots, plancher {entry['value']} "
                "(comptage large : apostrophe et trait d'union séparent deux mots)",
            }
        return {
            "status": "REJETE",
            "detail": f"{result['generous_words']} mots pour {entry['value']} au moins : "
            f"ajoute au moins {entry['value'] - result['generous_words']} mots "
            "(comptage large : apostrophe et trait d'union séparent deux mots)",
        }
    if entry["parameter"] == "ends_with_question":
        if result["passed"]:
            return {"status": "VERIFIE", "detail": "la réponse finit par une question"}
        return {
            "status": "REJETE",
            "detail": "la réponse ne finit pas par une question : le dernier caractère qui "
            "est une lettre, un chiffre ou un point d'interrogation doit être un point "
            "d'interrogation (ce qui décore la fin est ignoré)",
        }
    if entry["parameter"] == "not_contains":
        if result["passed"]:
            return {"status": "VERIFIE", "detail": "terme interdit absent"}
        return {
            "status": "REJETE",
            "detail": f"terme interdit présent : « {entry['value']} » "
            "(casse et accents comptent ; cherché comme mot entier, jamais dans un mot plus long)",
        }
    if result["passed"]:
        return {"status": "VERIFIE", "detail": "terme présent"}
    return {
        "status": "REJETE",
        "detail": f"terme absent : « {entry['value']} » (casse et accents comptent)",
    }


def counts(rows):
    keys = ("VERIFIE", "REJETE", "CANDIDAT", "ATTESTE", "NON_VERIFIE", "CONTEXTE")
    return {key: sum(row["status"] == key for row in rows) for key in keys}


def token(value):
    """An identifier a receipt shows as it is: short, printable, nothing else.

    A signed receipt is still data: a field rendered without being recomputed cannot be
    allowed to carry line ends or Markdown into what the reader is shown.
    """
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:@+-]{1,64}", value):
        raise Refusal(
            "RECEIPT_MALFORMED",
            "A receipt field shown to the reader is not a plain identifier",
            "It was not produced by this tool",
        )
    return value


def count(value):
    """A number a receipt shows as it is: a whole count, never a text to display."""
    if type(value) is not int or value < 0:
        raise Refusal(
            "RECEIPT_MALFORMED",
            "A receipt count is not a whole number",
            "It was not produced by this tool",
        )
    return value


def cell(text, limit=90):
    """A receipt shows data as data: what the reader sees is what the bytes hold.

    Control and direction characters become code points, and everything Markdown would
    read as syntax -- an HTML entity, an emphasis, a pipe, a backtick -- is neutralized,
    so a term made of the four characters "&lt;" is never displayed as "<".
    """
    shown = "".join(
        f"\\u{ord(c):04x}" if unicodedata.category(c) in ("Cc", "Cf") else c for c in text
    )
    shown = html.escape(shown, quote=False)
    shown = re.sub(r"([\\`*_\[\]|~])", r"\\\1", shown)
    return shown if len(shown) <= limit else shown[: limit - 1] + "…"


def table(rows):
    lines = ["| Unité | Consigne | Contrôle | Statut | Détail |", "|---|---|---|---|---|"]
    for row in rows:
        lines.append(
            f"| {row['unit']} | {cell(row['text'])} | {cell(row['check'])} | {row['status']} "
            f"| {cell(row['detail'])} |"
        )
    return "\n".join(lines)


def tally(rows):
    total = counts(rows)
    return " · ".join(f"{total[key]} {key}" for key in total)


def action_init(args):
    root = root_path(args.root)
    if (root / STATE).is_file():
        session = Session(root)
        return 0, {
            "ok": True,
            "initialized": False,
            "root": str(root),
            "key_id": session.controller.key_id,
            "summary": "Déjà initialisé : l'état existant est conservé tel quel.",
        }
    if root.exists() and any(root.iterdir()):
        raise Refusal(
            "ROOT_NOT_EMPTY",
            f"{root} is not empty and holds no skillflow-prompts state",
            "Choose an empty or new folder for --root",
            exit_code=2,
        )
    create_demo(root)
    config = strict_json((root / "config.json").read_bytes())
    clients = set(config["auto_admission"].get("clients", [])) | {CLIENT}
    config["auto_admission"]["clients"] = sorted(clients)
    write_atomic(root / "config.json", canonical(config))
    (root / "workspace" / ".chatgpt" / "skills").mkdir(parents=True, exist_ok=True)
    state = {
        "format": "skillflow-prompts-chatgpt-v1",
        "current": None,
        "last_receipt": None,
        "history": [],
    }
    write_atomic(root / STATE, canonical(state))
    session = Session(root)
    return 0, {
        "ok": True,
        "initialized": True,
        "root": str(root),
        "key_id": session.controller.key_id,
        "mode": "demo",
        "notice": NOTICE,
        "summary": "État créé. Prochaine étape : écrire le prompt dans un fichier, puis "
        "chatgpt prompt --file <fichier>.",
    }


def action_prompt(args):
    root = root_path(args.root)
    session = Session(root)
    try:
        compilation = prompts.compile_prompt(read_input(args.file))
    except UnicodeDecodeError as exc:
        raise Refusal("NOT_UTF8", "The file is not UTF-8 text", "Save it as UTF-8", 2) from exc
    except prompts.Conflict as exc:
        # Two explicit limits that contradict each other: nothing is opened, Said decides.
        raise Refusal(
            "CONTRACT_CONFLICT",
            f"Unit {exc.unit['n']} holds incompatible mechanical rules: {exc.detail}",
            "Ask Said which limit to keep, then paste the corrected prompt",
            details={"unit": exc.unit},
        ) from exc
    except prompts.TooLarge as exc:
        raise Refusal("PROMPT_TOO_LARGE", str(exc), "Shorten the prompt", exit_code=2) from exc
    # Once an answer has been read for this prompt, its confirmed checks come back with it.
    # Pasted again, the prompt would otherwise compile a contract carrying no decision at
    # all, which lowers every check at once, without naming anyone, and by a command that
    # `confirm --refuse` is refused for. Before any answer nothing is restored: pasting the
    # prompt again is then the documented way to start over. The author of each check comes
    # from the contract the authority ran, so the receipt still names who confirmed.
    confirmed, answered = session.confirmations(compilation["prompt_sha256"])
    if answered and confirmed:
        compilation = prompts.compile_prompt(
            compilation["contract"]["instructions"].encode("utf-8"), confirmed=confirmed
        )
    status, cancelled = open_case(root, session, compilation, "a new prompt")
    return 0, opened(status, cancelled, compilation)


def open_case(root, session, compilation, reason):
    """Store a compilation, register its prompt as the skill of a profile, cancel the other
    open ChatGPT cases and admit this one. Returns the case status and what was cancelled.

    Shared by `prompt` and `confirm`: a confirmation compiles a new contract, so it opens a
    new case exactly the way a new prompt does, and names the one it closes.
    """
    # The receipt will carry the contract, the unit rows and the snapshot: refuse now, before
    # anything is written, a compilation whose receipt could not be read back by verify.
    # Held here so that a confirmation is bounded exactly as a prompt is.
    if 3 * len(canonical(compilation)) > MAX_RECEIPT // 2:
        raise Refusal(
            "PROMPT_TOO_LARGE",
            "The receipt of this prompt would exceed what verify accepts",
            "Shorten the prompt, or confirm fewer obligations",
            exit_code=2,
        )
    contract, ref = compilation["contract"], compilation["contract_ref"]
    name = prompts.profile_name([ref])
    write_once(root / "contracts" / (contract["id"] + ".json"), canonical(contract))
    refresh_compilation(root / "compilations" / (contract["id"] + ".json"), compilation)
    prompt_bytes = contract["instructions"].encode("utf-8")
    write_once(session.workspace / ".chatgpt" / "skills" / name / "SKILL.md", prompt_bytes)
    config = strict_json((root / "config.json").read_bytes())
    config["profiles"][name] = {"coverage": "demo", "contracts": [ref]}
    config["skill_registry"][name] = [
        {"sha256": sha256(prompt_bytes), "contract": ref, "profile": name}
    ]
    write_atomic(root / "config.json", canonical(config))
    session = Session(root)
    cancelled = []
    for case in session.open_cases():
        if case["profile"] != name:
            session.call("operator", "flow_cancel", {"case_id": case["id"], "reason": reason})
            cancelled.append(case["id"])
    status = session.admit(name)
    current = session.state.get("current") or {}
    if current.get("case_id") != status["case_id"]:
        current = {
            "case_id": status["case_id"],
            "name": name,
            "contracts": [ref],
            "prompt_sha256": compilation["prompt_sha256"],
        }
    session.state["current"] = current
    session.save()
    return status, cancelled


def opened(status, cancelled, compilation):
    """What `prompt` and `confirm` both answer once a case is open on a compilation."""
    rows = []
    for unit in compilation["units"]:
        for entry in unit["obligations"]:
            row = {
                "unit": unit["n"],
                "text": unit["text"],
                "id": entry["id"],
                "kind": entry["class"],
                "check": "sens" if entry["class"] == "semantic" else describe(entry),
            }
            for key in ("demoted_by", "confirmed_by", "refused_by"):
                if key in entry:
                    row[key] = entry[key]
            rows.append(row)
    candidates = [r["id"] for r in rows if r["kind"] == "candidate"]
    summary = (
        f"{len(compilation['units'])} unités, "
        f"{sum(r['kind'] == 'mechanical' for r in rows)} contrôles mécaniques, "
        f"{len(candidates)} candidats affichés mais jamais rejetés, "
        f"{sum(r['kind'] == 'semantic' for r in rows)} règles de sens (NON_VERIFIE). "
    )
    if candidates:
        summary += (
            "Les candidats attendent une confirmation humaine : chatgpt confirm --by <qui> "
            "--id <identifiant> pour en faire un contrôle, --refuse <identifiant> pour en "
            "retirer un. Sinon, "
        )
    summary += "écrire la réponse dans un fichier, puis chatgpt answer --file <fichier>."
    return {
        "ok": True,
        "case_id": status["case_id"],
        "contract": compilation["contract_ref"],
        "contract_sha256": compilation["contract_sha256"],
        "prompt_sha256": compilation["prompt_sha256"],
        "compiled_by": compilation["compiled_by"],
        "confirmed": compilation["confirmed"],
        "refused": compilation["refused"],
        "confirmed_by": compilation["confirmed_by"],
        "units": rows,
        "cancelled_cases": cancelled,
        "summary": summary,
    }


def action_confirm(args):
    """Raise candidates into checks, or lower checks into candidates, under a name.

    A confirmation compiles a new contract from the same prompt, so the case opened on the
    old one is cancelled and a new one admitted, and the receipt will carry who confirmed.

    Once an answer has been read, only raising is still allowed. Raising adds a check and
    can therefore never excuse what was rejected; lowering would drop the very rule that
    rejected the answer, and nobody chooses the rules of a copy after reading it. Each
    raise opens a case on a stricter contract, with its own correction budget: a reader who
    buys attempts that way pays for them in checks, and once every candidate is raised
    there is nothing left to raise.
    """
    root = root_path(args.root)
    session = Session(root)
    current, status = current_case(session)
    if status["state"] in TERMINAL:
        raise Refusal(
            "CASE_CLOSED",
            f"The case is {status['state']}",
            "Paste the next prompt: chatgpt prompt --file <prompt>",
        )
    compilation = session.compilation(current["contracts"][0])
    if args.refuse and session.confirmations(compilation["prompt_sha256"])[1]:
        # Asked of the prompt and not of this case: a decision opens a case the authority
        # has read no answer for, so a per-case question would let the very next command
        # lower what a copy was already read against. Pasting the same prompt is no way out
        # either, and must not be advised as one: the case is addressed by its prompt.
        raise Refusal(
            "CONFIRM_AFTER_ANSWER",
            "A check cannot be lowered once an answer has been read against this prompt",
            "Raising is still open: --id only adds checks. To lower one, start from another "
            "prompt or another folder",
            exit_code=2,
        )
    # The name enters the contract's origin line, where fields are separated by ";" and
    # read back by `verify`, and the receipt shows it as a plain identifier. A free-form
    # name would be cut at its first ";" and the recompiled contract would no longer be
    # this one: only a plain identifier is accepted, and anything else is refused here.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}", args.by or ""):
        raise Refusal(
            "CONFIRM_BY_INVALID",
            "The name after --by must be a plain identifier",
            "Letters, digits, dot, underscore, @, + and dash, 1 to 64 characters: said, alice.b",
            exit_code=2,
        )
    raising, lowering = set(args.id or []), set(args.refuse or [])
    both = raising & lowering
    if both:
        raise Refusal(
            "CONFIRM_CONTRADICTION",
            "The same obligation is both confirmed and refused: " + ", ".join(sorted(both)),
            "Name each obligation once",
            exit_code=2,
        )
    # A later decision overrides an earlier one on the same obligation: what is raised now
    # leaves the refused set, what is lowered now leaves the confirmed set. Only one call
    # naming an obligation on both sides contradicts itself. Each decision keeps the name
    # of who made it, so a later confirmation by someone else renames nothing.
    # A compilation stored by an earlier version carries no decisions: read as none.
    confirmed = {i: who for i, who in compilation.get("confirmed", {}).items() if i not in lowering}
    confirmed.update(dict.fromkeys(raising, args.by))
    refused = {i: who for i, who in compilation.get("refused", {}).items() if i not in raising}
    refused.update(dict.fromkeys(lowering, args.by))
    prompt_bytes = compilation["contract"]["instructions"].encode("utf-8")
    try:
        recompiled = prompts.compile_prompt(prompt_bytes, confirmed=confirmed, refused=refused)
    except prompts.Confirmation as exc:
        raise Refusal(
            "CONFIRM_UNKNOWN",
            "Unknown obligations: " + ", ".join(exc.unknown),
            "Known obligations of this prompt: " + ", ".join(exc.known),
            exit_code=2,
            details={"known": exc.known},
        ) from exc
    except prompts.Conflict as exc:
        raise Refusal(
            "CONTRACT_CONFLICT",
            f"Unit {exc.unit['n']} holds incompatible mechanical rules: {exc.detail}",
            "Confirm only one of the limits",
            details={"unit": exc.unit},
        ) from exc
    except prompts.TooLarge as exc:
        # Every confirmed obligation repeats its unit in the contract: confirming many at
        # once can exceed what one receipt carries, and that is a refusal, not a crash.
        raise Refusal(
            "PROMPT_TOO_LARGE", str(exc), "Confirm fewer obligations at once", exit_code=2
        ) from exc
    status, cancelled = open_case(root, session, recompiled, "a confirmation")
    return 0, opened(status, cancelled, recompiled)


def current_case(session):
    current = session.state.get("current")
    if not current:
        raise Refusal(
            "NO_PROMPT",
            "No prompt is waiting for an answer",
            "Paste a prompt first: chatgpt prompt --file <prompt>",
        )
    status = session.status(current["case_id"])
    return current, status


def action_answer(args):
    root = root_path(args.root)
    session = Session(root)
    current, status = current_case(session)
    if status["state"] in TERMINAL:
        raise Refusal(
            "CASE_CLOSED",
            f"The case is {status['state']}",
            "Paste the next prompt: chatgpt prompt --file <prompt>",
        )
    case_id = current["case_id"]
    answer = normalized_input(args.file)
    digest = sha256(answer)
    compilations = [("prompt", session.compilation(ref)) for ref in current["contracts"]]
    rows = evaluate(compilations, answer)
    # Admission again: rebuilds the workspace binding of this process and rechecks the
    # stored prompt bytes before anything is submitted.
    status = session.admit(current["name"])
    if status["case_id"] != case_id:
        raise Refusal("CASE_MISMATCH", "The authority resumed another case", exit_code=3)
    # Whether this answer is new is decided by what the authority holds, never by a file
    # of this tool: a command killed halfway leaves the database right and the file wrong.
    changed = any(
        session.message_sha256(skill["invocation_id"]) != digest for skill in status["skills"]
    )
    # A new answer invalidates every step, validated ones included, and each step would need
    # one more check: with no check left, the authority must keep what it already accepted.
    spent = any(
        step["budget_remaining"] == 0 for skill in status["skills"] for step in skill["steps"]
    )
    if changed and spent:
        raise Refusal(
            "BUDGET_EXHAUSTED",
            "The correction budget of this case is spent: no new answer can be checked",
            "Deliver the answer the authority already accepted, if any; "
            "otherwise paste a new prompt to open another case",
            details={"rounds_used": session.rounds(case_id)},
        )
    if changed:
        for skill in status["skills"]:
            session.call(
                "writer",
                "message_submit",
                {
                    "case_id": case_id,
                    "invocation_id": skill["invocation_id"],
                    "content_base64": base64.b64encode(answer).decode(),
                },
            )
    refused = []
    for skill in session.status(case_id)["skills"]:
        for step in skill["steps"]:
            try:
                session.call(
                    "writer",
                    "flow_check",
                    {
                        "case_id": current["case_id"],
                        "invocation_id": skill["invocation_id"],
                        "step_id": step["name"],
                    },
                )
            except FlowError as exc:
                # CHECK_FAILED is the rejection itself; STAGNATION means the same rejected
                # bytes again, refused by the authority without consuming a round.
                if exc.code not in ("CHECK_FAILED", "STAGNATION"):
                    refused.append({"step": step["name"], "code": exc.code, "message": exc.message})
    status = session.status(case_id)
    rejected = [row for row in rows if row["status"] == "REJETE"]
    failed = any(step["state"] != "VALIDATED" for s in status["skills"] for step in s["steps"])
    if bool(rejected) != failed and not refused:
        raise Refusal(
            "VERDICT_MISMATCH",
            "The authority and the local rerun disagree on the mechanical checks",
            "Report this output: the installed code may have been modified",
            exit_code=3,
        )
    remaining = min(step["budget_remaining"] for s in status["skills"] for step in s["steps"])
    exhausted = any(r["code"] == "BUDGET_EXHAUSTED" for r in refused)
    ok = not rejected and not refused
    if ok:
        summary = "Tous les contrôles mécaniques passent. Prochaine étape : chatgpt deliver."
    elif exhausted or remaining == 0:
        summary = (
            "Budget de corrections épuisé : la remise est refusée pour ce prompt. "
            "Colle un nouveau prompt pour ouvrir un autre dossier."
        )
    else:
        summary = (
            "Corrige seulement les lignes REJETE, garde le reste, puis relance chatgpt answer."
        )
    if not changed:
        summary += " Réponse identique à celle que l'autorité détient : rien n'a été consommé."
    return (0 if ok else 1), {
        "ok": ok,
        "case_id": case_id,
        "answer_sha256": digest,
        "rows": rows,
        "counts": counts(rows),
        "refusals": refused,
        "rounds_used": session.rounds(case_id),
        "rounds_remaining": remaining,
        "table": table(rows),
        "summary": summary,
    }


def snapshot_of(session, case_id, delivery_id):
    return session.call(
        "operator", "delivery_read", {"case_id": case_id, "delivery_id": delivery_id}
    )["snapshot"]


def completed_delivery(session, case_id):
    with session.controller.store.transaction() as db:
        row = db.execute(
            "SELECT id FROM deliveries WHERE case_id=? AND state='COMPLETED' "
            "ORDER BY rowid DESC LIMIT 1",
            (case_id,),
        ).fetchone()
    return row["id"] if row else None


def action_deliver(args):
    root = root_path(args.root)
    session = Session(root)
    last = session.state.get("last_receipt")
    if not session.state.get("current") and last and (Path(last) / "receipt.json").is_file():
        # Delivering twice opens nothing: the last receipt is verified again, then shown.
        folder = Path(last)
        code, verified = run(
            [
                "verify",
                "--receipt",
                str(folder / "receipt.json"),
                "--answer",
                str(folder / "answer.txt"),
                "--root",
                str(root),
            ]
        )
        if code != 0:
            raise Refusal(
                "RECEIPT_CORRUPT",
                "The last receipt no longer verifies",
                "The receipt folder was modified after delivery; nothing is returned",
                details=verified,
            )
        return 0, {
            "ok": True,
            "already_delivered": True,
            "receipt_folder": last,
            "receipt": verified["receipt"],
            "summary": "Déjà remis : reçu de la dernière remise, revérifié avant affichage.",
        }
    current, status = current_case(session)
    case_id = current["case_id"]
    if status["state"] == "CANCELLED":
        raise Refusal("CASE_CLOSED", "The case was cancelled", "Paste the prompt again")
    if status["state"] != "COMPLETED":
        session.admit(current["name"])
        try:
            demand = session.call(
                "writer", "delivery_request", {"case_id": case_id, "destination": "local"}
            )
        except FlowError as exc:
            raise Refusal(
                "DELIVERY_REFUSED",
                "The authority refuses the delivery: " + exc.message,
                "Run chatgpt answer until every mechanical check passes; "
                "check rounds_remaining with chatgpt status",
                details={"code": exc.code, "missing": exc.missing},
            ) from exc
        session.call(
            "operator",
            "approval_submit",
            {
                "case_id": case_id,
                "delivery_id": demand["delivery_id"],
                "snapshot_hash": demand["snapshot_hash"],
                "decision": "approve",
            },
        )
        session.call(
            "operator",
            "delivery_commit",
            {"case_id": case_id, "delivery_id": demand["delivery_id"]},
        )
        drain_outbox(session.controller)
    completion = session.call("writer", "completion_read", {"case_id": case_id})
    snapshot = snapshot_of(session, case_id, completed_delivery(session, case_id))
    answer = completion["messages"][0]["content"].encode("utf-8")
    compilations = [("prompt", session.compilation(ref)) for ref in current["contracts"]]
    rows = evaluate(compilations, answer)
    public = session.controller.key.public_key().public_bytes_raw()
    text = answer.decode("utf-8")
    words = text.split()
    payload = {
        "kind": "skillflow-prompts-receipt-v1",
        "product": "skillflow-prompts",
        "version": __version__,
        "case_id": case_id,
        "prompt_sha256": current["prompt_sha256"],
        "kernel_sha256": None,
        "contracts": [
            {
                "role": role,
                "ref": c["contract_ref"],
                "sha256": c["contract_sha256"],
                "compiled_by": c["compiled_by"],
                # Absent from a compilation stored by an earlier version: no decision then.
                "confirmed_by": c.get("confirmed_by"),
                "contract": c["contract"],
            }
            for role, c in compilations
        ],
        "answer_sha256": sha256(answer),
        "answer_words": verifiers.words(text),
        "answer_start": " ".join(words[:6]),
        "answer_end": " ".join(words[-6:]),
        "units": rows,
        "counts": counts(rows),
        "rounds_used": session.rounds(case_id),
        "review": "NOT_RUN",
        "approval": "FICTIVE",
        "snapshot": snapshot,
        "completion": completion["completion"],
        "public_key": base64.b64encode(public).decode(),
        "key_id": session.controller.key_id,
        "code": {
            "verifiers_sha256": verifiers.implementation_hash(),
            "package_sha256": package_digest(),
        },
        "notice": NOTICE,
    }
    envelope = receipts.sign(payload, session.controller.key)
    if len(canonical(envelope)) > MAX_RECEIPT:
        raise Refusal("RECEIPT_TOO_LARGE", "The receipt exceeds what verify accepts", exit_code=3)
    folder = root / "receipts" / case_id
    prompt_text = compilations[0][1]["contract"]["instructions"].encode("utf-8")
    write_atomic(folder / "answer.txt", answer)
    write_atomic(folder / "prompt.txt", prompt_text)
    for role, c in compilations:
        write_atomic(folder / f"contract-{role}.json", canonical(c["contract"]))
    write_atomic(folder / "receipt.json", canonical(envelope))
    readable = render(payload)
    write_atomic(folder / "RECU.md", readable.encode("utf-8"))
    session.state["history"].append(
        {
            "case_id": case_id,
            "prompt_sha256": current["prompt_sha256"],
            "rounds": payload["rounds_used"],
        }
    )
    session.state["current"] = None
    session.state["last_receipt"] = str(folder)
    session.save()
    return 0, {
        "ok": True,
        "case_id": case_id,
        "receipt_folder": str(folder),
        "files": sorted(p.name for p in folder.iterdir()),
        "counts": counts(rows),
        "receipt": readable,
        "summary": "Remis. Recopie answer.txt à l'identique, puis le reçu lisible tel quel.",
    }


def render(payload):
    rows = payload["units"]
    return "\n".join(
        [
            f"# Reçu skillflow-prompts {token(payload['version'])} — "
            f"dossier {token(payload['case_id'])[:8]}",
            "",
            f"Réponse contrôlée : answer.txt, sha256 {token(payload['answer_sha256'])[:16]}…, "
            f"{payload['answer_words']} mots, commence par « {cell(payload['answer_start'], 60)} », "
            f"finit par « {cell(payload['answer_end'], 60)} ».",
            # When the file read is not the receipted bytes, the receipt shown says so
            # itself: it is the only artefact a reader is asked to read.
            *(
                [
                    "Le fichier relu n'est PAS octet pour octet celui du reçu : son sha256 est "
                    f"{token(payload['answer_read_sha256'])[:16]}…. Il redonne le texte attesté "
                    "après normalisation (marque d'ordre des octets, fins de ligne Windows, "
                    "composition Unicode), et c'est sur le texte attesté que tout est recalculé."
                ]
                if payload.get("answer_read_sha256")
                else []
            ),
            f"Prompt : sha256 {token(payload['prompt_sha256'])[:16]}…. "
            f"Contrat : {token(payload['contracts'][0]['ref'])} (compilé par : "
            f"{token(payload['contracts'][0]['compiled_by'])}"
            # Who decided, confirmations and refusals alike: naming them "confirmed by" would
            # credit a refusal as a confirmation. Each row says which of the two it was.
            + (
                " ; décisions déclarées par : "
                + ", ".join(token(name) for name in payload["contracts"][0]["confirmed_by"])
                + ", identité déclarée au clavier, non vérifiée"
                if payload["contracts"][0].get("confirmed_by")
                else " ; aucune décision : les candidats sont affichés, jamais rejetés"
            )
            + ").",
            "",
            table(rows),
            "",
            f"Comptes : {tally(rows)}. Tours utilisés : {count(payload['rounds_used'])} "
            "(compté par l'autorité à la remise ; verify ne peut pas le recalculer hors ligne).",
            f"Revue : {REVIEW}",
            f"Approbation : {APPROVAL}",
            f"Clé {token(payload['key_id'])[:16]}… : {NOTICE}",
            SCOPE,
        ]
    )


def rewritten(data):
    """The same text, read back the way a text editor saves it on its own.

    Deliberately narrower than `prompts.normalize`: a leading byte order mark, Windows
    line ends and Unicode composition are what an editor changes by itself, and none of
    them changes what a reader sees. A lone carriage return is not one of them. Windows
    writes CRLF, and a bare CR sends the cursor back to the start of the line in a
    terminal, so the line before it disappears from the display: a file holding one does
    not show the attested text, and it stays refused.
    """
    text = data.removeprefix(BOM).replace(b"\r\n", b"\n").decode("utf-8")
    return unicodedata.normalize("NFC", text).encode("utf-8")


def action_verify(args):
    try:
        envelope = strict_json(read_input(args.receipt, MAX_RECEIPT))
        raw = base64.b64decode(envelope["payload"], validate=True)
        payload = strict_json(raw)
        public = base64.b64decode(payload["public_key"], validate=True)
        Ed25519PublicKey.from_public_bytes(public).verify(
            base64.b64decode(envelope["signature"], validate=True), receipts.DOMAIN + raw
        )
    except (InvalidSignature, KeyError, TypeError, ValueError) as exc:
        raise Refusal(
            "RECEIPT_INVALID",
            "The receipt signature does not match its content",
            "The receipt was edited or is not a skillflow-prompts receipt",
        ) from exc
    # Read past the limit of what a receipt can hold, by the measured ceiling of a
    # rewriting, then let the comparison below decide. The normalized form is bounded by
    # the receipted bytes, so nothing larger is admitted by reading further.
    answer = read_input(args.answer, MAX_REWRITTEN_FACTOR * MAX_INPUT + len(BOM))
    try:
        return verify_payload(args, payload, public, answer)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        raise Refusal(
            "RECEIPT_MALFORMED",
            "The receipt is signed but its content is not a well-formed receipt",
            "It was not produced by this tool",
        ) from exc


def verify_payload(args, payload, public, answer):
    """Every status is recomputed here from the answer and the prompt, never read back."""
    checks = {"signature": True}
    if payload.get("kind") != "skillflow-prompts-receipt-v1":
        raise Refusal("RECEIPT_KIND", "Not a skillflow-prompts receipt")
    read_sha256 = None
    checks["answer"] = sha256(answer) == payload["answer_sha256"]
    if not checks["answer"]:
        # Whether the file had to be read again is a fact about the input, not a verdict on
        # it: `checks` holds verdicts, and it holds only booleans. The fact is told beside
        # them, under `answer_rewritten`, in the summary and in the receipt shown.
        # Written again by an editor, the same answer is the same answer. It is accepted
        # under its own name, "normalized", never silently: the digest of the file as read
        # goes into the receipt shown, so a reader sees that the bytes are not the
        # receipted ones. The normalized bytes then replace the ones read, so every figure
        # below is recomputed from exactly what the receipt attests.
        same = None
        try:
            same = rewritten(answer)
        except UnicodeDecodeError:
            pass
        if same is not None and sha256(same) == payload["answer_sha256"]:
            checks["answer"], read_sha256, answer = True, sha256(answer), same
        else:
            raise Refusal(
                "ANSWER_CHANGED",
                "The answer file differs from the receipted bytes",
                "Use the answer.txt delivered with this receipt",
            )
    stated = {c["sha256"] for c in payload["contracts"]}
    for path in args.contract or []:
        # The receipt is intact here: a file of the reader's own that cannot be read is
        # refused under its own name. `strict_json` raises ValueError, which the net held
        # out for a malformed receipt would otherwise catch and blame on the receipt.
        try:
            given = sha256(canonical(strict_json(read_input(path, MAX_RECEIPT))))
        except ValueError as exc:
            raise Refusal(
                "CONTRACT_NOT_JSON",
                f"{Path(path).name} is not JSON this tool can read",
                "Pass the contract-*.json of the receipt folder",
                exit_code=2,
            ) from exc
        if given not in stated:
            raise Refusal("CONTRACT_CHANGED", f"{path} is not the contract of this receipt")
    # The prompt is recompiled from the signed contract: the same tool must produce the
    # same contract, so the classification of every unit is reproduced, not trusted. The
    # reference, the compiler and the prompt digest are recomputed the same way.
    compilations = [
        (
            c["role"],
            recompile(c["contract"]),
        )
        for c in payload["contracts"]
    ]
    checks["contracts"] = all(
        sha256(canonical(c["contract"])) == c["sha256"] == compiled["contract_sha256"]
        and c["ref"] == compiled["contract_ref"]
        and c["compiled_by"] == compiled["compiled_by"]
        for c, (_, compiled) in zip(payload["contracts"], compilations, strict=True)
    )
    checks["prompt"] = payload["prompt_sha256"] == compilations[0][1]["prompt_sha256"]
    if args.prompt:
        # Same boundary as the contract above: this file belongs to the reader.
        try:
            expected = sha256(prompts.normalize(read_input(args.prompt)))
        except UnicodeDecodeError as exc:
            raise Refusal(
                "PROMPT_NOT_UTF8",
                f"{Path(args.prompt).name} is not UTF-8 text",
                "Pass the prompt.txt of the receipt folder",
                exit_code=2,
            ) from exc
        checks["prompt_file"] = expected == payload["prompt_sha256"]
    # Every figure shown about the answer is recomputed from the answer itself.
    text = answer.decode("utf-8")
    pieces = text.split()
    recomputed_meta = {
        "answer_words": verifiers.words(text),
        "answer_start": " ".join(pieces[:6]),
        "answer_end": " ".join(pieces[-6:]),
    }
    checks["metadata"] = all(payload[key] == value for key, value in recomputed_meta.items())
    completion = payload["completion"]
    completion_raw = base64.b64decode(completion["payload"], validate=True)
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(
            base64.b64decode(completion["signature"], validate=True),
            receipts.DOMAIN + completion_raw,
        )
    except InvalidSignature as exc:
        raise Refusal(
            "COMPLETION_INVALID", "The authority completion is not signed by this key"
        ) from exc
    closing = strict_json(completion_raw)
    snapshot = payload["snapshot"]
    flow = snapshot["flow"]
    hashes = {
        c["ref"]: sha256(canonical(Contract.model_validate(c["contract"]).model_dump()))
        for c in payload["contracts"]
    }
    checks["completion"] = (
        closing.get("kind") == "skillflow-workflow-completion-v1"
        and closing.get("case_id") == payload["case_id"]
        and closing.get("snapshot_sha256") == sha256(canonical(snapshot))
        and flow["case_id"] == payload["case_id"]
        and {i["contract_hash"] for i in flow["invocations"]} == set(hashes.values())
        and all(
            i["sha256"] == payload["answer_sha256"]
            for i in flow["inputs"]
            if i["name"] == "message"
        )
        and any(i["name"] == "message" for i in flow["inputs"])
    )
    recomputed = evaluate(compilations, answer)
    checks["statuses"] = recomputed == payload["units"]
    # The anchor is the key that actually verified the signature, never the identifier
    # the receipt claims for itself.
    actual = receipts.key_id(public)
    checks["key_consistent"] = payload["key_id"] == actual
    anchored = None
    if args.root:
        trust = strict_json((root_path(args.root) / "trust.json").read_bytes())
        anchored = actual in trust
    elif args.key_id:
        anchored = actual == args.key_id
    checks["key_anchored"] = anchored
    checks["same_code"] = payload["code"]["verifiers_sha256"] == verifiers.implementation_hash()
    ok = all(value for key, value in checks.items() if key not in ("key_anchored", "same_code"))
    if anchored is False:
        ok = False
    key_text = (
        "clé ancrée"
        if anchored
        else "clé non ancrée : la signature prouve seulement la cohérence interne du reçu"
    )
    # The receipt shown is rendered from the recomputed values, never from the stored text.
    shown = dict(
        payload,
        units=recomputed,
        counts=counts(recomputed),
        prompt_sha256=compilations[0][1]["prompt_sha256"],
        contracts=[
            {
                "role": role,
                "ref": compiled["contract_ref"],
                "sha256": compiled["contract_sha256"],
                "compiled_by": compiled["compiled_by"],
                "confirmed_by": compiled["confirmed_by"],
            }
            for role, compiled in compilations
        ],
        answer_read_sha256=read_sha256,
        **recomputed_meta,
    )
    # A different verifier code is not a forgery: the signature holds and nothing was
    # edited, so it stays out of the verdict. But it must not pass unsaid to a reader who
    # only reads this one line.
    code_text = (
        ""
        if checks["same_code"]
        else " Attention : les vérificateurs qui relisent ce reçu n'ont pas le code de ceux "
        "qui l'ont écrit, donc les statuts ont été recalculés par un autre code."
    )
    # A contract this version cannot reproduce is not always a contract someone edited: a
    # receipt written by another version was compiled by another compiler, which may read
    # a wording this one does not, or the other way round. The verdict does not move --
    # reproducing the contract from its own prompt is what makes a status trustworthy --
    # but a reader is owed the cause and the remedy instead of a bare failure. The version
    # is read from the signed origin, so a forged line changes this wording and nothing else.
    stated_versions = sorted({stated_version(c["contract"]) for c in payload["contracts"]} - {None})
    version_text = (
        ""
        if checks["contracts"] or not stated_versions or prompts.__version__ in stated_versions
        else " Ce reçu a été écrit par skillflow-prompts "
        + ", ".join(stated_versions)
        + f" et relu par la version {prompts.__version__} : son contrat ne se recompile pas "
        "à l'identique ici. Vérifie-le avec la version qui l'a écrit."
    )
    return (0 if ok else 1), {
        "ok": ok,
        "case_id": payload["case_id"],
        "answer_rewritten": read_sha256 is not None,
        "checks": checks,
        "counts": counts(recomputed),
        "receipt": render(shown),
        "summary": (
            "Reçu vérifié, statuts mécaniques recalculés. " if ok else "Vérification échouée. "
        )
        + key_text
        + "."
        + (
            " Le fichier de réponse relu n'est pas octet pour octet celui du reçu : il "
            "redonne le texte attesté après normalisation, marque d'ordre des octets, fins "
            "de ligne Windows ou composition Unicode. Son empreinte est dans le reçu affiché."
            if read_sha256
            else ""
        )
        + version_text
        + code_text,
    }


def origin_fields(contract):
    """The fields a contract's origin line states about its own compilation.

    `compiled_by` is required; the confirmations are present only when some were made.
    Read from the signed contract, never from anything beside it.
    """
    fields = {}
    for piece in contract["origin"].split(";"):
        key, sep, value = piece.strip().partition(":")
        if sep:
            fields[key] = value
    if "compiled_by" not in fields:
        raise ValueError("origin without compiled_by")
    return fields


def compiled_by_of(contract):
    """Who compiled a contract, as its own origin line states it."""
    return origin_fields(contract)["compiled_by"]


def stated_version(contract):
    """The version of this tool a contract's origin line claims wrote it, or None.

    Claimed, never trusted: it explains a contract this version does not reproduce, and
    decides nothing.
    """
    name, _, version = contract["origin"].split(";")[0].strip().partition(" ")
    return version.strip() if name == "skillflow-prompts" and version.strip() else None


def recompile(contract):
    """The same compilation again, from the signed contract alone: prompt, compiler and
    confirmations are all read from it, so a verification reproduces instead of trusting."""
    fields = origin_fields(contract)
    return prompts.compile_prompt(
        contract["instructions"].encode("utf-8"),
        fields["compiled_by"],
        confirmed=prompts.decided(fields.get("confirmed", "")),
        refused=prompts.decided(fields.get("refused", "")),
    )


def action_status(args):
    root = root_path(args.root)
    session = Session(root)
    current = session.state.get("current")
    result = {
        "ok": True,
        "root": str(root),
        "key_id": session.controller.key_id,
        "current": None,
        "last_receipt": session.state.get("last_receipt"),
        "delivered": len(session.state.get("history", [])),
    }
    if current:
        status = session.status(current["case_id"])
        result["current"] = {
            "case_id": current["case_id"],
            "state": status["state"],
            "rounds_used": session.rounds(current["case_id"]),
            "rounds_remaining": min(
                step["budget_remaining"] for s in status["skills"] for step in s["steps"]
            ),
            "contracts": current["contracts"],
        }
    result["summary"] = (
        f"Dossier en cours : {result['current']['state']}, "
        f"tours utilisés {result['current']['rounds_used']}."
        if current
        else "Aucun dossier en cours."
    )
    return 0, result


ACTIONS = {
    "init": action_init,
    "prompt": action_prompt,
    "confirm": action_confirm,
    "answer": action_answer,
    "deliver": action_deliver,
    "verify": action_verify,
    "status": action_status,
}


def parser():
    top = Parser(prog="skillflow-prompts chatgpt", description="One command, one process.")
    actions = top.add_subparsers(dest="action", required=True, parser_class=Parser)
    for name in ACTIONS:
        item = actions.add_parser(name)
        item.add_argument("--root", required=name != "verify")
        if name in ("prompt", "answer"):
            item.add_argument("--file", required=True)
        if name == "confirm":
            item.add_argument("--by", required=True)
            item.add_argument("--id", action="append")
            item.add_argument("--refuse", action="append")
        if name == "verify":
            item.add_argument("--receipt", required=True)
            item.add_argument("--answer", required=True)
            item.add_argument("--contract", action="append")
            item.add_argument("--prompt")
            item.add_argument("--key-id")
    return top


def run(argv):
    try:
        args = parser().parse_args(argv)
        # Asked of every action, not of `init` alone: the verification at home starts at
        # `verify` and never runs `init`.
        installed()
        return ACTIONS[args.action](args)
    except Help as exc:
        return 0, {"ok": True, "usage": exc.text, "summary": "Aide."}
    except Refusal as exc:
        result = {
            "ok": False,
            "error": {"code": exc.code, "message": exc.message, "hint": exc.hint},
        }
        if exc.details:
            result["details"] = exc.details
        return exc.exit_code, result
    except FlowError as exc:
        return 1, {
            "ok": False,
            "error": {"code": exc.code, "message": exc.message, "hint": "See chatgpt status"},
            "missing": exc.missing,
        }
    except Exception as exc:  # noqa: BLE001 - one JSON object, never a bare traceback
        return 3, {
            "ok": False,
            "error": {
                "code": "INTERNAL",
                "message": f"{type(exc).__name__}: {exc}",
                "hint": "Report this output; nothing was retried",
            },
        }


def main(argv=None):
    code, result = run(list(sys.argv[1:] if argv is None else argv))
    sys.stdout.buffer.write(
        json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    )
    sys.stdout.flush()
    return code

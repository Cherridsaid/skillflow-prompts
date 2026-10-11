"""Client admission against operator-bound cases, never client-selected contracts.

Hook events are untrusted observations. They may activate an already compulsory
contract; they cannot approve work, weaken a profile or supply authoritative proof.
"""

import re
import threading
from pathlib import Path
from typing import Literal

from pydantic import Field

from . import api
from .models import Strict
from .registry import Principal, covers
from .util import FlowError, canonical, require, sha256, strict_json


class ClientEvent(Strict):
    binding: str = Field(min_length=1, max_length=128)
    client: Literal["codex", "claude"]
    session_id: str = Field(min_length=1, max_length=128)
    event: Literal[
        "SessionStart",
        "UserPromptSubmit",
        "UserPromptExpansion",
        "PreToolUse",
        "PostToolUse",
        "PostToolBatch",
        "Stop",
        "PreCompact",
        "PostCompact",
        "SessionEnd",
    ]
    event_id: str = Field(min_length=1, max_length=128)
    cwd: str = Field(min_length=1, max_length=4096)
    prompt: str = Field(default="", max_length=100_000)
    command_name: str = Field(default="", max_length=256)
    tool_name: str = Field(default="", max_length=256)
    tool_input: dict = Field(default_factory=dict)


def skill_file_errors(config, case_key, registry=None):
    """Installation integrity also gates direct API calls and automatic delivery."""
    from .catalog import watch_errors

    errors = watch_errors(config)
    for binding_id, binding in config.get("client_bindings", {}).items():
        if binding["case_key"] != case_key:
            continue
        from .catalog import binding_errors

        errors.extend(binding_errors(config, binding, registry))
        for name, skill in binding["skills"].items():
            for source in skill["files"]:
                try:
                    changed = sha256(Path(source["path"]).read_bytes()) != source["sha256"]
                except OSError:
                    errors.append("native_skill_unavailable:" + binding_id + ":" + name)
                else:
                    if changed:
                        errors.append("native_skill_changed:" + binding_id + ":" + name)
    return errors


def profile_closure(controller, profile):
    """Compulsory contracts of a profile plus every child slot they declare, transitively."""
    pending = list(controller.config["profiles"][profile]["contracts"])
    closure = set()
    while pending:
        ref = pending.pop()
        if ref in closure:
            continue
        closure.add(ref)
        pending.extend(child.contract for child in controller.registry.get(ref).children)
    return closure


def validate_bindings(controller):
    scopes = set()
    for key, binding in controller.config.get("client_bindings", {}).items():
        validate_binding(controller, binding, scopes, dynamic=key.startswith("auto:"))


def validate_binding(controller, binding, scopes=None, *, dynamic=False):
    """One binding, static or created at admission: the same invariants either way."""
    scopes = set() if scopes is None else scopes
    if True:
        require(binding["client"] in ("codex", "claude"), "CLIENT_CONFIG", "Unknown client")
        key = binding["case_key"]
        profile = controller.config["case_bindings"].get(key)
        require(profile, "CLIENT_CONFIG", "Binding needs an operator-provisioned case")
        mandatory = profile_closure(controller, profile)
        require(binding.get("skills"), "CLIENT_CONFIG", "Approved skill mappings required")
        require(
            Path(binding["workspace"]).is_absolute(), "CLIENT_CONFIG", "Absolute workspace required"
        )
        scope = (binding["client"], Path(binding["workspace"]).resolve())
        # Dynamic bindings are keyed by workspace and profile: several may share one
        # workspace, but they never collide with an operator-installed binding.
        require(
            dynamic or scope not in scopes, "CLIENT_CONFIG", "Workspace has ambiguous case bindings"
        )
        if not dynamic:
            scopes.add(scope)
        writers = {p["id"] for p in controller.config["principals"] if p["role"] == "writer"}
        require(
            binding.get("principals") and set(binding["principals"]) <= writers,
            "CLIENT_CONFIG",
            "Binding needs existing writer credentials",
        )
        require(
            type(binding.get("automatic_delivery", False)) is bool,
            "CLIENT_CONFIG",
            "Delivery flag must be boolean",
        )
        if binding.get("automatic_delivery"):
            require(
                binding.get("destination") in controller.config["destinations"],
                "CLIENT_CONFIG",
                "Unknown delivery destination",
            )
        for name, skill in binding["skills"].items():
            require(re.fullmatch(r"[A-Za-z0-9_.:-]+", name), "CLIENT_CONFIG", "Invalid skill name")
            require(
                skill["contract"] in mandatory, "CLIENT_CONFIG", "Skill contract is not compulsory"
            )
            require(skill.get("files"), "CLIENT_CONFIG", "Exact approved skill files required")
            for source in skill["files"]:
                require(
                    Path(source["path"]).is_absolute()
                    and re.fullmatch(r"[a-f0-9]{64}", source["sha256"]),
                    "CLIENT_CONFIG",
                    "Approved file path and SHA-256 required",
                )


class ClientBridge:
    def __init__(self, controller):
        self.controller = controller
        # ponytail: one daemon serializes admission; use a durable queue if distributed.
        self.lock = threading.RLock()
        # Session -> case key it engaged by invoking a skill. The restriction lives only
        # for that session and that turn: a new prompt that names no skill releases it.
        # ponytail: in memory, a restart releases every session (fail-open, the hook is
        # not a security boundary); persist it if a restart must keep a turn restricted.
        self.engaged = {}

    def _call(self, actor, action, args, key=None):
        from .api import READ

        request = {"action": action, "arguments": args}
        if action not in READ:
            identity = "client:" + sha256(canonical([action, args, key]))
            with self.controller.store.transaction() as db:
                old = db.execute(
                    "SELECT response FROM operations WHERE principal=? AND key=?",
                    (actor.id, identity),
                ).fetchone()
                if old:
                    response = strict_json(old[0])
                    require(
                        response["ok"],
                        response.get("error", {}).get("code", "CLIENT_ADMISSION"),
                        response.get("error", {}).get("message", "Previous admission failed"),
                    )
                    return response
                if action == "flow_start":
                    case = db.execute(
                        "SELECT revision FROM cases WHERE case_key=?", (args["case_key"],)
                    ).fetchone()
                else:
                    case = db.execute(
                        "SELECT revision FROM cases WHERE id=?", (args["case_id"],)
                    ).fetchone()
                request.update(revision=case[0] if case else 0, idempotency_key=identity)
        response = self.controller.call(actor, request)
        require(
            response["ok"],
            response.get("error", {}).get("code", "CLIENT_ADMISSION"),
            response.get("error", {}).get("message", "Client admission failed"),
        )
        return response

    def handle(self, principal, payload):
        event = ClientEvent.model_validate(payload)
        bindings = self.controller.config.get("client_bindings", {})
        auto = self.controller.config.get("auto_admission")
        child = None
        with self.lock:
            if event.binding in bindings:
                binding_id, binding = event.binding, bindings[event.binding]
            elif auto and event.binding == auto.get("binding", "auto"):
                require(
                    principal.role == "writer" and principal.id in auto["principals"],
                    "FORBIDDEN",
                    "Credential is not an automatic-admission writer",
                )
                require(
                    event.client in auto.get("clients", ["codex", "claude"]),
                    "CLIENT_SCOPE",
                    "Client is not enabled for automatic admission",
                )
                require(
                    self.controller.config["mode"] != "protected"
                    and not self.controller.qualification,
                    "CLIENT_SCOPE",
                    "Automatic admission is limited to the local non-isolated mode",
                )
                binding_id, binding, child, verdict = self._auto_binding(event)
                if binding is None:
                    return verdict
                self.engaged[(event.client, event.session_id)] = binding_id
            else:
                require(False, "CLIENT_BINDING", "This client binding is not installed")
            require(
                principal.role == "writer" and principal.id in binding["principals"],
                "FORBIDDEN",
                "Credential does not own this client binding",
            )
            require(
                covers(principal, binding["case_key"]),
                "SCOPE",
                "Credential does not cover the bound case",
            )
            require(
                event.client == binding["client"]
                and Path(event.cwd).resolve() == Path(binding["workspace"]).resolve(),
                "CLIENT_SCOPE",
                "Client or workspace differs from operator binding",
            )
            return self._handle(binding_id, binding, event, child)

    @staticmethod
    def skill_names(event):
        """Skill activation signals: a Skill tool call, a slash or dollar command."""
        names = set(
            re.findall(r"(?:^|[\s(])[$/]([A-Za-z0-9_.:-]+)(?![A-Za-z0-9_.:-])", event.prompt)
        )
        if event.event == "UserPromptExpansion":
            names.add(event.command_name.lstrip("/$"))
        if event.event == "PreToolUse" and event.tool_name == "Skill":
            names.add(str(event.tool_input.get("skill", "")))
        return {n for n in names if n}

    @staticmethod
    def skill_file(client, cwd, name):
        # The workspace copy wins; a skill installed at user level (the ordinary case for a
        # personal catalogue) is found next. Absent everywhere, the workspace path is
        # returned so the refusal still names the workspace.
        catalogs = [".agents", ".codex"] if client == "codex" else [".claude"]
        candidates = [Path(cwd).resolve() / catalogs[0] / "skills" / name / "SKILL.md"]
        candidates += [Path.home() / c / "skills" / name / "SKILL.md" for c in catalogs]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return candidates[0]

    def resolve_skill(self, client, cwd, name):
        """(entry, path, digest, reason): reason names why a skill is not managed."""
        registry = self.controller.config.get("skill_registry", {})
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name not in registry:
            return None, None, None, "skill is not registered"
        try:
            path = self.skill_file(client, cwd, name)
            digest = sha256(path.read_bytes())
        except (OSError, ValueError):
            return None, None, None, "skill file is unavailable in this workspace"
        for entry in registry[name]:
            if entry["sha256"] == digest:
                return entry, path, digest, None
        return None, path, digest, "skill version is not registered"

    # A finished case stays consultable and keeps its proofs; it just cannot be worked on.
    # A case whose budget is spent is not finished: it is stuck, and stays the same case.
    FINISHED = ("DELIVERED", "COMPLETED", "CANCELLED")

    @staticmethod
    def auto_case_key(client, cwd, profile):
        digest = sha256(canonical([client, str(Path(cwd).resolve()), profile]))
        return "auto:" + client + ":" + digest[:32]

    def _case_state(self, key):
        with self.controller.store.transaction() as db:
            row = db.execute("SELECT state FROM cases WHERE case_key=?", (key,)).fetchone()
        return row["state"] if row else None

    def _lineage(self, base):
        """Every case this workspace and profile ever had, oldest first.

        The first request keeps the bare key, so cases created before succession existed
        resolve unchanged. Each new request after a finished one takes `base#2`, `base#3`.
        """
        with self.controller.store.transaction() as db:
            rows = db.execute(
                "SELECT id,case_key,state FROM cases WHERE case_key=? OR case_key LIKE ?",
                (base, base + "#%"),
            ).fetchall()

        def index(row):
            _, _, suffix = row["case_key"].partition("#")
            return int(suffix) if suffix.isdigit() else 1

        return sorted((dict(r) for r in rows), key=index)

    def _next_auto_case(self, client, cwd, profile):
        """(case key to open, predecessor id): resume the live case, or follow a finished one.

        The distinction the product needs: a repeated event, a resumed session or a new
        call of the same unfinished work all land on the same case. Only a request made
        after the previous case is finished gets a new one, with its lineage recorded.
        """
        base = self.auto_case_key(client, cwd, profile)
        lineage = self._lineage(base)
        if not lineage:
            return base, None
        latest = lineage[-1]
        if latest["state"] not in self.FINISHED:
            return latest["case_key"], None
        # Next index after the highest one seen, not the row count: a gap in the
        # sequence must never make a new key collide with an existing case.
        _, _, suffix = latest["case_key"].partition("#")
        highest = int(suffix) if suffix.isdigit() else 1
        return base + "#" + str(highest + 1), latest["id"]

    def _active_auto_bindings(self, client, cwd):
        """Dynamic bindings of this workspace: in memory, or rebuilt from a persisted case."""
        found = {}
        bindings = self.controller.config.setdefault("client_bindings", {})
        for profile in self.controller.config["profiles"]:
            lineage = self._lineage(self.auto_case_key(client, cwd, profile))
            if not lineage or lineage[-1]["state"] in self.FINISHED:
                continue
            key = lineage[-1]["case_key"]
            if key in bindings:
                found[key] = bindings[key]
                continue
            skills = {}
            for name in self.controller.config.get("skill_registry", {}):
                entry, path, digest, reason = self.resolve_skill(client, cwd, name)
                if entry and entry["profile"] == profile:
                    skills[name] = {
                        "contract": entry["contract"],
                        "files": [{"path": str(path), "sha256": digest}],
                    }
            if skills:
                found[key] = self._inject_binding(client, cwd, profile, key, skills)
        return found

    def _inject_binding(self, client, cwd, profile, key, skills):
        auto = self.controller.config["auto_admission"]
        binding = {
            "client": client,
            "workspace": str(Path(cwd).resolve()),
            "case_key": key,
            "principals": list(auto["principals"]),
            "automatic_delivery": False,
            "skills": skills,
        }
        self.controller.config.setdefault("case_bindings", {})[key] = profile
        validate_binding(self.controller, binding, dynamic=True)
        self.controller.config.setdefault("client_bindings", {})[key] = binding
        return binding

    def _auto_binding(self, event):
        """Resolve the workspace's dynamic binding for this event; may refuse or observe."""
        auto = self.controller.config["auto_admission"]
        names = self.skill_names(event)
        terminal = ("CANCELLED", "DELIVERED", "COMPLETED", "RESULT_UNKNOWN")
        active = {
            key: binding
            for key, binding in self._active_auto_bindings(event.client, event.cwd).items()
            if self._case_state(key) not in terminal
        }

        def observe(reason, allow=True):
            verdict = {
                "ok": True,
                "allow": allow,
                "reason": reason,
                "activated": [],
                "admission_basis": "auto_admission",
                "status": None,
                "hook_is_security_boundary": False,
            }
            return None, None, None, verdict

        session = (event.client, event.session_id)
        if not names:
            # Only the session that invoked the skill stays bound, and only until its
            # next prompt that names no skill. Another session, or the same session
            # once the skill is done, is never restricted by a case left open.
            if event.event == "UserPromptSubmit":
                self.engaged.pop(session, None)
            key = self.engaged.get(session)
            if key in active:
                return key, active[key], None, None
            return observe(None)
        name = sorted(names)[0]
        entry, path, digest, reason = self.resolve_skill(event.client, event.cwd, name)
        if entry is None:
            message = "SKILL_NOT_MANAGED: " + name + ": " + reason
            if auto.get("unmanaged", "observe") == "deny":
                return observe(message, allow=False)
            key = self.engaged.get(session)
            if key in active:
                # An unregistered skill inside a controlled workflow stays observed only.
                return key, active[key], {"unmanaged": name, "reason": message}, None
            return observe(message)
        ref = entry["contract"]
        for key, binding in active.items():
            if name in binding["skills"]:
                return key, binding, None, None
        for key, binding in active.items():
            # A registered skill invoked while a workflow is active must be an approved child.
            child = self._child_slot(key, ref)
            if child is not None:
                binding["skills"][name] = {
                    "contract": ref,
                    "files": [{"path": str(path), "sha256": digest}],
                }
                validate_binding(self.controller, binding, dynamic=True)
                return key, binding, {"name": name, "parent": child[0], "slot": child[1]}, None
            allowed = self._allowed_children(key)
            hint = (
                "use one of: " + ", ".join(allowed)
                if allowed
                else "the active contract declares no child slot"
            )
            return observe(
                "SUBSKILL_NOT_ALLOWED: "
                + name
                + " is not an approved child of the active workflow; "
                + hint,
                allow=False,
            )
        profile = entry["profile"]
        key, predecessor = self._next_auto_case(event.client, event.cwd, profile)
        skills = {name: {"contract": ref, "files": [{"path": str(path), "sha256": digest}]}}
        binding = self._inject_binding(event.client, event.cwd, profile, key, skills)
        if predecessor is not None:
            binding["predecessor"] = predecessor
        return key, binding, None, None

    def _invocations(self, key):
        with self.controller.store.transaction() as db:
            case = db.execute("SELECT id FROM cases WHERE case_key=?", (key,)).fetchone()
            if case is None:
                return []
            rows = db.execute(
                "SELECT id, contract, parent_id, slot FROM invocations WHERE case_id=?",
                (case["id"],),
            )
            return [dict(r) for r in rows]

    def _child_slot(self, key, ref):
        """(parent invocation id, slot id) of an invocation whose contract offers this child."""
        for inv in self._invocations(key):
            for slot in strict_json(inv["contract"]).get("children", []):
                if slot["contract"] == ref:
                    return inv["id"], slot["id"]
        return None

    def _allowed_children(self, key):
        allowed = set()
        for inv in self._invocations(key):
            for slot in strict_json(inv["contract"]).get("children", []):
                allowed.add(slot["id"] + " -> " + slot["contract"])
        return sorted(allowed)

    def _handle(self, binding_id, binding, event, child=None):
        actor = Principal("admission:" + binding_id, "connector", (binding["case_key"],))
        marker = sha256(canonical([binding_id, event.session_id, event.event, event.event_id]))
        identity = "native-event:" + marker
        event_digest = sha256(canonical(event.model_dump()))
        with self.controller.store.transaction() as db:
            old = db.execute(
                "SELECT digest FROM operations WHERE principal=? AND key=?", (actor.id, identity)
            ).fetchone()
            require(
                not old or old[0] == event_digest,
                "IDEMPOTENCY_CONFLICT",
                "Native event identity was reused with different content",
            )
        errors = skill_file_errors(
            self.controller.config, binding["case_key"], self.controller.registry
        )
        require(
            not errors, "SKILL_CHANGED", "An approved skill file changed or is unavailable", errors
        )

        start = {"case_key": binding["case_key"]}
        if binding.get("predecessor"):
            start["predecessor"] = binding["predecessor"]
        case_id = self._call(actor, "flow_start", start)["result"]["case_id"]
        session = sha256(canonical([binding_id, event.session_id]))
        status = self._call(actor, "flow_status", {"case_id": case_id})["result"]
        if status["state"] not in ("CANCELLED", "DELIVERED", "COMPLETED", "RESULT_UNKNOWN"):
            self._call(actor, "session_bind", {"case_id": case_id, "session_id": session})
            if child and "slot" in child:
                # A registered child skill: the approved slot is instantiated once, through
                # the same path a writer would use, then entered like any invocation.
                self._call(
                    actor,
                    "skill_request",
                    {"case_id": case_id, "invocation_id": child["parent"], "slot": child["slot"]},
                )
                status = self._call(actor, "flow_status", {"case_id": case_id})["result"]
            # The dossier imposes every current invocation before model/tool choices.
            # Native activation events are observations, not permission to omit work.
            for invocation in status["skills"]:
                if not invocation["entered"]:
                    self._call(
                        actor,
                        "skill_enter",
                        {
                            "case_id": case_id,
                            "invocation_id": invocation["invocation_id"],
                            "session_id": session,
                        },
                    )

        names = set(
            re.findall(r"(?:^|[\s(])[$/]([A-Za-z0-9_.:-]+)(?![A-Za-z0-9_.:-])", event.prompt)
        )
        if event.event == "UserPromptExpansion":
            names.add(event.command_name.lstrip("/$"))
        if event.event == "PreToolUse" and event.tool_name == "Skill":
            names.add(str(event.tool_input.get("skill", "")))
        selected = names & set(binding["skills"])
        activated = sorted(selected)
        unmanaged = child.get("unmanaged") if child else None

        allow = True
        reason = None
        if event.event == "Stop" and binding.get("enforce_termination") is True:
            allow = status["state"] in ("DELIVERED", "COMPLETED", "CANCELLED")
            if not allow:
                reason = "Workflow is incomplete; inspect missing evidence and required approvals"
        if event.event == "PreToolUse":
            # A positive list avoids pretending shell strings can be safely classified.
            # Persistent terminals and hosted tools remain separate acceptance routes.
            allowed_mcp = event.tool_name in {
                "mcp__skillflow-prompts__" + name for name in api.WRITER
            }
            # Each approved contract names the client tools its own work needs. A tool
            # nobody declared stays out; the refusal says what this case does admit.
            declared = sorted(
                {
                    tool
                    for inv in self._invocations(binding["case_key"])
                    for tool in strict_json(inv["contract"]).get("tools", [])
                }
            )
            allow = (
                allowed_mcp
                or event.tool_name == "ToolSearch"
                or event.tool_name in declared
                or (event.tool_name == "Skill" and bool(selected))
            )
            if not allow:
                reason = (
                    "Bound case: this tool is not in the approved contract; admitted here: "
                    + (", ".join(declared) if declared else "SkillFlow tools only")
                )
            if unmanaged and event.tool_name == "Skill":
                policy = self.controller.config.get("auto_admission", {}).get("unmanaged")
                allow = (policy or "observe") == "observe"
                reason = child["reason"] + (" (observed, not covered)" if allow else "")

        with self.controller.store.transaction() as db:
            old = db.execute(
                "SELECT response FROM operations WHERE principal=? AND key=?", (actor.id, identity)
            ).fetchone()
            if not old:
                body = {
                    "client": event.client,
                    "event": event.event,
                    "session": session,
                    "activated": activated,
                    "tool": event.tool_name,
                    "tool_input_sha256": sha256(canonical(event.tool_input)),
                    "decision": "ALLOW" if allow else "DENY",
                    "observation_trust": "client_claim",
                    "admission_basis": (
                        "auto_admission"
                        if binding_id.startswith("auto:")
                        else "operator_case_policy"
                    ),
                    "effect_verified": False,
                }
                self.controller._event(db, case_id, "client_event", actor.id, body)
                if not allow:
                    self.controller._event(
                        db,
                        case_id,
                        "refusal",
                        actor.id,
                        {
                            "action": "native_stop" if event.event == "Stop" else "native_tool",
                            "code": "WORKFLOW_INCOMPLETE"
                            if event.event == "Stop"
                            else "CLIENT_TOOL_DENIED",
                            "effect_verified": False,
                        },
                    )
                db.execute(
                    "INSERT INTO operations VALUES(?,?,?,?,?)",
                    (
                        actor.id,
                        identity,
                        event_digest,
                        case_id,
                        canonical({"logged": True}).decode(),
                    ),
                )
        status = self._call(actor, "flow_status", {"case_id": case_id})["result"]
        return {
            "ok": True,
            "allow": allow,
            "reason": reason,
            "activated": activated,
            "admission_basis": (
                "auto_admission" if binding_id.startswith("auto:") else "operator_case_policy"
            ),
            "status": status,
            "hook_is_security_boundary": False,
        }

    def poll_deliveries(self):
        """Commit automatically only after the exact required human approval exists."""
        from .delivery import approval_current, current_snapshot, human_required

        with self.lock:
            for binding_id, binding in self.controller.config.get("client_bindings", {}).items():
                if binding.get("automatic_delivery") is not True:
                    continue
                with self.controller.store.transaction() as db:
                    case = db.execute(
                        "SELECT * FROM cases WHERE case_key=?", (binding["case_key"],)
                    ).fetchone()
                    if not case or case["state"] in (
                        "DELIVERED",
                        "COMPLETED",
                        "CANCELLED",
                        "RESULT_UNKNOWN",
                    ):
                        continue
                    if self.controller._missing(db, case):
                        continue
                    destination = binding["destination"]
                    snapshot = canonical(
                        current_snapshot(self.controller, db, case, destination)
                    ).decode()
                    delivery = db.execute(
                        "SELECT * FROM deliveries WHERE case_id=? AND snapshot=? "
                        "ORDER BY rowid DESC LIMIT 1",
                        (case["id"], snapshot),
                    ).fetchone()
                    if delivery is not None and delivery["state"] != "REQUESTED":
                        # Rejected by the operator, or already committed: nothing to do
                        # until the objects change. Requesting again every tick would
                        # only pile refusals into the journal and move the revision.
                        continue
                    approved = delivery is not None and (
                        not human_required(self.controller, db, case)
                        or approval_current(db, case, delivery) is not None
                    )
                actor = Principal("admission:" + binding_id, "connector", (binding["case_key"],))
                try:
                    if delivery is None:
                        self._call(
                            actor,
                            "delivery_request",
                            {"case_id": case["id"], "destination": destination},
                            key=[sha256(snapshot.encode()), case["revision"]],
                        )
                    elif approved:
                        operator = Principal(
                            "delivery-worker:" + binding_id, "operator", (binding["case_key"],)
                        )
                        self._call(
                            operator,
                            "delivery_commit",
                            {"case_id": case["id"], "delivery_id": delivery["id"]},
                            key=case["revision"],
                        )
                except FlowError:
                    # Concurrent changes are refused by the core and recorded there.
                    # Re-read the current snapshot on the next bounded worker tick.
                    continue

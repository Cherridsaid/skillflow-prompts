"""Operator stop switch, persisted in the existing immutable event ledger."""

from .util import canonical, require, sha256, strict_json


def state(db):
    row = db.execute(
        "SELECT id,data FROM events WHERE case_id IS NULL AND kind='control' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {
        "paused": strict_json(row["data"])["paused"] if row else False,
        "revision": row["id"] if row else 0,
    }


def operator(principal):
    require(
        principal.role == "operator" and "*" in principal.case_keys,
        "FORBIDDEN",
        "Global operator required",
    )


def set_state(controller, principal, request):
    operator(principal)
    require(
        isinstance(request, dict) and set(request) == {"paused", "revision", "idempotency_key"},
        "INVALID_ARGUMENT",
        "Exact control envelope required",
    )
    require(
        type(request["paused"]) is bool
        and type(request["revision"]) is int
        and request["revision"] >= 0
        and isinstance(request["idempotency_key"], str)
        and 0 < len(request["idempotency_key"]) <= 128,
        "INVALID_ARGUMENT",
        "Invalid control values",
    )
    digest = sha256(canonical({"control": request}))
    with controller.store.transaction() as db:
        old = db.execute(
            "SELECT * FROM operations WHERE principal=? AND key=?",
            (principal.id, request["idempotency_key"]),
        ).fetchone()
        if old:
            require(old["digest"] == digest, "IDEMPOTENCY_CONFLICT", "Key already used")
            return strict_json(old["response"])
        require(
            state(db)["revision"] == request["revision"],
            "REVISION_CONFLICT",
            "Reload control state",
        )
        controller._event(db, None, "control", principal.id, {"paused": request["paused"]})
        result = {"ok": True, "result": state(db)}
        db.execute(
            "INSERT INTO operations VALUES(?,?,?,?,?)",
            (principal.id, request["idempotency_key"], digest, None, canonical(result).decode()),
        )
        return result


def overview(controller, principal):
    operator(principal)
    with controller.store.transaction() as db:
        cases = [
            dict(row) for row in db.execute("SELECT id,case_key FROM cases ORDER BY created,id")
        ]
        control = state(db)
    # ponytail: full projection per refresh; paginate when measured case volume requires it.
    rows = []
    for case in cases:
        status = controller.call(
            principal, {"action": "flow_status", "arguments": {"case_id": case["id"]}}
        )
        rows.append({"case_key": case["case_key"], **status})
    return {
        "control": control,
        "cases": rows,
        "measurement": {
            "legitimate_successes": None,
            "false_refusals": None,
            "bypasses": None,
            "pilot_started": False,
        },
        "limits": [
            "Chaque dossier possède son horodatage et sa révision.",
            "Après confirmation, la pause bloque les nouvelles opérations contrôlées.",
            "Les processus supervisés s’arrêtent au prochain contrôle (intervalle 1 seconde, plus transport).",
            "Les clients ouverts ailleurs échappent à cet arrêt.",
            "Les effets déjà réalisés ne sont pas annulés.",
            "L’installation native et le pilote restent à qualifier.",
        ],
    }

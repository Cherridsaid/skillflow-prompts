"""Explicit local provisioning; this never claims an isolated deployment."""

import base64
import secrets
from importlib.resources import files
from pathlib import Path

from . import receipts
from .util import canonical, sha256


def prepare_client_demo(root, workspace, client, skill, port):
    """Prepare one exact native skill and bound case; install no client settings."""
    from .util import require, strict_json

    require(client in ("codex", "claude"), "CLIENT_CONFIG", "Unknown client")
    mapping = {
        name: (item["profile"], item["contract"].split("@")[0])
        for name, item in demo_skills().items()
    }
    require(
        skill in mapping and 1024 <= port <= 65535,
        "CLIENT_CONFIG",
        "Unknown fixture or invalid port",
    )
    root, workspace = Path(root).resolve(), Path(workspace).resolve()
    require(
        not workspace.exists() or not any(workspace.iterdir()),
        "CLIENT_CONFIG",
        "Use an empty new workspace",
    )
    create_demo(root)
    workspace.mkdir(parents=True, exist_ok=True)
    catalog = workspace / (".agents" if client == "codex" else ".claude") / "skills" / skill
    catalog.mkdir(parents=True)
    text = files("skillflow_prompts").joinpath("data", skill + ".SKILL.md").read_bytes()
    (catalog / "SKILL.md").write_bytes(text)
    profile, contract_id = mapping[skill]
    contract_path = root / "contracts" / (contract_id + ".json")
    contract = strict_json(contract_path.read_bytes())
    contract["resources"] = [
        {
            "id": "native-skill",
            "version": "1.0.0",
            "sha256": sha256(text),
            "content": text.decode("utf-8"),
        }
    ]
    contract_path.write_bytes(canonical(contract))
    config_path = root / "config.json"
    config = strict_json(config_path.read_bytes())
    config["case_bindings"] = {"native-demo": profile}
    for principal in config["principals"]:
        principal["case_keys"] = ["native-demo", "auto:*"]
    config["client_bindings"] = {
        "native-demo": {
            "client": client,
            "workspace": str(workspace),
            "case_key": "native-demo",
            "principals": ["demo-writer"],
            "automatic_delivery": True,
            "destination": "local",
            "skills": {
                skill: {
                    "contract": contract_id + "@1.0.0",
                    "files": [{"path": str(catalog / "SKILL.md"), "sha256": sha256(text)}],
                }
            },
        }
    }
    config_path.write_bytes(canonical(config))
    connection = {
        "binding": "native-demo",
        "client": client,
        "workspace": str(workspace),
        "url": f"http://127.0.0.1:{port}",
        "token_file": str(root / "writer.token"),
    }
    connection_path = root / "client-connections.json"
    connection_path.write_bytes(
        canonical({"connections": [connection], "supported_skills": [skill]})
    )
    return {
        "mode": "demo",
        "case_created": False,
        "client_installed": False,
        "root": str(root),
        "workspace": str(workspace),
        "connections": str(connection_path),
        "skill_sha256": sha256(text),
        "human_approval_required": bool(contract.get("human_approval_required", True)),
        "server_port": port,
    }


def demo_skills():
    """Synthetic skill declarations shipped as data: name -> {contract, profile}."""
    from .util import strict_json

    return strict_json(files("skillflow_prompts").joinpath("data", "skills.json").read_bytes())[
        "skills"
    ]


def skill_registry():
    """Registry entries pinned to the exact shipped SKILL.md bytes."""
    registry = {}
    for name, item in demo_skills().items():
        text = files("skillflow_prompts").joinpath("data", name + ".SKILL.md").read_bytes()
        registry[name] = [
            {"sha256": sha256(text), "contract": item["contract"], "profile": item["profile"]}
        ]
    return registry


def create_demo(root, port=8765):
    root = Path(root)
    if root.exists() and any(root.iterdir()):
        raise FileExistsError("Use an empty directory; existing state is never reset")
    root.mkdir(parents=True, exist_ok=True)
    (root / "contracts").mkdir()
    for item in files("skillflow_prompts").joinpath("data").iterdir():
        if item.name.endswith(".json") and item.name != "skills.json":
            (root / "contracts" / item.name).write_bytes(item.read_bytes())
    key = receipts.generate_key(root / "signing.key")
    public = key.public_key().public_bytes_raw()
    trust = {
        receipts.key_id(public): {
            "issuer": "skillflow-prompts-demo",
            "public_key": base64.b64encode(public).decode(),
            "profiles": ["demo"],
        }
    }
    (root / "trust.json").write_bytes(canonical(trust))
    principals = []
    for role in ("writer", "connector", "reviewer", "operator"):
        token = secrets.token_urlsafe(32)
        (root / (role + ".token")).write_text(token, encoding="utf-8")
        principals.append(
            {
                "id": "demo-" + role,
                "role": role,
                "token_sha256": sha256(token.encode()),
                "case_keys": ["*"],
            }
        )
    config = {
        "format": "1",
        "mode": "demo",
        "issuer": "skillflow-prompts-demo",
        "principals": principals,
        "profiles": {
            "legal": {"coverage": "demo", "contracts": ["synthetic-legal@1.0.0"]},
            "quality": {"coverage": "demo", "contracts": ["json-quality@1.0.0"]},
            "editorial": {"coverage": "demo", "contracts": ["editorial@1.0.0"]},
            "brief": {"coverage": "demo", "contracts": ["brief@1.0.0"]},
            "glossary": {"coverage": "demo", "contracts": ["glossary@1.0.0"]},
            "memo": {"coverage": "demo", "contracts": ["memo@1.0.0"]},
        },
        "case_bindings": {
            "demo-legal": "legal",
            "demo-quality": "quality",
            "demo-editorial": "editorial",
        },
        "destinations": {"local": "outbox"},
        "review_budget": 3,
        "source_budget": 3,
        # Automatic admission: a registered skill invoked in any workspace opens or
        # resumes its own case. Unregistered skills are observed, never covered.
        "skill_registry": skill_registry(),
        "auto_admission": {
            "binding": "auto",
            "principals": ["demo-writer"],
            "clients": ["codex", "claude"],
            "unmanaged": "observe",
        },
    }
    (root / "config.json").write_bytes(canonical(config))
    for client in ("codex", "claude"):
        connection = {
            "binding": "auto",
            "client": client,
            "workspace": "*",
            "url": f"http://127.0.0.1:{port}",
            "token_file": str(root / "writer.token"),
        }
        (root / f"auto-connections-{client}.json").write_bytes(
            canonical(
                {
                    "connections": [connection],
                    "supported_skills": sorted(demo_skills()),
                    # A global connection reaches every workspace: a stopped local
                    # controller must not deny tools in unrelated sessions.
                    "fail_open": True,
                }
            )
        )
    return root

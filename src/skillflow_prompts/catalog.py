"""Lossless local skill inventory; discovery is never contract approval.

Catalogs are private authority inputs. Only an operator-controlled configuration
can bind their digest to approved contracts. No writer endpoint approves them.
"""

import os
from pathlib import Path

from .util import canonical, sha256, strict_json

IGNORED = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules", ".venv"}


def package_files(directory):
    """Hash all readable package files, including additions. Never traverse links.

    Exclusions and links remain explicit gaps, not implicitly trusted resources.
    The deployment must keep this tree read-only to the writer: hashing cannot
    prevent a concurrent writer from replacing a file after a successful check.
    """
    root = Path(directory).resolve(strict=True)
    files, gaps = {}, []
    for parent, dirs, names in os.walk(
        root, followlinks=False, onerror=lambda error: gaps.append(str(error.filename))
    ):
        for name in list(dirs):
            path = Path(parent) / name
            relative = path.relative_to(root).as_posix()
            if name in IGNORED or path.is_symlink() or _reparse(path):
                dirs.remove(name)
                gaps.append(relative + "/")
        for name in sorted(names):
            path = Path(parent) / name
            relative = path.relative_to(root).as_posix()
            try:
                if path.is_symlink() or _reparse(path):
                    gaps.append(relative)
                    continue
                files[relative] = sha256(path.read_bytes())
            except OSError:
                gaps.append(relative)
    return {"files": dict(sorted(files.items())), "gaps": sorted(gaps)}


def _reparse(path):
    return bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)


def instruction_units(text):
    """Lossless paragraph coordinates, not an interpretation of obligations."""
    units = []
    start, lines = 1, []
    for number, line in enumerate(text.splitlines() + [""], 1):
        if line.strip():
            if not lines:
                start = number
            lines.append(line)
        elif lines:
            units.append({"line": start, "text": "\n".join(lines), "review": "NOT_REVIEWED"})
            lines = []
    return units


def inventory_entry(client, name, path, *, discovery, enabled=None, metadata=None, blobs=None):
    path = Path(path).resolve(strict=True)
    package = package_files(path.parent)
    data = path.read_bytes()
    # Keep original bytes in the content-addressed archive. Decoding failures are gaps.
    text = data.decode("utf-8-sig", errors="replace")
    units = instruction_units(text)
    if blobs is not None:
        blobs = Path(blobs)
        blobs.mkdir(parents=True, exist_ok=True)
        for relative, digest in package["files"].items():
            content = (path.parent / relative).read_bytes()
            if sha256(content) != digest:
                raise ValueError("Skill changed during inventory; retry discovery")
            target = blobs / digest
            if not target.exists():
                target.write_bytes(content)
            elif sha256(target.read_bytes()) != digest:
                raise ValueError("Corrupt inventory archive")
    return {
        "id": sha256(canonical([client, name, str(path)])),
        "client": client,
        "name": name,
        "path": str(path),
        "discovery": discovery,
        "enabled": enabled,
        "metadata": metadata or {},
        "version": "sha256:" + sha256(data),
        "package": package,
        "package_sha256": sha256(canonical(package)),
        "instructions": text,
        "instruction_units": units,
        "resource_review": "NOT_REVIEWED",
        "contract": None,
        "contract_state": "NOT_APPROVED",
        "installation": "NOT_INSTALLED",
        "activation_paths": {
            mode: {"measured": False, "result": "non mesuré"}
            for mode in ("explicit", "implicit", "resume", "delegation", "natural_end")
        },
    }


def load(config):
    setting = config.get("skill_catalog")
    if not setting:
        return None
    data = Path(setting["path"]).read_bytes()
    if sha256(data) != setting["sha256"]:
        raise ValueError("Catalog differs from the operator-pinned digest")
    catalog = strict_json(data)
    if catalog.get("schema") != "skillflow-catalog-v1":
        raise ValueError("Unsupported catalog schema")
    entries = {entry["id"]: entry for entry in catalog["entries"]}
    if len(entries) != len(catalog["entries"]):
        raise ValueError("Duplicate catalog identity")
    return entries


def unique_entries(entries):
    """Deduplicate identical discovery results; conflicting versions abort capture."""
    unique = {}
    for entry in entries:
        previous = unique.get(entry["id"])
        if previous is not None and canonical(previous) != canonical(entry):
            raise ValueError("Conflicting catalog identity; repeat native discovery")
        unique[entry["id"]] = entry
    return list(unique.values())


def binding_errors(config, binding, registry=None):
    """Gate direct calls as well as hooks; source inventory grants no authority."""
    if not config.get("catalog_required"):
        return []
    try:
        entries = load(config)
    except (OSError, ValueError, KeyError, TypeError):
        return ["catalog_unavailable_or_changed"]
    if not entries:
        return ["catalog_missing"]
    operators = {p["id"] for p in config["principals"] if p["role"] == "operator"}
    approvals = config.get("catalog_approvals", {})
    errors = []
    for name, skill in binding["skills"].items():
        entry = entries.get(skill.get("catalog_id"))
        approval = approvals.get(skill.get("catalog_id"))
        if not entry or entry["client"] != binding["client"] or entry["name"] != name:
            errors.append("catalog_unknown:" + name)
            continue
        if entry.get("enabled") is False:
            errors.append("catalog_skill_disabled:" + name)
        if (
            not approval
            or approval.get("actor") not in operators
            or not approval.get("decision_id")
            or approval.get("contract") != skill["contract"]
            or approval.get("package_sha256") != entry["package_sha256"]
            or approval.get("catalog_sha256") != config["skill_catalog"]["sha256"]
        ):
            errors.append("catalog_not_approved:" + name)
        if registry and approval:
            if approval.get("contract_sha256") != registry.fingerprint(skill["contract"]):
                errors.append("catalog_contract_changed:" + name)
        mapping_required = config.get("catalog_mapping_required", False) or (
            config.get("mode") == "protected"
        )
        mapping_errors = []
        if mapping_required:
            from .contract_mapping import approved_errors

            mapping_errors = (
                approved_errors(entry, approval, registry)
                if approval and registry
                else ["mapping_unavailable"]
            )
            errors.extend(error + ":" + name for error in mapping_errors)
        try:
            current = package_files(Path(entry["path"]).parent)
            if sha256(canonical(current)) != entry["package_sha256"]:
                errors.append("catalog_package_changed:" + name)
            # Only an exact operator-pinned mapping can classify each excluded
            # path. A generated draft or a required missing resource still blocks.
            if current["gaps"] and (not mapping_required or mapping_errors):
                errors.append("catalog_resource_gap:" + name)
        except (OSError, ValueError):
            errors.append("catalog_package_unavailable:" + name)
    return errors


def watch_errors(config):
    """Operator-pinned discovery scopes also catch additions outside bound packages.

    A directory watch hashes its entire scope without following links. File
    watches pin plugin/configuration indexes. The operator must select the actual
    active scopes: this does not discover unknown providers outside those roots.
    """
    errors = []
    for number, watch in enumerate(config.get("catalog_watches", [])):
        label = str(number)
        try:
            path = Path(watch["path"])
            if not path.is_absolute() or path.is_symlink() or _reparse(path):
                raise ValueError("Nonordinary discovery scope")
            current = (
                sha256(canonical(package_files(path)))
                if path.is_dir()
                else sha256(path.read_bytes())
            )
            if current != watch["sha256"]:
                errors.append("catalog_scope_changed:" + label)
        except (OSError, ValueError, KeyError, TypeError):
            errors.append("catalog_scope_unavailable:" + label)
    return errors


def report(controller):
    entries = load(controller.config) or {}
    bindings = controller.config.get("client_bindings", {})
    scope_errors = watch_errors(controller.config)
    rows = []
    for entry in entries.values():
        associated = [
            b
            for b in bindings.values()
            if any(s.get("catalog_id") == entry["id"] for s in b["skills"].values())
        ]
        errors = sorted(
            set(scope_errors)
            | {
                e
                for b in associated
                for e in binding_errors(controller.config, b, controller.registry)
            }
        )
        rows.append(
            {
                "id": entry["id"],
                "client": entry["client"],
                "name": entry["name"],
                "version": entry["version"],
                "package_sha256": entry["package_sha256"],
                "discovery": entry["discovery"],
                "enabled": entry["enabled"],
                "contract_approved": bool(associated)
                and not errors
                and controller.config.get("catalog_required") is True,
                "blockers": errors or ([] if associated else ["no_approved_binding"]),
                # Native installation/effect proof is not implied by server configuration.
                "installed": None,
                "activation_verified": None,
                "effect_protected": None,
            }
        )
    return {
        "entries": rows,
        "detected": len(rows),
        "contracts_approved": sum(r["contract_approved"] for r in rows),
        "installed": None,
        "activation_verified": None,
        "effect_protected": None,
    }

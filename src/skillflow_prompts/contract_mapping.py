"""Exact instruction-to-contract review. A draft never grants approval.

The operator pins this mapping with the contract and package. Structural checks
cannot establish that a semantic interpretation is correct: that remains the
independent reviewer's and operator's decision, against the original bytes.
"""

import posixpath
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .util import canonical, sha256, strict_json


def references(path, text):
    """Keep individual literal occurrences, including examples, for review.

    ponytail: inline Markdown links only; resource classification also requires
    review of commands, dynamic paths and reference-style links in whole files.
    """
    result = []
    for match in re.finditer(r"\[[^\]\n]*\]\(([^)\n]+)\)", text):
        target = match[1].strip().split(' "', 1)[0].strip("<>")
        parts = urlsplit(target)
        normalized = None
        if not parts.scheme and not parts.netloc:
            normalized = (
                posixpath.normpath(posixpath.join(posixpath.dirname(path), unquote(parts.path)))
                if parts.path
                else path
            )
        result.append(
            {
                "id": sha256(canonical([path, match.start(), target])),
                "source": path,
                "line": text[: match.start()].count("\n") + 1,
                "target": target,
                "normalized": normalized,
            }
        )
    return result


def basis(entry, read):
    """Read and verify every archived file, not just the top-level SKILL.md."""
    from .catalog import instruction_units

    refs, units, primary = [], {}, False
    for path, digest in entry["package"]["files"].items():
        if (
            path.startswith("/")
            or any(p in ("", ".", "..") for p in path.split("/"))
            or ":" in path
            or "\\" in path
        ):
            raise ValueError("Invalid package resource path")
        data = read(path, digest)
        if sha256(data) != digest:
            raise ValueError("Resource changed while preparing its review")
        if path.lower().endswith(".md"):
            text = data.decode("utf-8-sig")
            refs.extend(references(path, text))
            for unit in instruction_units(text):
                key = sha256(canonical([path, digest, unit["line"], unit["text"]]))
                units[key] = {"source": path, "line": unit["line"], "text": unit["text"]}
            if path == Path(entry["path"]).name:
                if (
                    "sha256:" + digest != entry["version"]
                    or text != entry["instructions"]
                    or instruction_units(text) != entry["instruction_units"]
                ):
                    raise ValueError("Catalog instruction text or units differ from original bytes")
                primary = True
    if not primary:
        raise ValueError("Primary instruction file absent from package")
    return {
        "version": entry["version"],
        "package_sha256": entry["package_sha256"],
        "units": units,
        "files": entry["package"]["files"],
        "gaps": entry["package"]["gaps"],
        "references": {ref["id"]: ref for ref in refs},
    }


def draft(source):
    """Unresolved review form; never classify or approve instructions by heuristic."""
    return {
        "format": "skillflow-contract-mapping-v1",
        "basis_sha256": sha256(canonical(source)),
        "contract_sha256": None,
        "units": {key: None for key in source["units"]},
        "files": {key: None for key in source["files"]},
        "gaps": {key: None for key in source["gaps"]},
        "references": {key: None for key in source["references"]},
    }


def validate(source, mapping, contract):
    """Return actionable omissions. Called before admission and direct effects."""
    errors = []
    if mapping.get("format") != "skillflow-contract-mapping-v1":
        return ["mapping_format"]
    if mapping.get("basis_sha256") != sha256(canonical(source)):
        errors.append("mapping_sources_changed")
    if mapping.get("contract_sha256") != sha256(canonical(contract.model_dump())):
        errors.append("mapping_contract_changed")
    definitions = {o.id: (s, o) for s in contract.steps for o in s.obligations}
    resources = {r.id: r.sha256 for r in contract.resources}
    sources = {s.id: s.url for s in contract.sources}
    mapped = set()
    for section in ("units", "files", "gaps", "references"):
        rows = mapping.get(section)
        expected = set(source[section])
        if not isinstance(rows, dict) or set(rows) != expected:
            errors.append("mapping_items:" + section)
            continue
        for key, decision in rows.items():
            tag = section + ":" + key
            if (
                not isinstance(decision, dict)
                or decision.get("kind") not in ("required", "context", "example", "metadata")
                or not isinstance(decision.get("reason"), str)
                or not decision["reason"].strip()
            ):
                errors.append("mapping_unreviewed:" + tag)
                continue
            if decision["kind"] != "required":
                continue  # Only the operator-pinned exact decision can exclude an item.
            if section == "units":
                obligations = decision.get("obligations")
                operations = decision.get("operations")
                if (
                    not isinstance(obligations, list)
                    or not obligations
                    or not all(isinstance(o, str) and o in definitions for o in obligations)
                    or len(set(obligations)) != len(obligations)
                    or not isinstance(operations, list)
                    or not all(isinstance(o, str) for o in operations)
                ):
                    errors.append("mapping_obligations:" + tag)
                    continue
                mapped.update(obligations)
                for obligation in obligations:
                    step, definition = definitions[obligation]
                    if definition.coverage != "complete":
                        errors.append("mapping_uncovered:" + obligation)
                    for operation in operations:
                        if step.id not in contract.operation_prerequisites.get(operation, []):
                            errors.append("mapping_precondition:" + operation + ":" + step.id)
            elif section == "gaps":
                errors.append("mapping_required_gap:" + key)
            elif section == "files":
                if resources.get(decision.get("resource")) != source["files"][key]:
                    errors.append("mapping_resource:" + key)
            else:
                ref = source["references"][key]
                digest = source["files"].get(ref["normalized"])
                resource_ok = (
                    digest is not None and resources.get(decision.get("resource")) == digest
                )
                policy_ok = sources.get(decision.get("source")) == ref["target"] and any(
                    "source:" + str(decision.get("source")) in step.inputs
                    and any(
                        o.verifier == "source" and o.coverage == "complete"
                        for o in step.obligations
                    )
                    and step.branch is None
                    for step in contract.steps
                )
                if not resource_ok and not policy_ok:
                    errors.append("mapping_reference:" + key)
    if set(definitions) - mapped:
        errors.append("mapping_untraced_obligations")
    return sorted(set(errors))


def approved_errors(entry, approval, registry):
    """The writer cannot submit or modify this operator-owned mapping path."""
    try:
        setting = approval["mapping"]
        path = Path(setting["path"])
        if not path.is_absolute() or path.is_symlink():
            return ["mapping_path"]
        raw = path.read_bytes()
        if sha256(raw) != setting["sha256"]:
            return ["mapping_changed"]
        source = basis(entry, lambda path, _: (Path(entry["path"]).parent / path).read_bytes())
        return validate(source, strict_json(raw), registry.get(approval["contract"]))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return ["mapping_unavailable"]

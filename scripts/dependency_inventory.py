"""Generate public lock/license/release-age data from official PyPI metadata."""

import argparse
import json
import tomllib
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path


def inspect(package):
    name, version = package["name"], package["version"]
    with urllib.request.urlopen(
        f"https://pypi.org/pypi/{name}/{version}/json", timeout=20
    ) as response:
        metadata = json.load(response)
    info = metadata["info"]
    uploads = sorted(
        item["upload_time_iso_8601"] for item in metadata["urls"] if not item.get("yanked")
    )
    license_name = info.get("license_expression") or info.get("license")
    if not license_name or len(license_name) > 120:
        license_name = (
            "; ".join(x for x in info.get("classifiers", []) if x.startswith("License ::"))
            or "Review package license files"
        )
    return {
        "name": name,
        "version": version,
        "license_metadata": license_name,
        "first_file_published": uploads[0] if uploads else None,
        "source": f"https://pypi.org/project/{name}/{version}/",
        "redistributed_in_project_wheel": False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path("."))
    args = parser.parse_args()
    lock = tomllib.loads((args.repo / "uv.lock").read_text())
    packages = [p for p in lock["package"] if p["name"] != "skillflow-mcp"]
    with ThreadPoolExecutor(max_workers=6) as pool:
        entries = sorted(pool.map(inspect, packages), key=lambda x: x["name"])
    cutoff = datetime(2026, 9, 11, tzinfo=UTC)
    for entry in entries:
        assert (
            entry["first_file_published"]
            and datetime.fromisoformat(entry["first_file_published"].replace("Z", "+00:00"))
            < cutoff
        )
    output = {
        "format": "1",
        "source": "Official PyPI metadata",
        "selection_cutoff": cutoff.isoformat(),
        "packages": entries,
        "scope": "Locked direct and transitive dependencies, including development tools",
    }
    (args.repo / "docs" / "dependencies.json").write_text(
        json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    lines = [
        "# Third-party dependency notices",
        "",
        "Original SkillFlow code is Apache-2.0. Dependencies retain their own licenses.",
        "No dependency source or wheel is bundled inside the SkillFlow wheel. This is an",
        "inventory, not relicensing or an added restriction. When redistributing dependency",
        "wheels, retain their included license files and notices. No NOTICE file is required",
        "for additional borrowed project source: none has been copied.",
        "",
        "The official Apache-2.0 license text is reproduced unmodified in LICENSE.",
        "Package license metadata below is from official PyPI; inspect the installed distribution",
        "license files when redistributing it. Full versions and publication dates: [inventory](docs/dependencies.json).",
        "",
        "| Package | Version | Reported license |",
        "|---|---|---|",
    ]
    lines += [
        f"| [{p['name']}]({p['source']}) | {p['version']} | {p['license_metadata'].replace('|', '/')} |"
        for p in entries
    ]
    (args.repo / "THIRD_PARTY_NOTICES.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"packages": len(entries), "release_age_cutoff_verified": True}))


if __name__ == "__main__":
    main()

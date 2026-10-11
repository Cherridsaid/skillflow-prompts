"""Prepare hashes and a distributable inventory; never upload or publish."""

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, default=Path("release-local"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    wheels = list((args.repo / "dist").glob("*.whl"))
    sdists = list((args.repo / "dist").glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("Expected exactly one freshly built wheel and source distribution")
    selected = (
        wheels
        + sdists
        + [
            args.repo / p
            for p in [
                "LICENSE",
                "THIRD_PARTY_NOTICES.md",
                "docs/dependencies.json",
                "uv.lock",
                "requirements.lock.txt",
            ]
        ]
    )
    hashes = {}
    for source in selected:
        target = args.output / source.name
        shutil.copyfile(source, target)
        hashes[target.name] = hashlib.sha256(target.read_bytes()).hexdigest()
    revision = subprocess.check_output(
        ["git", "-C", str(args.repo), "rev-parse", "HEAD"], text=True
    ).strip()
    manifest = {
        "format": "1",
        "version": "0.1.0a1",
        "source_commit": revision,
        "sha256": hashes,
        "publication": "NOT_PUBLISHED",
        "provenance_signature": None,
        "limitation": "Checksums establish integrity, not authenticated build provenance.",
    }
    (args.output / "release-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    (args.output / "SHA256SUMS").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(hashes.items())), encoding="ascii"
    )
    print(json.dumps({"prepared": len(hashes), "source_commit": revision, "published": False}))


if __name__ == "__main__":
    main()

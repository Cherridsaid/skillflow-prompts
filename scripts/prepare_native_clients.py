"""Copy exact installed Windows clients into a reviewable private bundle."""

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

from skillflow.util import canonical, require, sha256


def ordinary(path):
    require(path.is_file() and not path.is_symlink(), "CLIENT_SOURCE", "Ordinary file required")
    require(
        not (getattr(path.stat(), "st_file_attributes", 0) & 0x400),
        "CLIENT_SOURCE",
        "Reparse points are refused",
    )
    return path


def version(executable):
    run = subprocess.run(
        [str(executable), "--version"], capture_output=True, text=True, timeout=15, check=False
    )
    require(run.returncode == 0, "CLIENT_VERSION", "Client version command failed")
    return run.stdout.strip() or run.stderr.strip()


def prepare(npm_root, output):
    require(os.name == "nt", "WINDOWS_REQUIRED", "Native bundle requires Windows")
    npm_root = Path(npm_root).resolve(strict=True)
    output = Path(output).absolute()
    require(not output.exists(), "CLIENT_BUNDLE_EXISTS", "Use a new bundle directory")
    codex_package = npm_root / "@openai/codex"
    codex_vendor = codex_package / "node_modules/@openai/codex-win32-x64/vendor"
    codex_exe = ordinary(next(codex_vendor.glob("*/bin/codex.exe")))
    claude_package = npm_root / "@anthropic-ai/claude-code"
    claude_exe = ordinary(claude_package / "bin/claude.exe")
    sources = {
        "codex/vendor": codex_vendor,
        "codex/package.json": ordinary(codex_package / "package.json"),
        "codex/README.md": ordinary(codex_package / "README.md"),
        "claude/claude.exe": claude_exe,
        "claude/package.json": ordinary(claude_package / "package.json"),
        "claude/README.md": ordinary(claude_package / "README.md"),
        "claude/LICENSE.md": ordinary(claude_package / "LICENSE.md"),
    }
    for source in sources.values():
        if source.is_dir():
            require(
                not any(
                    item.is_symlink() or getattr(item.stat(), "st_file_attributes", 0) & 0x400
                    for item in source.rglob("*")
                ),
                "CLIENT_SOURCE",
                "Client tree contains a reparse point",
            )
    output.mkdir(parents=True)
    for relative, source in sources.items():
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
    codex_relative = Path("codex/vendor") / codex_exe.relative_to(codex_vendor)
    manifest = {
        "format": "skillflow-native-clients-v1",
        "platform": "windows-x86_64",
        "clients": {
            "codex": {
                "version": version(codex_exe),
                "executable": codex_relative.as_posix(),
            },
            "claude": {"version": version(claude_exe), "executable": "claude/claude.exe"},
        },
        "files": [],
    }
    for path in sorted(item for item in output.rglob("*") if item.is_file()):
        manifest["files"].append(
            {
                "path": path.relative_to(output).as_posix(),
                "sha256": sha256(path.read_bytes()),
                "bytes": path.stat().st_size,
            }
        )
    (output / "manifest.json").write_bytes(canonical(manifest))
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--npm-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.npm_root, args.output), indent=2))


if __name__ == "__main__":
    main()

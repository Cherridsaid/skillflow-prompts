import json
import os

import pytest

from scripts.prepare_native_clients import prepare
from skillflow.util import sha256

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows native client bundle")


def executable(path, version):
    # The system command is copied to simulate an ordinary signed native client.
    source = __import__("pathlib").Path(os.environ["SystemRoot"]) / "System32/cmd.exe"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(source.read_bytes())
    wrapper = path.with_name(path.stem + "-version.cmd")
    wrapper.write_text(f"@echo {version}\n")


def test_private_native_bundle_has_exact_file_manifest(tmp_path, monkeypatch):
    npm = tmp_path / "npm"
    codex = npm / "@openai/codex"
    vendor = codex / "node_modules/@openai/codex-win32-x64/vendor/x/bin"
    claude = npm / "@anthropic-ai/claude-code"
    executable(vendor / "codex.exe", "codex-cli synthetic")
    executable(claude / "bin/claude.exe", "claude synthetic")
    for path in (
        codex / "package.json",
        codex / "README.md",
        claude / "package.json",
        claude / "README.md",
        claude / "LICENSE.md",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic")

    def fake_version(path):
        return "codex-cli synthetic" if path.name == "codex.exe" else "claude synthetic"

    monkeypatch.setattr("scripts.prepare_native_clients.version", fake_version)
    result = prepare(npm, tmp_path / "bundle")
    manifest = json.loads((tmp_path / "bundle/manifest.json").read_text())
    assert result == manifest
    assert {item["path"] for item in manifest["files"]} == {
        "claude/LICENSE.md",
        "claude/README.md",
        "claude/claude.exe",
        "claude/package.json",
        "codex/README.md",
        "codex/package.json",
        "codex/vendor/x/bin/codex-version.cmd",
        "codex/vendor/x/bin/codex.exe",
    }
    for item in manifest["files"]:
        assert sha256((tmp_path / "bundle" / item["path"]).read_bytes()) == item["sha256"]

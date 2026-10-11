"""Install native user hooks without replacing other hooks or trusting their code."""

import json
import os
import queue
import shlex
import subprocess
import sys
import tempfile
import threading
import tomllib
from pathlib import Path

from .api import WRITER
from .util import canonical, require, sha256, strict_json


def write_atomic(path, content):
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".skillflow-", delete=False
        ) as output:
            temporary = Path(output.name)
            if path.exists():
                if os.name == "nt":
                    import win32security

                    flags = (
                        win32security.OWNER_SECURITY_INFORMATION
                        | win32security.DACL_SECURITY_INFORMATION
                    )
                    descriptor = win32security.GetFileSecurity(str(path), flags)
                    win32security.SetFileSecurity(str(temporary), flags, descriptor)
                else:
                    os.chmod(temporary, path.stat().st_mode)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class CodexConfig:
    """The installed app-server's native config/trust API; no model session."""

    def __init__(self, executable, cwd):
        self.sequence = 0
        self.messages = queue.Queue()
        self.process = subprocess.Popen(
            [executable, "app-server", "--stdio"],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        def read():
            for line in self.process.stdout:
                try:
                    self.messages.put(strict_json(line))
                except ValueError:
                    continue

        threading.Thread(target=read, daemon=True).start()
        self.call(
            "initialize",
            {
                "clientInfo": {"name": "skillflow-installer", "version": "0.1.0a1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        self.process.stdin.write(b'{"jsonrpc":"2.0","method":"initialized","params":{}}\n')
        self.process.stdin.flush()

    def call(self, method, params):
        self.sequence += 1
        self.process.stdin.write(
            canonical({"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params})
            + b"\n"
        )
        self.process.stdin.flush()
        while True:
            message = self.messages.get(timeout=30)
            if message.get("id") == self.sequence:
                require("error" not in message, "CLIENT_INSTALL", str(message.get("error")))
                return message["result"]

    def close(self):
        self.process.terminate()
        self.process.wait(timeout=10)
        self.process.stdin.close()
        self.process.stdout.close()


def install(client, config_path, *, executable=None, apply=False):
    config_path = Path(config_path).resolve()
    config = strict_json(config_path.read_bytes())
    connections = config.get("connections", [config])
    require(
        connections and all(c["client"] == client for c in connections),
        "CLIENT_INSTALL",
        "Connections must belong to the selected client",
    )
    launcher = Path(__file__).with_name("hook_launcher.py").resolve()
    entry = config_path.parent / (client + "-hook-entry.py")
    entry_text = (
        "import runpy, sys\nsys.argv = "
        + repr(["skillflow-hook", "--python", sys.executable, "--config", str(config_path)])
        + "\nrunpy.run_path("
        + repr(str(launcher))
        + ", run_name='__main__')\n"
    )
    argv = ["python", entry.as_posix()]
    command = (
        subprocess.list2cmdline(argv) if client == "codex" and os.name == "nt" else shlex.join(argv)
    )
    home = Path.home() / (".codex" if client == "codex" else ".claude")
    path = home / ("hooks.json" if client == "codex" else "settings.json")
    events = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]
    if client == "claude":
        events += ["UserPromptExpansion", "PostToolBatch"]
    before = path.read_bytes() if path.exists() else b"{}"
    data = strict_json(before)
    for event in events:
        groups = data.setdefault("hooks", {}).setdefault(event, [])
        # Only replace this configuration's own prior installation.
        for group in groups:
            group["hooks"] = [
                h
                for h in group["hooks"]
                if not (
                    entry.as_posix() in h.get("command", "")
                    or (
                        launcher.as_posix() in h.get("command", "")
                        and config_path.as_posix() in h.get("command", "")
                    )
                )
            ]
        data["hooks"][event] = [g for g in groups if g["hooks"]] + [
            {"hooks": [{"type": "command", "command": command, "timeout": 30}]}
        ]
    server = {
        "command": sys.executable,
        "args": ["-m", "skillflow_prompts", "mcp-client", "--config", str(config_path)],
    }
    if client == "claude":
        allow = data.setdefault("permissions", {}).setdefault("allow", [])
        data["permissions"]["allow"] = sorted(
            set(allow) | {"mcp__skillflow-prompts__" + name for name in WRITER}
        )
    report = {
        "client": client,
        "state": "PLANNED",
        "hooks_file": str(path),
        "events": events,
        "hook_command": command,
        "hook_entry": str(entry),
        "hook_entry_content": entry_text,
        "mcp_server": server,
        "credential_roles": ["writer"],
        "workspaces": [c["workspace"] for c in connections],
        "administrative": False,
        "protection_demonstrated": False,
    }
    manifest = config_path.parent / (client + "-installation.json")
    if not apply:
        return report
    require(executable, "CLIENT_INSTALL", "Installed native client executable required")
    if client == "codex":
        prior_server = (
            tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
            .get("mcp_servers", {})
            .get("skillflow-prompts")
        )
        if prior_server:
            args = prior_server.get("args", [])
            owned = (
                prior_server.get("command") == sys.executable
                and args[:2] == ["-m", "skillflow_prompts"]
            ) or (
                Path(prior_server.get("command", ""))
                == Path(sys.executable).with_name("skillflow-prompts.exe")
                and args[:1] == ["mcp"]
            )
            require(
                owned,
                "CLIENT_INSTALL",
                "Existing SkillFlow MCP registration is not owned by this runtime; explicit migration required",
            )
    else:
        user_state = Path.home() / ".claude.json"
        current = (
            strict_json(user_state.read_bytes()).get("mcpServers", {}).get("skillflow-prompts")
            if user_state.exists()
            else None
        )
        require(
            current is None or all(current.get(k) == v for k, v in server.items()),
            "CLIENT_INSTALL",
            "Existing SkillFlow MCP registration differs; explicit migration required",
        )
    backup = config_path.parent / ("install-backup-" + client)
    backup.mkdir(exist_ok=True)
    original = backup / (sha256(before) + ".json")
    if not original.exists():
        original.write_bytes(before)
    if client == "codex":
        require(executable, "CLIENT_INSTALL", "Installed native Codex executable required")
        config_file = home / "config.toml"
        content = config_file.read_bytes()
        saved = backup / (sha256(content) + ".toml")
        if not saved.exists():
            saved.write_bytes(content)
    home.mkdir(exist_ok=True)
    require(
        (path.read_bytes() if path.exists() else b"{}") == before,
        "CLIENT_INSTALL",
        "Client settings changed during installation; retry after reviewing the changes",
    )
    write_atomic(entry, entry_text.encode("utf-8"))
    write_atomic(path, canonical(data))
    if client == "codex":
        rpc = CodexConfig(executable, connections[0]["workspace"])
        try:
            found = rpc.call("hooks/list", {"cwds": [c["workspace"] for c in connections]})
            hooks = {
                h["key"]: h
                for group in found["data"]
                for h in group["hooks"]
                if h.get("command") == command and Path(h["sourcePath"]).resolve() == path.resolve()
            }
            require(
                len(hooks) == len(events),
                "CLIENT_INSTALL",
                "Native client did not discover every installed hook",
            )
            for hook in hooks.values():
                rpc.call(
                    "config/value/write",
                    {
                        "keyPath": "hooks.state." + json.dumps(hook["key"]),
                        "value": {"enabled": True, "trusted_hash": hook["currentHash"]},
                        "mergeStrategy": "upsert",
                    },
                )
            server.update(
                enabled_tools=sorted(WRITER),
                tools={name: {"approval_mode": "approve"} for name in WRITER},
            )
            rpc.call(
                "config/value/write",
                {
                    "keyPath": "mcp_servers.skillflow-prompts",
                    "value": server,
                    "mergeStrategy": "upsert",
                },
            )
        finally:
            rpc.close()
    else:
        require(executable, "CLIENT_INSTALL", "Installed native Claude executable required")
        result = subprocess.run(
            [executable, "mcp", "get", "skillflow-prompts"]
            if current
            else [
                executable,
                "mcp",
                "add-json",
                "--scope",
                "user",
                "skillflow-prompts",
                json.dumps(server),
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
        require(
            result.returncode == 0,
            "CLIENT_INSTALL",
            "Native MCP registration failed; inspect the partial installation and saved backup",
        )
    report.update(
        state="INSTALLED", backup=str(backup), configuration_sha256=sha256(config_path.read_bytes())
    )
    write_atomic(manifest, canonical(report))
    return report

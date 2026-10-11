"""Read-only, publication-safe capability inventory."""

import importlib.metadata
import os
import platform
import shutil
import subprocess

from .util import now


def diagnose():
    clients = {}
    for name in ("codex", "claude", "git"):
        executable = (
            shutil.which(name + ".cmd") if os.name == "nt" and name != "git" else shutil.which(name)
        )
        if executable:
            try:
                run = subprocess.run(
                    [executable, "--version"], capture_output=True, timeout=10, check=False
                )
                clients[name] = {
                    "version": run.stdout.decode(errors="replace").strip(),
                    "exit_code": run.returncode,
                    "interaction_test": "NOT_RUN",
                }
            except (OSError, subprocess.TimeoutExpired):
                clients[name] = {"version": None, "interaction_test": "NOT_RUN"}
        else:
            clients[name] = {"version": None, "interaction_test": "NOT_RUN"}
    packages = {}
    for name in ("skillflow-prompts", "mcp", "cryptography", "pydantic", "fastapi", "uvicorn"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {
        "format": "1",
        "timestamp": now(),
        "os": platform.system(),
        "os_release": platform.release(),
        "machine_arch": platform.machine(),
        "cpu_count": os.cpu_count(),
        "python": platform.python_version(),
        "packages": packages,
        "clients": clients,
        "protected_windows_acceptance": "NOT_RUN",
        "public_settings": "NOT_CONFIGURED",
        "paths_and_usernames_redacted": True,
    }

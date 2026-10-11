"""Isolated synthetic HTTP harness, shared by integration tests and benchmarks."""

import socket
import subprocess
import sys
import time
from contextlib import contextmanager

import httpx

from .storage import state_path
from .util import strict_json


@contextmanager
def live_server(root):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    mutable = state_path(root, strict_json((root / "config.json").read_bytes())).parent
    mutable.mkdir(parents=True, exist_ok=True)
    with (mutable / "server-test.log").open("wb") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "skillflow_prompts",
                "serve",
                "--root",
                str(root),
                "--port",
                str(port),
            ],
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    if httpx.get(url + "/health", timeout=0.5, trust_env=False).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if process.poll() is not None:
                    raise RuntimeError("Synthetic server exited; inspect its local log")
                time.sleep(0.05)
            else:
                raise RuntimeError("Synthetic server startup timeout")
            yield url, process
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

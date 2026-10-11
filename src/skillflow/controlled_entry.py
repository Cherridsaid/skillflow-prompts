"""Pinned Windows client entry with admission before spawn and retained raw output.

This supervisor is not a network sandbox, an implicit skill resolver or proof of
account separation. Only live authority results leave its output gate. A native
client opened elsewhere is not covered by this entry.
"""

import os
import queue
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Literal

from pydantic import Field

from .client_hook import connection
from .delivery import safe_directory
from .models import Digest, Strict
from .server import Client
from .util import FlowError, canonical, require, sha256, strict_json, uid
from .windows_process import RestrictedProcess


class Launch(Strict):
    format: Literal["skillflow-controlled-client-v1"]
    client: Literal["codex", "claude"]
    argv: list[str] = Field(min_length=1, max_length=100)
    cwd: str
    env: dict[str, str]
    writer_sid: str = Field(pattern=r"^S-1-")
    appcontainer_sid: str | None = Field(default=None, pattern=r"^S-1-15-2(?:-[0-9]+)+$")
    unconfined_network: bool = False  # explicit opt-out; never implied by an absent SID
    connection_file: str
    pinned_files: dict[str, Digest] = Field(min_length=2)
    timeout_seconds: int = Field(ge=1, le=3600)
    max_output_bytes: int = Field(default=16_000_000, ge=1024, le=64_000_000)


def run(manifest, prompt, output):
    """Run one ordinary input under a reviewed manifest; never echo client stdout."""
    require(os.name == "nt", "WINDOWS_REQUIRED", "This entry uses Windows restricted tokens")
    manifest = Path(manifest)
    require(manifest.is_absolute(), "LAUNCH_PATH", "Manifest path must be absolute")
    safe_directory(manifest.parent)
    require(
        not manifest.is_symlink()
        and not (
            getattr(manifest.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ),
        "UNSAFE_PATH",
        "Manifest must be an ordinary file",
    )
    raw = manifest.read_bytes()
    launch = Launch.model_validate(strict_json(raw))
    require(
        (launch.appcontainer_sid is not None) != launch.unconfined_network,
        "NETWORK_BOUNDARY_REQUIRED",
        "Declare appcontainer_sid, or set unconfined_network to true explicitly, never both",
    )
    require(0 < len(prompt.encode("utf-8")) <= 100_000, "PROMPT_SIZE", "Input size outside bounds")
    for name in [launch.argv[0], launch.connection_file, launch.cwd, *launch.pinned_files]:
        require(Path(name).is_absolute(), "LAUNCH_PATH", "Every launch path must be absolute")
    require(
        launch.argv[0] in launch.pinned_files and launch.connection_file in launch.pinned_files,
        "LAUNCH_PIN",
        "Executable and controller connection must be pinned",
    )
    for name, digest in launch.pinned_files.items():
        safe_directory(Path(name).parent)
        require(
            not Path(name).is_symlink()
            and not (
                getattr(Path(name).lstat(), "st_file_attributes", 0)
                & stat.FILE_ATTRIBUTE_REPARSE_POINT
            )
            and sha256(Path(name).read_bytes()) == digest,
            "LAUNCH_CHANGED",
            "Pinned launch material changed: " + name,
        )
    config = connection(strict_json(Path(launch.connection_file).read_bytes()), Path(launch.cwd))
    require(config and config["client"] == launch.client, "CLIENT_SCOPE", "No exact client binding")
    client = Client(config["url"], config["token_file"], config.get("ca"))
    monitor = Client(config["url"], config["token_file"], config.get("ca"), timeout=2)
    output = Path(output).absolute()
    require(not output.exists(), "EVIDENCE_EXISTS", "Use a new evidence directory")
    safe_directory(output.parent)
    output.mkdir()
    session_id = uid()
    result = {
        "manifest_sha256": sha256(raw),
        "client": launch.client,
        "session_id": session_id,
        "started": False,
        "native_exit_code": None,
        "natural_parent_exit": False,
        "raw_output_released": False,
        "authority_result": None,
        "mechanism": (
            "SkillFlow admission, output gate, restricted token and Job"
            + (
                "; AppContainer network identity"
                if launch.appcontainer_sid
                else "; no network confinement (unconfined_network declared)"
            )
        ),
        "account_separation_verified": False,
    }
    trace = (output / "authority.jsonl").open("ab", buffering=0)

    def event(name):
        response = client.request(
            "/v1/client-event",
            {
                "binding": config["binding"],
                "client": launch.client,
                "cwd": launch.cwd,
                "session_id": session_id,
                "event": name,
                "event_id": session_id + ":" + name,
                "prompt": prompt if name == "UserPromptSubmit" else "",
            },
        )
        trace.write(canonical({"event": name, "response": response}) + b"\n")
        require(
            response.get("ok") and response.get("allow"),
            "ENTRY_DENIED",
            "Controller did not authorize " + name,
            response.get("status", {}).get("delivery", {}).get("missing", []),
        )
        return response

    started = time.monotonic()
    try:
        event("SessionStart")
        admission = event("UserPromptSubmit")
        result["case_id"] = admission["status"]["case_id"]
        require(
            admission["status"]["state"] not in ("COMPLETED", "DELIVERED", "CANCELLED"),
            "CASE_TERMINAL",
            "Provision a new case before starting another task",
        )
        context = {
            "case_id": result["case_id"],
            "revision": admission["status"]["revision"],
            "skills": [
                {"invocation_id": skill["invocation_id"], "contract_id": skill["contract_id"]}
                for skill in admission["status"]["skills"]
            ],
        }
        submitted_prompt = (
            prompt + "\n\nSkillFlow entry already admitted this workflow before launch. "
            "Use the installed SkillFlow MCP skill_read tool for each exact contract, then the required "
            "artifact_submit and checks, or message_submit only if that contract declares a message input. "
            "Stop normally once the server reports DELIVERED or COMPLETED. "
            "The entry releases only authority-accepted results. "
            "The writer cannot review or approve its own work. This context informs tool routing; "
            "it does not replace the external controller gates.\n"
            + canonical(context).decode("utf-8")
        )
        result.update(
            user_prompt_sha256=sha256(prompt.encode("utf-8")),
            submitted_prompt_sha256=sha256(submitted_prompt.encode("utf-8")),
        )
        messages = queue.Queue(maxsize=64)
        require(
            time.monotonic() - started < launch.timeout_seconds,
            "CLIENT_TIMEOUT",
            "Admission exceeded the launch deadline",
        )
        with RestrictedProcess(
            launch.argv,
            cwd=launch.cwd,
            env=launch.env,
            expected_sid=launch.writer_sid,
            appcontainer_sid=launch.appcontainer_sid,
        ) as process:
            result.update(
                started=True,
                pid=process.pid,
                actual_sid=process.sid,
                restricted_sids=process.restricted_sids,
                appcontainer_sid=process.appcontainer_sid,
            )
            stopped = threading.Event()

            def read():
                try:
                    while data := process.stdout.read(65536):
                        while not stopped.is_set():
                            try:
                                messages.put(data, timeout=0.1)
                                break
                            except queue.Full:
                                continue
                        if stopped.is_set():
                            return
                except OSError:
                    pass
                finally:
                    stopped.set()

            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            # Input goes through stdin, never shell interpolation or additional flags.
            input_errors = []

            def write():
                try:
                    process.stdin.write(submitted_prompt.encode("utf-8") + b"\n")
                    process.stdin.close()
                except (OSError, ValueError) as exc:
                    input_errors.append(type(exc).__name__)

            writer = threading.Thread(target=write, daemon=True)
            writer.start()
            total = 0
            next_check = 0
            try:
                with (output / "native-output.bin").open("xb") as capture:
                    while True:
                        require(
                            time.monotonic() - started < launch.timeout_seconds,
                            "CLIENT_TIMEOUT",
                            "Native session exceeded its bounded duration",
                        )
                        if time.monotonic() >= next_check:
                            response = monitor.call(
                                "control_status", {"case_id": result["case_id"]}
                            )
                            trace.write(
                                canonical({"event": "control_status", "response": response}) + b"\n"
                            )
                            require(
                                response.get("ok") and response["result"]["allow_process"],
                                "PROCESS_AUTHORITY_LOST",
                                "Authority stopped or cannot admit this process",
                            )
                            next_check = time.monotonic() + 1
                        if stopped.is_set() and messages.empty():
                            try:
                                code = process.wait(0.1)
                                break
                            except subprocess.TimeoutExpired:
                                continue
                        try:
                            data = messages.get(timeout=0.1)
                        except queue.Empty:
                            continue
                        total += len(data)
                        require(
                            total <= launch.max_output_bytes,
                            "CLIENT_OUTPUT_LIMIT",
                            "Native output exceeded the capture limit",
                        )
                        capture.write(data)
                        capture.flush()
                result.update(native_exit_code=code, natural_parent_exit=True, output_bytes=total)
            finally:
                stopped.set()
                # Closing the Job also ends detached descendants retaining stdout.
                process.close()
                reader.join(timeout=2)
                writer.join(timeout=2)
        require(code == 0, "CLIENT_FAILED", "Native client exited unsuccessfully; output retained")
        require(
            not input_errors and not writer.is_alive(),
            "CLIENT_INPUT_FAILED",
            "The native client did not receive the complete input",
        )
        final = event("Stop")
        require(
            final["status"]["state"] in ("COMPLETED", "DELIVERED", "CANCELLED"),
            "WORKFLOW_INCOMPLETE",
            "Successful client exit does not complete the workflow",
        )
        if final["status"]["state"] == "COMPLETED":
            completion = client.call("completion_read", {"case_id": result["case_id"]})
            trace.write(canonical({"event": "completion_read", "response": completion}) + b"\n")
            require(
                completion.get("ok"), "COMPLETION_DENIED", "Authority did not release completion"
            )
            for message in completion["result"]["messages"]:
                require(
                    sha256(message["content"].encode("utf-8")) == message["sha256"],
                    "COMPLETION_CHANGED",
                    "Response bytes differ from the authority hash",
                )
            result["authority_result"] = completion["result"]
        else:
            # File delivery remains in the existing signed outbox; cancellation
            # returns status only. Never substitute the native client's last text.
            result["authority_result"] = final["status"]
        result["ok"] = True
    except (FlowError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        result.update(
            ok=False,
            error=exc.result()["error"]
            if isinstance(exc, FlowError)
            else {
                "code": type(exc).__name__,
                "message": "Native entry failed; inspect local evidence",
            },
        )
    finally:
        result["duration_seconds"] = time.monotonic() - started
        trace.close()
        (output / "result.json").write_bytes(canonical(result))
    return result

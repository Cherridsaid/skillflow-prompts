"""Actual restricted processes and HTTP authority; synthetic clients, not native acceptance."""

import base64
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from skillflow.core import Controller
from skillflow.harness import live_server
from skillflow.util import canonical, sha256, strict_json
from tests.integration.test_conversation import conversation

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows process boundary required")


def complete(session):
    session.invoke(
        "writer",
        "message_submit",
        {
            "invocation_id": session.invocation_id,
            "content_base64": base64.b64encode(b"SYNTHETIC approved answer").decode(),
        },
    )
    session.check("message-content")
    session.review()
    session.deliver()


def prepare(tmp_path):
    from scripts.windows.restricted_probe import prepare_python, set_acl
    from skillflow.boundary import windows_identity

    session = conversation(tmp_path)
    runtime = prepare_python(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    set_acl(workspace, public_read=True)
    skill = workspace / "SKILL.md"
    skill.write_text(
        "Synthetic conversation; full exact message, independent review, human approval."
    )
    set_acl(skill, public_read=True)
    config = strict_json((session.root / "config.json").read_bytes())
    config["client_bindings"] = {
        "conversation": {
            "client": "codex",
            "workspace": str(workspace),
            "case_key": "demo-quality",
            "principals": ["demo-writer"],
            "enforce_termination": False,
            "skills": {
                "conversation": {
                    "contract": "conversation@1.0.0",
                    "files": [{"path": str(skill), "sha256": sha256(skill.read_bytes())}],
                }
            },
        }
    }
    (session.root / "config.json").write_bytes(canonical(config))
    session.controller = Controller(session.root)
    launch = {
        "format": "skillflow-controlled-client-v1",
        "client": "codex",
        "argv": [
            str(runtime),
            "-c",
            "import sys;sys.stdin.read();print('UNREVIEWED CLIENT OUTPUT')",
        ],
        "cwd": str(workspace),
        "env": {"SystemRoot": os.environ["SystemRoot"]},
        "writer_sid": windows_identity(),
        "unconfined_network": True,
        "timeout_seconds": 15,
        "connection_file": str(tmp_path / "connection.json"),
        "pinned_files": {str(runtime): sha256(runtime.read_bytes())},
    }
    return session, launch


def write_launch(tmp_path, launch, session, url):
    connection = tmp_path / "connection.json"
    connection.write_bytes(
        canonical(
            {
                "binding": "conversation",
                "client": "codex",
                "workspace": launch["cwd"],
                "url": url,
                "token_file": str(session.root / "writer.token"),
            }
        )
    )
    launch["pinned_files"][str(connection)] = sha256(connection.read_bytes())
    manifest = tmp_path / "launch.json"
    manifest.write_bytes(canonical(launch))
    return manifest


def test_unfinished_and_outage_never_release_or_spawn_unadmitted_process(tmp_path):
    from skillflow.controlled_entry import run

    session, launch = prepare(tmp_path)
    with live_server(session.root) as (url, _):
        manifest = write_launch(tmp_path, launch, session, url)
        blocked = run(manifest, "$conversation ordinary activation", tmp_path / "blocked")
        assert blocked["started"] and blocked["natural_parent_exit"]
        assert not blocked["ok"] and blocked["error"]["code"] == "WORKFLOW_INCOMPLETE"
        assert blocked["authority_result"] is None and not blocked["raw_output_released"]
        assert b"UNREVIEWED CLIENT OUTPUT" in (tmp_path / "blocked/native-output.bin").read_bytes()
    outage = run(manifest, "$conversation ordinary activation", tmp_path / "outage")
    assert not outage["started"] and not outage["ok"]
    assert not (tmp_path / "outage/native-output.bin").exists()


def test_completed_entry_returns_only_exact_authority_message(tmp_path):
    from skillflow.controlled_entry import run

    session, launch = prepare(tmp_path)
    launch["argv"][-1] += (
        ";import pathlib,time\nwhile not pathlib.Path('done').exists(): time.sleep(0.02)"
    )
    with live_server(session.root) as (url, _):
        manifest = write_launch(tmp_path, launch, session, url)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(
                run, manifest, "$conversation ordinary activation", tmp_path / "complete"
            )
            deadline = time.monotonic() + 10
            while not (tmp_path / "complete/native-output.bin").exists():
                assert not pending.done() and time.monotonic() < deadline
                time.sleep(0.02)
            complete(session)
            (Path(launch["cwd"]) / "done").write_text("synthetic operator completed the case")
            result = pending.result(timeout=10)
    assert result["ok"] and result["natural_parent_exit"]
    assert result["authority_result"]["messages"][0]["content"] == "SYNTHETIC approved answer"
    assert "UNREVIEWED CLIENT OUTPUT" not in canonical(result).decode()
    assert not (session.root / "outbox").exists()


def test_client_which_never_reads_stdin_cannot_escape_timeout(tmp_path):
    from skillflow.controlled_entry import run

    session, launch = prepare(tmp_path)
    launch.update(timeout_seconds=2)
    launch["argv"][-1] = "import time;time.sleep(30)"
    with live_server(session.root) as (url, _):
        manifest = write_launch(tmp_path, launch, session, url)
        result = run(manifest, "x" * 90_000, tmp_path / "timeout")
    assert not result["ok"] and result["error"]["code"] == "CLIENT_TIMEOUT"
    assert result["duration_seconds"] < 8
    assert not result["natural_parent_exit"] and result["authority_result"] is None


@pytest.mark.parametrize("interruption", ["cancel", "pause", "authority-outage"])
def test_interruption_ends_actual_process_and_retains_output(tmp_path, interruption):
    import win32api
    import win32con
    import win32event

    from skillflow.control import set_state
    from skillflow.controlled_entry import run

    session, launch = prepare(tmp_path)
    launch["argv"][-1] = "import os,time;print(os.getpid(),flush=True);time.sleep(30)"
    with ThreadPoolExecutor(max_workers=1) as pool:
        with live_server(session.root) as (url, _):
            manifest = write_launch(tmp_path, launch, session, url)
            pending = pool.submit(run, manifest, "$conversation activation", tmp_path / "interrupt")
            capture = tmp_path / "interrupt/native-output.bin"
            deadline = time.monotonic() + 10
            while not capture.exists() or not capture.read_bytes().strip():
                assert not pending.done() and time.monotonic() < deadline
                time.sleep(0.02)
            process = win32api.OpenProcess(win32con.SYNCHRONIZE, False, int(capture.read_bytes()))
            assert win32event.WaitForSingleObject(process, 0) == win32con.WAIT_TIMEOUT
            if interruption == "cancel":
                session.invoke("operator", "flow_cancel", {"reason": "actual process test"})
            elif interruption == "pause":
                set_state(
                    session.controller,
                    session.principals["operator"],
                    {"paused": True, "revision": 0, "idempotency_key": "stop"},
                )
            if interruption != "authority-outage":
                result = pending.result(timeout=8)
        if interruption == "authority-outage":
            result = pending.result(timeout=8)
        try:
            assert win32event.WaitForSingleObject(process, 2000) == 0
        finally:
            process.Close()
    assert not result["ok"] and result["error"]["code"] == "PROCESS_AUTHORITY_LOST"
    assert not result["natural_parent_exit"] and not result["raw_output_released"]
    assert result["authority_result"] is None
    assert not (session.root / "outbox").exists()


def test_entry_refuses_a_manifest_without_an_explicit_network_decision(tmp_path):
    from skillflow.controlled_entry import run
    from skillflow.util import FlowError

    manifest = tmp_path / "launch.json"
    launch = {
        "format": "skillflow-controlled-client-v1",
        "client": "codex",
        "argv": ["C:/absent/client.exe"],
        "cwd": "C:/absent",
        "env": {},
        "writer_sid": "S-1-5-21-1-2-3-1001",
        "timeout_seconds": 5,
        "connection_file": "C:/absent/connection.json",
        "pinned_files": {"C:/absent/client.exe": "0" * 64, "C:/absent/connection.json": "0" * 64},
    }
    for extra in ({}, {"appcontainer_sid": "S-1-15-2-1-2-3-4-5-6-7", "unconfined_network": True}):
        manifest.write_bytes(canonical({**launch, **extra}))
        with pytest.raises(FlowError) as exc:
            run(manifest, "hello", tmp_path / "evidence")
        assert exc.value.code == "NETWORK_BOUNDARY_REQUIRED"
        assert not (tmp_path / "evidence").exists()

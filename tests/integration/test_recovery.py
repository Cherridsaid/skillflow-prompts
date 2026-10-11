import shutil
import sqlite3
from pathlib import Path

import pytest

import skillflow_prompts
from skillflow_prompts.bootstrap import create_demo
from skillflow_prompts.core import Controller
from skillflow_prompts.demo import DemoSession, run_demo
from skillflow_prompts.recovery import relative_path, restore_copy, snapshot
from skillflow_prompts.server import DaemonLock
from skillflow_prompts.util import FlowError, canonical, sha256, strict_json


def installation(tmp_path, delivered=False):
    layout = tmp_path / "installation"
    root = layout / "authority"
    if delivered:
        run_demo(root)
    else:
        create_demo(root)
        DemoSession(root)
    # Snapshot real package bytes along with the synthetic workflow, never a
    # claimed installed or qualified Windows runtime.
    shutil.copytree(
        Path(skillflow_prompts.__file__).parent,
        layout / "runtime" / "skillflow",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    return layout, root


def test_delivered_workflow_code_keys_and_outbox_restore_together(tmp_path):
    layout, root = installation(tmp_path, delivered=True)
    saved = tmp_path / "snapshot"
    result = snapshot(layout, saved)
    copy = tmp_path / "restored"
    restored = restore_copy(saved, copy, result["manifest_sha256"])
    assert restored["ok"] and not restored["activation_allowed"] and not restored["service_started"]
    with pytest.raises(FlowError, match="Restored copy"):
        Controller(copy / "authority")
    assert (root / "signing.key").read_bytes() == (copy / "authority/signing.key").read_bytes()
    assert (root / "config.json").read_bytes() == (copy / "authority/config.json").read_bytes()
    assert (copy / "runtime/skillflow/core.py").read_bytes() == (
        layout / "runtime/skillflow/core.py"
    ).read_bytes()
    for original in (root / "outbox").rglob("*"):
        if original.is_file():
            assert original.read_bytes() == (copy / original.relative_to(layout)).read_bytes()
    for name in (
        "cases",
        "events",
        "operations",
        "artifacts",
        "proofs",
        "reviews",
        "approvals",
        "deliveries",
    ):
        with (
            sqlite3.connect(root / "state.db") as before,
            sqlite3.connect(copy / "authority/state.db") as after,
        ):
            assert (
                before.execute("SELECT * FROM " + name).fetchall()
                == after.execute("SELECT * FROM " + name).fetchall()
            )
    with pytest.raises(FileExistsError):
        restore_copy(saved, copy, result["manifest_sha256"])
    with pytest.raises(ValueError, match="fingerprint"):
        restore_copy(saved, tmp_path / "wrong", "0" * 64)


def test_snapshot_refuses_live_daemon_and_external_configuration(tmp_path):
    layout, root = installation(tmp_path)
    with DaemonLock(root), pytest.raises(OSError):
        snapshot(layout, tmp_path / "live")
    assert not (tmp_path / "live").exists()
    config = strict_json((root / "config.json").read_bytes())
    config["destinations"]["local"] = "../../outside/sub/outbox"
    (root / "config.json").write_bytes(canonical(config))
    with pytest.raises(ValueError, match="inside its root"):
        snapshot(layout, tmp_path / "external")
    assert not (tmp_path / "external").exists()
    assert not (tmp_path / "outside").exists()


def test_restore_rejects_tampering_extra_files_and_path_traversal_before_writing(tmp_path):
    for name in (".. /escaped", "folder./file", "NUL.txt", "sub/COM1"):
        with pytest.raises(ValueError, match="ambiguous Windows"):
            relative_path(name)
    layout, _ = installation(tmp_path)
    saved = tmp_path / "snapshot"
    result = snapshot(layout, saved)
    extra = saved / "payload/unexpected.txt"
    extra.write_text("not in the accepted manifest")
    with pytest.raises(ValueError, match="incomplete or changed"):
        restore_copy(saved, tmp_path / "extra", result["manifest_sha256"])
    assert not (tmp_path / "extra").exists()
    manifest = strict_json((saved / "manifest.json").read_bytes())
    manifest["files"]["../escaped.txt"] = sha256(extra.read_bytes())
    raw = canonical(manifest)
    (saved / "manifest.json").write_bytes(raw)
    with pytest.raises(ValueError, match="escapes"):
        restore_copy(saved, tmp_path / "traversal", sha256(raw))
    assert not (tmp_path / "traversal").exists() and not (tmp_path / "escaped.txt").exists()

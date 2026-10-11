"""Real Windows effects on disposable ACLs, not deployment acceptance."""

import os
import sys

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows restricted token and NTFS required")
def test_restricted_writer_cannot_access_authority_or_escape_in_child(tmp_path):
    from scripts.windows.restricted_probe import run

    result = run(tmp_path / "probe")
    assert len(result["after"]["trials"]) == 10
    assert result["legitimate_observed"]
    assert not result["account_separation"]
    assert not result["job_descendant_effect"]


@pytest.mark.skipif(os.name != "nt", reason="Windows restricted process required")
def test_invalid_launch_never_falls_back_to_unrestricted_process(tmp_path):
    from skillflow_prompts.util import FlowError
    from skillflow_prompts.windows_process import RestrictedProcess

    target = tmp_path / "must-not-exist"
    command = [sys.executable, "-c", f"import pathlib;pathlib.Path({str(target)!r}).touch()"]
    for parameters, code in [
        ({"expected_sid": "S-1-5-21-1-2-3-1001", "env": {}}, "PROCESS_IDENTITY"),
        ({"env": {"Path": "a", "PATH": "b"}}, "PROCESS_CONFIG"),
    ]:
        with pytest.raises(FlowError) as exc:
            RestrictedProcess(command, cwd=str(tmp_path), **parameters)
        assert exc.value.code == code
        assert not target.exists()
    invalid = tmp_path / "invalid.exe"
    invalid.write_text("not a Windows executable")
    with pytest.raises(OSError):
        RestrictedProcess([str(invalid)], cwd=str(tmp_path), env={})
    assert not target.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows restricted process required")
def test_restricted_process_reports_the_token_it_actually_carries(tmp_path):
    from scripts.windows.restricted_probe import prepare_python, set_acl
    from skillflow_prompts.windows_process import RestrictedProcess

    runtime = prepare_python(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    set_acl(work, public_read=True)
    env = {"SystemRoot": os.environ["SystemRoot"]}
    with RestrictedProcess([str(runtime), "-c", "pass"], cwd=str(work), env=env) as process:
        # Read back from the child token, not echoed from the request.
        assert {"S-1-5-32-545", "S-1-1-0"} <= set(process.restricted_sids)
        assert process.appcontainer_sid is None
        assert process.wait(60) == 0

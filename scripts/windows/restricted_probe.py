"""Actual disposable before/after probes. Not Windows deployment acceptance."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from skillflow.util import canonical
from skillflow.windows_process import RestrictedProcess


def set_acl(path, *, public_read=False, public_write=False):
    import win32api
    import win32con
    import win32security as security

    token = security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        user = security.GetTokenInformation(token, security.TokenUser)[0]
    finally:
        token.Close()
    dacl = security.ACL()
    dacl.AddAccessAllowedAceEx(2, 3, win32con.GENERIC_ALL, user)
    if public_read or public_write:
        mask = (
            win32con.GENERIC_READ
            | win32con.GENERIC_WRITE
            | win32con.GENERIC_EXECUTE
            | win32con.DELETE
            if public_write
            else win32con.GENERIC_READ | win32con.GENERIC_EXECUTE
        )
        dacl.AddAccessAllowedAceEx(2, 3, mask, security.ConvertStringSidToSid("S-1-5-32-545"))
    security.SetNamedSecurityInfo(
        str(path),
        security.SE_FILE_OBJECT,
        security.DACL_SECURITY_INFORMATION | security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        dacl,
        None,
    )


def prepare_python(root):
    runtime = root / "python"
    runtime.mkdir()
    base = Path(sys.base_prefix)
    for name in (
        "python.exe",
        "python3.dll",
        f"python{sys.version_info.major}{sys.version_info.minor}.dll",
        "vcruntime140.dll",
        "vcruntime140_1.dll",
    ):
        if (base / name).is_file():
            shutil.copyfile(base / name, runtime / name)
    archive_name = f"python{sys.version_info.major}{sys.version_info.minor}"
    with zipfile.ZipFile(runtime / (archive_name + ".zip"), "w", zipfile.ZIP_DEFLATED) as archive:
        for file in (base / "Lib").rglob("*.py"):
            relative = file.relative_to(base / "Lib")
            if not {"site-packages", "test", "tests", "__pycache__"} & set(relative.parts):
                archive.write(file, relative.as_posix())
    (runtime / (archive_name + "._pth")).write_text(archive_name + ".zip\n.\n", encoding="utf-8")
    set_acl(runtime, public_read=True)
    for file in runtime.iterdir():
        set_acl(file, public_read=True)
    return runtime / "python.exe"


PROBE = r"""
import hashlib,json,os,pathlib,subprocess,sys
root=pathlib.Path(sys.argv[1]); phase=sys.argv[2]; trials=[]
for name in ['signing.key','reviewer.token','operator.token','contracts','state','outbox','direct-python']:
    target=root/'authority'/name
    try:
        if '.' in name: effect=hashlib.sha256(target.read_bytes()).hexdigest()
        else:
            target=target/(phase+'.txt'); target.write_text('SYNTHETIC WRITE'); effect=True
        trials.append({'name':name,'obtained':True,'effect':effect})
    except OSError as exc:
        trials.append({'name':name,'obtained':False,'errno':exc.errno,'winerror':exc.winerror})
# Changing authority ACLs or a child replacement must not regain write access.
acl=subprocess.run([os.environ['SystemRoot']+'/System32/icacls.exe',str(root/'authority'),' /grant'.strip(),'*S-1-1-0:(OI)(CI)F'],capture_output=True)
try:
    (root/'authority'/('acl-'+phase)).write_text('MUST NOT SUCCEED')
    acl_effect=True
except OSError: acl_effect=False
trials.append({'name':'acl-rewrite','obtained':acl_effect,'native_exit':acl.returncode})
child=subprocess.run([sys.executable,'-c','import pathlib,sys;pathlib.Path(sys.argv[1]).write_text("CHILD")',str(root/'authority'/('child-'+phase))],capture_output=True)
trials.append({'name':'child-process','obtained':(root/'authority'/('child-'+phase)).exists(),'native_exit':child.returncode})
try:
    import msvcrt
    fd=msvcrt.open_osfhandle(int(sys.argv[3]),os.O_RDONLY|os.O_BINARY)
    data=os.read(fd,1000); os.close(fd)
    trials.append({'name':'inherited-secret-handle','obtained':data==b'SYNTHETIC-signing.key'})
except OSError as exc:
    trials.append({'name':'inherited-secret-handle','obtained':False,'errno':exc.errno})
(root/'workspace'/('legitimate-'+phase)).write_text('ALLOWED WORKSPACE')
print(json.dumps({'trials':trials,'legitimate':True}),flush=True)
"""


def run(root):
    started = time.monotonic()
    root = Path(root).resolve()
    if root.exists():
        raise ValueError("Use a new directory; previous evidence is immutable")
    root.mkdir(parents=True)
    executable = prepare_python(root)
    authority = root / "authority"
    authority.mkdir()
    set_acl(authority)
    expected = {}
    for name in ("signing.key", "reviewer.token", "operator.token"):
        content = ("SYNTHETIC-" + name).encode()
        (authority / name).write_bytes(content)
        set_acl(authority / name)
        expected[name] = hashlib.sha256(content).hexdigest()
    for name in ("contracts", "state", "outbox", "direct-python"):
        (authority / name).mkdir()
        set_acl(authority / name)
    workspace = root / "workspace"
    workspace.mkdir()
    set_acl(workspace, public_write=True)
    environment = {"SystemRoot": os.environ["SystemRoot"]}
    args = [str(executable), "-c", PROBE, str(root)]
    import msvcrt

    secret = (authority / "signing.key").open("rb")
    secret_handle = msvcrt.get_osfhandle(secret.fileno())
    os.set_handle_inheritable(secret_handle, True)
    startup = subprocess.STARTUPINFO()
    startup.lpAttributeList = {"handle_list": [secret_handle]}
    try:
        baseline = subprocess.run(
            args + ["before", str(secret_handle)],
            env=environment,
            capture_output=True,
            timeout=30,
            startupinfo=startup,
        )
        (root / "before.stdout").write_bytes(baseline.stdout)
        (root / "before.stderr").write_bytes(baseline.stderr)
        if baseline.returncode:
            raise RuntimeError("Unrestricted baseline did not run")
        # Restore the fixture DACL; leave the secret handle inheritable. The
        # launcher's explicit handle list must keep it out of the child.
        for path in [authority, *authority.rglob("*")]:
            set_acl(path)
        with RestrictedProcess(
            args + ["after", str(secret_handle)], cwd=str(executable.parent), env=environment
        ) as process:
            process.stdin.close()
            exit_code = process.wait(30)
            output = process.stdout.read(65536)
            identity = {
                "sid": process.sid,
                "restricted_sids": process.restricted_sids,
                "pid": process.pid,
            }
    finally:
        secret.close()
    (root / "after.stdout").write_bytes(output)
    if exit_code:
        raise RuntimeError(f"Restricted probe did not run: {exit_code}; not prevention evidence")
    before = json.loads(baseline.stdout)
    after = json.loads(output)
    observed = {
        name: (authority / name / "after.txt").exists()
        for name in ("contracts", "state", "outbox", "direct-python")
    }
    observed.update(
        {
            "acl-rewrite": (authority / "acl-after").exists(),
            "child-process": (authority / "child-after").exists(),
        }
    )
    result = {
        "format": "skillflow-restricted-token-probe-v1",
        "before": before,
        "after": after,
        "observer_files": observed,
        "expected_secret_hashes": expected,
        "identity": identity,
        "mechanism": "Windows restricted SID access check + fixture NTFS ACL, no Codex sandbox or SkillFlow hook",
        "account_separation": False,
        "deployment_accepted": False,
        "legitimate_observed": (workspace / "legitimate-after").read_text() == "ALLOWED WORKSPACE",
        "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "wrapper_sha256": hashlib.sha256(
            (Path(__file__).resolve().parents[2] / "src/skillflow/windows_process.py").read_bytes()
        ).hexdigest(),
        "runtime_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in executable.parent.iterdir()
            if p.is_file()
        },
        "python_version": sys.version,
        "windows_version": list(sys.getwindowsversion()),
    }
    assert all(t["obtained"] for t in before["trials"])
    assert all(before["trials"][i]["effect"] == expected[name] for i, name in enumerate(expected))
    assert all(not t["obtained"] for t in after["trials"])
    assert not any(observed.values()) and result["legitimate_observed"]
    # Check a real live descendant handle, not only an absent delayed file.
    import win32api
    import win32con
    import win32event

    delayed = workspace / "delayed.txt"
    code = (
        "import subprocess,sys;p=subprocess.Popen([sys.executable,'-c',"
        + repr(
            "import time,pathlib;time.sleep(10);pathlib.Path("
            + repr(str(delayed))
            + ").write_text('ESCAPED')"
        )
        + "]);print(p.pid,flush=True)"
    )
    with RestrictedProcess(
        [str(executable), "-c", code], cwd=str(executable.parent), env=environment
    ) as process:
        process.stdin.close()
        child_pid = int(process.stdout.readline())
        descendant = win32api.OpenProcess(win32con.SYNCHRONIZE, False, child_pid)
        assert process.wait(10) == 0
        assert win32event.WaitForSingleObject(descendant, 0) == win32con.WAIT_TIMEOUT
    result["job_descendant_exited"] = win32event.WaitForSingleObject(descendant, 5000) == 0
    descendant.Close()
    result["job_descendant_effect"] = delayed.exists()
    result["duration_seconds"] = time.monotonic() - started
    assert result["job_descendant_exited"] and not result["job_descendant_effect"]
    (root / "result.json").write_bytes(canonical(result))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    result = run(arguments.output)
    print(
        json.dumps(
            {
                "forbidden_before": sum(t["obtained"] for t in result["before"]["trials"]),
                "forbidden_after": sum(t["obtained"] for t in result["after"]["trials"]),
                "legitimate": result["legitimate_observed"],
                "separated_accounts": False,
            }
        )
    )

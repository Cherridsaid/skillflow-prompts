"""Stopped, self-contained installation snapshots; restore only into a new copy.

SQLite, code, contracts, credentials and outbox travel together. Restoration
never starts a service, rewrites configuration or claims Windows ACL acceptance.
"""

import os
import sqlite3
from contextlib import closing
from pathlib import Path, PurePosixPath

from .server import DaemonLock
from .storage import state_path
from .util import canonical, now, sha256, strict_json


def ordinary_path(path):
    """Validate ancestry without creating directories or following reparse points."""
    path = Path(path).absolute()
    for node in [*reversed(path.parents), path]:
        if node.is_symlink() or (
            node.exists() and getattr(node.lstat(), "st_file_attributes", 0) & 0x400
        ):
            raise ValueError("Recovery refuses links and reparse points")
    return path.resolve()


def private_directory(path):
    """Protect the destination before writing any credential bytes."""
    path.mkdir(mode=0o700, parents=False, exist_ok=False)
    if os.name == "nt":
        import win32api
        import win32con
        import win32security as security

        token = security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            user = security.GetTokenInformation(token, security.TokenUser)[0]
        finally:
            token.Close()
        dacl = security.ACL()
        for sid in (
            user,
            security.ConvertStringSidToSid("S-1-5-18"),
            security.ConvertStringSidToSid("S-1-5-32-544"),
        ):
            dacl.AddAccessAllowedAceEx(2, 3, win32con.GENERIC_ALL, sid)
        security.SetNamedSecurityInfo(
            str(path),
            security.SE_FILE_OBJECT,
            security.DACL_SECURITY_INFORMATION | security.PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )


def inventory(root, skip=()):
    files, directories = {}, []
    ordinary_path(root)
    if not root.is_dir():
        raise ValueError("Recovery source directory is absent")

    def unreadable(error):
        raise error

    for parent, dirs, names in os.walk(root, followlinks=False, onerror=unreadable):
        for name in dirs + names:
            path = Path(parent) / name
            if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
                raise ValueError("Recovery refuses links and reparse points")
            relative = path.relative_to(root).as_posix()
            if path.is_dir():
                directories.append(relative)
            elif relative not in skip:
                if not path.is_file():
                    raise ValueError("Recovery requires ordinary files")
                files[relative] = sha256(path.read_bytes())
    return dict(sorted(files.items())), sorted(directories)


def relative_path(name):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise ValueError("Invalid recovery member")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or str(path) != name or name == ".":
        raise ValueError("Recovery member escapes its root")
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(10)),
        *(f"LPT{i}" for i in range(10)),
    }
    if any(
        part.endswith((".", " ")) or part.split(".", 1)[0].upper() in reserved
        for part in path.parts
    ):
        raise ValueError("Recovery member has an ambiguous Windows name")
    return path


def contained(root, path):
    path = ordinary_path(path)
    if not path.is_relative_to(root):
        raise ValueError("Snapshot requires all installation paths inside its root: " + str(path))
    return path


def snapshot(root, output, authority="authority"):
    root, output = ordinary_path(root), ordinary_path(output)
    if not root.is_dir() or not output.parent.is_dir():
        raise ValueError("Installation and snapshot parent must already exist")
    if output == root or output.is_relative_to(root) or root.is_relative_to(output):
        raise ValueError("Snapshot and installation must be disjoint directories")
    authority_path = contained(root, root / relative_path(authority))
    raw_config = (authority_path / "config.json").read_bytes()
    config = strict_json(raw_config)
    db_path = contained(root, state_path(authority_path, config))
    if not db_path.is_file() or not (root / "runtime").is_dir():
        raise ValueError("A complete installation requires an existing database and runtime")
    paths = [authority_path / p for p in config.get("destinations", {}).values()]
    paths += [authority_path / p for p in config.get("tls", {}).values()]
    if config.get("skill_catalog"):
        paths.append(Path(config["skill_catalog"]["path"]))
    for approval in config.get("catalog_approvals", {}).values():
        if approval.get("mapping"):
            paths.append(Path(approval["mapping"]["path"]))
    paths += [Path(w["path"]) for w in config.get("catalog_watches", [])]
    for binding in config.get("client_bindings", {}).values():
        paths.append(Path(binding["workspace"]))
        paths += [Path(f["path"]) for s in binding["skills"].values() for f in s["files"]]
    for path in paths:
        contained(root, path)
    if config.get("mode") == "protected":
        for role in ("writer", "reviewer", "operator", "connector"):
            if not (root / "credentials" / role / (role + ".token")).is_file():
                raise ValueError("A protected snapshot must include all role credentials")
    db_relative = db_path.relative_to(root).as_posix()
    skip = {
        db_relative,
        db_relative + "-wal",
        db_relative + "-shm",
        (db_path.parent / "daemon.lock").relative_to(root).as_posix(),
    }
    # The daemon and SQLite writer locks protect the DB/outbox consistency window.
    # Administrators must also stop launchers and other installation writers.
    with (
        DaemonLock(db_path.parent),
        closing(sqlite3.connect(db_path, isolation_level=None)) as lock,
    ):
        lock.execute("BEGIN IMMEDIATE")
        try:
            before, directories = inventory(root, skip)
            if sha256((authority_path / "config.json").read_bytes()) != sha256(raw_config):
                raise ValueError("Configuration changed during snapshot preparation")
            private_directory(output)
            payload = output / "payload"
            payload.mkdir()
            for directory in directories:
                (payload / directory).mkdir(parents=True, exist_ok=True)
            for name, digest in before.items():
                data = (root / name).read_bytes()
                if sha256(data) != digest:
                    raise ValueError("Installation changed during snapshot")
                with (payload / name).open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            destination = payload / db_relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with (
                closing(sqlite3.connect(db_path)) as source,
                closing(sqlite3.connect(destination)) as target,
            ):
                source.backup(target)
                if (
                    target.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                    or target.execute("PRAGMA foreign_key_check").fetchone()
                ):
                    raise ValueError("Database integrity failed")
                schema = target.execute("PRAGMA user_version").fetchone()[0]
                if schema != 1:
                    raise ValueError("Unsupported database schema")
            if inventory(root, skip) != (before, directories):
                raise ValueError("Installation changed during snapshot")
            files, dirs = inventory(payload)
            manifest = {
                "format": "skillflow-recovery-v1",
                "created_at": now(),
                "source_root": str(root),
                "authority": authority,
                "database": db_relative,
                "schema_version": schema,
                "files": files,
                "directories": dirs,
                "configuration_sha256": sha256(raw_config),
                "service_started": False,
                "acl_restored": False,
            }
            with (output / "manifest.json").open("xb") as stream:
                stream.write(canonical(manifest))
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            lock.execute("ROLLBACK")
    return {
        "ok": True,
        "snapshot": str(output),
        "files": len(files),
        "manifest_sha256": sha256(canonical(manifest)),
        "service_started": False,
    }


def restore_copy(snapshot_path, output, expected_sha256):
    source, output = ordinary_path(snapshot_path), ordinary_path(output)
    if not source.is_dir() or not output.parent.is_dir():
        raise ValueError("Snapshot and restore parent must already exist")
    raw = (source / "manifest.json").read_bytes()
    if sha256(raw) != expected_sha256:
        raise ValueError("Manifest differs from the operator's retained fingerprint")
    manifest = strict_json(raw)
    if manifest.get("format") != "skillflow-recovery-v1" or manifest.get("schema_version") != 1:
        raise ValueError("Unsupported recovery format")
    names = list(manifest["files"]) + manifest["directories"]
    for name in names + [manifest["database"], manifest["authority"]]:
        relative_path(name)
    if len({n.casefold() for n in names}) != len(names):
        raise ValueError("Recovery paths collide")
    if output == source or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("Restore target must be disjoint from the snapshot")
    payload = source / "payload"
    if inventory(payload) != (manifest["files"], manifest["directories"]):
        raise ValueError("Snapshot content is incomplete or changed")
    with closing(
        sqlite3.connect(
            f"{(payload / manifest['database']).as_uri()}?mode=ro&immutable=1", uri=True
        )
    ) as db:
        if (
            db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            or db.execute("PRAGMA user_version").fetchone()[0] != 1
        ):
            raise ValueError("Snapshot database is invalid")
    private_directory(output)
    for name in manifest["directories"]:
        (output / name).mkdir(parents=True, exist_ok=True)
    # Block authority startup even if restoration is interrupted halfway through.
    marker = output / manifest["authority"] / "RESTORE_PENDING.json"
    with marker.open("xb") as stream:
        stream.write(canonical({"manifest_sha256": expected_sha256, "activation_allowed": False}))
        stream.flush()
        os.fsync(stream.fileno())
    for name, digest in manifest["files"].items():
        data = (payload / name).read_bytes()
        if sha256(data) != digest:
            raise ValueError("Snapshot changed during restore")
        with (output / name).open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    if inventory(output, skip=(marker.relative_to(output).as_posix(),)) != (
        manifest["files"],
        manifest["directories"],
    ):
        raise ValueError("Restored contents differ")
    return {
        "ok": True,
        "restored_copy": str(output),
        "files": len(manifest["files"]),
        "manifest_sha256": expected_sha256,
        "service_started": False,
        "activation_allowed": False,
        "acl_restored": False,
        "next": "Review exact paths, identity permissions and acceptance before activation",
    }

"""One daemon, transactional SQLite authority; immutable evidence and BLOBs."""

import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

MIGRATION_1 = """
CREATE TABLE cases(id TEXT PRIMARY KEY, case_key TEXT UNIQUE NOT NULL, profile TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0,
 created INTEGER NOT NULL, updated INTEGER NOT NULL);
CREATE TABLE invocations(id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
 contract_hash TEXT NOT NULL, contract TEXT NOT NULL, parent_id TEXT REFERENCES invocations(id),
 slot TEXT, entered INTEGER NOT NULL DEFAULT 0, exited INTEGER NOT NULL DEFAULT 0,
 UNIQUE(case_id,parent_id,slot));
CREATE TABLE steps(id TEXT PRIMARY KEY, invocation_id TEXT NOT NULL REFERENCES invocations(id),
 name TEXT NOT NULL, definition TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'NOT_STARTED',
 attempts INTEGER NOT NULL DEFAULT 0, corrections INTEGER NOT NULL DEFAULT 0,
 last_input TEXT, UNIQUE(invocation_id,name));
CREATE TABLE obligations(id TEXT PRIMARY KEY, step_id TEXT NOT NULL REFERENCES steps(id),
 definition TEXT NOT NULL);
CREATE TABLE artifacts(id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
 invocation_id TEXT NOT NULL REFERENCES invocations(id), name TEXT NOT NULL, media_type TEXT NOT NULL,
 sha256 TEXT NOT NULL, content BLOB NOT NULL, provenance TEXT NOT NULL, created INTEGER NOT NULL);
CREATE TABLE inputs(invocation_id TEXT NOT NULL REFERENCES invocations(id), name TEXT NOT NULL,
 artifact_id TEXT NOT NULL REFERENCES artifacts(id), PRIMARY KEY(invocation_id,name));
CREATE TABLE proofs(id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
 invocation_id TEXT NOT NULL REFERENCES invocations(id), step_id TEXT NOT NULL REFERENCES steps(id),
 obligation_id TEXT NOT NULL REFERENCES obligations(id), input_hash TEXT NOT NULL,
 verifier TEXT NOT NULL, version TEXT NOT NULL, verifier_hash TEXT NOT NULL,
 result TEXT NOT NULL, issuer TEXT NOT NULL, created INTEGER NOT NULL);
CREATE TABLE reviews(id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
 snapshot TEXT NOT NULL, generation INTEGER NOT NULL, raw TEXT, verdict TEXT,
 issuer TEXT, created INTEGER NOT NULL, completed INTEGER, provider TEXT);
CREATE TABLE approvals(id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
 delivery_id TEXT NOT NULL, snapshot TEXT NOT NULL, revision INTEGER NOT NULL,
 actor TEXT NOT NULL, created INTEGER NOT NULL);
CREATE TABLE deliveries(id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
 snapshot TEXT NOT NULL, destination TEXT NOT NULL, state TEXT NOT NULL,
 receipt TEXT, created INTEGER NOT NULL);
CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT REFERENCES cases(id),
 kind TEXT NOT NULL, actor TEXT NOT NULL, data TEXT NOT NULL, created INTEGER NOT NULL);
CREATE TABLE operations(principal TEXT NOT NULL, key TEXT NOT NULL, digest TEXT NOT NULL,
 case_id TEXT, response TEXT NOT NULL, PRIMARY KEY(principal,key));
CREATE TABLE branches(invocation_id TEXT NOT NULL REFERENCES invocations(id), name TEXT NOT NULL,
 value TEXT NOT NULL, PRIMARY KEY(invocation_id,name));
CREATE TABLE sessions(id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
 principal TEXT NOT NULL, created INTEGER NOT NULL);
CREATE TABLE revocations(id TEXT PRIMARY KEY, reason TEXT NOT NULL, created INTEGER NOT NULL);
CREATE INDEX events_case ON events(case_id,id);
CREATE INDEX proofs_step ON proofs(step_id,input_hash);
"""


def state_path(root, config):
    """Resolve the administrator-selected mutable directory independently of authority files."""
    return (Path(root) / config.get("state_directory", ".") / "state.db").resolve()


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError("Unsupported database schema; restore a compatible release")
            if version == 0:
                triggers = ""
                for table in ("artifacts", "proofs", "approvals", "events", "revocations"):
                    for action in ("UPDATE", "DELETE"):
                        triggers += (
                            f"CREATE TRIGGER immutable_{table}_{action} BEFORE {action} "
                            f"ON {table} BEGIN SELECT RAISE(ABORT,'immutable record'); END;"
                        )
                db.executescript(
                    "BEGIN IMMEDIATE;" + MIGRATION_1 + triggers + "PRAGMA user_version=1; COMMIT;"
                )

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def backup(self, destination):
        target = Path(destination)
        if target.exists():
            raise FileExistsError("Backup destination already exists")
        with closing(self.connect()) as source, closing(sqlite3.connect(target)) as dest:
            source.backup(dest)
            if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup integrity failed")

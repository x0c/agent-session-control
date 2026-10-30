"""Durable operation intent, events, messages and audit in one SQLite file.

Adapted from Corral ``src/corral/control_store.py`` (MIT, corral contributors).
Reads never create or repair storage. Every write commits its actor/action/
target/timestamp audit row in the same transaction as the change.

Differences from the source host, all neutral and documented:

- The state root defaults to ``XDG_CONFIG_HOME/agent-session-control/control``;
  hosts inject their historical root explicitly.
- The supervisor identity recorded in audit rows is configurable
  (``supervisor=...``) instead of hard-coding the downstream product name.
- Namespaces map to separate database files inside one root
  (``runs.sqlite3`` for ``"default"``, ``runs.<namespace>.sqlite3``
  otherwise), so same-user namespaces cannot mistake each other's runs.
  Namespaces are a mistake guard, not tenant isolation.
- ``create``/``enqueue`` tolerate the cross-process insert race: when two
  processes pass the pre-check together, the ``UNIQUE`` constraint admits one
  and the loser re-reads the winner inside a new transaction, preserving the
  immutable-payload conflict rule.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from agent_session_control.errors import (
    EXIT_CONFLICT,
    ControlError,
    conflict,
    idempotency_conflict,
    not_found,
)

TERMINAL = frozenset({"completed", "failed", "interrupted"})
# A run stops needing supervision once its outcome is final or unknowable.
SETTLED = TERMINAL | {"unknown"}
DEFAULT_SUPERVISOR = "agent-session-control-supervisor"
DEFAULT_NAMESPACE = "default"

SCHEMA_VERSION = 1
MAX_ACTIVE_RUNS = 8

_NAMESPACE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
    id TEXT PRIMARY KEY, actor TEXT NOT NULL, request_key TEXT NOT NULL,
    digest TEXT NOT NULL, spec TEXT NOT NULL, data TEXT NOT NULL,
    created_at REAL NOT NULL, UNIQUE(request_key));
CREATE TABLE IF NOT EXISTS events(
    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
    identity TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(run_id, identity));
CREATE TABLE IF NOT EXISTS messages(
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, actor TEXT NOT NULL, request_key TEXT NOT NULL,
    digest TEXT NOT NULL, body TEXT NOT NULL, state TEXT NOT NULL, detail TEXT,
    created_at REAL NOT NULL, UNIQUE(run_id, request_key));
CREATE TABLE IF NOT EXISTS supervisors(
    run_id TEXT PRIMARY KEY, pid INTEGER NOT NULL, started_at REAL NOT NULL, heartbeat_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit(
    seq INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
    target TEXT NOT NULL, timestamp REAL NOT NULL);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id, seq);
"""


def default_home() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "agent-session-control" / "control"


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def run_version(record: dict) -> str:
    """Fingerprint only what a follow-up write depends on, so progress does not churn it."""
    basis = {
        "execution": record.get("execution"),
        "delivery": record.get("delivery"),
        "questions": sorted(str(q.get("request_id")) for q in record.get("questions") or []),
    }
    return "v2:" + digest(basis)[:16]


class RunStore:
    def __init__(
        self,
        root: Path | str | None = None,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        supervisor: str = DEFAULT_SUPERVISOR,
    ):
        if not _NAMESPACE_RE.match(namespace):
            raise ValueError(f"Unsafe namespace: {namespace!r}")
        self.root = Path(root) if root is not None else default_home()
        self.namespace = namespace
        self.supervisor = supervisor
        filename = "runs.sqlite3" if namespace == DEFAULT_NAMESPACE else f"runs.{namespace}.sqlite3"
        self.path = self.root / filename

    def exists(self) -> bool:
        return self.path.is_file()

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if write:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.path.is_symlink():
                raise conflict("The control database must not be a symlink.")
            conn = sqlite3.connect(self.path, timeout=15)
            os.chmod(self.path, 0o600)
        else:
            if not self.path.is_file():
                raise not_found("No delegation runs exist yet.")
            conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            if write:
                conn.executescript(_SCHEMA)
                current = conn.execute("PRAGMA user_version").fetchone()[0]
                if current > SCHEMA_VERSION:
                    raise conflict("Run storage was written by a newer version; upgrade first.")
                if current < SCHEMA_VERSION:
                    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        finally:
            conn.close()

    # -- primitives (caller holds the connection) ---------------------------

    @staticmethod
    def _audit(conn: sqlite3.Connection, actor: str, action: str, target: str) -> None:
        conn.execute(
            "INSERT INTO audit(actor,action,target,timestamp) VALUES(?,?,?,?)",
            (actor, action, target, time.time()),
        )

    def _supervisor_audit(self, conn: sqlite3.Connection, action: str, target: str) -> None:
        self._audit(conn, self.supervisor, action, target)

    @staticmethod
    def _row(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise not_found(f"Unknown run: {run_id}")
        return row

    def _append(self, conn: sqlite3.Connection, run_id: str, identity: str, body: dict) -> bool:
        cur = conn.execute(
            "INSERT OR IGNORE INTO events(run_id,identity,body) VALUES(?,?,?)",
            (run_id, identity, json.dumps({"at": time.time(), **body}, ensure_ascii=False)),
        )
        return bool(cur.rowcount)

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        data = json.loads(row["data"])
        data["version"] = run_version(data)
        return data

    def _actor(self, actor: str | None) -> str:
        return self.supervisor if actor is None else actor

    # -- runs -----------------------------------------------------------------

    def get(self, run_id: str, *, private: bool = False) -> dict:
        with self.connection() as conn:
            row = self._row(conn, run_id)
            sup = conn.execute("SELECT * FROM supervisors WHERE run_id=?", (run_id,)).fetchone()
        data = self._decode(row)
        data["supervisor"] = dict(sup) if sup else None
        if private:
            data["spec"] = json.loads(row["spec"])
        return data

    def recent(self, limit: int, actor: str | None = None) -> list[dict]:
        query = "SELECT * FROM runs" + (" WHERE actor=?" if actor else "") + " ORDER BY created_at DESC LIMIT ?"
        params: tuple[Any, ...] = (actor, limit) if actor else (limit,)
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._decode(r) for r in rows]

    def find(self, key: str) -> dict | None:
        if not self.exists():
            return None
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM runs WHERE request_key=?", (key,)).fetchone()
        return {**self._decode(row), "_digest": row["digest"]} if row else None

    def unsettled(self) -> list[dict]:
        if not self.exists():
            return []
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM runs ORDER BY created_at").fetchall()
        return [d for d in (self._decode(r) for r in rows) if d.get("execution") not in SETTLED]

    def for_target(self, session_ref: str) -> list[dict]:
        return [r for r in self.unsettled() if r.get("session_ref") == session_ref]

    def create(self, actor: str, key: str, spec: dict, initial: dict) -> tuple[dict, bool]:
        """Insert one run per user key; the same key with another spec conflicts.

        Returns ``(record, created)``. A repeated identical request converges
        on the original run; a changed payload raises ``idempotency_conflict``.
        """
        fingerprint = digest(spec)
        try:
            with self.connection(write=True) as conn:
                row = conn.execute("SELECT * FROM runs WHERE request_key=?", (key,)).fetchone()
                if row is not None:
                    if row["digest"] != fingerprint:
                        raise idempotency_conflict(
                            "This idempotency key already identifies a different request.",
                            hint="Reuse the key only for an identical retry; pick a new key for new work.",
                        )
                    return self._decode(row), False
                run_id = "run_" + uuid.uuid4().hex
                now = time.time()
                data = {
                    "run_id": run_id,
                    "revision": 1,
                    "actor": actor,
                    "created_at": now,
                    "updated_at": now,
                    **initial,
                }
                conn.execute(
                    "INSERT INTO runs VALUES(?,?,?,?,?,?,?)",
                    (
                        run_id,
                        actor,
                        key,
                        fingerprint,
                        json.dumps(spec, ensure_ascii=False),
                        json.dumps(data, ensure_ascii=False),
                        now,
                    ),
                )
                self._audit(conn, actor, "run.create", run_id)
                self._append(
                    conn,
                    run_id,
                    "state:1",
                    {"kind": "state", "delivery": data.get("delivery"), "execution": data.get("execution")},
                )
        except sqlite3.IntegrityError:
            # Cross-process race: the loser re-reads the winner and applies the
            # same immutable-payload rule instead of inventing a second run.
            with self.connection() as conn:
                row = conn.execute("SELECT * FROM runs WHERE request_key=?", (key,)).fetchone()
            if row is None:  # pragma: no cover - defensive; constraint names the conflict
                raise conflict("Concurrent creation collided; retry the identical request.") from None
            if row["digest"] != fingerprint:
                raise idempotency_conflict(
                    "This idempotency key already identifies a different request.",
                    hint="Reuse the key only for an identical retry; pick a new key for new work.",
                ) from None
            return self._decode(row), False
        data["version"] = run_version(data)
        return data, True

    def update(
        self,
        run_id: str,
        changes: dict,
        *,
        actor: str | None = None,
        expected: str | None = None,
        allow_settled: bool = False,
        settled_messages: set[str] | None = None,
    ) -> dict:
        supervisor = self._actor(actor)
        with self.connection(write=True) as conn:
            data = json.loads(self._row(conn, run_id)["data"])
            if expected is not None and run_version(data) != expected:
                raise conflict("The run changed; read its current version before writing.")
            if data.get("execution") in TERMINAL and not allow_settled:
                raise ControlError(
                    "terminal_run",
                    "A finished run is immutable; submit a new run instead.",
                    EXIT_CONFLICT,
                )
            if settled_messages is not None and changes.get("execution") in TERMINAL:
                messages = conn.execute("SELECT id, body, state FROM messages WHERE run_id=?", (run_id,)).fetchall()
                for message in messages:
                    if message["state"] in {"queued", "dispatching", "unknown"} or (
                        json.loads(message["body"])["action"] == "steer"
                        and message["state"] != "rejected"
                        and message["id"] not in settled_messages
                    ):
                        return {**data, "version": run_version(data)}
            changed = {k: v for k, v in changes.items() if data.get(k) != v}
            if not changed:
                return {**data, "version": run_version(data)}
            data.update(changed)
            data["revision"] += 1
            data["updated_at"] = time.time()
            data["version"] = run_version(data)
            conn.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), run_id))
            self._audit(conn, supervisor, "run.update", run_id)
            visible = {
                k: v
                for k, v in changed.items()
                if k in {"delivery", "execution", "error", "session_ref", "attribution", "observed_model"}
            }
            if "questions" in changed:
                visible["questions"] = len(changed["questions"] or [])
            if visible:
                self._append(conn, run_id, f"state:{data['revision']}", {"kind": "state", **visible})
        data["version"] = run_version(data)
        return data

    # -- events ---------------------------------------------------------------

    def append(self, run_id: str, identity: str, body: dict) -> bool:
        with self.connection(write=True) as conn:
            self._row(conn, run_id)
            added = self._append(conn, run_id, identity, body)
            if added:
                self._supervisor_audit(conn, "event.append", run_id)
        return added

    def events(self, run_id: str, after: int = 0, limit: int = 50) -> dict:
        with self.connection() as conn:
            self._row(conn, run_id)
            rows = conn.execute(
                "SELECT seq, body FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                (run_id, after, limit + 1),
            ).fetchall()
        page = rows[:limit]
        return {
            "events": [{"seq": r["seq"], **json.loads(r["body"])} for r in page],
            "next_cursor": page[-1]["seq"] if page else after,
            "has_more": len(rows) > limit,
        }

    # -- follow-up messages (steer / answer / interrupt) ----------------------

    def enqueue(self, run_id: str, actor: str, key: str, body: dict, expected: str) -> tuple[dict, bool]:
        fingerprint = digest(body)
        try:
            with self.connection(write=True) as conn:
                row = self._row(conn, run_id)
                old = conn.execute("SELECT * FROM messages WHERE run_id=? AND request_key=?", (run_id, key)).fetchone()
                if old is not None:
                    if old["digest"] != fingerprint:
                        raise idempotency_conflict("This message key was used for different input.")
                    return self._message(old), False
                data = json.loads(row["data"])
                if run_version(data) != expected:
                    raise conflict("The run changed; read its current version before writing.")
                if data.get("execution") in SETTLED:
                    raise ControlError(
                        "terminal_run",
                        "This run has settled; submit a new run on the same session.",
                        EXIT_CONFLICT,
                    )
                ident = "msg_" + uuid.uuid4().hex[:16]
                conn.execute(
                    "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        ident,
                        run_id,
                        actor,
                        key,
                        fingerprint,
                        json.dumps(body, ensure_ascii=False),
                        "queued",
                        None,
                        time.time(),
                    ),
                )
                self._audit(conn, actor, f"message.{body['action']}", ident)
                self._append(
                    conn,
                    run_id,
                    ident,
                    {"kind": "message", "message_id": ident, "action": body["action"], "state": "queued"},
                )
                new = conn.execute("SELECT * FROM messages WHERE id=?", (ident,)).fetchone()
        except sqlite3.IntegrityError:
            with self.connection() as conn:
                old = conn.execute("SELECT * FROM messages WHERE run_id=? AND request_key=?", (run_id, key)).fetchone()
            if old is None:  # pragma: no cover - defensive
                raise conflict("Concurrent message enqueue collided; retry the identical request.") from None
            if old["digest"] != fingerprint:
                raise idempotency_conflict("This message key was used for different input.") from None
            return self._message(old), False
        return self._message(new), True

    @staticmethod
    def _message(row: sqlite3.Row) -> dict:
        return {
            "message_id": row["id"],
            "run_id": row["run_id"],
            "action": json.loads(row["body"])["action"],
            "state": row["state"],
            "detail": row["detail"],
            "created_at": row["created_at"],
        }

    def pending_messages(self, run_id: str) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE run_id=? AND state='queued' ORDER BY created_at", (run_id,)
            ).fetchall()
        return [{"id": r["id"], "body": json.loads(r["body"])} for r in rows]

    def messages(self, run_id: str) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM messages WHERE run_id=? ORDER BY created_at", (run_id,)).fetchall()
        return [self._message(r) for r in rows]

    def message_state(self, ident: str, state: str, detail: str | None = None) -> None:
        with self.connection(write=True) as conn:
            row = conn.execute("SELECT run_id FROM messages WHERE id=?", (ident,)).fetchone()
            conn.execute("UPDATE messages SET state=?, detail=? WHERE id=?", (state, detail, ident))
            self._supervisor_audit(conn, f"message.{state}", ident)
            if row is not None:
                self._append(
                    conn,
                    row["run_id"],
                    f"{ident}:{state}",
                    {"kind": "message", "message_id": ident, "state": state, "detail": detail},
                )

    # -- supervision ----------------------------------------------------------

    def heartbeat(self, run_id: str, *, started: bool = False) -> None:
        now = time.time()
        with self.connection(write=True) as conn:
            if started:
                conn.execute("INSERT OR REPLACE INTO supervisors VALUES(?,?,?,?)", (run_id, os.getpid(), now, now))
                self._supervisor_audit(conn, "supervisor.start", run_id)
            else:
                conn.execute(
                    "UPDATE supervisors SET heartbeat_at=? WHERE run_id=? AND pid=?", (now, run_id, os.getpid())
                )

    def active_hosts(self) -> set[str] | None:
        """Hosted terminals with unsettled runs; ``None`` when the store is unreadable."""
        if not self.exists():
            return set()
        try:
            with self.connection() as conn:
                rows = conn.execute("SELECT data FROM runs").fetchall()
        except (sqlite3.Error, ControlError):
            return None
        names = set()
        for row in rows:
            data = json.loads(row["data"])
            if data.get("execution") not in SETTLED and data.get("host"):
                names.add(str(data["host"]))
        return names

    def audit_trail(self, target: str | None = None, limit: int = 100) -> list[dict]:
        """Read-only audit inspection; never creates storage."""
        with self.connection() as conn:
            if target is None:
                rows = conn.execute(
                    "SELECT seq, actor, action, target, timestamp FROM audit ORDER BY seq DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT seq, actor, action, target, timestamp FROM audit WHERE target=? ORDER BY seq DESC LIMIT ?",
                    (target, limit),
                ).fetchall()
        return [dict(r) for r in rows]


__all__ = [
    "DEFAULT_NAMESPACE",
    "DEFAULT_SUPERVISOR",
    "MAX_ACTIVE_RUNS",
    "SCHEMA_VERSION",
    "SETTLED",
    "TERMINAL",
    "RunStore",
    "default_home",
    "digest",
    "run_version",
]

"""Production controller over owned tmux hosting and SessKit observation.

:class:`LocalController` implements the ``SessionController`` port declared in
the shared contract (``docs/CONTRACT.md``) and in foundation ``models.py``, so
the ACP adapter can code and test against the port independently:

- constructor is side-effect free: no directories, processes or storage are
  created until the first operation needs them;
- ``root``/``namespace`` select one authoritative operation store plus
  process-owned target/input locks shared by every SDK/CLI process on the
  same pair; the store file itself is namespaced by the foundation store, and
  the session registry follows the same scheme;
- exact native identity is persisted (never guessed from cwd, newest file or
  PID); binding resolves through exact preassigned IDs or host-claimed
  identity only; resume fails rather than starting a blank replacement;
- ``start_turn`` returns after durable acceptance; supervision continues in a
  detached process (or an in-process thread when ``spawn`` is disabled) so
  accepted work survives the initiating CLI process exit;
- queue versus true steering stays distinct: backends without true steering
  reject ``on_busy="steer"`` instead of disguising a queued follow-up;
- cancellation request, confirmed interruption, process close and detach are
  distinct operations.

Launch plans, capability descriptions and prompt validation build on the
foundation ``runtime``/``models``/``correlation`` modules; unknown runtimes
are never guessed from the built-ins. A host may inject its own registry (or
duck-typed profiles) instead.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_session_control.errors import ControlError
from agent_session_control.history import DEFAULT_NAMESPACE as WIRE_NAMESPACE
from agent_session_control.history import HistoryObserver
from agent_session_control.models import ControlEvent, RunResult, SessionHandle
from agent_session_control.runtime import RuntimeProfile, default_registry
from agent_session_control.store import RunStore
from agent_session_control.supervisor import SupervisorHooks, run_supervisor
from agent_session_control.terminal import TmuxBackend

DEFAULT_HOOKS_FACTORY = "agent_session_control.supervisor:default_hooks"
DEFAULT_STORE_FACTORY = "agent_session_control.supervisor:default_store"

SESSIONS_DB_DEFAULT = "sessions.sqlite3"
LIST_PAGE_SIZE = 50
RESUME_LOCK_TIMEOUT = 30.0

TEXT_BLOCK = "text"
RESOURCE_LINK_BLOCK = "resource_link"
LEGACY_RESOURCE_BLOCK = "resource"
MAX_PROMPT_CHARS = 100_000

# In-process lifecycle mutex: resume, close and detach serialize here, and
# cross-process they serialize on the "lifecycle:<session>" file lock. Never
# held across model waits or observation loops — only across check-act-update.
_LIFECYCLE_MUTEX = threading.Lock()
LIFECYCLE_LOCK_TIMEOUT = 30.0

_SESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
    id TEXT PRIMARY KEY, runtime TEXT NOT NULL, cwd TEXT NOT NULL,
    native_id TEXT, instance_id TEXT, pane TEXT, title TEXT, history_path TEXT,
    identity_evidence TEXT, status TEXT NOT NULL, actor TEXT NOT NULL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit(
    seq INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
    target TEXT NOT NULL, timestamp REAL NOT NULL);
"""


def default_root() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "agent-session-control" / "control"


def sessions_filename(namespace: str) -> str:
    if namespace == "default":
        return SESSIONS_DB_DEFAULT
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in namespace) or "default"
    return f"sessions.{safe}.sqlite3"


def session_row_to_binding(row: dict) -> dict:
    """Run-record/supervisor view of a persisted session row."""
    return {
        "session_ref": row["id"],
        "runtime": row.get("runtime", ""),
        "pane": row.get("pane"),
        "native_id": row.get("native_id"),
        "history_path": row.get("history_path"),
        "cwd": row.get("cwd", ""),
    }


def _mcp_fingerprint(mcp_servers: Any) -> dict:
    """Idempotency-relevant MCP summary with credential isolation.

    The digest covers the full server payloads so different configurations
    conflict instead of converging; the persisted summary keeps only names
    (never values), so no credential survives in the operation store.
    """
    servers = list(mcp_servers or ())
    if not servers:
        return {"names": [], "digest": hashlib.sha256(b"[]").hexdigest()}
    names = []
    for server in servers:
        if isinstance(server, dict):
            names.append(str(server.get("name") or server.get("uri") or server.get("command") or "?"))
        else:
            names.append(str(server))
    canonical = json.dumps(servers, sort_keys=True, ensure_ascii=False, default=str)
    return {"names": sorted(names), "digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}


@dataclass
class HostHooks:
    """Host-injectable preparation around terminal hosting.

    - ``prepare_launch(plan, runtime, ident)``: managed identity prep (plugin
      install, proxy wrapping, per-session env). Returns ``(plan, env)``.
      Default passes the plan through unchanged.
    - ``restore_composer(pane, runtime)``: optional composer-navigation
      restore before typing (host-owned terminal navigation). Default no-op.
    - ``session_title(spec)``: human title for a fresh hosted session.
    - ``claim_native(session_ref, runtime, pane, native_hint)``: proven
      managed process/history claim for runtimes without a preassignable
      native id. Returns ``{"native_id": ..., "history_path": ...,
      "evidence": ...}`` or None. The path must exist; anything else leaves
      the session unbound (honest unknown, never a guess).
    All four are plain callbacks: they only run in-process. Detached
    supervision with custom callbacks requires an explicit serializable
    ``supervisor_factory``; see ``LocalController``.
    """

    prepare_launch: Any = None
    restore_composer: Any = None
    session_title: Any = None
    claim_native: Any = None


class _SessionRegistry:
    """Engine-owned durable session bindings with same-transaction audit."""

    def __init__(self, root: Path, namespace: str = "default"):
        self.root = root
        self.path = root / sessions_filename(namespace)

    def _connect(self, write: bool):
        if write:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.path.is_symlink():
                raise ControlError("unsafe_storage", "the session registry must not be a symlink")
            conn = sqlite3.connect(self.path, timeout=15)
            os.chmod(self.path, 0o600)
        else:
            if not self.path.is_file():
                raise ControlError("not_found", "no controlled sessions exist yet")
            conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure(self, conn: sqlite3.Connection) -> None:
        conn.executescript(_SESSIONS_SCHEMA)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        if "identity_evidence" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN identity_evidence TEXT")
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        if current > 1:
            raise ControlError("store_too_new", "session registry was written by a newer version")
        if current < 1:
            conn.execute("PRAGMA user_version=1")

    def put(self, row: dict, *, actor: str, action: str) -> dict:
        now = time.time()
        row = {**row, "updated_at": now}
        conn = self._connect(write=True)
        try:
            self._ensure(conn)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR REPLACE INTO sessions(id,runtime,cwd,native_id,instance_id,pane,title,"
                "history_path,identity_evidence,status,actor,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["id"],
                    row["runtime"],
                    row["cwd"],
                    row.get("native_id"),
                    row.get("instance_id"),
                    row.get("pane"),
                    row.get("title"),
                    row.get("history_path"),
                    row.get("identity_evidence"),
                    row.get("status", "hosted"),
                    actor,
                    row.get("created_at", now),
                    row["updated_at"],
                ),
            )
            conn.execute(
                "INSERT INTO audit(actor,action,target,timestamp) VALUES(?,?,?,?)", (actor, action, row["id"], now)
            )
            conn.commit()
        finally:
            conn.close()
        return row

    def get(self, session_id: str) -> dict:
        conn = self._connect(write=False)
        try:
            row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise ControlError("not_found", f"unknown controlled session: {session_id}")
        return dict(row)

    def find_by_native(self, runtime: str, native_id: str) -> dict | None:
        if not self.path.is_file():
            return None
        conn = self._connect(write=False)
        try:
            row = conn.execute(
                "SELECT * FROM sessions WHERE runtime=? AND native_id=?", (runtime, native_id)
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row is not None else None

    def list(self, *, cwd: str | None = None) -> list[dict]:
        if not self.path.is_file():
            return []
        conn = self._connect(write=False)
        try:
            if cwd is None:
                rows = conn.execute("SELECT * FROM sessions ORDER BY created_at").fetchall()
            else:
                rows = conn.execute("SELECT * FROM sessions WHERE cwd=? ORDER BY created_at", (cwd,)).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]


def _validate_cwd(cwd: str | None, *, what: str = "cwd") -> str | None:
    """Absolute, existing project path without resolving symlinks.

    Symlinks are preserved lexically (``abspath``, not ``realpath``) so the
    stored path always equals the caller's path; alias-tolerant comparisons
    resolve both sides explicitly where they compare.
    """
    if cwd is None:
        return None
    if not isinstance(cwd, str):
        raise ControlError("usage_error", f"{what} must be an absolute project path.")
    expanded = os.path.expanduser(cwd)
    if not os.path.isabs(expanded):
        raise ControlError("usage_error", f"{what} must be an absolute project path.")
    absolute = os.path.abspath(expanded)
    if not os.path.isdir(absolute):
        raise ControlError("invalid_directory", f"the project directory does not exist: {absolute}")
    return absolute


def _marker_run_for_session(text: Any, namespaces: tuple[str, ...], session_id: str, run_store: Any) -> str | None:
    """Return the marked run id when it belongs to this session, else None.

    A marker alone is not enough: the run id inside it is resolved through
    the operation store, and only a run whose ``session_ref`` is this session
    counts. A sibling session's marker never binds here.
    """
    import re

    if not isinstance(text, str):
        return None
    for namespace in namespaces:
        match = re.search(r"\[" + re.escape(namespace) + r" run=(run_[0-9a-f]+) message=([\w-]+)\]", text)
        if not match:
            continue
        try:
            record = run_store.get(match.group(1))
        except Exception:
            continue
        if record.get("session_ref") == session_id:
            return match.group(1)
    return None


def _bind_by_marker_evidence(
    *,
    row: dict,
    marker_namespace: str,
    legacy_namespaces: tuple[str, ...],
    run_store: Any,
    limit: int = 10,
) -> dict | None:
    """Bind by exact wire-marker evidence in a cwd-matching candidate.

    The cwd only bounds the search; the proof is a run-unique marker whose
    run belongs to this session (resolved through the operation store).
    Newest candidates first, bounded count, unreadable candidates skipped.
    Never binds on recency, title or mtime alone.
    """
    runtime = row.get("runtime", "")
    try:
        from sesskit.registry import default_registry, load_session_conversation  # pyrefly: ignore[missing-import]
    except ImportError:
        return None
    try:
        parser = default_registry().get(runtime)
    except Exception:
        return None
    try:
        sessions = parser.scan_sessions(limit, keep_ids=None)
    except Exception:
        return None
    namespaces = (marker_namespace, *legacy_namespaces)

    def _mtime(session: Any) -> float:
        try:
            value = session.get("mtime", 0) if isinstance(session, dict) else getattr(session, "mtime", 0)
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    candidates = []
    for session in sessions:
        mapping = dict(session) if isinstance(session, dict) else None
        if mapping is None:
            continue
        if not _same_path(mapping.get("cwd"), row.get("cwd")):
            continue
        candidates.append(mapping)
    candidates.sort(key=_mtime, reverse=True)
    for candidate in candidates[:limit]:
        try:
            conversation = load_session_conversation(dict(candidate))
        except Exception:
            continue
        for message in conversation:
            if getattr(message, "role", None) != "user":
                continue
            run_id = _marker_run_for_session(getattr(message, "text", None), namespaces, row["id"], run_store)
            if run_id is None:
                continue
            native_id = candidate.get("id") or row.get("native_id")
            if not native_id or not candidate.get("path"):
                continue
            return {
                **row,
                "native_id": native_id,
                "history_path": str(candidate["path"]),
                "title": candidate.get("native_title") or row.get("title"),
                "identity_evidence": "marker",
            }
    return None


def bind_session_row(
    *,
    sessions: _SessionRegistry,
    observer: Any,
    run_store: Any,
    host_hooks: HostHooks,
    marker_namespace: str,
    legacy_namespaces: tuple[str, ...],
    row: dict,
    actor: str,
) -> dict | None:
    """Single shared native-binding implementation for all supervisor paths.

    Order: exact preassigned-id scan, wire-marker evidence, host claim hook.
    Persists the binding to the session registry on success. Returns the
    updated binding or None when nothing is provable.
    """
    if row.get("history_path"):
        return session_row_to_binding(row)
    native_id = row.get("native_id")
    if native_id:
        try:
            found = observer.find_exact(row["runtime"], native_id)
        except Exception:
            found = None
        if found and found.get("path"):
            updated = {
                **row,
                "history_path": str(found["path"]),
                "title": found.get("native_title") or row.get("title"),
                "identity_evidence": "preassigned",
            }
            sessions.put(updated, actor=actor, action="session.bound")
            return session_row_to_binding(updated)
    claimed = _bind_by_marker_evidence(
        row=row,
        marker_namespace=marker_namespace,
        legacy_namespaces=legacy_namespaces,
        run_store=run_store,
    )
    if claimed is not None:
        sessions.put(claimed, actor=actor, action="session.marker_bound")
        return session_row_to_binding(claimed)
    claim = host_hooks.claim_native if host_hooks is not None else None
    if claim is not None:
        try:
            result = claim(row["id"], row["runtime"], row.get("pane"), native_id)
        except Exception:
            result = None
        if isinstance(result, dict) and result.get("history_path"):
            if not os.path.isfile(str(result["history_path"])):
                return None
            claimed_id = result.get("native_id") or native_id
            if not claimed_id:
                return None
            updated = {
                **row,
                "native_id": claimed_id,
                "history_path": str(result["history_path"]),
                "identity_evidence": str(result.get("evidence") or "claimed"),
            }
            sessions.put(updated, actor=actor, action="session.claimed")
            return session_row_to_binding(updated)
    return None


def _same_path(first: str | None, second: str | None) -> bool:
    if not first or not second:
        return first == second
    try:
        return os.path.realpath(first) == os.path.realpath(second)
    except OSError:
        return os.path.abspath(first) == os.path.abspath(second)


class LocalController:
    """Production ``SessionController`` over an owned tmux backend.

    ``runtime_profiles`` is a foundation ``RuntimeRegistry``, a plain mapping
    of runtime id to profile, or None for the built-in registry. ``terminal``
    is a ``TmuxBackend`` (or a compatible fake in tests). ``store`` is a
    foundation ``RunStore`` (or a compatible fake). ``approval_policy`` is an
    explicit host policy mapping, e.g. ``{"auto_approve": True}``; absence
    preserves runtime defaults. ``legacy_namespaces`` lists additional
    accepted wire namespaces for reading (a compatibility host injects its
    legacy namespace here; generic code never hardcodes one).

    Detached execution: ``spawn=True`` (default) supervises each accepted run
    in a separate process built by ``supervisor_factory``. Injected ports
    (custom terminal/observer/profiles/store/host callbacks) are plain
    in-process objects and cannot cross that boundary: with the default
    factory they are rejected before acceptance. Pass an explicit
    serializable ``supervisor_factory`` (dotted ``module:attr`` taking
    ``(root, namespace, run_id=None)``) to supervise custom ports detached,
    or use ``spawn=False`` for in-process supervision.
    """

    def __init__(
        self,
        *,
        root: Path | str | None = None,
        namespace: str = "default",
        runtime_profiles: Any = None,
        terminal: Any = None,
        observer: Any = None,
        store: Any = None,
        approval_policy: dict | None = None,
        host_hooks: HostHooks | None = None,
        marker_namespace: str | None = None,
        legacy_namespaces: tuple[str, ...] = (),
        spawn: bool = True,
        supervisor_factory: str = DEFAULT_HOOKS_FACTORY,
        supervisor_store_factory: str = DEFAULT_STORE_FACTORY,
    ):
        self.root = Path(root) if root is not None else default_root()
        self.namespace = namespace or "default"
        self._profiles = runtime_profiles
        self._terminal = terminal
        self._observer = observer
        self._store = store
        # Lazily created defaults must not count as custom injections: only
        # explicitly passed ports need a serializable factory to detach.
        self._injected = {
            "terminal": terminal is not None,
            "observer": observer is not None,
            "runtime_profiles": runtime_profiles is not None,
            "store": store is not None,
        }
        self.approval_policy = dict(approval_policy or {})
        self.host_hooks = host_hooks or HostHooks()
        self.marker_namespace = marker_namespace or WIRE_NAMESPACE
        self.legacy_namespaces = tuple(legacy_namespaces or ())
        self.spawn = spawn
        self.supervisor_factory = supervisor_factory
        self.supervisor_store_factory = supervisor_store_factory
        self._threads: list[threading.Thread] = []
        self._children: dict[str, subprocess.Popen] = {}
        self._children_guard = threading.Lock()

    def __del__(self):  # pragma: no cover - best effort at interpreter teardown
        try:
            self.reap_children()
        except Exception:
            pass

    # -- lazy dependencies ------------------------------------------------

    @property
    def sessions(self) -> _SessionRegistry:
        return _SessionRegistry(self.root, self.namespace)

    @property
    def profiles(self) -> Any:
        if self._profiles is None:
            self._profiles = default_registry()
        return self._profiles

    def _profile(self, runtime: str) -> Any:
        profiles = self.profiles
        get = getattr(profiles, "get", None)
        if callable(get):
            try:
                profile = get(runtime)
            except Exception as exc:
                raise ControlError(
                    getattr(exc, "code", "unsupported_runtime"),
                    getattr(exc, "message", str(exc)) or f"unsupported runtime: {runtime}",
                ) from exc
            if profile is None:
                raise ControlError("unsupported_runtime", f"unsupported runtime: {runtime}")
            return profile
        try:
            return profiles[runtime]
        except KeyError as exc:
            raise ControlError("unsupported_runtime", f"unsupported runtime: {runtime}") from exc

    @property
    def backend(self) -> Any:
        if self._terminal is None:
            self._terminal = TmuxBackend(self.namespace, root=str(self.root))
        return self._terminal

    @property
    def history(self) -> Any:
        if self._observer is None:
            self._observer = HistoryObserver(namespaces=(self.marker_namespace, *self.legacy_namespaces))
        return self._observer

    @property
    def run_store(self) -> Any:
        if self._store is None:
            self._store = RunStore(self.root, namespace=self.namespace)
        return self._store

    def _registry_for_hooks(self) -> Any:
        profiles = self.profiles
        if isinstance(profiles, RuntimeProfile):
            return profiles
        if type(profiles).__name__ == "RuntimeRegistry":
            return profiles
        return None

    # -- plan / capability helpers ----------------------------------------------

    def _build_create(self, profile: Any, cwd: str, *, model: str | None, mcp_servers: tuple) -> Any:
        from agent_session_control.runtime import build_create_plan

        if isinstance(profile, RuntimeProfile):
            return build_create_plan(
                profile,
                cwd,
                model=model,
                auto_approve=bool(self.approval_policy.get("auto_approve", False)),
                mcp_servers=mcp_servers,
                registry=self._registry_for_hooks(),
            )
        build = getattr(profile, "build_create_plan", None)
        if build is None:  # pragma: no cover - profile contract gap
            raise ControlError("unsupported_runtime", "this profile cannot build a create plan")
        return build(
            cwd,
            model=model,
            auto_approve=bool(self.approval_policy.get("auto_approve", False)),
            mcp_servers=mcp_servers,
        )

    def _build_resume(self, profile: Any, session_map: dict, *, mcp_servers: tuple) -> Any:
        from agent_session_control.runtime import build_resume_plan

        if isinstance(profile, RuntimeProfile):
            return build_resume_plan(
                profile,
                session_map,
                auto_approve=bool(self.approval_policy.get("auto_approve", False)),
                mcp_servers=mcp_servers,
                registry=self._registry_for_hooks(),
            )
        build = getattr(profile, "build_resume_plan", None)
        if build is None:  # pragma: no cover - profile contract gap
            raise ControlError("unsupported_runtime", "this profile cannot build a resume plan")
        return build(
            session_map, auto_approve=bool(self.approval_policy.get("auto_approve", False)), mcp_servers=mcp_servers
        )

    def _interrupt_keys(self, profile: Any) -> tuple:
        return tuple(getattr(profile, "interrupt_keys", ("Escape",)) or ("Escape",))

    def _composer_fn(self, profile: Any, runtime_id: str) -> Any:
        region = getattr(profile, "composer_region", None)
        if callable(region):
            return region
        if isinstance(profile, RuntimeProfile):
            from agent_session_control.runtime import composer_region

            return functools.partial(composer_region, runtime_id)
        return None

    def _store_error(self, exc: Exception, action: str) -> ControlError:
        code = getattr(exc, "code", None)
        if isinstance(exc, ControlError):
            return exc
        if code in ("idempotency_conflict", "conflict", "not_found", "terminal_run"):
            return ControlError(code, getattr(exc, "message", str(exc)) or action)
        return ControlError("store_error", f"{action} failed: {exc}")

    @staticmethod
    def _check_blocks(prompt: list[dict]) -> tuple[str, list[dict]]:
        """Validate canonical prompt blocks; returns (wire brief, normalized).

        Canonical types are ``text`` and ``resource_link`` (uri plus optional
        name/title/mimeType). The legacy neutral ``resource`` alias means a
        URI link only and is normalized to ``resource_link`` without dropping
        metadata. Anything richer is explicitly rejected. The wire rendering
        keeps every link's metadata on an explicit line.
        """
        if not isinstance(prompt, list) or not prompt:
            raise ControlError("usage_error", "a turn needs a nonempty list of content blocks")
        normalized: list[dict] = []
        for block in prompt:
            if not isinstance(block, dict):
                raise ControlError("usage_error", "each prompt block must be an object")
            kind = block.get("type", TEXT_BLOCK)
            if kind == TEXT_BLOCK:
                if not isinstance(block.get("text"), str):
                    raise ControlError("usage_error", "a text block needs a string 'text' field")
                normalized.append({"type": TEXT_BLOCK, "text": block["text"]})
            elif kind in (RESOURCE_LINK_BLOCK, LEGACY_RESOURCE_BLOCK):
                uri = block.get("uri")
                if not uri or not isinstance(uri, str):
                    raise ControlError("usage_error", "a resource link needs a string 'uri' field")
                item: dict = {"type": RESOURCE_LINK_BLOCK, "uri": uri}
                for field in ("name", "title", "mimeType"):
                    if block.get(field) is not None:
                        item[field] = block[field]
                normalized.append(item)
            else:
                raise ControlError(
                    "unsupported_content",
                    f"rich content type {kind!r} is not supported by this backend",
                    hint="send plain text or resource_link blocks",
                )
        parts: list[str] = []
        for block in normalized:
            if block["type"] == TEXT_BLOCK:
                if block["text"]:
                    parts.append(block["text"])
            else:
                label = block.get("title") or block.get("name") or block["uri"]
                detail = f'title="{label}" uri="{block["uri"]}"'
                if block.get("mimeType"):
                    detail += f' mimeType="{block["mimeType"]}"'
                parts.append(f"[resource_link {detail}]")
        brief = "\n\n".join(parts).strip()
        if not brief:
            raise ControlError("usage_error", "the prompt is empty")
        if "\x00" in brief:
            raise ControlError("usage_error", "the prompt contains a NUL byte")
        if len(brief) > MAX_PROMPT_CHARS:
            raise ControlError("usage_error", f"the prompt exceeds {MAX_PROMPT_CHARS} characters")
        return brief, normalized

    # -- port: capabilities ------------------------------------------------------

    def capabilities(self, runtime: str) -> dict:
        profile = self._profile(runtime)
        if isinstance(profile, RuntimeProfile):
            from agent_session_control.runtime import runtime_profile

            info = runtime_profile(profile)
            interrupt_keys = [str(k) for k in info.get("interrupt_keys", ())]
            resume_requires = "exact native history path" if runtime == "pi" else "exact native session id"
            return {
                "runtime": runtime,
                "queue": True,
                "steer": False,
                "interrupt": bool(interrupt_keys),
                "interrupt_keys": interrupt_keys,
                "model_selection": bool(info.get("model_selection", False)),
                "mcp": "supported" if info.get("mcp_configuration") else "unsupported",
                "prompt_blocks": [TEXT_BLOCK, RESOURCE_LINK_BLOCK],
                "resume": {"supported": True, "requires": resume_requires},
                "load_history": {"supported": True, "requires": "bound native history"},
                "close": True,
                "list": True,
                "detach": True,
            }
        describe = getattr(profile, "describe", None)
        if callable(describe):
            info = describe()
        else:
            info = {
                "interrupt_keys": list(self._interrupt_keys(profile)),
                "model_selection": bool(getattr(profile, "model_flag", None)),
                "steer": bool(getattr(profile, "can_steer", False)),
                "mcp": getattr(profile, "mcp_support", "unsupported"),
            }
        keys = info.get("interrupt_keys", ())
        return {
            "runtime": runtime,
            "queue": True,
            "steer": bool(info.get("steer", False)),
            "interrupt": bool(keys),
            "interrupt_keys": [str(k) for k in keys] if isinstance(keys, (list, tuple)) else [],
            "model_selection": bool(info.get("model_selection", False)),
            "mcp": info.get("mcp", "unsupported"),
            "prompt_blocks": [TEXT_BLOCK, RESOURCE_LINK_BLOCK],
            "resume": {"supported": True, "requires": "exact native session id"},
            "load_history": {"supported": True, "requires": "bound native history"},
            "close": True,
            "list": True,
            "detach": True,
        }

    # -- native binding ------------------------------------------------------------

    def bind_native_session(self, session_id: str, *, actor: str = "local-user") -> dict | None:
        """Bind the exact native session for a controlled session, if provable.

        Single shared implementation; see :func:`bind_session_row` for the
        exact-id, marker-evidence and host-claim order and guarantees.
        """
        row = self.sessions.get(session_id)
        return bind_session_row(
            sessions=self.sessions,
            observer=self.history,
            run_store=self.run_store,
            host_hooks=self.host_hooks,
            marker_namespace=self.marker_namespace,
            legacy_namespaces=self.legacy_namespaces,
            row=row,
            actor=actor,
        )

    # -- port: create / resume -----------------------------------------------------

    async def create(
        self, runtime: str, cwd: str, *, model: str | None = None, mcp_servers: Any = (), actor: str = "local-user"
    ) -> SessionHandle:
        profile = self._profile(runtime)
        resolved = _validate_cwd(cwd)
        assert resolved is not None
        # No native identity is reserved or fabricated here: every runtime
        # binds through exact evidence (preassigned ids from host claims,
        # wire-marker matches, or host-claimed identity) after first write.
        native_id = None
        title = None
        if self.host_hooks.session_title is not None:
            try:
                title = self.host_hooks.session_title(
                    {"runtime": runtime, "cwd": resolved, "mode": "create", "model": model}
                )
            except Exception:
                title = None
        handle = SessionHandle(
            id=f"ses_{uuid.uuid4().hex[:12]}", runtime=runtime, cwd=resolved, native_id=native_id, instance_id=None
        )
        failed_row = {
            "id": handle.id,
            "runtime": runtime,
            "cwd": handle.cwd,
            "native_id": native_id,
            "instance_id": None,
            "pane": None,
            "title": title,
            "history_path": None,
            "identity_evidence": None,
            "status": "failed",
        }
        self.sessions.put(
            {**failed_row, "status": "creating"},
            actor=actor,
            action="session.create",
        )
        try:
            plan = self._build_create(profile, resolved, model=model, mcp_servers=tuple(mcp_servers or ()))
        except ControlError:
            self.sessions.put(failed_row, actor=actor, action="session.plan_failed")
            raise
        except Exception as exc:
            self.sessions.put(failed_row, actor=actor, action="session.plan_failed")
            raise ControlError(
                getattr(exc, "code", "plan_failed"),
                getattr(exc, "message", str(exc)) or "could not build a launch plan",
            ) from exc
        if self.host_hooks.prepare_launch is not None:
            try:
                plan, extra_env = self.host_hooks.prepare_launch(plan, runtime, handle.id)
            except Exception as exc:
                raise ControlError("identity_prep_failed", f"managed identity prep failed: {exc}") from exc
        else:
            extra_env = {}
        new_ident = self.backend.new_ident() if hasattr(self.backend, "new_ident") else uuid.uuid4().hex[:8]
        try:
            pane = self.backend.host(
                tuple(plan.argv),
                runtime=runtime,
                cwd=handle.cwd,
                env=dict(getattr(plan, "env", {}) or {}) | dict(extra_env or {}),
                ident=new_ident,
            )
        except Exception as exc:
            self.sessions.put(
                {
                    "id": handle.id,
                    "runtime": runtime,
                    "cwd": handle.cwd,
                    "native_id": native_id,
                    "instance_id": None,
                    "pane": None,
                    "title": title,
                    "history_path": None,
                    "identity_evidence": None,
                    "status": "failed",
                },
                actor=actor,
                action="session.host_failed",
            )
            raise ControlError(
                getattr(exc, "code", "launch_failed"), getattr(exc, "message", str(exc)) or "could not host the session"
            ) from exc
        self.sessions.put(
            {
                "id": handle.id,
                "runtime": runtime,
                "cwd": handle.cwd,
                "native_id": native_id,
                "instance_id": None,
                "pane": pane,
                "title": title,
                "history_path": None,
                "identity_evidence": None,
                "status": "hosted",
            },
            actor=actor,
            action="session.hosted",
        )
        return handle

    def _resolve_handle(self, session_id: str) -> SessionHandle:
        try:
            row = self.sessions.get(session_id)
        except ControlError as exc:
            runtime_id, sep, native = session_id.partition(":")
            if not sep or not native:
                raise
            found = self.sessions.find_by_native(runtime_id, native)
            if found is None:
                raise ControlError("not_found", f"unknown controlled session: {session_id}") from exc
            row = found
        return SessionHandle(
            id=row["id"],
            runtime=row["runtime"],
            cwd=row["cwd"],
            native_id=row.get("native_id"),
            instance_id=row.get("instance_id"),
        )

    async def resume(
        self, session_id: str, *, cwd: str | None = None, mcp_servers: Any = (), actor: str = "local-user"
    ) -> SessionHandle:
        handle = self._resolve_handle(session_id)
        override_cwd = _validate_cwd(cwd) if cwd is not None else None
        store_root = str(getattr(self.run_store, "root", self.root))
        deadline = time.monotonic() + RESUME_LOCK_TIMEOUT
        while True:
            from agent_session_control.supervisor import file_lock as _file_lock

            # Non-blocking attempts on every layer: the mutex serializes
            # same-process asyncio/thread lifecycle ops, fcntl serializes
            # processes. The lifecycle lock is shared with close/detach so a
            # concurrent close cannot be undone by a resurrected pane.
            if _LIFECYCLE_MUTEX.acquire(blocking=False):
                try:
                    with _file_lock(store_root, "target:" + handle.id) as owned_target:
                        if owned_target:
                            with _file_lock(store_root, "lifecycle:" + handle.id) as owned_life:
                                if owned_life:
                                    return await self._resume_locked(handle, override_cwd, mcp_servers, actor)
                finally:
                    _LIFECYCLE_MUTEX.release()
            if time.monotonic() >= deadline:
                raise ControlError(
                    "target_locked",
                    "another operation owns this session; retry the resume rather than forking it",
                )
            await asyncio.sleep(0.2)

    async def _resume_locked(
        self, handle: SessionHandle, override_cwd: str | None, mcp_servers: Any, actor: str
    ) -> SessionHandle:
        from agent_session_control.errors import lost_ownership

        row = self.sessions.get(handle.id)
        if row.get("status") == "closed":
            # Re-read under lifecycle ownership: a concurrent close won.
            # Never resurrect a closed session with a fresh pane.
            raise ControlError(
                "conflict",
                "this session is closed; create a new session instead of resuming it",
            )
        profile = self._profile(handle.runtime)
        pane = row.get("pane")
        if pane:
            live = self.backend.liveness(pane)
            if hasattr(self.backend, "live_owned"):
                live_owned = self.backend.live_owned(pane)
            elif hasattr(self.backend, "is_owned"):
                live_owned = live == "alive" and bool(self.backend.is_owned(pane))
            else:
                live_owned = live == "alive"
            if live == "alive" and live_owned:
                # Proven-live owned binding: reuse it, never fork the
                # conversation with a second pane.
                updated = {
                    **row,
                    "cwd": override_cwd or row["cwd"],
                    "pane": pane,
                    "status": "hosted",
                }
                self.sessions.put(updated, actor=actor, action="session.reused")
                return SessionHandle(
                    id=row["id"],
                    runtime=row["runtime"],
                    cwd=updated["cwd"],
                    native_id=row.get("native_id"),
                    instance_id=row.get("instance_id"),
                )
            if live == "unknown":
                raise ControlError(
                    "liveness_unknown",
                    "the hosted pane's liveness is unknown; refusing to fork the conversation",
                )
            if live == "alive":
                # Alive but not verifiably owned: another process owns this
                # conversation now. Recovery would start a competing native
                # process on the same history; refuse as lost ownership.
                raise lost_ownership(
                    "this session is alive outside this controller; refusing to fork its conversation",
                    hint="Adopt it through an explicit host claim instead of resuming blindly.",
                )
            # Dead pane: fall through to native recovery below.
        session_map = {
            "id": handle.native_id or "",
            "path": row.get("history_path"),
            "cwd": override_cwd or row["cwd"],
        }
        try:
            plan = self._build_resume(profile, session_map, mcp_servers=tuple(mcp_servers or ()))
        except ControlError:
            raise
        except Exception as exc:
            raise ControlError(
                getattr(exc, "code", "resume_failed"),
                getattr(exc, "message", str(exc)) or "could not build a resume plan",
            ) from exc
        if self.host_hooks.prepare_launch is not None:
            plan, extra_env = self.host_hooks.prepare_launch(plan, handle.runtime, handle.id)
        else:
            extra_env = {}
        new_ident = self.backend.new_ident() if hasattr(self.backend, "new_ident") else uuid.uuid4().hex[:8]
        try:
            new_pane = self.backend.host(
                tuple(plan.argv),
                runtime=handle.runtime,
                cwd=override_cwd or handle.cwd,
                env=dict(getattr(plan, "env", {}) or {}) | dict(extra_env or {}),
                ident=new_ident,
            )
        except Exception as exc:
            raise ControlError(
                getattr(exc, "code", "resume_failed"),
                getattr(exc, "message", str(exc)) or "could not resume the session",
            ) from exc
        # Preserve the exact native identity, history, cwd and title across
        # recovery; only the pane and status change.
        self.sessions.put(
            {
                "id": handle.id,
                "runtime": handle.runtime,
                "cwd": override_cwd or row["cwd"],
                "native_id": handle.native_id,
                "instance_id": handle.instance_id,
                "pane": new_pane,
                "title": row.get("title"),
                "history_path": row.get("history_path"),
                "identity_evidence": row.get("identity_evidence"),
                "status": "hosted",
            },
            actor=actor,
            action="session.resumed",
        )
        return SessionHandle(
            id=handle.id,
            runtime=handle.runtime,
            cwd=override_cwd or row["cwd"],
            native_id=handle.native_id,
            instance_id=handle.instance_id,
        )

    # -- port: start_turn ---------------------------------------------------------------

    def _require_detachable(self) -> None:
        """Reject detached acceptance for unserializable injected ports.

        In-process objects (custom terminal/observer/profiles/store, host
        callbacks) cannot cross into the supervisor subprocess. With the
        default production factory they are rejected here, before any
        durable acceptance; an explicit serializable ``supervisor_factory``
        takes responsibility for rebuilding them.
        """
        if not self.spawn or self.supervisor_factory != DEFAULT_HOOKS_FACTORY:
            return
        customs = [name for name, injected in self._injected.items() if injected]
        hooks = self.host_hooks
        if (
            hooks.prepare_launch is not None
            or hooks.restore_composer is not None
            or hooks.session_title is not None
            or hooks.claim_native is not None
        ):
            customs.append("host_hooks")
        if customs:
            raise ControlError(
                "unsupported",
                f"detached supervision cannot rebuild custom {', '.join(customs)}; "
                "pass an explicit serializable supervisor_factory or use spawn=False",
            )

    async def start_turn(
        self, session_id: str, prompt: list[dict], *, request_id: str, on_busy: str = "queue", actor: str = "local-user"
    ) -> str:
        if on_busy not in ("queue", "steer", "reject"):
            raise ControlError("usage_error", f"unknown on_busy mode: {on_busy}")
        row = self.sessions.get(session_id)
        if row.get("status") == "closed":
            raise ControlError("not_found", "this session is closed; create or resume a new one")
        brief, _normalized = self._check_blocks(prompt)
        if on_busy == "steer" and not self.capabilities(row["runtime"])["steer"]:
            raise ControlError(
                "steer_unsupported",
                f"runtime {row['runtime']} has no true steering; use on_busy=queue or reject instead",
            )
        self._require_detachable()
        spec = {
            "mode": "submit",
            "runtime": row["runtime"],
            "cwd": row["cwd"],
            "session_ref": row["id"],
            "history_path": row.get("history_path"),
            "prompt": brief,
            "model": None,
            "on_busy": on_busy,
            "queue_timeout": 3600,
            "mcp": _mcp_fingerprint([]),
            "marker_namespace": self.marker_namespace,
            "legacy_namespaces": list(self.legacy_namespaces),
        }
        key = f"{row['id']}:{request_id}"
        initial = {"delivery": "accepted", "execution": "accepted", "session_ref": row["id"], "runtime": row["runtime"]}
        try:
            data, created = self.run_store.create(actor, key, spec, initial)
        except Exception as exc:
            raise self._store_error(exc, "start_turn") from exc
        run_id = data["run_id"]
        if created:
            self._launch_supervisor(run_id, actor)
        else:
            # Converged on an existing run (e.g. an identical retry after a
            # caller/spawn failure): make sure it actually has supervision
            # instead of leaving accepted work orphaned.
            self._ensure_supervision(run_id, actor)
        return run_id

    def _ensure_supervision(self, run_id: str, actor: str) -> str:
        """Reconcile an existing run with live supervision, then return its id.

        Uses actual run-lock ownership: a lock held elsewhere means a live
        supervisor owns the run and nothing more is needed. A free lock means
        the run is orphaned (caller died, spawn failed, crash before
        supervision) and supervision is relaunched. Relaunch never replays
        uncertain writes: the runner itself replays nothing past an accepted
        intent and reconciles dispatching records as unknown.
        """
        from agent_session_control.supervisor import SETTLED_EXECUTION, file_lock

        try:
            record = self.run_store.get(run_id)
        except Exception as exc:
            raise self._store_error(exc, "recover_run") from exc
        if record.get("execution") in SETTLED_EXECUTION:
            return run_id
        root = str(getattr(self.run_store, "root", self.root))
        # Advisory check only: a held lock proves a live owner elsewhere. A
        # free lock lets us launch, and the launched supervisor's own lock
        # acquisition remains the authoritative serialization — never hold
        # this check across the launch itself, or a spawned child process
        # would block behind us and exit without supervising. A duplicate
        # launch from a lost race is benign: exactly one owner wins the run
        # lock and the other exits quietly.
        with file_lock(root, "run:" + run_id) as owned:
            if not owned:
                return run_id
            try:
                fresh = self.run_store.get(run_id)
            except Exception as exc:
                raise self._store_error(exc, "recover_run") from exc
            if fresh.get("execution") in SETTLED_EXECUTION:
                return run_id
        self._launch_supervisor(run_id, actor)
        return run_id

    async def recover_run(self, run_id: str, *, actor: str = "local-user") -> str:
        """Explicitly recover one run: converge supervision without new input.

        Returns the run id once a supervisor owns it (or it is already
        settled). Never submits, retypes or replays uncertain writes.
        """
        return self._ensure_supervision(run_id, actor)

    def reap_children(self) -> list[str]:
        """Poll tracked supervisor processes; drop the reaped ones.

        Returns the run ids that are still alive. Never terminates a child:
        a live supervisor owns its run and must keep it.
        """
        live: list[str] = []
        with self._children_guard:
            for run_id, proc in list(self._children.items()):
                try:
                    finished = proc.poll() is not None
                except Exception:
                    finished = True
                if finished:
                    self._children.pop(run_id, None)
                else:
                    live.append(run_id)
        return live

    async def _join_child(self, run_id: str, *, timeout: float = 10.0) -> None:
        with self._children_guard:
            proc = self._children.get(run_id)
        if proc is None:
            return
        try:
            await asyncio.to_thread(proc.wait, timeout)
        except Exception:
            pass
        finally:
            self.reap_children()

    def _launch_supervisor(self, run_id: str, actor: str) -> None:
        self.reap_children()
        if self.spawn:
            log_dir = self.root / "logs" / self.namespace
            log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            src_dir = str(Path(__file__).resolve().parents[1])
            env = dict(os.environ)
            python_path = src_dir + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            # Test-only extension: extra import roots (e.g. fixture hooks) for the child.
            extra_roots = os.environ.get("ASC_TEST_FIXTURES")
            if extra_roots:
                python_path += os.pathsep + extra_roots
            env["PYTHONPATH"] = python_path
            log_path = log_dir / f"{run_id}.log"
            try:
                log_stream = open(log_path, "ab")
            except OSError:
                log_stream = None
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "agent_session_control.supervisor",
                    run_id,
                    "--root",
                    str(self.root),
                    "--namespace",
                    self.namespace,
                    "--hooks-factory",
                    self.supervisor_factory,
                    "--store-factory",
                    self.supervisor_store_factory,
                ],
                stdout=log_stream if log_stream is not None else subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
                close_fds=True,
            )
            if log_stream is not None:
                log_stream.close()
            with self._children_guard:
                self._children[run_id] = proc
        else:
            hooks = self._inline_hooks()
            thread = threading.Thread(
                target=run_supervisor,
                args=(self.run_store, run_id, hooks),
                kwargs={"marker_namespace": self.marker_namespace},
                daemon=True,
                name=f"asc-supervisor-{run_id[:12]}",
            )
            thread.start()
            self._threads.append(thread)

    def _inline_hooks(self) -> Any:
        """In-process hooks over this controller's backend/observer/profiles."""
        return build_hooks(
            root=str(self.root),
            namespace=self.namespace,
            backend=self.backend,
            observer=self.history,
            registry=self.profiles,
            store=self.run_store,
            host_hooks=self.host_hooks,
            marker_namespace=self.marker_namespace,
            legacy_namespaces=self.legacy_namespaces,
        )

    # -- port: watch / wait ---------------------------------------------------------------

    def watch(self, run_id: str, *, after: int = 0) -> AsyncIterator[ControlEvent]:
        return self._watch_gen(run_id, after)

    async def _watch_gen(self, run_id: str, after: int) -> AsyncIterator[ControlEvent]:
        cursor = after
        while True:
            try:
                page = self.run_store.events(run_id, after=cursor, limit=50)
            except Exception as exc:
                raise self._store_error(exc, "watch") from exc
            for item in page["events"]:
                cursor = item["seq"]
                yield ControlEvent(
                    seq=item["seq"],
                    kind=item.get("kind", "state"),
                    session_id=item.get("session_ref", ""),
                    run_id=run_id,
                    data=item,
                    evidence=item.get("evidence", "unknown"),
                    generation=item.get("generation", ""),
                )
            try:
                record = self.run_store.get(run_id)
            except Exception as exc:
                raise self._store_error(exc, "watch") from exc
            if record.get("execution") in ("completed", "failed", "interrupted", "unknown"):
                if not page.get("has_more"):
                    return
            await asyncio.sleep(0.5)

    async def wait(self, run_id: str, *, timeout: float | None = None) -> RunResult:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                record = self.run_store.get(run_id)
            except Exception as exc:
                raise self._store_error(exc, "wait") from exc
            state = record.get("execution")
            if state in ("completed", "failed", "interrupted", "unknown"):
                result = self._to_result(run_id, record)
                # Give a just-settled detached supervisor a bounded moment to
                # exit so its handle is reaped, not garbage-collected mid-run.
                await self._join_child(run_id)
                return result
            if deadline is not None and time.monotonic() >= deadline:
                # A wait timeout never cancels work; the run continues.
                raise ControlError(
                    "wait_timeout", "the wait deadline passed; the run continues", details={"run_id": run_id}
                )
            await asyncio.sleep(0.5)

    @staticmethod
    def _to_result(run_id: str, record: dict) -> RunResult:
        result = record.get("result") or {}
        return RunResult(
            run_id=run_id,
            state=record.get("execution", "unknown"),
            final_response=result.get("final_response"),
            stop_reason=record.get("stop_reason") or result.get("stop_reason"),
            error=record.get("error"),
            evidence=record.get("finality", "unknown"),
        )

    # -- port: interrupt / close / list / history ----------------------------------------------

    async def interrupt(self, run_id: str, *, actor: str = "local-user") -> None:
        try:
            record = self.run_store.get(run_id)
        except Exception as exc:
            raise self._store_error(exc, "interrupt") from exc
        if record.get("execution") in ("completed", "failed", "interrupted", "unknown"):
            return
        body = {"action": "interrupt"}
        try:
            self.run_store.enqueue(run_id, actor, f"interrupt:{uuid.uuid4().hex[:12]}", body, record.get("version", ""))
        except Exception as exc:
            raise self._store_error(exc, "interrupt") from exc

    async def close_session(self, session_id: str, *, actor: str = "local-user") -> None:
        """Cancel accepted work, release the owned pane, keep native history.

        The binding is released only once the pane is actually gone. A failed
        or uncertain kill keeps the session hosted and recoverable and raises
        instead of reporting a close that did not happen. A run that settled
        naturally while closing keeps its result (terminal runs are never
        overwritten). Close shares the cross-process lifecycle lock with
        resume/detach and re-reads the session under it, so a concurrent
        resume cannot resurrect a pane after close reported it gone.
        """
        row = self.sessions.get(session_id)
        if row.get("status") == "closed":
            return
        from agent_session_control.supervisor import file_lock as _file_lock

        store_root = str(getattr(self.run_store, "root", self.root))
        deadline = time.monotonic() + LIFECYCLE_LOCK_TIMEOUT
        while True:
            if _LIFECYCLE_MUTEX.acquire(blocking=False):
                try:
                    with _file_lock(store_root, "lifecycle:" + session_id) as owned:
                        if owned:
                            return await self._close_locked(session_id, actor)
                finally:
                    _LIFECYCLE_MUTEX.release()
            if time.monotonic() >= deadline:
                raise ControlError(
                    "target_locked",
                    "another lifecycle operation owns this session; retry the close",
                )
            await asyncio.sleep(0.2)

    async def _close_locked(self, session_id: str, actor: str) -> None:
        row = self.sessions.get(session_id)
        if row.get("status") == "closed":
            return
        pane = row.get("pane")
        if pane:
            try:
                stopped = self.backend.close(pane)
            except ControlError:
                raise
            except Exception as exc:
                raise ControlError(
                    getattr(exc, "code", "close_failed"),
                    getattr(exc, "message", str(exc)) or "could not release the owned pane",
                ) from exc
            if not stopped:
                try:
                    live = self.backend.liveness(pane)
                except Exception:
                    live = "unknown"
                if live != "dead":
                    raise ControlError(
                        "close_uncertain",
                        "the owned pane may still be alive; the binding is retained",
                        details={"pane_liveness": live},
                    )
        try:
            unsettled = [r for r in self.run_store.unsettled() if r.get("session_ref") == session_id]
        except Exception:
            unsettled = []
        for run in unsettled:
            if run.get("delivery") == "accepted":
                try:
                    self.run_store.update(
                        run["run_id"],
                        {
                            "delivery": "rejected",
                            "execution": "interrupted",
                            "error": {"code": "cancelled", "message": "session closed before dispatch"},
                        },
                        actor=actor,
                    )
                except ControlError as exc:
                    if exc.code != "terminal_run":
                        raise
                    # Settled naturally while closing: its result stands.
                except Exception as exc:
                    raise self._store_error(exc, "close_session") from exc
            else:
                try:
                    self.run_store.enqueue(
                        run["run_id"],
                        actor,
                        f"interrupt:{uuid.uuid4().hex[:12]}",
                        {"action": "interrupt"},
                        run.get("version", ""),
                    )
                except ControlError as exc:
                    if exc.code != "terminal_run":
                        raise
                except Exception as exc:
                    raise self._store_error(exc, "close_session") from exc
        self.sessions.put({**row, "pane": None, "status": "closed"}, actor=actor, action="session.close")

    async def detach_session(self, session_id: str, *, actor: str = "local-user") -> None:
        """Stop observing without cancelling accepted work; it continues."""
        from agent_session_control.supervisor import file_lock as _file_lock

        store_root = str(getattr(self.run_store, "root", self.root))
        deadline = time.monotonic() + LIFECYCLE_LOCK_TIMEOUT
        while True:
            if _LIFECYCLE_MUTEX.acquire(blocking=False):
                try:
                    with _file_lock(store_root, "lifecycle:" + session_id) as owned:
                        if owned:
                            row = self.sessions.get(session_id)
                            self.sessions.put({**row, "status": "detached"}, actor=actor, action="session.detach")
                            return
                finally:
                    _LIFECYCLE_MUTEX.release()
            if time.monotonic() >= deadline:
                raise ControlError(
                    "target_locked",
                    "another lifecycle operation owns this session; retry the detach",
                )
            await asyncio.sleep(0.2)

    async def list_sessions(self, *, cwd: str | None = None, cursor: str | None = None) -> dict:
        rows = self.sessions.list(cwd=cwd)
        offset = 0
        if cursor is not None:
            try:
                offset = int(cursor)
            except (TypeError, ValueError) as exc:
                raise ControlError(
                    "usage_error", f"unsupported list cursor: {cursor!r}; use the opaque next_cursor value"
                ) from exc
            if offset < 0:
                raise ControlError("usage_error", f"unsupported list cursor: {cursor!r}")
        page = rows[offset : offset + LIST_PAGE_SIZE]
        next_cursor = str(offset + LIST_PAGE_SIZE) if len(rows) > offset + LIST_PAGE_SIZE else None
        items = []
        for row in page:
            pane = row.get("pane")
            live: str | None = None
            if pane:
                try:
                    live = self.backend.liveness(pane)
                except Exception:
                    live = "unknown"
            items.append(
                {
                    "session_id": row["id"],
                    "runtime": row["runtime"],
                    "cwd": row["cwd"],
                    "native_id": row.get("native_id"),
                    "instance_id": row.get("instance_id"),
                    "status": row.get("status"),
                    "pane_liveness": live,
                }
            )
        return {"sessions": items, "next_cursor": next_cursor}

    async def load_history(self, session_id: str) -> list[ControlEvent]:
        row = self.sessions.get(session_id)
        if not row.get("history_path"):
            raise ControlError("history_missing", "this session has no bound native history yet")
        try:
            snapshot = self.history.load_snapshot({"path": row["history_path"], "source": row["runtime"]})
        except Exception as exc:
            raise ControlError(getattr(exc, "code", "history_unavailable"), getattr(exc, "message", str(exc))) from exc
        from agent_session_control.history import compact_event

        out: list[Any] = []
        for index, event in enumerate(snapshot.events):
            body = compact_event(event)
            evidence = getattr(getattr(event, "evidence", None), "origin", "unknown") or "unknown"
            out.append(
                ControlEvent(
                    seq=index,
                    kind=body.get("type", "activity"),
                    session_id=session_id,
                    run_id=None,
                    data=body,
                    evidence=evidence,
                    generation=str(getattr(snapshot, "generation", "")),
                )
            )
        return out


def build_hooks(
    *,
    root: str,
    namespace: str = "default",
    run_id: str | None = None,
    store: Any = None,
    backend: Any = None,
    observer: Any = None,
    registry: Any = None,
    host_hooks: HostHooks | None = None,
    marker_namespace: str | None = None,
    legacy_namespaces: tuple[str, ...] = (),
) -> SupervisorHooks:
    """Build the single shared hook implementation for one root/namespace.

    In-process callers pass their live backend/observer/registry/store; the
    detached supervisor factory calls with only ``(root, namespace, run_id)``
    and everything is reconstructed from persisted state plus explicit
    defaults (owned tmux backend, foundation registry, SessKit observer).
    The run record carries the wire namespaces accepted at ``start_turn`` so
    both paths correlate identically. Custom host callbacks only apply when
    passed explicitly (in-process); detached runs accepted with custom
    callbacks are rejected before acceptance by ``LocalController``.
    """
    from agent_session_control.history import DEFAULT_NAMESPACE

    sessions = _SessionRegistry(Path(root), namespace)
    store = store if store is not None else RunStore(Path(root), namespace=namespace)
    marker = marker_namespace or DEFAULT_NAMESPACE
    legacy = tuple(legacy_namespaces or ())
    if run_id is not None:
        try:
            spec = store.get(run_id, private=True).get("spec", {})
        except Exception:
            spec = {}
        marker = spec.get("marker_namespace") or marker
        legacy = tuple(spec.get("legacy_namespaces") or legacy)
    if backend is None:
        backend = TmuxBackend(namespace, root=root)
    if observer is None:
        observer = HistoryObserver(namespaces=(marker, *legacy))
    if registry is None:
        registry = default_registry()
    hooks_config = host_hooks or HostHooks()

    def resolve_target(session_ref: str) -> dict:
        row = sessions.get(session_ref)
        pane = row.get("pane")
        live = backend.liveness(pane) if pane else "unknown"
        return {
            "id": row.get("native_id") or row["id"],
            "source": row["runtime"],
            "runtime": row["runtime"],
            "cwd": row["cwd"],
            "path": row.get("history_path"),
            "pane": pane,
            "live": live == "alive",
            "native_id": row.get("native_id"),
            "title": row.get("title"),
            "identity_evidence": row.get("identity_evidence"),
        }

    def lookup_profile(runtime_id: str) -> Any:
        get = getattr(registry, "get", None)
        if callable(get):
            return get(runtime_id)
        return registry[runtime_id]

    def prepare(session_ref: str, deadline: float) -> Any:
        from agent_session_control.supervisor import CertainFailure
        from agent_session_control.terminal import blocked_input_cause

        row = sessions.get(session_ref)
        pane = row.get("pane")
        if not pane or backend.liveness(pane) == "dead":
            raise CertainFailure("target_unavailable", "the hosted pane is gone")
        profile = lookup_profile(row["runtime"])
        markers = tuple(getattr(profile, "readiness_markers", ()) or ())
        capture = backend.capture(pane)
        if cause := blocked_input_cause(capture):
            # The pane's keyboard belongs to a dialog (workspace trust,
            # permission prompt, picker): typing into it would answer the
            # dialog instead of reaching the agent. Refuse before any byte.
            raise CertainFailure("startup_dialog", cause)
        backend.wait_ready(pane, markers, timeout=max(0.0, deadline - time.monotonic() - 1.0))
        return {"pane": pane}

    def submit(session_ref: str, text: str) -> None:
        from agent_session_control.supervisor import CertainFailure, UncertainDelivery
        from agent_session_control.terminal import TmuxError, UncertainInjection

        row = sessions.get(session_ref)
        pane = row.get("pane")
        profile = lookup_profile(row["runtime"])
        region = getattr(profile, "composer_region", None)
        if callable(region):
            region_fn = region
        elif isinstance(profile, RuntimeProfile):
            from agent_session_control.runtime import composer_region

            region_fn = functools.partial(composer_region, row["runtime"])
        else:
            region_fn = None
        restore = hooks_config.restore_composer
        if restore is not None:
            try:
                restore(pane, row["runtime"])
            except Exception as exc:
                raise CertainFailure("target_unavailable", f"composer restore failed: {exc}") from exc
        try:
            backend.submit_text(pane, text, composer_region=region_fn)
        except UncertainInjection as exc:
            raise UncertainDelivery(exc.message) from exc
        except TmuxError as exc:
            raise CertainFailure(exc.code, exc.message) from exc

    def verify(session_ref: str, baseline: Any) -> None:
        from agent_session_control.supervisor import UncertainDelivery

        row = sessions.get(session_ref)
        try:
            backend.verify_alive_after_inject(row.get("pane"))
        except Exception as exc:
            raise UncertainDelivery(str(exc)) from exc

    def interrupt(session_ref: str) -> None:
        from agent_session_control.supervisor import CertainFailure, UncertainDelivery
        from agent_session_control.terminal import TmuxError, UncertainInjection

        row = sessions.get(session_ref)
        profile = lookup_profile(row["runtime"])
        keys = tuple(getattr(profile, "interrupt_keys", ("Escape",)) or ("Escape",))
        try:
            backend.interrupt(row.get("pane"), keys)
        except UncertainInjection as exc:
            raise UncertainDelivery(exc.message) from exc
        except TmuxError as exc:
            raise CertainFailure(exc.code, exc.message) from exc

    handles: dict[str, Any] = {}

    def open_observation(binding: dict) -> str:
        """Open one observation handle with one retained SessKit reader.

        The reader (and its cursor) lives in the handle for the whole
        observation: polls are incremental against it, so an unchanged file
        never reports a generation reset. Opening a cold reader per poll
        would mistake every initial materialization for a replacement.
        """
        handle_id = f"obs_{uuid.uuid4().hex[:12]}"
        session = {"path": binding.get("history_path"), "source": binding.get("runtime", "")}
        try:
            reader = observer.open_reader(session)
        except Exception:
            reader = None
        handles[handle_id] = {"binding": dict(binding), "events": {}, "reader": reader}
        return handle_id

    def poll_observation(handle_id: str, poll_run_id: str, base_seq: int) -> tuple[dict, bool, str]:
        """Poll the handle's retained reader; tolerate 3-tuple fakes.

        The documented observer protocol returns ``(observation, reset)``.
        Some harnesses return the hook-shaped ``(observation, reset, state)``;
        both are honored so a shape difference can never silently void a run.
        A missing reader is re-attempted (the history may have appeared
        since open); a still-unreadable history stays unavailable.
        """
        handle = handles[handle_id]
        binding = handle["binding"]
        session = {"path": binding.get("history_path"), "source": binding.get("runtime", "")}
        reader = handle.get("reader")
        if reader is None:
            try:
                reader = observer.open_reader(session)
            except Exception:
                reader = None
            handle["reader"] = reader
            if reader is None:
                return ({"recorded": False}, False, "unavailable")
        polled = observer.observe(reader, poll_run_id, base_seq, handle["events"], binding.get("runtime", ""))
        if isinstance(polled, tuple) and len(polled) == 3:
            observation, reset, _state = polled
        else:
            observation, reset = polled
        if isinstance(observation, dict):
            obs = observation
        else:
            obs = observation.as_dict()
        return (obs, reset, "ready")

    def close_observation(handle_id: str) -> None:
        handle = handles.pop(handle_id, None)
        reader = (handle or {}).get("reader")
        close = getattr(reader, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def pane_of(ref: str) -> Any:
        return sessions.get(ref).get("pane")

    def baseline_of(binding: dict) -> int:
        try:
            value = observer.baseline_seq({"path": binding.get("history_path"), "source": binding.get("runtime", "")})
        except Exception:
            return -1
        return value if isinstance(value, int) else -1

    def bind_native(binding: dict) -> dict | None:
        session_ref = binding.get("session_ref") or ""
        try:
            row = sessions.get(session_ref)
        except Exception:
            return None
        return bind_session_row(
            sessions=sessions,
            observer=observer,
            run_store=store,
            host_hooks=hooks_config,
            marker_namespace=marker,
            legacy_namespaces=legacy,
            row=row,
            actor="supervisor",
        )

    return SupervisorHooks(
        resolve_target=resolve_target,
        prepare=prepare,
        submit=submit,
        verify=verify,
        interrupt=interrupt,
        open_observation=open_observation,
        poll_observation=poll_observation,
        close_observation=close_observation,
        baseline_of=baseline_of,
        bind_native=bind_native,
        is_alive=lambda ref: backend.liveness(pane_of(ref)),
        screen_hash=lambda ref: _hash_screen(backend.capture(pane_of(ref))),
        phase=lambda ref: "unknown",
        close_owned=lambda ref: backend.close(pane_of(ref)),
    )


def _hash_screen(capture: str | None) -> str | None:
    if capture is None:
        return None
    return hashlib.sha1(capture.encode("utf-8", "replace")).hexdigest()

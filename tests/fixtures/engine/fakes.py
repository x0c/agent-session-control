"""Shared fakes for engine tests: file-backed store, panes, scripted history.

- :class:`MemoryStore`: in-process operation store implementing the
  supervisor/controller store protocol (idempotent create, versioned update,
  events, follow-up messages, heartbeats).
- :class:`FileStore`: same protocol backed by one JSON file guarded with
  ``fcntl`` locks, so a test can prove durable work survives the initiating
  process exit and a detached supervisor process can settle it.
- :class:`ScriptBackend`: in-memory pane simulation recording submissions and
  interrupts; never touches tmux.
- :class:`ScriptHistory`: SessKit-free scripted observation queue returning
  plain observation dicts shaped like :func:`history.correlate` output.
- :class:`FileBackend` / :class:`FileHistory`: cross-process pane simulation
  over files under a root, for the detached-supervisor test.
- :class:`EchoProfile`: fixture runtime profile hosting the echo agent.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import sys
import time
import uuid

FIXTURE_DIR = os.path.dirname(os.path.abspath(__file__))
ECHO_AGENT = os.path.join(FIXTURE_DIR, "echo_agent.py")

# Wire markers written by the engine use the foundation correlation default
# namespace ("session-control"); fixtures that parse them take the namespace
# explicitly so the parameter itself stays covered.
MARKER_NAMESPACE = "session-control"


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _run_version(data: dict) -> str:
    basis = {
        "execution": data.get("execution"),
        "delivery": data.get("delivery"),
        "questions": sorted(str(q.get("request_id")) for q in data.get("questions") or []),
    }
    return "v2:" + _digest(basis)[:16]


TERMINAL = frozenset({"completed", "failed", "interrupted"})
SETTLED = TERMINAL | {"unknown"}


class MemoryStore:
    """In-process store; one instance per test."""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(os.path.join(root, "locks"), exist_ok=True)
        self.runs: dict[str, dict] = {}
        self.specs: dict[str, dict] = {}
        self.digests: dict[str, str] = {}
        self.keys: dict[str, str] = {}
        self.event_log: dict[str, list] = {}
        self.event_idents: dict[str, set] = {}
        self._messages: dict[str, dict] = {}
        self.msg_by_key: dict[tuple, str] = {}
        self.beats: dict[str, dict] = {}

    # -- runs --
    def create(self, actor: str, key: str, spec: dict, initial: dict):
        fingerprint = _digest(spec)
        if key in self.keys:
            run_id = self.keys[key]
            if self.digests[key] != fingerprint:
                raise _conflict("idempotency_conflict", "this idempotency key already identifies a different request")
            return copy.deepcopy(self.runs[run_id]), False
        run_id = "run_" + uuid.uuid4().hex
        now = time.time()
        data = {
            "run_id": run_id,
            "revision": 1,
            "actor": actor,
            "created_at": now,
            "updated_at": now,
            **copy.deepcopy(initial),
        }
        data["version"] = _run_version(data)
        self.runs[run_id] = data
        self.specs[run_id] = copy.deepcopy(spec)
        self.digests[key] = fingerprint
        self.keys[key] = run_id
        self.event_log[run_id] = []
        self.event_idents[run_id] = set()
        return copy.deepcopy(data), True

    def get(self, run_id: str, *, private: bool = False) -> dict:
        if run_id not in self.runs:
            raise _conflict("not_found", f"unknown run: {run_id}")
        data = copy.deepcopy(self.runs[run_id])
        data["version"] = _run_version(self.runs[run_id])
        if private:
            data["spec"] = copy.deepcopy(self.specs[run_id])
        return data

    def update(self, run_id: str, changes: dict, **kwargs) -> dict:
        if run_id not in self.runs:
            raise _conflict("not_found", f"unknown run: {run_id}")
        data = self.runs[run_id]
        expected = kwargs.get("expected")
        if expected is not None and _run_version(data) != expected:
            raise _conflict("conflict", "the run changed; read its current version first")
        if data.get("execution") in TERMINAL and not kwargs.get("allow_settled"):
            raise _conflict("terminal_run", "a finished run is immutable")
        settled_messages = kwargs.get("settled_messages")
        if settled_messages is not None and changes.get("execution") in TERMINAL:
            for msg in self._messages.values():
                if msg["run_id"] != run_id:
                    continue
                body = json.loads(msg["body"])
                if msg["state"] in {"queued", "dispatching", "unknown"} or (
                    body.get("action") == "steer" and msg["state"] != "rejected" and msg["id"] not in settled_messages
                ):
                    out = copy.deepcopy(data)
                    out["spec"] = copy.deepcopy(self.specs[run_id])
                    out["version"] = _run_version(data)
                    return out
        changed = {k: v for k, v in changes.items() if data.get(k) != v}
        if changed:
            data.update(copy.deepcopy(changed))
            data["revision"] += 1
            data["updated_at"] = time.time()
        data["version"] = _run_version(data)
        out = copy.deepcopy(data)
        out["spec"] = copy.deepcopy(self.specs[run_id])
        out["version"] = _run_version(data)
        return out

    # -- events --
    def append(self, run_id: str, identity: str, body: dict) -> bool:
        if run_id not in self.runs:
            raise _conflict("not_found", f"unknown run: {run_id}")
        if identity in self.event_idents[run_id]:
            return False
        self.event_idents[run_id].add(identity)
        seq = len(self.event_log[run_id]) + 1
        self.event_log[run_id].append({"seq": seq, **copy.deepcopy(body)})
        return True

    def events(self, run_id: str, after: int = 0, limit: int = 50) -> dict:
        if run_id not in self.runs:
            raise _conflict("not_found", f"unknown run: {run_id}")
        page = [e for e in self.event_log[run_id] if e["seq"] > after][: limit + 1]
        shown = page[:limit]
        return {
            "events": copy.deepcopy(shown),
            "next_cursor": shown[-1]["seq"] if shown else after,
            "has_more": len(page) > limit,
        }

    # -- messages --
    def enqueue(self, run_id: str, actor: str, key: str, body: dict, expected: str):
        if run_id not in self.runs:
            raise _conflict("not_found", f"unknown run: {run_id}")
        if (run_id, key) in self.msg_by_key:
            ident = self.msg_by_key[(run_id, key)]
            existing = self._messages[ident]
            if existing["digest"] != _digest(body):
                raise _conflict("idempotency_conflict", "this message key was used for different input")
            return _public_message(existing), False
        data = self.runs[run_id]
        if _run_version(data) != expected:
            raise _conflict("conflict", "the run changed; read its current version first")
        if data.get("execution") in SETTLED:
            raise _conflict("terminal_run", "this run has settled")
        ident = "msg_" + uuid.uuid4().hex[:16]
        self._messages[ident] = {
            "id": ident,
            "run_id": run_id,
            "actor": actor,
            "request_key": key,
            "digest": _digest(body),
            "body": json.dumps(body),
            "state": "queued",
            "detail": None,
            "created_at": time.time(),
        }
        self.msg_by_key[(run_id, key)] = ident
        return _public_message(self._messages[ident]), True

    def pending_messages(self, run_id: str) -> list:
        return [
            {"id": m["id"], "body": json.loads(m["body"])}
            for m in sorted(self._messages.values(), key=lambda m: m["created_at"])
            if m["run_id"] == run_id and m["state"] == "queued"
        ]

    def messages(self, run_id: str) -> list:
        return [
            _public_message(m)
            for m in sorted(self._messages.values(), key=lambda m: m["created_at"])
            if m["run_id"] == run_id
        ]

    def message_state(self, ident: str, state: str, detail=None) -> None:
        if ident in self._messages:
            self._messages[ident]["state"] = state
            self._messages[ident]["detail"] = detail

    # -- supervision --
    def heartbeat(self, run_id: str, started: bool = False) -> None:
        self.beats[run_id] = {"pid": os.getpid(), "at": time.time(), "started": started}

    def unsettled(self) -> list:
        out = []
        for run_id, data in self.runs.items():
            if data.get("execution") not in SETTLED:
                row = copy.deepcopy(data)
                row["spec"] = copy.deepcopy(self.specs[run_id])
                row["version"] = _run_version(data)
                out.append(row)
        return sorted(out, key=lambda r: r["created_at"])

    def for_target(self, session_ref: str) -> list:
        return [r for r in self.unsettled() if r.get("session_ref") == session_ref]


def _public_message(row: dict) -> dict:
    return {
        "message_id": row["id"],
        "run_id": row["run_id"],
        "action": json.loads(row["body"])["action"],
        "state": row["state"],
        "detail": row["detail"],
        "created_at": row["created_at"],
    }


class _Conflict(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _conflict(code: str, message: str) -> _Conflict:
    return _Conflict(code, message)


class FileStore(MemoryStore):
    """Cross-process store: the whole doc under one fcntl-guarded JSON file."""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(os.path.join(root, "locks"), exist_ok=True)
        self.doc_path = os.path.join(root, "fake-runs.json")
        self.lock_path = os.path.join(root, "locks", "fake-store.lock")
        if not os.path.exists(self.doc_path):
            with open(self.doc_path, "w") as fh:
                json.dump(
                    {
                        "runs": {},
                        "specs": {},
                        "digests": {},
                        "keys": {},
                        "events": {},
                        "idents": {},
                        "messages": {},
                        "msg_by_key": {},
                    },
                    fh,
                )

    def _load(self) -> dict:
        with open(self.doc_path) as fh:
            return json.load(fh)

    def _save(self, doc: dict) -> None:
        tmp = self.doc_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(doc, fh)
        os.replace(tmp, self.doc_path)

    def _guarded(self, fn, *args, **kwargs):
        with open(self.lock_path, "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                self._sync_in()
                result = fn(*args, **kwargs)
                self._sync_out()
                return result
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _sync_in(self) -> None:
        doc = self._load()
        self.runs = doc["runs"]
        self.specs = doc["specs"]
        self.digests = doc["digests"]
        self.keys = doc["keys"]
        self.event_log = {k: v for k, v in doc["events"].items()}
        self.event_idents = {k: set(v) for k, v in doc["idents"].items()}
        self._messages = doc["messages"]
        self.msg_by_key = {tuple(k.split("\x00")): v for k, v in doc["msg_by_key"].items()}
        if not hasattr(self, "beats"):
            self.beats = {}

    def _sync_out(self) -> None:
        doc = {
            "runs": self.runs,
            "specs": self.specs,
            "digests": self.digests,
            "keys": self.keys,
            "events": self.event_log,
            "idents": {k: sorted(v) for k, v in self.event_idents.items()},
            "messages": self._messages,
            "msg_by_key": {"\x00".join(k): v for k, v in self.msg_by_key.items()},
        }
        self._save(doc)

    def create(self, actor, key, spec, initial):
        return self._guarded(super().create, actor, key, spec, initial)

    def get(self, run_id, *, private=False):
        with open(self.lock_path, "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                self._sync_in()
                return MemoryStore.get(self, run_id, private=private)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def update(self, run_id, changes, **kwargs):
        return self._guarded(MemoryStore.update, self, run_id, changes, **kwargs)

    def append(self, run_id, identity, body):
        return self._guarded(super().append, run_id, identity, body)

    def events(self, run_id, after=0, limit=50):
        with open(self.lock_path, "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                self._sync_in()
                return MemoryStore.events(self, run_id, after, limit)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def enqueue(self, run_id, actor, key, body, expected):
        return self._guarded(super().enqueue, run_id, actor, key, body, expected)

    def pending_messages(self, run_id):
        with open(self.lock_path, "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                self._sync_in()
                return MemoryStore.pending_messages(self, run_id)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def messages(self, run_id):
        with open(self.lock_path, "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                self._sync_in()
                return MemoryStore.messages(self, run_id)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def message_state(self, ident, state, detail=None):
        return self._guarded(super().message_state, ident, state, detail)

    def heartbeat(self, run_id, started=False):
        with open(self.lock_path, "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                if not hasattr(self, "beats"):
                    self.beats = {}
                self.beats[run_id] = {"pid": os.getpid(), "at": time.time()}
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def unsettled(self):
        with open(self.lock_path, "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                self._sync_in()
                return MemoryStore.unsettled(self)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def for_target(self, session_ref):
        return [r for r in self.unsettled() if r.get("session_ref") == session_ref]


class ScriptBackend:
    """In-memory pane simulation; records submissions, never touches tmux."""

    def __init__(self):
        self.panes: dict[str, dict] = {}
        self.submitted: list[tuple] = []
        self.interrupts: list[tuple] = []
        self.submit_hook = None

    def host(self, argv, *, runtime, cwd=None, env=None, ident=None):
        pane = f"fake-{runtime}-{ident or uuid.uuid4().hex[:8]}"
        self.panes[pane] = {
            "argv": list(argv),
            "cwd": cwd,
            "env": dict(env or {}),
            "alive": True,
            "composer": "",
            "transcript": [],
        }
        return pane

    def new_ident(self):
        return uuid.uuid4().hex[:8]

    def liveness(self, pane):
        node = self.panes.get(pane)
        if node is None:
            return "dead"
        return "alive" if node["alive"] else "dead"

    def is_owned(self, pane):
        return pane in self.panes

    def capture(self, pane):
        node = self.panes.get(pane)
        if node is None or not node["alive"]:
            return None
        return "\n".join(node["transcript"])

    def wait_ready(self, pane, markers=(), timeout=0, quiet=0):
        return True

    def submit_text(self, pane, text, composer_region=None, submit=True):
        node = self.panes[pane]
        if not node["alive"]:
            from agent_session_control.terminal import TmuxError

            raise TmuxError("target_unavailable", "the pane is gone")
        if self.submit_hook is not None:
            self.submit_hook(pane, text)
        else:
            node["composer"] = text
            if submit:
                node["transcript"].append(text)
                node["composer"] = ""
        self.submitted.append((pane, text))

    def verify_alive_after_inject(self, pane):
        node = self.panes.get(pane)
        if node is None or not node["alive"]:
            from agent_session_control.supervisor import UncertainDelivery

            raise UncertainDelivery("the pane died right after injection")

    def interrupt(self, pane, keys=("Escape",), gap=0):
        node = self.panes.get(pane)
        if node is None or not node["alive"]:
            from agent_session_control.terminal import TmuxError

            raise TmuxError("target_unavailable", "no hosted pane to interrupt")
        self.interrupts.append((pane, tuple(keys)))

    def close(self, pane):
        node = self.panes.get(pane)
        if node is None:
            raise RuntimeError("not owned")
        node["alive"] = False
        return True


def script_observation(
    run_id,
    message_ids=("initial",),
    outcome="completed",
    evidence="native",
    attribution="correlated",
    first_seq=1,
    pending=False,
    error=None,
    namespace=MARKER_NAMESPACE,
):
    """Plain observation dict shaped like history.correlate output."""
    events = [
        {
            "native_seq": first_seq,
            "type": "user_message",
            "text": f"brief [{namespace} run={run_id} message={message_ids[0]}]",
        }
    ]
    if outcome not in ("in_progress",):
        events.append({"native_seq": first_seq + 1, "type": "assistant_message", "text": "done"})
    return {
        "recorded": True,
        "messages": list(message_ids),
        "first_seq": first_seq,
        "end_seq": first_seq + (1 if len(events) > 1 else 0),
        "turn_id": "turn_1",
        "outcome": outcome,
        "outcome_evidence": evidence,
        "outcome_error": error,
        "stop_reason": None,
        "attribution": attribution,
        "current_turn": True,
        "pending_interaction": pending,
        "events": events,
    }


class ScriptHistory:
    """Scripted observation queue: each poll pops the next script entry."""

    def __init__(self, script, exact=None):
        self.script = list(script)
        self.calls = 0
        self.exact = exact

    def _next(self):
        self.calls += 1
        if len(self.script) > 1:
            return self.script.pop(0)
        return self.script[0]

    def open_reader(self, session):
        return {"session": dict(session), "closed": False}

    def observe(self, reader, run_id, base_seq, events, runtime=""):
        item = self._next()
        if item == "reset":
            return ({"recorded": bool(events)}, True)
        if item == "unrecorded":
            return ({"recorded": False}, False)
        return (dict(item), False)

    def baseline_seq(self, session):
        return -1

    def find_exact(self, runtime_id, native_id, limit=50):
        if self.exact is None:
            return None
        if self.exact.get("id") == native_id:
            return dict(self.exact)
        return None

    def load_snapshot(self, session):
        raise RuntimeError("no snapshot in script history")


def script_hooks(
    store_root,
    session_ref="ses_test",
    runtime="echo",
    observations=(),
    submit_hook=None,
    busy=False,
    waiting=False,
    steer_supported=True,
    questions=(),
    bind_native=None,
    resolve_path="fake-history",
):
    """Build SupervisorHooks over ScriptBackend + ScriptHistory."""
    from agent_session_control.supervisor import CertainFailure, SupervisorHooks

    backend = ScriptBackend()
    pane = backend.host((sys.executable, ECHO_AGENT), runtime=runtime)
    if submit_hook is not None:
        backend.submit_hook = submit_hook
    history = ScriptHistory(list(observations) or [script_observation("run_x", outcome="completed")])

    def resolve_target(ref):
        if ref != session_ref:
            raise CertainFailure("not_found", f"unknown session: {ref}")
        return {
            "id": "native-1",
            "source": runtime,
            "cwd": "/tmp",
            "path": resolve_path,
            "pane": pane,
            "live": True,
        }

    def submit(ref, text):
        backend.submit_text(pane, text)

    def steer(ref, text):
        if not steer_supported:
            raise CertainFailure("steer_unsupported", "this backend cannot steer")
        backend.submit_text(pane, text)

    def poll_observation(handle, run_id, base):
        observation, reset = history.observe(handle, run_id, base, {})
        if isinstance(observation, dict):
            return (observation, reset, "ready")
        return (observation.as_dict(), reset, "ready")

    return (
        SupervisorHooks(
            resolve_target=resolve_target,
            target_busy=lambda session: busy,
            target_waiting=lambda session: waiting,
            prepare=lambda ref, deadline: {"pane": pane},
            submit=submit,
            verify=lambda ref, baseline: backend.verify_alive_after_inject(pane),
            interrupt=lambda ref: backend.interrupt(pane, ("C-c",)),
            steer=steer,
            open_observation=lambda binding: history.open_reader(binding),
            poll_observation=poll_observation,
            baseline_of=lambda binding: -1,
            bind_native=bind_native,
            is_alive=lambda ref: backend.liveness(pane),
            screen_hash=lambda ref: "screen-1",
            phase=lambda ref: "idle",
            list_questions=lambda ref: list(questions),
            close_owned=lambda ref: backend.close(pane),
        ),
        backend,
        history,
    )


class FileBackend:
    """Cross-process pane simulation: one transcript file per pane."""

    def __init__(self, root: str):
        self.root = root
        self.pane_dir = os.path.join(root, "panes")
        os.makedirs(self.pane_dir, exist_ok=True)
        self.submitted: list[tuple] = []

    def _path(self, pane):
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in pane)
        return os.path.join(self.pane_dir, safe + ".txt")

    def host(self, argv, *, runtime, cwd=None, env=None, ident=None):
        pane = f"file-{runtime}-{ident or uuid.uuid4().hex[:8]}"
        with open(self._path(pane), "w") as fh:
            fh.write("FILE-READY\n")
        return pane

    def new_ident(self):
        return uuid.uuid4().hex[:8]

    def liveness(self, pane):
        return "alive" if os.path.exists(self._path(pane)) else "dead"

    def capture(self, pane):
        try:
            with open(self._path(pane)) as fh:
                return fh.read()
        except OSError:
            return None

    def wait_ready(self, pane, markers=(), timeout=0, quiet=0):
        return True

    def submit_text(self, pane, text, composer_region=None, submit=True):
        with open(self._path(pane), "a") as fh:
            fh.write(text + "\n")
        self.submitted.append((pane, text))

    def verify_alive_after_inject(self, pane):
        if not os.path.exists(self._path(pane)):
            from agent_session_control.supervisor import UncertainDelivery

            raise UncertainDelivery("the pane died right after injection")

    def interrupt(self, pane, keys=("Escape",), gap=0):
        with open(self._path(pane), "a") as fh:
            fh.write("<INTERRUPT>\n")

    def close(self, pane):
        try:
            os.remove(self._path(pane))
        except OSError:
            pass
        return True


class FileHistory:
    """Cross-process observation: scans pane transcript files for markers."""

    def __init__(self, root: str, outcome="completed", evidence="native", namespace=MARKER_NAMESPACE):
        self.root = root
        self.outcome = outcome
        self.evidence = evidence
        self.namespace = namespace

    def _read(self, binding):
        pane = binding.get("pane", "")
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in pane)
        try:
            with open(os.path.join(self.root, "panes", safe + ".txt")) as fh:
                return fh.read()
        except OSError:
            return ""

    def open_reader(self, binding):
        return {"binding": dict(binding)}

    def observe(self, reader, run_id, base_seq, events, runtime=""):
        import re as _re

        text = self._read(reader["binding"])
        pattern = r"\[" + _re.escape(self.namespace) + r" run=(run_[0-9a-f]+) message=([\w-]+)\]"
        found = _re.findall(pattern, text)
        mine = sorted({m for r, m in found if r == run_id})
        if not mine:
            return ({"recorded": False}, False)
        obs = script_observation(run_id, message_ids=mine, outcome=self.outcome, evidence=self.evidence)
        return (obs, False)

    def baseline_seq(self, binding):
        return -1

    def load_snapshot(self, session):
        raise RuntimeError("no snapshot in file history")


class EchoProfile:
    """Fixture runtime profile: hosts the echo agent, C-c interrupts."""

    id = "echo"
    interrupt_keys = ("C-c",)
    model_flag = None
    session_id_flag = None
    can_steer = False
    readiness_markers = ("ECHO-READY",)

    def describe(self):
        return {
            "interrupt_keys": list(self.interrupt_keys),
            "model_selection": False,
            "steer": False,
            "mcp": "unsupported",
        }

    def build_create_plan(self, cwd, *, model=None, auto_approve=False, mcp_servers=()):
        if model:
            raise RuntimeError("echo has no models")
        if mcp_servers:
            raise RuntimeError("echo has no MCP")
        return _Plan((sys.executable, ECHO_AGENT), cwd)

    def build_resume_plan(self, session, *, auto_approve=False, mcp_servers=()):
        if not session.get("id"):
            raise RuntimeError("refusing to resume without an exact native id")
        return _Plan((sys.executable, ECHO_AGENT), session.get("cwd"))


class PiLikeProfile:
    """Fixture profile with pi-shaped resume semantics: resume addresses the
    exact native history path, never a short id."""

    id = "pi-like"
    interrupt_keys = ("Escape",)
    model_flag = "--model"
    session_id_flag = None
    can_steer = False
    readiness_markers = ("ECHO-READY",)

    def describe(self):
        return {
            "interrupt_keys": list(self.interrupt_keys),
            "model_selection": True,
            "steer": False,
            "mcp": "unsupported",
        }

    def build_create_plan(self, cwd, *, model=None, auto_approve=False, mcp_servers=()):
        argv = [sys.executable, ECHO_AGENT]
        if model:
            argv += ["--model", model]
        return _Plan(argv, cwd)

    def build_resume_plan(self, session, *, auto_approve=False, mcp_servers=()):
        if not session.get("path"):
            raise RuntimeError("refusing to resume pi-like without the exact native history path")
        return _Plan((sys.executable, ECHO_AGENT, "--session", str(session["path"])), session.get("cwd"))


class McpEchoProfile(EchoProfile):
    """Echo fixture that genuinely consumes MCP servers into the plan env."""

    id = "mcp-echo"

    def describe(self):
        info = dict(super().describe())
        info["mcp"] = "supported"
        return info

    def build_create_plan(self, cwd, *, model=None, auto_approve=False, mcp_servers=()):
        import json as _json

        plan = super().build_create_plan(cwd, model=model, auto_approve=auto_approve)
        servers = list(mcp_servers or ())
        if servers:
            plan.env["ASC_MCP_SERVERS"] = _json.dumps(servers, ensure_ascii=False)
        return plan


class _Plan:
    def __init__(self, argv, cwd, env=None):
        self.argv = tuple(argv)
        self.cwd = cwd
        self.env = dict(env or {})


def file_store_factory(root: str, namespace: str = "default"):
    """Store factory for the detached-supervisor test (importable cross-process)."""
    return FileStore(root)


def file_hooks_factory(root: str, namespace: str):
    """Hooks factory for the detached-supervisor test (importable cross-process)."""
    import sqlite3 as _sqlite3

    from agent_session_control.controller import sessions_filename
    from agent_session_control.supervisor import SupervisorHooks

    backend = FileBackend(root)
    history = FileHistory(root)

    def read_session(session_ref):
        db = os.path.join(root, sessions_filename(namespace))
        conn = _sqlite3.connect(db)
        conn.row_factory = _sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_ref,)).fetchone()
        finally:
            conn.close()
        if row is None:
            from agent_session_control.supervisor import TargetError

            raise TargetError("not_found", f"unknown session: {session_ref}")
        return dict(row)

    def resolve_target(ref):
        row = read_session(ref)
        pane = row.get("pane")
        return {
            "id": row.get("native_id") or row["id"],
            "source": row["runtime"],
            "cwd": row["cwd"],
            "path": row.get("history_path"),
            "pane": pane,
            "live": backend.liveness(pane) == "alive" if pane else False,
        }

    def submit(ref, text):
        row = read_session(ref)
        backend.submit_text(row["pane"], text)

    def bind_native(binding):
        # File-backed runs bind through the persisted registry row, the same
        # shape the production factory uses; the canned history locator keeps
        # this lane free of native-history coupling.
        row = read_session(binding.get("session_ref") or "")
        history_path = row.get("history_path") or "file-history"
        return {
            "session_ref": row["id"],
            "runtime": row["runtime"],
            "pane": row.get("pane"),
            "native_id": row.get("native_id"),
            "history_path": history_path,
            "cwd": row.get("cwd", ""),
        }

    def poll_observation(handle, run_id, base):
        observation, reset = history.observe(handle, run_id, base, {})
        if isinstance(observation, dict):
            return (observation, reset, "ready")
        return (observation.as_dict(), reset, "ready")

    return SupervisorHooks(
        resolve_target=resolve_target,
        prepare=lambda ref, deadline: {"pane": read_session(ref)["pane"]},
        submit=submit,
        verify=lambda ref, baseline: backend.verify_alive_after_inject(read_session(ref)["pane"]),
        interrupt=lambda ref: backend.interrupt(read_session(ref)["pane"], ("C-c",)),
        open_observation=lambda binding: history.open_reader(binding),
        poll_observation=poll_observation,
        baseline_of=lambda binding: -1,
        bind_native=bind_native,
        is_alive=lambda ref: backend.liveness(read_session(ref).get("pane") or ""),
        screen_hash=lambda ref: "screen-file",
        phase=lambda ref: "idle",
        close_owned=lambda ref: backend.close(read_session(ref).get("pane") or ""),
    )

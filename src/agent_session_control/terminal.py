"""Owned tmux hosting for controlled coding-agent sessions.

This module adapts the proven generic mechanics of Corral's ``embed.py`` and
``keepalive.py`` without copying their product coupling (themes, control
channels, viewer registries, navigation state):

- every backend instance owns one tmux server socket derived from its
  namespace, so tests and unrelated users never share a server;
- pane names are created by this backend and tracked as owned; only owned
  panes are ever signalled or killed;
- injection results distinguish certain success, certain failure and
  *uncertain* delivery (timeout/ambiguity), which the supervisor must treat
  as unknown and never silently replay;
- cross-process input serialization uses one advisory file lock per pane at
  the outer edge plus a reentrant thread lock inside the process (the same
  shape as Corral's ``InputLock``).

Standard library only. No SessKit, ACP, UI or host imports.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass

_CALL_TIMEOUT = 10.0
_CREATE_TIMEOUT = 30.0
_READY_POLL = 0.2
_COMPOSER_POLL = 0.15
_PASTE_VISIBLE_POLLS = 10
_SUBMIT_CONFIRM_POLLS = 4
_SUBMIT_RETRIES = 2
_INTERRUPT_PRESS_GAP = 0.3
_DEFAULT_WIDTH = 120
_DEFAULT_HEIGHT = 40

_ANSI_RE = re.compile(r"\x1b\[[0-9;:?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

_MISSING_TARGET_RE = re.compile(
    r"can'?t find (session|pane)|couldn'?t find (session|pane)"
    r"|no such (session|pane)|unknown session|invalid (session|pane)"
    r"|can't find window|no window|no server running|error connecting to",
    re.IGNORECASE,
)

_PASTE_PLACEHOLDER_RE = re.compile(r"\[(?:pasted|paste)\b[^\]]*\]", re.IGNORECASE)
_COMPOSER_MARKER_CHARS = 24

# A runtime confirmation dialog (workspace trust, resume choices, permission
# prompts) owns the keyboard: pasted text plus Enter would answer it instead
# of reaching the agent. This is generic terminal-input safety, not product
# UI: any backend must refuse to type into such a pane.
_BLOCKING_DIALOG_RE = re.compile(r"\benter to confirm\b", re.IGNORECASE)
_DIALOG_FOOTER_LINES = 6


def blocked_input_cause(raw: str | None) -> str | None:
    """Retryable refusal reason when a pane's keyboard belongs to a dialog.

    Returns None when the pane shows no confirmation footer. The check reads
    only the bottom lines of a plain-text capture and never touches the pane.
    """
    if not raw:
        return None
    lines = [line for line in plain_text(raw).splitlines() if line.strip()]
    if any(_BLOCKING_DIALOG_RE.search(line) for line in lines[-_DIALOG_FOOTER_LINES:]):
        return "the pane shows a confirmation dialog; its keyboard does not belong to the agent"
    return None


@dataclass
class InjectionResult:
    """One injection attempt: success, certain failure, or uncertain delivery.

    ``uncertain=True`` means the bytes may already have reached the pane
    (timeout or ambiguous outcome). Callers must surface partial/unknown and
    must never treat it as a certain failure that permits retry or resume.
    The payload text is never stored in the result.
    """

    ok: bool
    cause: str = ""
    uncertain: bool = False

    def __iter__(self):  # type: ignore[override]
        yield self.ok
        yield self.cause


class TmuxError(RuntimeError):
    """Certain, retryable terminal failure with a stable machine code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class UncertainInjection(RuntimeError):
    """Something may already have reached the pane; do not replay."""

    def __init__(self, message: str = "terminal injection outcome is uncertain"):
        super().__init__(message)
        self.code = "delivery_uncertain"
        self.message = message


def plain_text(raw: str | None) -> str:
    """Strip SGR/OSC sequences from a pane capture for marker matching."""
    if not raw:
        return ""
    return _ANSI_RE.sub("", raw)


def _decode_tmux_stderr(raw: object) -> str:
    try:
        if isinstance(raw, bytes):
            return raw.decode("utf-8", "replace")
        if raw:
            text = str(raw)
            return text if len(text) < 500 else text[:500]
    except Exception:
        pass
    return ""


def tmux_child_env(base: dict | None = None) -> dict:
    """Environment for spawning system tmux without host-poisoned settings."""
    env = dict(os.environ if base is None else base)
    env.pop("LD_LIBRARY_PATH", None)
    env.pop("RUNNER_TRACKING_ID", None)
    env.pop("TMUX", None)
    env["TMUX_TMPDIR"] = "/tmp"
    lang = (env.get("LANG") or "").lower()
    if "utf-8" not in lang and "utf8" not in lang:
        env["LANG"] = "C.UTF-8"
        env["LC_ALL"] = "C.UTF-8"
    return env


def socket_name_for(root: str | None, namespace: str) -> str:
    """One tmux server socket per control root and namespace.

    The human-readable prefix carries a truncated namespace, but identity is
    the collision-resistant hash over the full root plus the full namespace:
    sanitization and truncation can never alias two different pairs onto one
    server. Pass ``root=None`` only for throwaway uses that must never share
    a server with a rooted backend (each call then hashes a fresh nonce).
    """
    # Identity is the FULL root plus the EXACT namespace (case-sensitive):
    # sanitization and truncation apply to the display prefix only and can
    # never alias two different pairs onto one server.
    namespace = namespace or "default"
    if root is None:
        fingerprint = f"ephemeral-{uuid.uuid4().hex}"
    else:
        fingerprint = os.path.abspath(root) + "\0" + namespace
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:12]
    short = re.sub(r"[^a-z0-9-]", "-", namespace.lower()).strip("-")[:12] or "default"
    return f"asc-{short}-{digest}"


def pane_name_for(namespace: str, runtime: str, ident: str) -> str:
    """Owned pane name. The name is only a handle; ownership is proven by the
    pane registry (see :class:`PaneRegistry`), never by prefix matching."""
    ns = re.sub(r"[^a-z0-9]", "", (namespace or "default").lower())[:12] or "default"
    rt = re.sub(r"[^a-z0-9]", "", (runtime or "agent").lower())[:12] or "agent"
    return f"asc-{ns}-{rt}-{ident[:8]}"


def _safe_pane_filename(pane: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", pane)[:128] or "pane"


class PaneRegistry:
    """Persisted proof of which panes one backend owns.

    Records live under ``state_dir/panes/<pane>.json`` so every process on
    the same root agrees on ownership. A live pane with no matching record
    (or a record for another socket) is foreign: it is never signalled,
    reused or killed. Records are written before the pane is used and removed
    only after the pane is proven dead, so a crash always leaves recoverable
    ownership behind, never a silent adoption.
    """

    def __init__(self, state_dir: str, socket: str = ""):
        self.state_dir = state_dir
        self.socket = socket

    def _path(self, pane: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9-]", "_", self.socket)[:48]
        name = f"{slug}-{_safe_pane_filename(pane)}.json" if slug else _safe_pane_filename(pane) + ".json"
        return os.path.join(self.state_dir, "panes", name)

    def _read(self, pane: str) -> dict | None:
        import json

        try:
            with open(self._path(pane), encoding="utf-8") as stream:
                record = json.load(stream)
        except (OSError, ValueError):
            return None
        return record if isinstance(record, dict) else None

    def record(self, pane: str, entry: dict) -> None:
        import json
        import time as _time

        path = self._path(pane)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        body = {"pane": pane, "recorded_at": _time.time(), **entry}
        tmp = path + f".tmp-{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as stream:
            json.dump(body, stream, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def lookup(self, pane: str) -> dict | None:
        return self._read(pane)

    def forget(self, pane: str) -> None:
        try:
            os.remove(self._path(pane))
        except OSError:
            pass

    def owned_on_socket(self, socket: str, panes: list[str]) -> list[str]:
        """Filter live ``panes`` down to records bound to ``socket``."""
        return [p for p in panes if (r := self._read(p)) is not None and r.get("socket") == socket]


class PaneInputLock:
    """Reentrant thread lock with one advisory file lock at the outer edge."""

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._path = path
        import fcntl

        self._fcntl = fcntl
        self._fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        self._thread = threading.RLock()
        self._depth = 0

    def __enter__(self):
        self._thread.acquire()
        try:
            if self._depth == 0:
                self._fcntl.flock(self._fd, self._fcntl.LOCK_EX)
            self._depth += 1
            return self
        except BaseException:
            self._thread.release()
            raise

    def __exit__(self, *exc):
        self._depth -= 1
        if self._depth == 0:
            self._fcntl.flock(self._fd, self._fcntl.LOCK_UN)
        self._thread.release()
        return False

    def __del__(self):  # pragma: no cover - best effort cleanup
        try:
            if hasattr(self, "_fd"):
                os.close(self._fd)
        except OSError:
            pass


@contextlib.contextmanager
def input_transaction(backend: TmuxBackend, pane: str):
    """Serialize all writers to one pane across threads and processes."""
    with backend.pane_lock(pane):
        yield


class TmuxBackend:
    """Headless tmux hosting bound to one root/namespace socket.

    Side effects (server start, pane launch) happen only through explicit
    method calls; constructing the backend touches nothing.
    """

    def __init__(
        self,
        namespace: str,
        *,
        root: str | None = None,
        lock_dir: str | None = None,
        state_dir: str | None = None,
        width: int = _DEFAULT_WIDTH,
        height: int = _DEFAULT_HEIGHT,
    ):
        self.namespace = namespace or "default"
        self.root = os.path.abspath(root) if root else None
        self.socket = socket_name_for(self.root, self.namespace)
        if lock_dir is not None:
            self.lock_dir = lock_dir
        elif self.root is not None:
            self.lock_dir = os.path.join(self.root, "locks", "panes")
        else:
            self.lock_dir = os.path.join("/tmp", f"asc-{os.getuid()}", "pane-locks")
        if state_dir is not None:
            self.state_dir = state_dir
        elif self.root is not None:
            self.state_dir = os.path.join(self.root, "panes")
        else:
            self.state_dir = os.path.join("/tmp", f"asc-{os.getuid()}", "pane-records")
        self.registry = PaneRegistry(self.state_dir, self.socket)
        self.width = max(40, width)
        self.height = max(10, height)
        self._locks: dict[str, PaneInputLock] = {}
        self._locks_guard = threading.Lock()

    # -- argv ------------------------------------------------------------

    def _argv(self, *args: str) -> list[str]:
        return ["tmux", "-L", self.socket, "-f", "/dev/null", *args]

    def _run(
        self, *args: str, input_bytes: bytes | None = None, timeout: float = _CALL_TIMEOUT
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            self._argv(*args),
            input=input_bytes,
            stdout=subprocess.PIPE if input_bytes is None else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env=tmux_child_env(),
        )

    # -- availability -----------------------------------------------------

    def available(self) -> bool:
        return shutil.which("tmux") is not None

    def require_available(self) -> None:
        if not self.available():
            raise TmuxError("tmux_unavailable", "tmux is not installed; cannot host a session.")

    def ensure_server(self) -> None:
        self.require_available()
        try:
            proc = self._run("start-server")
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TmuxError("tmux_unavailable", f"tmux server could not start: {exc}") from exc
        if proc.returncode != 0 and "already" not in _decode_tmux_stderr(proc.stderr).lower():
            # start-server is idempotent; a nonzero exit without a clear
            # reason is unknown transport trouble, not proof of anything.
            pass

    # -- pane lifecycle ----------------------------------------------------

    def new_ident(self) -> str:
        return uuid.uuid4().hex[:8]

    def pane_lock(self, pane: str) -> PaneInputLock:
        with self._locks_guard:
            lock = self._locks.get(pane)
            if lock is None:
                fingerprint = hashlib.sha256(f"{self.socket}\0{pane}".encode()).hexdigest()[:32]
                lock = PaneInputLock(os.path.join(self.lock_dir, f"{fingerprint}.lock"))
                self._locks[pane] = lock
            return lock

    def is_owned(self, pane: str) -> bool:
        """True only with a persisted record bound to this backend's socket.

        Prefix matching alone is never ownership proof: a foreign live pane
        that happens to share the name shape is refused. Liveness and
        instance identity are checked separately (see :meth:`live_owned`):
        a dead owned pane is still owned for cleanup purposes.
        """
        record = self.registry.lookup(pane)
        return record is not None and record.get("socket") == self.socket

    def current_instance(self, pane: str) -> tuple[str, str] | None:
        """The live ``(pane_id, pane_pid)`` of a pane, or None when unreadable.

        Both values come from the server in one query. A replacement process
        under a reused session name always reports a different pair.
        """
        try:
            proc = self._run("display-message", "-p", "-t", pane, "#{pane_id}:#{pane_pid}")
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        text = (proc.stdout or b"").decode("utf-8", "replace").strip()
        pane_id, sep, pane_pid = text.partition(":")
        if not sep or not pane_id.startswith("%") or not pane_pid.strip().isdigit():
            return None
        return (pane_id, pane_pid.strip())

    def live_owned(self, pane: str) -> bool:
        """True only for a live pane whose current instance matches the record.

        This is the gate for reuse and control: a live pane with a missing,
        foreign-socket, or stale (replacement-process) record is refused, and
        an unreadable instance is never treated as proof.
        """
        record = self.registry.lookup(pane)
        if record is None or record.get("socket") != self.socket:
            return False
        if not record.get("pane_id") or not record.get("pane_pid"):
            return False
        if self.liveness(pane) != "alive":
            return False
        return self.current_instance(pane) == (record.get("pane_id"), record.get("pane_pid"))

    def host(
        self,
        argv: tuple[str, ...] | list[str],
        *,
        runtime: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        ident: str | None = None,
    ) -> str:
        """Launch ``argv`` detached in an owned pane; same ident reuses it.

        Reuse requires verified live ownership: a live pane with no record,
        a foreign record, or a stale record (a replacement process under a
        reused name) raises instead of being adopted. Only a byte-identical
        live instance is ever reused.
        """
        self.require_available()
        if not argv:
            raise TmuxError("usage_error", "Refusing to host an empty argv.")
        self.ensure_server()
        ident = ident or self.new_ident()
        pane = pane_name_for(self.namespace, runtime, ident)
        if self.liveness(pane) == "alive":
            if not self.live_owned(pane):
                raise TmuxError(
                    "already_hosted",
                    f"pane {pane} is alive but not verifiably owned by this backend; refusing to adopt it",
                )
            return pane
        cmd: list[str] = [
            "new-session",
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-s",
            pane,
            "-x",
            str(self.width),
            "-y",
            str(self.height),
        ]
        if cwd:
            cmd += ["-c", cwd]
        for key, value in (env or {}).items():
            cmd += ["-e", f"{key}={value}"]
        cmd += ["--", *argv]
        try:
            proc = self._run(*cmd, timeout=_CREATE_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TmuxError("launch_failed", f"tmux did not accept the new session: {exc}") from exc
        if proc.returncode != 0:
            detail = _decode_tmux_stderr(proc.stderr)
            if self.liveness(pane) == "alive":
                if not self.live_owned(pane):
                    raise TmuxError(
                        "already_hosted",
                        f"pane {pane} already exists outside this backend; refusing to adopt it",
                    )
                return pane
            suffix = f": {detail}" if detail else ""
            raise TmuxError("launch_failed", f"tmux could not create pane {pane}{suffix}")
        instance = self.current_instance(pane)
        if instance is None:
            # Launched but unverifiable: never record a blind claim. Tear the
            # pane down rather than leaving an unprovable execution behind.
            try:
                self._run("kill-session", "-t", pane)
            except (OSError, subprocess.TimeoutExpired):
                pass
            raise TmuxError(
                "launch_failed", f"pane {pane} launched but its instance identity is unreadable; removed it"
            )
        pane_id, pane_pid = instance
        self.registry.record(
            pane,
            {
                "socket": self.socket,
                "namespace": self.namespace,
                "root": self.root,
                "runtime": runtime,
                "ident": ident,
                "pid": os.getpid(),
                "pane_id": pane_id,
                "pane_pid": pane_pid,
            },
        )
        return pane

    def liveness(self, pane: str) -> str:
        """``alive`` | ``dead`` | ``unknown``; only authoritative target loss is dead."""
        if not pane or shutil.which("tmux") is None:
            return "unknown"
        try:
            proc = self._run("has-session", "-t", pane)
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        if proc.returncode == 0:
            return "alive"
        if _MISSING_TARGET_RE.search(_decode_tmux_stderr(proc.stderr)):
            return "dead"
        return "unknown"

    def capture(self, pane: str) -> str | None:
        """Raw styled capture; None when the pane cannot be read."""
        try:
            proc = self._run("capture-pane", "-p", "-e", "-t", pane)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        return (proc.stdout or b"").decode("utf-8", "replace")

    def close(self, pane: str) -> bool:
        """Kill a verifiably owned pane.

        Refuses panes without a matching ownership record, and refuses live
        panes whose current instance no longer matches the record (a
        replacement process under a reused name is never killed). The record
        is removed only after the pane is proven dead, so a crash between the
        kill and the cleanup leaves recoverable ownership behind. A failed or
        uncertain kill keeps the record: the pane stays owned and recoverable.
        """
        if not self.is_owned(pane):
            raise TmuxError("not_owned", f"Refusing to kill a pane not owned by this backend (socket {self.socket}).")
        if self.liveness(pane) == "dead":
            self.registry.forget(pane)
            return True
        if not self.live_owned(pane):
            raise TmuxError(
                "not_owned",
                f"pane {pane} is alive but its instance does not match the ownership record; "
                "refusing to kill a possible foreign process",
            )
        try:
            self._run("kill-session", "-t", pane)
        except (OSError, subprocess.TimeoutExpired):
            return False
        # Verify death regardless of the kill exit code: forgetting the
        # record for a still-live pane would orphan it beyond recovery.
        if self.liveness(pane) != "dead":
            return False
        self.registry.forget(pane)
        return True

    # -- readiness ----------------------------------------------------------

    def wait_ready(
        self, pane: str, markers: tuple[str, ...] = (), *, timeout: float = 45.0, quiet: float = 1.5
    ) -> bool:
        """Wait until the pane shows non-empty content (plus markers) stably.

        Returns True when ready, False on timeout. Never raises for a slow
        pane; the caller decides whether to proceed or report unknown.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        last = "\0-none-\0"
        stable_since = time.monotonic()
        while True:
            screen = plain_text(self.capture(pane))
            now = time.monotonic()
            if screen != last:
                last, stable_since = screen, now
            ready = bool(screen.strip()) and all(m in screen for m in markers)
            if ready and now - stable_since >= quiet:
                return True
            if now >= deadline:
                return False
            time.sleep(_READY_POLL)

    # -- injection ------------------------------------------------------------

    def _send_detailed(self, pane: str, *args: str) -> InjectionResult:
        try:
            completed = subprocess.run(
                self._argv(*args),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=_CALL_TIMEOUT,
                check=False,
                env=tmux_child_env(),
            )
        except FileNotFoundError:
            return InjectionResult(False, "tmux_unavailable", False)
        except (OSError, subprocess.TimeoutExpired):
            return InjectionResult(False, "uncertain", True)
        if completed.returncode == 0:
            return InjectionResult(True)
        err = _decode_tmux_stderr(completed.stderr)
        if _MISSING_TARGET_RE.search(err or "") and self.liveness(pane) == "dead":
            return InjectionResult(False, "pane_gone", False)
        return InjectionResult(False, "tmux_error", True)

    def send_key_detailed(self, pane: str, *keys: str) -> InjectionResult:
        """Send tmux key names (Enter, Escape, C-c, ...)."""
        if not keys:
            return InjectionResult(True)
        with self.pane_lock(pane):
            return self._send_detailed(pane, "send-keys", "-t", pane, "--", *keys)

    def paste_detailed(self, pane: str, text: str) -> InjectionResult:
        """Paste through a per-call buffer (concurrent pastes never mix).

        ``load-buffer`` has no terminal side effect, so its failure is certain
        and retryable. Once ``paste-buffer`` runs, any timeout or ambiguity is
        uncertain and must not be retried.
        """
        if not text:
            return InjectionResult(True)
        with self.pane_lock(pane):
            return self._paste_locked(pane, text)

    def _paste_locked(self, pane: str, text: str) -> InjectionResult:
        buffer_name = f"asc-{os.getpid()}-{threading.get_ident()}-{time.monotonic_ns()}"
        # load-buffer (not set-buffer): this tmux reads stdin only on the
        # load-buffer path. Writing the buffer has no terminal side
        # effect, so its failure stays certain and retryable.
        try:
            proc = subprocess.run(
                self._argv("load-buffer", "-b", buffer_name, "-"),
                input=text.encode("utf-8", "replace"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=_CALL_TIMEOUT,
                check=False,
                env=tmux_child_env(),
            )
        except FileNotFoundError:
            return InjectionResult(False, "tmux_unavailable", False)
        except (OSError, subprocess.TimeoutExpired):
            return InjectionResult(False, "tmux_busy", False)
        if proc.returncode != 0:
            self._delete_buffer(pane, buffer_name)
            return InjectionResult(False, "tmux_error", False)
        res = self._send_detailed(pane, "paste-buffer", "-p", "-d", "-b", buffer_name, "-t", pane)
        if not res.ok and not res.uncertain:
            self._delete_buffer(pane, buffer_name)
        return res

    def _delete_buffer(self, pane: str, buffer_name: str) -> None:
        try:
            subprocess.run(
                self._argv("delete-buffer", "-b", buffer_name),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_CALL_TIMEOUT,
                check=False,
                env=tmux_child_env(),
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    # -- verified submit --------------------------------------------------------

    @staticmethod
    def composer_holds(composer_region, screen: str | None, text: str) -> bool | None:
        """Whether the runtime input box still shows ``text``; None if unknowable."""
        tail = "".join(ch for ch in (text or "") if ch.isalnum())[-_COMPOSER_MARKER_CHARS:]
        if composer_region is None or not tail:
            return None
        try:
            region = composer_region(plain_text(screen))
        except Exception:
            return None
        if region is None:
            return None
        if tail in "".join(ch for ch in region if ch.isalnum()):
            return True
        return bool(_PASTE_PLACEHOLDER_RE.search(region))

    def _await_composer_holds(self, pane: str, composer_region, text: str) -> bool:
        holds = self.composer_holds(composer_region, self.capture(pane), text)
        if holds is None:
            time.sleep(_COMPOSER_POLL)
            return False
        for _ in range(_PASTE_VISIBLE_POLLS):
            if holds:
                return True
            time.sleep(_COMPOSER_POLL)
            holds = self.composer_holds(composer_region, self.capture(pane), text)
        return bool(holds)

    def _composer_keeps(self, pane: str, composer_region, text: str) -> bool:
        for _ in range(_SUBMIT_CONFIRM_POLLS):
            time.sleep(_COMPOSER_POLL)
            if not self.composer_holds(composer_region, self.capture(pane), text):
                return False
        return True

    def submit_text(self, pane: str, text: str, *, composer_region=None, submit: bool = True) -> None:
        """Paste ``text`` and press Enter with verification.

        The whole paste-to-Enter sequence holds the pane's input lock, so two
        concurrent writers can never have their bytes concatenated into one
        line (the lock this method shares with ``paste``/``send_key`` is
        reentrant within the thread).

        Raises :class:`TmuxError` when nothing reached the pane (certain,
        retryable) and :class:`UncertainInjection` when anything may already
        have reached it (must become unknown, never replayed).
        """
        with self.pane_lock(pane):
            self._submit_locked(pane, text, composer_region=composer_region, submit=submit)

    def _submit_locked(self, pane: str, text: str, *, composer_region=None, submit: bool = True) -> None:
        if not text:
            if submit:
                res = self._send_detailed(pane, "send-keys", "-t", pane, "--", "Enter")
                if res.uncertain or not res.ok:
                    raise UncertainInjection("empty submit outcome is uncertain")
            return
        pasted = self._paste_locked(pane, text)
        if pasted.uncertain:
            raise UncertainInjection("paste outcome is uncertain")
        if not pasted.ok:
            if pasted.cause == "pane_gone" or self.liveness(pane) == "dead":
                raise TmuxError("target_unavailable", "the pane is gone; nothing was injected")
            raise TmuxError("input_rejected", f"paste was rejected ({pasted.cause or 'tmux_error'})")
        if not submit:
            return
        verifiable = self._await_composer_holds(pane, composer_region, text)
        res = self._send_detailed(pane, "send-keys", "-t", pane, "--", "Enter")
        if res.uncertain or (not res.ok):
            raise UncertainInjection("Enter outcome is uncertain after a successful paste")
        if verifiable:
            self._ensure_turn_submitted_locked(pane, composer_region, text)

    def _ensure_turn_submitted_locked(self, pane: str, composer_region, text: str) -> None:
        """Press Enter again while the prompt still sits in the composer.

        A repeat Enter submits after a dropped Enter, a newline-absorbed Enter
        or an Enter that only confirmed a completion popup. Never Escape: with
        no popup open it would interrupt the running turn. The pane lock is
        already held by the enclosing submit.
        """
        for attempt in range(_SUBMIT_RETRIES + 1):
            if not self._composer_keeps(pane, composer_region, text):
                return
            if attempt == _SUBMIT_RETRIES:
                break
            res = self._send_detailed(pane, "send-keys", "-t", pane, "--", "Enter")
            if res.uncertain or not res.ok:
                raise UncertainInjection("repeat Enter outcome is uncertain")
        raise UncertainInjection("prompt still sits in the composer after repeated Enter")

    def verify_alive_after_inject(self, pane: str) -> None:
        """Post-injection liveness gate: only a proven-dead pane stays uncertain."""
        if self.liveness(pane) == "dead":
            raise UncertainInjection("the pane died right after injection")

    def interrupt(self, pane: str, keys: tuple[str, ...] = ("Escape",), gap: float = _INTERRUPT_PRESS_GAP) -> None:
        """Stop the running turn with the runtime's own interrupt keys.

        Presses go out as separate writes. Never resumes or recovers: an
        interrupt must not start an ended conversation.
        """
        if not pane:
            raise TmuxError("target_unavailable", "no hosted pane to interrupt")
        if not keys:
            raise TmuxError("unsupported", "this runtime profile has no interrupt sequence")
        for index, press in enumerate(keys):
            if index:
                time.sleep(gap)
            res = self.send_key_detailed(pane, press)
            if res.ok:
                continue
            if res.uncertain or index:
                raise UncertainInjection("interrupt outcome is uncertain")
            raise TmuxError("interrupt_failed", f"interrupt key was rejected ({res.cause})")

    # -- housekeeping -------------------------------------------------------------

    def owned_panes(self) -> list[str]:
        """Live panes of this socket with a matching ownership record."""
        try:
            proc = self._run("list-sessions", "-F", "#{session_name}")
        except (OSError, subprocess.TimeoutExpired):
            return []
        if proc.returncode != 0:
            return []
        names = (proc.stdout or b"").decode("utf-8", "replace").split()
        return self.registry.owned_on_socket(self.socket, names)

    __all__ = ["TmuxBackend"]

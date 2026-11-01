"""Generic per-run supervision: admit, deliver once, observe, settle.

One supervisor owns one run. It drives the run from durable intent stored in
the operation store to a settled result, using only the injected
:class:`SupervisorHooks`. The same runner serves the library's
``LocalController`` and an existing host that wants to delegate execution
without maintaining a second executor: the host implements the hooks with its
own terminal and observer and passes them in.

Adapted from Corral's ``control_worker.py`` Supervisor. Product coupling
(attention boards, layout stores, provisional cards, phone channels) stays in
the host; only these generic mechanics move here:

- run/target file locks shared between processes on the same root;
- admission with queue/steer/reject on a busy target;
- exactly-once dispatch with certain-rejected versus uncertain-unknown
  outcomes; recovery never retypes a brief that may already have been sent;
- observation through normalized activity with exact run/message correlation;
- honest settlement: native evidence settles at once, inferred evidence only
  after a quiet window, mixed attribution never becomes an exclusive result;
- split cancellation request, confirmed interruption, process close and
  detach: ``interrupt`` targets one exact run, ``close`` releases owned
  resources, ``detach`` stops observing while accepted work continues.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib
import json
import os
import sys
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

POLL_INTERVAL = 1.0
HEARTBEAT_INTERVAL = 5.0
BIND_REFRESH = 3.0
BUSY_POLL = 3.0
SETTLE_QUIET = 8.0
DISPATCH_TIMEOUT = 150.0
HISTORY_WAIT_TIMEOUT = DISPATCH_TIMEOUT
STARTUP_QUIET = 1.5

TERMINAL_EXECUTION = frozenset({"completed", "failed", "interrupted"})
SETTLED_EXECUTION = TERMINAL_EXECUTION | {"unknown"}


class TargetError(Exception):
    """Admission or binding failure with a stable machine code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class CertainFailure(Exception):
    """Nothing reached the pane; the run is rejected and may be retried anew."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class UncertainDelivery(Exception):
    """Something may have reached the pane; the run is unknown, never replayed."""

    def __init__(self, message: str = "input delivery is uncertain"):
        super().__init__(message)
        self.code = "delivery_uncertain"
        self.message = message


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@contextmanager
def file_lock(root: str, identity: str, *, blocking: bool = False):
    """Cross-process advisory lock shared by every process on ``root``.

    Yields True when the lock was acquired, False when it is held elsewhere.
    """
    os.makedirs(os.path.join(root, "locks"), mode=0o700, exist_ok=True)
    path = os.path.join(root, "locks", _digest(identity)[:32] + ".lock")
    with open(path, "a+") as stream:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(stream.fileno(), flags)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def lock_held(root: str, identity: str) -> bool:
    path = os.path.join(root, "locks", _digest(identity)[:32] + ".lock")
    if not os.path.exists(path):
        return False
    with open(path, "a+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return False


@dataclass
class SupervisorHooks:
    """Every side effect the runner needs, supplied by the backend/host.

    All callables raise :class:`TargetError` (admission/binding),
    :class:`CertainFailure` (nothing injected) or :class:`UncertainDelivery`
    (unknown, do not replay). Returning ``None`` where a probe is unavailable
    keeps the run honest instead of inventing evidence.

    - ``resolve_target(session_ref)``: exact session mapping, no guessing.
    - ``target_busy(session)``: True while another turn owns the target.
    - ``target_waiting(session)``: True while a question blocks new input.
    - ``host_fresh(spec, ident)``: launch and host a fresh session for a
      create run; returns binding ``{session_ref, pane, native_id?}``.
      Reusing an existing pane for a repeated ident must be idempotent.
    - ``prepare(session_ref, deadline)``: readiness gate; returns a baseline
      token the later ``verify`` understands.
    - ``submit(session_ref, wire_text)``: verified paste plus submit.
    - ``verify(session_ref, baseline)``: post-inject liveness gate.
    - ``interrupt(session_ref)``: runtime interrupt keys for the active turn.
    - ``answer(session_ref, request_id, answers)``: structured question path;
      returns ``{"status": "delivered"|..., "detail": ...}``.
    - ``steer(session_ref, wire_text)``: mid-turn follow-up input. Backends
      without true steering raise ``CertainFailure("steer_unsupported", ...)``.
    - ``open_observation(binding)`` / ``poll_observation(handle, run_id,
      baseline_seq)`` / ``close_observation(handle)``: SessKit reader
      lifecycle. Poll returns ``(observation_dict, reset, state)`` where the
      dict carries ``recorded, messages, first_seq, end_seq, turn_id,
      outcome, outcome_evidence, outcome_error, stop_reason, attribution,
      current_turn, pending_interaction, events`` (see history.correlate).
    - ``baseline_of(binding)``: last native seq before dispatch (-1 unknown).
    - ``bind_native(binding)``: resolve the exact native session for an
      unbound run (exact preassigned id or host-claimed identity); returns an
      updated binding or None. Never guesses from recency, cwd or PID.
    - ``is_alive(session_ref)``: ``alive`` | ``dead`` | ``unknown``.
    - ``screen_hash(session_ref)``: hash of the current pane screen, or None.
    - ``phase(session_ref)``: ``working`` | ``waiting`` | ``idle`` | ``unknown``.
    - ``list_questions(session_ref)``: pending interaction records (may be []).
    - ``close_owned(session_ref)``: release owned pane/process (for close).
    """

    resolve_target: Callable[[str], dict]
    target_busy: Callable[[dict], bool] = lambda session: False
    target_waiting: Callable[[dict], bool] = lambda session: False
    host_fresh: Callable[[dict, str], dict] | None = None
    prepare: Callable[[str, float], Any] | None = None
    submit: Callable[[str, str], None] | None = None
    verify: Callable[[str, Any], None] | None = None
    interrupt: Callable[[str], None] | None = None
    answer: Callable[[str, str, Any], dict] | None = None
    steer: Callable[[str, str], None] | None = None
    open_observation: Callable[[dict], Any] | None = None
    poll_observation: Callable[[Any, str, int], tuple[dict, bool, str]] | None = None
    close_observation: Callable[[Any], None] | None = None
    baseline_of: Callable[[dict], int] | None = None
    bind_native: Callable[[dict], dict | None] | None = None
    is_alive: Callable[[str], str] = lambda session_ref: "unknown"
    screen_hash: Callable[[str], str | None] = lambda session_ref: None
    phase: Callable[[str], str] = lambda session_ref: "unknown"
    list_questions: Callable[[str], list] = lambda session_ref: []
    close_owned: Callable[[str], Any] | None = None


def _need(hook: Callable | None, name: str) -> Callable:
    if hook is None:
        raise TargetError("unsupported", f"this backend does not implement the {name} hook")
    return hook


class SupervisorRunner:
    """Owns one run from durable intent to settlement."""

    def __init__(
        self,
        store: Any,
        run_id: str,
        hooks: SupervisorHooks,
        *,
        marker_namespace: str | None = None,
        settle_quiet: float = SETTLE_QUIET,
        history_wait: float = HISTORY_WAIT_TIMEOUT,
    ):
        from agent_session_control.history import DEFAULT_NAMESPACE as _marker_default

        self.store = store
        self.run_id = run_id
        self.hooks = hooks
        self.marker_namespace = marker_namespace or _marker_default
        self.settle_quiet = settle_quiet
        self.history_wait = history_wait
        self.binding: dict = {}
        self._beat = 0.0

    # -- store helpers ----------------------------------------------------

    def record(self) -> dict:
        # The shared store keeps the spec private; supervision needs it, so it
        # reads the private projection. Callers without that access fail here
        # instead of supervising blind.
        return self.store.get(self.run_id, private=True)

    def update(self, **changes: Any) -> dict:
        return self.store.update(self.run_id, changes)

    def heartbeat(self) -> None:
        now = time.monotonic()
        if now - self._beat >= HEARTBEAT_INTERVAL:
            self._beat = now
            try:
                self.store.heartbeat(self.run_id)
            except Exception:
                pass

    def fail(self, delivery: str, code: str, message: str) -> None:
        execution = "failed" if delivery == "rejected" else "unknown"
        self.update(delivery=delivery, execution=execution, error={"code": code, "message": message})

    # -- admission ----------------------------------------------------------

    def admit(self) -> None:
        record = self.record()
        spec = record.get("spec", {})
        if spec.get("mode") == "create":
            host_fresh = _need(self.hooks.host_fresh, "host_fresh")
            ident = record.get("host_ident") or self.run_id.removeprefix("run_")[:8]
            self.update(execution="starting", host_ident=ident)
            try:
                self.binding = dict(host_fresh(spec, ident))
            except (TargetError, CertainFailure) as exc:
                self.fail("rejected", exc.code, exc.message)
                return
            except UncertainDelivery as exc:
                self.fail("unknown", exc.code, exc.message)
                return
            self.update(
                session_ref=self.binding.get("session_ref"),
                pane=self.binding.get("pane"),
                native_id=self.binding.get("native_id"),
                history_path=self.binding.get("history_path"),
            )
            return
        try:
            session = self.hooks.resolve_target(spec["session_ref"])
        except TargetError as exc:
            self.fail("rejected", exc.code, exc.message)
            return
        if str(session.get("cwd") or "") != str(spec.get("cwd") or "") or str(session.get("path") or "") != str(
            spec.get("history_path") or ""
        ):
            self.fail("rejected", "target_changed", "the session's project or history changed after submission")
            return
        if session.get("live") and not session.get("pane"):
            self.fail("rejected", "external_session", "a process outside this controller now owns the session")
            return
        # Preserve the complete exact binding: baseline, observation and
        # recovery all read the native history through the recorded runtime.
        # Dropping the runtime here makes later observation use source="",
        # which yields unavailable for a perfectly good history.
        self.binding = {
            "session_ref": spec["session_ref"],
            "pane": session.get("pane"),
            "history_path": session.get("path"),
            "runtime": session.get("runtime") or session.get("source") or spec.get("runtime", ""),
            "native_id": session.get("native_id") or spec.get("native_id"),
            "cwd": session.get("cwd") or spec.get("cwd", ""),
            "title": session.get("title"),
            "identity_evidence": session.get("identity_evidence"),
        }
        while True:
            if self.hooks.target_waiting(session):
                self.fail(
                    "rejected", "target_waiting_input", "the session waits on an earlier question; answer it first"
                )
                return
            if not self.hooks.target_busy(session):
                return
            if spec.get("on_busy") == "reject":
                self.fail("rejected", "target_busy", "the session is executing another turn")
                return
            if spec.get("on_busy") == "steer":
                return
            if _expired(spec, record):
                self.fail(
                    "rejected", "queue_expired", "the target stayed busy past the queue timeout; nothing was sent"
                )
                return
            self.update(execution="queued", queue_reason="target_busy")
            self.heartbeat()
            self.process_messages(None)
            if self.record().get("execution") in SETTLED_EXECUTION:
                return
            time.sleep(BUSY_POLL)
            record = self.record()

    # -- dispatch -------------------------------------------------------------

    def dispatch(self) -> int:
        """Deliver the initial brief once. Returns the native baseline seq."""
        from agent_session_control.history import wire_prompt

        prepare = _need(self.hooks.prepare, "prepare")
        submit = _need(self.hooks.submit, "submit")
        verify = _need(self.hooks.verify, "verify")
        self.update(execution="starting")
        session_ref = self.binding.get("session_ref") or self.record()["spec"]["session_ref"]
        deadline = time.monotonic() + DISPATCH_TIMEOUT
        try:
            baseline = prepare(session_ref, deadline)
        except CertainFailure as exc:
            self.fail("rejected", exc.code, exc.message)
            return -1
        except (TargetError, UncertainDelivery) as exc:
            code = getattr(exc, "code", "target_unavailable")
            self.fail("unknown", code, exc.message)
            return -1
        base_seq = -1
        if self.hooks.baseline_of is not None:
            try:
                baseline_value = self.hooks.baseline_of(self.binding)
            except Exception:
                baseline_value = -1
            base_seq = baseline_value if isinstance(baseline_value, int) else -1
        self.update(pane=self.binding.get("pane"), baseline_seq=base_seq, delivery="dispatching", execution="running")
        namespace = self.record()["spec"].get("marker_namespace") or self.marker_namespace
        text = wire_prompt(self.run_id, "initial", self.record()["spec"]["prompt"], namespace=namespace)
        try:
            submit(session_ref, text)
            verify(session_ref, baseline)
        except CertainFailure as exc:
            # Certain rejection: nothing reached the composer.
            self.fail("rejected", exc.code, exc.message)
            return base_seq
        except UncertainDelivery as exc:
            self.update(delivery="unknown", error={"code": exc.code, "message": exc.message})
            return base_seq
        self.update(delivery="input_submitted")
        return base_seq

    # -- follow-up messages -----------------------------------------------------

    def process_messages(self, observation: dict | None) -> None:
        try:
            pending = self.store.pending_messages(self.run_id)
        except Exception:
            return
        for item in pending:
            body, ident = item["body"], item["id"]
            record = self.record()
            dispatched = record.get("delivery") not in {"accepted"}
            try:
                if body.get("action") == "interrupt":
                    self._process_interrupt(item, observation, dispatched)
                    if self.record().get("execution") in SETTLED_EXECUTION:
                        return
                elif not dispatched:
                    self.store.message_state(ident, "queued", "waiting for the initial brief to be delivered")
                elif body.get("action") == "answer":
                    self._process_answer(item, observation)
                else:
                    self._process_steer(item)
            except CertainFailure as exc:
                self.store.message_state(ident, "rejected", exc.message)
            except UncertainDelivery as exc:
                self.store.message_state(ident, "unknown", exc.message)

    def _process_interrupt(self, item: dict, observation: dict | None, dispatched: bool) -> None:
        ident = item["id"]
        if not dispatched:
            self.store.message_state(ident, "applied", "run cancelled before any input was sent")
            self.update(
                delivery="rejected",
                execution="interrupted",
                error={"code": "cancelled", "message": "interrupted before dispatch"},
            )
            return
        if (
            not observation
            or not observation.get("recorded")
            or observation.get("attribution") != "correlated"
            or not observation.get("current_turn")
        ):
            self.store.message_state(ident, "rejected", "current execution does not belong to this run")
            return
        self.store.message_state(ident, "dispatching")
        interrupt = _need(self.hooks.interrupt, "interrupt")
        session_ref = self.binding.get("session_ref") or self.record()["spec"].get("session_ref")
        interrupt(session_ref)
        self.store.message_state(ident, "input_submitted")

    def _process_answer(self, item: dict, observation: dict | None) -> None:
        body, ident = item["body"], item["id"]
        if not observation or not observation.get("recorded") or not observation.get("current_turn"):
            self.store.message_state(ident, "rejected", "question ownership is not proven for this run")
            return
        answer = _need(self.hooks.answer, "answer")
        self.store.message_state(ident, "dispatching")
        session_ref = self.binding.get("session_ref") or self.record()["spec"].get("session_ref")
        outcome = answer(session_ref, body.get("request_id"), body.get("answers"))
        status = (outcome or {}).get("status")
        self.store.message_state(
            ident, "input_submitted" if status == "delivered" else "rejected", (outcome or {}).get("detail")
        )

    def _process_steer(self, item: dict) -> None:
        from agent_session_control.history import wire_prompt

        body, ident = item["body"], item["id"]
        steer = _need(self.hooks.steer, "steer")
        self.store.message_state(ident, "dispatching")
        session_ref = self.binding.get("session_ref") or self.record()["spec"].get("session_ref")
        namespace = self.record()["spec"].get("marker_namespace") or self.marker_namespace
        steer(session_ref, wire_prompt(self.run_id, ident, body.get("prompt", ""), namespace=namespace))
        self.store.message_state(ident, "input_submitted")

    # -- observation ---------------------------------------------------------------

    def recover(self) -> int:
        """Restart path: mark in-flight writes unknown; never replay them."""
        for item in self.store.messages(self.run_id):
            if item.get("state") == "dispatching":
                self.store.message_state(
                    item.get("message_id") or item.get("id"),
                    "unknown",
                    "supervisor stopped during dispatch; input will not be replayed",
                )
        record = self.record()
        if record.get("delivery") == "dispatching":
            self.update(
                delivery="unknown",
                error={"code": "delivery_uncertain", "message": "the supervisor stopped while sending the brief"},
            )
        return int(record.get("baseline_seq", -1))

    def observe(self, base_seq: int) -> None:
        open_observation = _need(self.hooks.open_observation, "open_observation")
        poll_observation = _need(self.hooks.poll_observation, "poll_observation")
        if not self.binding.get("history_path"):
            if not self._await_binding():
                return
            if self.record().get("execution") in SETTLED_EXECUTION:
                return
        handle = None
        try:
            handle = open_observation(self.binding)
        except (TargetError, CertainFailure) as exc:
            self.fail("unknown", getattr(exc, "code", "observation_unavailable"), exc.message)
            return
        published: set = set()
        quiet_since = time.monotonic()
        screen, screen_since = self.hooks.screen_hash(self._session_ref()), time.monotonic()
        missing_since: float | None = None
        try:
            while True:
                self.heartbeat()
                record = self.record()
                if record.get("execution") in SETTLED_EXECUTION:
                    return
                try:
                    observation, reset, _state = poll_observation(handle, self.run_id, base_seq)
                except Exception as exc:
                    self.fail("unknown", "observation_unavailable", str(exc) or "observation failed")
                    return
                if reset:
                    self.fail("unknown", "history_reset", "the native history was replaced; attribution is unknown")
                    return
                if observation.get("recorded"):
                    missing_since = None
                    if _publish_new_events(self.store, self.run_id, observation, published):
                        quiet_since = time.monotonic()
                else:
                    if self.hooks.is_alive(self._session_ref()) == "dead":
                        self.fail("unknown", "terminal_exited", "the terminal exited before the brief was recorded")
                        return
                    now = time.monotonic()
                    if missing_since is None:
                        missing_since = now
                    if now - missing_since >= self.history_wait:
                        self.update(
                            execution="unknown",
                            error={
                                "code": "native_history_missing",
                                "message": "no native history appeared in time; "
                                "the brief was not resent and the run stays recoverable",
                            },
                        )
                        return
                    # Follow-ups still need verdicts while input is unrecorded:
                    # an interrupt without provable ownership is rejected here.
                    self.process_messages(observation)
                    if self.record().get("execution") in SETTLED_EXECUTION:
                        return
                    time.sleep(POLL_INTERVAL)
                    continue
                try:
                    for message_id in observation.get("messages", []):
                        self._mark_message_recorded(message_id)
                except Exception:
                    pass
                self.process_messages(observation)
                if self.record().get("execution") in SETTLED_EXECUTION:
                    return
                current = self.hooks.screen_hash(self._session_ref())
                if current != screen:
                    screen, screen_since = current, time.monotonic()
                dead = self.hooks.is_alive(self._session_ref()) == "dead"
                quiet = min(time.monotonic() - quiet_since, time.monotonic() - screen_since)
                try:
                    changes = self.observation_changes(self.record(), observation, dead, quiet)
                except Exception as exc:
                    self.fail("unknown", "observation_unavailable", str(exc))
                    return
                if changes:
                    settled = self.store.update(
                        self.run_id, changes, settled_messages=set(observation.get("messages", []))
                    )
                    if settled.get("execution") in SETTLED_EXECUTION:
                        return
                time.sleep(POLL_INTERVAL)
        finally:
            if handle is not None and self.hooks.close_observation is not None:
                try:
                    self.hooks.close_observation(handle)
                except Exception:
                    pass

    def _await_binding(self) -> bool:
        """Bind the exact native session before observing; True when bound.

        Retries the ``bind_native`` hook until the history budget runs out.
        An unbound run becomes unknown and stays recoverable; nothing is
        resent and no blank history is invented.
        """
        if self.hooks.bind_native is None:
            return False
        last_try = 0.0
        missing_since: float | None = None
        while True:
            self.heartbeat()
            if self.record().get("execution") in SETTLED_EXECUTION:
                return False
            now = time.monotonic()
            if now - last_try >= BIND_REFRESH:
                last_try = now
                try:
                    bound = self.hooks.bind_native(dict(self.binding))
                except Exception:
                    bound = None
                if bound and bound.get("history_path"):
                    self.binding = dict(bound)
                    try:
                        self.update(
                            session_ref=bound.get("session_ref", self.binding.get("session_ref")),
                            pane=bound.get("pane"),
                            native_id=bound.get("native_id"),
                            history_path=bound.get("history_path"),
                        )
                    except Exception:
                        pass
                    return True
            if self.hooks.is_alive(self._session_ref()) == "dead":
                self.fail("unknown", "terminal_exited", "the terminal exited before native history appeared")
                return False
            if missing_since is None:
                missing_since = now
            if now - missing_since >= self.history_wait:
                self.update(
                    execution="unknown",
                    error={
                        "code": "native_binding_missing",
                        "message": "no exact native session could be bound in time; "
                        "the brief was not resent and the run stays recoverable",
                    },
                )
                return False
            self.process_messages({"recorded": False})
            if self.record().get("execution") in SETTLED_EXECUTION:
                return False
            time.sleep(POLL_INTERVAL)

    def _session_ref(self) -> str:
        return self.binding.get("session_ref") or self.record()["spec"].get("session_ref", "")

    def _mark_message_recorded(self, message_id: str) -> None:
        for item in self.store.messages(self.run_id):
            if item.get("message_id") == message_id and item.get("state") in {
                "dispatching",
                "unknown",
                "input_submitted",
            }:
                self.store.message_state(message_id, "native_recorded")

    def observation_changes(self, record: dict, observation: dict, dead: bool, quiet: float) -> dict:
        from agent_session_control.history import ActivityObservation, build_result

        if isinstance(observation, ActivityObservation):
            observation = observation.as_dict()
        changes: dict = {
            "delivery": "native_recorded",
            "turn_id": observation.get("turn_id"),
            "attribution": observation.get("attribution"),
            "error": None,
        }
        outcome = observation.get("outcome")
        phase = "idle" if dead else self.hooks.phase(self._session_ref())
        waiting = observation.get("pending_interaction") or phase == "waiting" or outcome == "awaiting_input"
        if observation.get("attribution") == "mixed":
            # Foreign input inside the bound range: never an exclusive result.
            if outcome in {"completed", "failed", "interrupted"} and not waiting:
                changes.update(
                    execution="unknown",
                    error={
                        "code": "mixed_attribution",
                        "message": "other input overlaps this run's turn; the outcome cannot be attributed to it alone",
                    },
                )
                return changes
        if waiting:
            changes["execution"] = "waiting_input"
            try:
                changes["questions"] = self.hooks.list_questions(self._session_ref())
            except Exception:
                changes["questions"] = []
        else:
            changes["execution"] = "running"
            changes["questions"] = []
        final = {"completed": "completed", "failed": "failed", "rejected": "failed", "interrupted": "interrupted"}.get(
            outcome or ""
        )
        if not waiting and observation.get("outcome_evidence") != "native" and self._interrupt_delivered():
            # Some runtimes record no native end for an interrupted turn while
            # the history tail still reads as working. This run's own delivered
            # interrupt plus quiet evidence is the confirmation.
            final = "interrupted"
            observation = {**observation, "outcome_evidence": "inferred"}
        if final and not waiting:
            events = observation.get("events") or []
            last = events[-1] if events else None
            last_is_reply = (
                last.get("type") if isinstance(last, dict) else getattr(last, "type", None)
            ) == "assistant_message"
            settled = (
                observation.get("outcome_evidence") == "native"
                or dead
                or (
                    quiet >= self.settle_quiet
                    and (phase not in {"working", "waiting"} or final == "interrupted")
                    and (final != "completed" or last_is_reply)
                )
            )
            if settled:
                result = build_result(observation, text_limit=None)
                observed = (result.get("observed_models") or [None])[-1]
                finality = "native" if observation.get("outcome_evidence") == "native" else "inferred"
                result["finality"] = finality
                changes.update(execution=final, result=result, observed_model=observed, finality=finality)
                if observation.get("outcome_error"):
                    changes["error"] = {"code": "runtime_error", "message": observation["outcome_error"]}
                if observation.get("stop_reason"):
                    changes["stop_reason"] = observation["stop_reason"]
                return changes
        if dead:
            changes.update(
                execution="unknown",
                error={"code": "terminal_exited", "message": "the terminal exited before the turn settled"},
            )
        return changes

    def _interrupt_delivered(self) -> bool:
        try:
            messages = self.store.messages(self.run_id)
        except Exception:
            return False
        return any(m.get("action") == "interrupt" and m.get("state") == "input_submitted" for m in messages)

    # -- entry --------------------------------------------------------------------------

    def run(self) -> None:
        for item in self.store.messages(self.run_id):
            if item.get("state") == "dispatching":
                self.store.message_state(
                    item.get("message_id") or item.get("id"),
                    "unknown",
                    "supervisor stopped during dispatch; input will not be replayed",
                )
        record = self.record()
        if record.get("delivery") == "accepted":
            # A cancellation queued before any input cancels locally here,
            # mirroring the controller entry: no target ownership is needed.
            self.process_messages(None)
            if self.record().get("execution") in SETTLED_EXECUTION:
                return
            self.admit()
            if self.record().get("execution") in SETTLED_EXECUTION:
                return
            base_seq = self.dispatch()
        else:
            base_seq = self.recover()
            binding = {
                k: record.get(k)
                for k in ("session_ref", "pane", "history_path", "native_id", "runtime")
                if record.get(k) is not None
            }
            if binding:
                self.binding = binding
        if self.record().get("execution") in SETTLED_EXECUTION:
            return
        self.observe(base_seq)


def settle_crash(store: Any, run_id: str, exc: BaseException) -> None:
    current = store.get(run_id)
    if current.get("execution") in SETTLED_EXECUTION:
        return
    code = getattr(exc, "code", "supervisor_error")
    message = getattr(exc, "message", None) or str(exc) or exc.__class__.__name__
    before_input = current.get("delivery") == "accepted"
    store.update(
        run_id,
        {
            "delivery": "rejected" if before_input else current.get("delivery"),
            "execution": "failed" if before_input else "unknown",
            "error": {"code": code, "message": message},
        },
    )


def _event_seq(event: Any) -> int | None:
    if isinstance(event, dict):
        seq = event.get("native_seq", event.get("seq"))
    else:
        seq = getattr(event, "seq", None)
    return seq if isinstance(seq, int) else None


def _assert_json_safe(value: Any, *, path: str = "$") -> None:
    """Reject values ``json.dumps`` would silently corrupt or emit invalidly.

    ``json.dumps`` with defaults stringifies non-string object keys
    (``{1: ...}`` becomes ``{"1": ...}``) and emits ``NaN``/``Infinity``
    literals that are not valid JSON. Both would persist or emit content
    whose identity differs from the native observation, and a later reader
    cannot recover the original keys. Anything reaching durable storage must
    therefore contain only ``str`` dict keys and finite JSON scalars. The
    raised ``ValueError`` carries only the structural path and type names,
    never raw payload values.
    """
    import math as _math

    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not _math.isfinite(value):
            raise ValueError(f"non-finite float at {path}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_json_safe(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"non-string dict key of type {type(key).__name__} at {path}")
            segment = key if (key.isidentifier() and len(key) <= 40) else f"key#{len(key)}"
            _assert_json_safe(item, path=f"{path}.{segment}")
        return
    raise ValueError(f"unsupported type {type(value).__name__} at {path}")


def _publish_new_events(store: Any, run_id: str, observation: dict, published: Any) -> bool:
    """Append unseen correlated events to the run log. Returns True if any.

    Uses the shared lossless ``history.project_event`` with
    ``text_limit=None`` so full text, message/call identity, roles, tool
    result/error/interaction/evidence records and Unicode survive. The native
    sequence is stored as ``native_seq`` (not ``seq``) to preserve the store's
    own ``seq`` pagination cursor; the poll generation rides alongside for
    stable identity. Dict-shaped observations are projected identically.

    Durability: each event is projected, strictly JSON-validated (finite
    values, ``str``-only dict keys, no silent conversion), then durably
    appended before its native identity is marked published. Identical
    repeats deduplicate (same content digest yields the same versioned store
    identity, so the store's ``UNIQUE(run_id, identity)`` converges); an
    expanded cumulative observation at the same native sequence carries a new
    digest and is preserved as a new row under the same ``native_seq`` /
    ``generation`` in the body. Storage cursors stay distinct from native
    sequence/generation. Unrepresentable events raise a sanitized
    ``ValueError`` explicitly instead of being silently dropped or converted;
    storage failures propagate without marking published so a later poll
    reconciles idempotently. ``published`` accepts the legacy ``set`` (seqs
    plus version keys) or a ``dict`` mapping ``(generation, seq)`` to content
    digest for compatibility.
    """
    import json as _json

    from agent_session_control.history import project_event

    advanced = False
    generation = str(observation.get("generation", "") or "")
    for event in observation.get("events", []):
        seq = _event_seq(event)
        if seq is None:
            raise ValueError("unrepresentable event without native sequence")
        try:
            projected = project_event(event, text_limit=None)
        except Exception as exc:
            raise ValueError(f"unrepresentable event at native_seq {seq}: {exc}") from exc
        native = projected.pop("seq", seq)
        body = {"kind": "activity", **projected, "native_seq": native, "generation": generation}
        try:
            _assert_json_safe(body)
            _json.dumps(body, ensure_ascii=False, allow_nan=False)
        except ValueError as exc:
            raise ValueError(f"unrepresentable event at native_seq {seq}: {exc}") from exc
        except Exception as exc:
            raise ValueError(f"unrepresentable event at native_seq {seq}: {type(exc).__name__}") from exc
        content_digest = _digest(projected)
        if generation:
            identity = f"activity:{generation}:{seq}:{content_digest[:16]}"
        else:
            identity = f"activity:{seq}:{content_digest[:16]}"
        if isinstance(published, dict):
            key = (generation, seq)
            if published.get(key) == content_digest:
                continue
            added = store.append(run_id, identity, body)
            published[key] = content_digest
            if added:
                advanced = True
        else:
            version_key = (generation, seq, content_digest)
            if version_key in published:
                continue
            added = store.append(run_id, identity, body)
            try:
                published.add(version_key)
            except Exception:
                pass
            try:
                published.add(seq)
            except Exception:
                pass
            if added:
                advanced = True
    return advanced


def _expired(spec: dict, record: dict) -> bool:
    import time as _time

    limit = int(spec.get("queue_timeout") or 0)
    return (
        bool(limit)
        and record.get("delivery") == "accepted"
        and _time.time() - float(record.get("created_at", 0)) > limit
    )


def _get_private(store: Any, run_id: str) -> dict:
    """Read the run with its spec; fall back to the public view on old fakes."""
    try:
        return store.get(run_id, private=True)
    except TypeError:
        record = store.get(run_id)
        if "spec" not in record and hasattr(store, "specs"):
            record["spec"] = copy_spec(store, run_id)
        return record


def copy_spec(store: Any, run_id: str) -> dict:
    import copy as _copy

    return _copy.deepcopy(store.specs[run_id])


def run_supervisor(store: Any, run_id: str, hooks: SupervisorHooks, **kwargs: Any) -> int:
    """Own ``run_id`` under its run lock and supervise it to settlement.

    Runs on one session dispatch while an older unsettled run holds that
    session's target lock; the wait is bounded by the spec's queue timeout.
    """
    root = getattr(store, "root", None)
    root_str = str(root) if root is not None else os.path.join("/tmp", "asc-fallback")
    with file_lock(root_str, "run:" + run_id) as owned:
        if not owned:
            return 0
        try:
            store.heartbeat(run_id, started=True)
        except Exception:
            pass
        try:
            record = _get_private(store, run_id)
        except Exception:
            return 1
        if record.get("execution") in SETTLED_EXECUTION:
            return 0
        spec = record.get("spec", {})
        target = spec.get("session_ref") or ("run:" + run_id)
        try:
            with file_lock(root_str, "target:" + target) as owns_target:
                if owns_target:
                    return _run_owned(store, run_id, hooks, kwargs)
                return _wait_for_target(store, run_id, target, hooks, kwargs)
        except Exception as exc:  # every exit leaves an honest terminal state
            try:
                settle_crash(store, run_id, exc)
            except Exception:
                pass
            return 1


def _run_owned(store: Any, run_id: str, hooks: SupervisorHooks, kwargs: dict) -> int:
    runner = SupervisorRunner(store, run_id, hooks, **kwargs)
    try:
        runner.run()
        return 0
    except Exception as exc:  # every exit leaves an honest terminal state
        try:
            settle_crash(store, run_id, exc)
        except Exception:
            pass
        return 1


def _wait_for_target(store: Any, run_id: str, target: str, hooks: SupervisorHooks, kwargs: dict) -> int:
    import time as _time

    root = getattr(store, "root", None)
    root_str = str(root) if root is not None else os.path.join("/tmp", "asc-fallback")
    while True:
        try:
            current = _get_private(store, run_id)
        except Exception:
            return 1
        if current.get("execution") in SETTLED_EXECUTION:
            return 0
        if _expired(current.get("spec", {}), current):
            try:
                # Queue expiry never dispatched: rejected intent, failed work.
                # "rejected" is a delivery state, never an execution state.
                store.update(
                    run_id,
                    {
                        "delivery": "rejected",
                        "execution": "failed",
                        "queue_reason": "earlier_run_on_session",
                        "error": {
                            "code": "queue_expired",
                            "message": "an earlier run kept the session past the queue timeout",
                        },
                    },
                )
            except Exception:
                pass
            return 0
        try:
            store.update(run_id, {"execution": "queued", "queue_reason": "earlier_run_on_session"})
        except Exception:
            pass
        try:
            store.heartbeat(run_id)
        except Exception:
            pass
        _time.sleep(BUSY_POLL)
        with file_lock(root_str, "target:" + target) as owns_target:
            if owns_target:
                return _run_owned(store, run_id, hooks, kwargs)


def default_hooks(root: str, namespace: str) -> SupervisorHooks:
    """Production hooks over the persisted session registry and owned backend.

    This is the detached-execution twin of the controller's in-process hooks:
    both are built by ``controller.build_hooks`` from the same persisted
    session registry, owned tmux backend, foundation runtime registry and
    SessKit observer. There is exactly one hook implementation, not a second
    executor. Custom host callbacks (identity prep, composer restore, native
    claims) are NOT reconstructible here; runs accepted with custom hooks
    require an explicit serializable factory (see ``LocalController``) and
    are rejected before acceptance otherwise.
    """
    from agent_session_control.controller import build_hooks

    return build_hooks(root=root, namespace=namespace)


def default_store(root: str, namespace: str = "default") -> Any:
    """Production store factory for the detached supervisor entry."""
    from agent_session_control.store import RunStore

    return RunStore(root, namespace=namespace)


def main(argv: list[str] | None = None) -> int:
    """Detached entry: ``python -m agent_session_control.supervisor <run> --root R --namespace N``."""
    parser = argparse.ArgumentParser(prog="agent-session-control-supervisor")
    parser.add_argument("run_id")
    parser.add_argument("--root", required=True)
    parser.add_argument("--namespace", default="default")
    parser.add_argument(
        "--hooks-factory",
        default="agent_session_control.supervisor:default_hooks",
        help="dotted path to a callable (root, namespace) -> SupervisorHooks",
    )
    parser.add_argument(
        "--store-factory",
        default="agent_session_control.supervisor:default_store",
        help="dotted path to a callable (root) -> operation store",
    )
    args = parser.parse_args(argv)

    def load(dotted: str) -> Any:
        module_name, _, attr = dotted.partition(":")
        try:
            module = importlib.import_module(module_name)
            return getattr(module, attr)
        except (ImportError, AttributeError) as exc:
            print(f"supervisor: cannot load {dotted}: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc

    hooks_factory = load(args.hooks_factory)
    store_factory = load(args.store_factory)
    try:
        store = store_factory(args.root, args.namespace)
    except TypeError:
        # Factories that predate the namespace parameter take only the root.
        store = store_factory(args.root)
    except TargetError as exc:
        print(f"supervisor: {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"supervisor: cannot open store: {exc}", file=sys.stderr)
        return 2
    try:
        # Factories accept (root, namespace, run_id); older ones take only
        # (root, namespace) and rebuild stateless equivalents instead.
        hooks = hooks_factory(args.root, args.namespace, args.run_id)
    except TypeError:
        hooks = hooks_factory(args.root, args.namespace)
    except TargetError as exc:
        print(f"supervisor: {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    return run_supervisor(store, args.run_id, hooks)


if __name__ == "__main__":
    raise SystemExit(main())

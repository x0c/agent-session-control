"""Best-effort session creation: ordered candidates, backoff, fallback.

``create_session_best_effort`` implements the ``session create`` auto mode
(``--runtime`` omitted): candidates are tried in registry order, each with
bounded attempts and exponential backoff, falling back to the next runtime on
failure. An optional first prompt is submitted through the same durable path
as ``run submit`` and confirmed inside a bounded window, so a runtime that
hosts a pane but cannot run the first turn (missing login, exhausted quota,
CLI crash) also falls back instead of reporting success.

Classification policy (observable in the returned attempt log):

- Caller-level errors (bad cwd/prompt, unauthorized, store incompatibility)
  abort the whole call: no runtime can fix them.
- ``unavailable``/``unsupported`` from a candidate is deterministic: the
  candidate is skipped once, never retried.
- Other create/submit failures are treated as transient: the same runtime is
  retried with exponential backoff, then the next candidate.
- A first turn reaching terminal ``failed`` inside the confirm window is not
  retried on the same runtime (a fresh session of that runtime would hit the
  same limit); the failed session is closed before falling back.
- Uncertainty is respected, never retried: an ``unknown`` run state and a
  turn still running when the window closes are both reported as started,
  because unknown delivery is not retry permission.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from agent_session_control.errors import ControlError, create_exhausted
from agent_session_control.models import SessionHandle, text_block
from agent_session_control.runtime import BUILTIN_RUNTIME_IDS, RuntimeRegistry, is_available

DEFAULT_ATTEMPTS_PER_RUNTIME = 2
DEFAULT_BACKOFF_BASE = 0.5
DEFAULT_BACKOFF_CAP = 4.0
DEFAULT_CONFIRM_WINDOW = 45.0

#: Error codes that abort every candidate: they describe caller intent or
#: the shared store, not one runtime's health.
_ABORT_CODES = frozenset({"usage_error", "not_found", "not_authorized", "idempotency_conflict", "store_too_new"})
#: Error codes that mark a candidate deterministically unusable: skip, no retry.
_SKIP_CODES = frozenset({"unavailable", "unsupported", "model_selection_unsupported"})
#: Terminal run states that count as started; ``failed`` falls back, and a
#: still-running turn (wait timeout) is reported as ``running``.
_STARTED_STATES = frozenset({"completed", "interrupted", "unknown"})


@dataclass(frozen=True)
class BestEffortAttempt:
    """One observable step of the best-effort attempt log."""

    runtime: str
    attempt: int  # 0 for availability skips, 1-based for real attempts
    outcome: str  # skipped_unavailable | create_failed | submit_failed | turn_failed | started
    code: str | None = None
    message: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    run_state: str | None = None
    backoff_seconds: float | None = None
    cleanup: str | None = None

    def to_dict(self) -> dict:
        return {
            "runtime": self.runtime,
            "attempt": self.attempt,
            "outcome": self.outcome,
            "code": self.code,
            "message": self.message,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "run_state": self.run_state,
            "backoff_seconds": self.backoff_seconds,
            "cleanup": self.cleanup,
        }


@dataclass(frozen=True)
class BestEffortResult:
    """A session that hosted (and, with a prompt, accepted its first turn)."""

    session: SessionHandle
    run_id: str | None = None
    request_id: str | None = None
    run_state: str | None = None
    final_response: str | None = None
    attempts: tuple[BestEffortAttempt, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        handle = self.session
        to_dict = getattr(handle, "to_dict", None)
        session_data = to_dict() if callable(to_dict) else {"id": getattr(handle, "id", None)}
        run_data = None
        if self.run_id is not None:
            run_data = {"run_id": self.run_id, "request_id": self.request_id, "state": self.run_state}
        return {
            "runtime": self.session.runtime,
            "session": session_data,
            "run": run_data,
            "final_response": self.final_response,
            "attempts": [entry.to_dict() for entry in self.attempts],
        }


def _candidate_order(controller: Any, runtimes: Sequence[str] | None) -> list[str]:
    """Explicit ordered candidates, or the controller's registry order."""
    if runtimes is not None:
        order: list[str] = []
        for item in runtimes:
            runtime_id = str(item).strip()
            if runtime_id and runtime_id not in order:
                order.append(runtime_id)
        if not order:
            raise ControlError("usage_error", "runtimes must list at least one runtime id.", 2)
        return order
    registry = getattr(controller, "profiles", None)
    ids = getattr(registry, "ids", None)
    if ids:
        return [str(runtime_id) for runtime_id in ids]
    return list(BUILTIN_RUNTIME_IDS)


def _availability(controller: Any, runtime: str) -> tuple[bool, str | None, str | None]:
    """``(available, code, message)``; code is set only when probing failed.

    A controller that declares ``capabilities["available"]`` is honored; the
    stock controller does not declare it, so availability falls back to the
    same executable-presence probe ``doctor`` uses (never login or quota).
    """
    try:
        capabilities = controller.capabilities(runtime)
    except ControlError as exc:
        if exc.code in ("not_found", "unsupported"):
            return False, exc.code, exc.message
        raise
    declared = capabilities.get("available")
    if declared is None:
        registry = getattr(controller, "profiles", None)
        declared = is_available(runtime, registry=registry if isinstance(registry, RuntimeRegistry) else None)
    if declared:
        return True, None, None
    return False, "unavailable", "runtime executable is not installed"


async def _close_quietly(controller: Any, session_id: str, *, actor: str) -> str:
    """Best-effought pane release; the label records what actually happened."""
    try:
        await controller.close_session(session_id, actor=actor)
    except ControlError as exc:
        return f"close_failed:{exc.code}"
    return "closed"


def _backoff_delay(backoff_base: float, backoff_cap: float, prior_failures: int) -> float:
    return min(backoff_base * (2 ** (prior_failures - 1)), backoff_cap)


async def create_session_best_effort(
    controller: Any,
    cwd: str,
    *,
    prompt: str | None = None,
    model: str | None = None,
    mcp_servers: Any = (),
    runtimes: Sequence[str] | None = None,
    attempts_per_runtime: int = DEFAULT_ATTEMPTS_PER_RUNTIME,
    backoff_base: float = DEFAULT_BACKOFF_BASE,
    backoff_cap: float = DEFAULT_BACKOFF_CAP,
    confirm_window: float = DEFAULT_CONFIRM_WINDOW,
    request_id: str | None = None,
    actor: str = "local-user",
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> BestEffortResult:
    """Host a session without pinning a runtime, retrying and falling back.

    See the module docstring for the classification policy. ``confirm_window``
    seconds watch the optional first turn for fast failure; ``0`` performs a
    single immediate check instead of sustained waiting.
    """
    if attempts_per_runtime < 1:
        raise ControlError("usage_error", "attempts_per_runtime must be at least 1.", 2)
    if backoff_base < 0 or confirm_window < 0:
        raise ControlError("usage_error", "backoff_base and confirm_window must not be negative.", 2)
    backoff_cap = max(backoff_cap, backoff_base)
    if prompt is not None and (not isinstance(prompt, str) or not prompt.strip()):
        raise ControlError("usage_error", "prompt must be a nonempty string.", 2)
    blocks = text_block(prompt) if prompt is not None else None
    pause = sleep or asyncio.sleep
    request = request_id or uuid.uuid4().hex
    candidates = _candidate_order(controller, runtimes)
    if not candidates:
        raise create_exhausted(
            "no candidate runtime is registered; pass runtimes explicitly",
            hint="Register at least one runtime profile or pass a nonempty runtimes order.",
        )
    attempts: list[BestEffortAttempt] = []
    for runtime in candidates:
        available, gate_code, gate_message = _availability(controller, runtime)
        if not available:
            attempts.append(BestEffortAttempt(runtime, 0, "skipped_unavailable", gate_code, gate_message))
            continue
        transient_failures = 0
        for attempt_no in range(1, attempts_per_runtime + 1):
            backoff_taken = None
            if attempt_no > 1:
                backoff_taken = _backoff_delay(backoff_base, backoff_cap, transient_failures)
                await pause(backoff_taken)
            try:
                handle = await controller.create(runtime, cwd, model=model, mcp_servers=mcp_servers, actor=actor)
            except ControlError as exc:
                if exc.code in _ABORT_CODES:
                    raise
                attempts.append(
                    BestEffortAttempt(
                        runtime,
                        attempt_no,
                        "create_failed",
                        exc.code,
                        exc.message,
                        backoff_seconds=backoff_taken,
                    )
                )
                if exc.code in _SKIP_CODES:
                    break  # deterministic for this runtime: next candidate
                transient_failures += 1
                continue
            run_id: str | None = None
            if blocks is not None:
                try:
                    run_id = await controller.start_turn(
                        handle.id, [blocks], request_id=request, on_busy="queue", actor=actor
                    )
                except ControlError as exc:
                    cleanup = await _close_quietly(controller, handle.id, actor=actor)
                    if exc.code in _ABORT_CODES:
                        raise
                    attempts.append(
                        BestEffortAttempt(
                            runtime,
                            attempt_no,
                            "submit_failed",
                            exc.code,
                            exc.message,
                            session_id=handle.id,
                            backoff_seconds=backoff_taken,
                            cleanup=cleanup,
                        )
                    )
                    if exc.code in _SKIP_CODES:
                        break
                    transient_failures += 1
                    continue
                try:
                    result = await controller.wait(run_id, timeout=confirm_window)
                except ControlError as exc:
                    if exc.code != "wait_timeout":
                        raise
                    attempts.append(
                        BestEffortAttempt(
                            runtime,
                            attempt_no,
                            "started",
                            session_id=handle.id,
                            run_id=run_id,
                            run_state="running",
                            backoff_seconds=backoff_taken,
                        )
                    )
                    return BestEffortResult(handle, run_id, request, "running", None, tuple(attempts))
                if result.state in _STARTED_STATES:
                    attempts.append(
                        BestEffortAttempt(
                            runtime,
                            attempt_no,
                            "started",
                            session_id=handle.id,
                            run_id=run_id,
                            run_state=result.state,
                            backoff_seconds=backoff_taken,
                        )
                    )
                    return BestEffortResult(
                        handle, run_id, request, result.state, result.final_response, tuple(attempts)
                    )
                error = result.error or {}
                cleanup = await _close_quietly(controller, handle.id, actor=actor)
                attempts.append(
                    BestEffortAttempt(
                        runtime,
                        attempt_no,
                        "turn_failed",
                        error.get("code"),
                        error.get("message") or "the first turn failed",
                        session_id=handle.id,
                        run_id=run_id,
                        backoff_seconds=backoff_taken,
                        cleanup=cleanup,
                    )
                )
                break  # a fresh session of this runtime would hit the same limit
            attempts.append(
                BestEffortAttempt(runtime, attempt_no, "started", session_id=handle.id, backoff_seconds=backoff_taken)
            )
            return BestEffortResult(handle, None, None, None, None, tuple(attempts))
    tried = any(entry.attempt >= 1 for entry in attempts)
    if not tried:
        raise create_exhausted(
            "no candidate runtime is installed; install one, then retry without pinning a runtime",
            hint=f"Install at least one of: {', '.join(candidates)} — and log in.",
            next_commands=["doctor --json"],
            details={"attempts": [entry.to_dict() for entry in attempts], "candidates": list(candidates)},
        )
    raise create_exhausted(
        "no candidate runtime could host a session" + (" and run the first turn" if blocks is not None else ""),
        hint="Inspect error.details.attempts: fix the failing runtime (login, quota, CLI version) "
        "or pin one explicitly with --runtime.",
        next_commands=["doctor --json"],
        details={"attempts": [entry.to_dict() for entry in attempts], "candidates": list(candidates)},
    )

"""Deterministic SessionController for ACP adapter tests.

Implements the locked controller port without terminals, storage or model
calls. Event shapes mirror the production engine: ``kind`` is one of
``activity``/``state``/``message`` and ``data`` carries the compact native
projection (``type``/``native_seq``/``text``/``tool``/``call_id``/
``result``/``error``). Prompt text selects scripted behavior so stdio tests
stay deterministic:

- ``SLOW <secs>``: settle after a delay (cancellation tests).
- ``FAIL``: stream a failure note, settle as failed with a structured error.
- ``UNKNOWN``: settle as unknown.
- ``TOOL``: emit tool_call start then a late result update, then complete.
- ``LARGE``: emit one ~360 KiB mixed Unicode assistant message.
- ``SHORT``: stream ``partial`` but settle with a longer ``final_response``
  (tail-fill coverage).
- ``DUP``: emit the same native snapshot twice (no-duplicate coverage).
- ``GEN``: same native seq under two generations (reset coverage).
- anything else: one assistant turn echoing the prompt.

History replay returns a fixed six-event fixture covering text, tool calls,
Unicode and a non-textual event.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from agent_session_control.errors import ControlError
from agent_session_control.models import ControlEvent, RunResult, SessionHandle


def _handle(session_id: str, runtime: str = "fake-test", cwd: str = "/tmp/asc-proj") -> SessionHandle:
    return SessionHandle(
        id=session_id, runtime=runtime, cwd=cwd, native_id=f"native-{session_id}", instance_id=f"inst-{session_id}"
    )


REPLAY_TEXT = "replayed assistant line with Unicode: \u6743\u9650\u540c\u6b65 \u2211 caf\u00e9 \U0001f680"


def replay_events(session_id: str) -> list[ControlEvent]:
    def ev(seq: int, kind: str, data: dict, evidence: str = "native") -> ControlEvent:
        return ControlEvent(seq=seq, kind=kind, session_id=session_id, data=data, evidence=evidence, generation="g0")

    return [
        ev(1, "activity", {"type": "user_message", "native_seq": 11, "text": "original question"}),
        ev(2, "activity", {"type": "assistant_message", "native_seq": 12, "text": REPLAY_TEXT}),
        ev(3, "activity", {"type": "tool_call", "native_seq": 13, "tool": "read", "call_id": "call_1"}),
        ev(
            4,
            "activity",
            {
                "type": "tool_result",
                "native_seq": 14,
                "tool": "read",
                "call_id": "call_1",
                "result": "ok",
                "text": "file contents here",
            },
        ),
        ev(5, "activity", {"type": "thinking", "native_seq": 15, "text": "quiet deliberation"}),
        ev(
            6,
            "activity",
            {
                "type": "assistant_message",
                "native_seq": 16,
                "text": "LARGE " + ("\u6d4b\u8bd5\u6570\u636e\u6d41" * 100),
            },
        ),
    ]


def scripted_prompt(kind: str, extra: str = "") -> str:
    stamp = f"{time.time_ns()}-{uuid.uuid4().hex[:6]}"
    return f"{kind} {stamp} {extra}".strip()


class FakeController:
    """In-memory port implementation with scripted runs."""

    def __init__(self, *, runtime: str = "fake-test", delay: float = 0.05) -> None:
        self.runtime = runtime
        self.delay = delay
        self.sessions: dict[str, SessionHandle] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.submitted: list[dict[str, Any]] = []
        self.closed: list[str] = []
        self._counter = 0

    # -- port ------------------------------------------------------------

    def capabilities(self, runtime: str) -> dict:
        return {
            "runtime": runtime,
            "queue": True,
            "steer": False,
            "interrupt": True,
            "interrupt_keys": ["Escape"],
            "model_selection": False,
            "mcp": "unsupported",
            "prompt_blocks": ["text", "resource"],
            "resume": {"supported": True},
            "load_history": {"supported": True},
            "close": True,
            "list": True,
        }

    async def create(
        self, runtime: str, cwd: str, *, model: str | None = None, mcp_servers: Any = (), actor: str = "local-user"
    ) -> SessionHandle:
        if runtime != self.runtime:
            raise ControlError("usage_error", f"FakeController serves {self.runtime!r}.", 2)
        if mcp_servers:
            raise ControlError("unsupported", "FakeController has no MCP path.", 2)
        self._counter += 1
        handle = _handle(f"ses_{self._counter:04d}", runtime=runtime, cwd=cwd)
        self.sessions[handle.id] = handle
        return handle

    async def resume(
        self, session_id: str, *, cwd: str | None = None, mcp_servers: Any = (), actor: str = "local-user"
    ) -> SessionHandle:
        handle = self.sessions.get(session_id)
        if handle is None:
            raise ControlError("not_found", f"Unknown session: {session_id}.", 3)
        return handle

    async def start_turn(
        self, session_id: str, prompt: list[dict], *, request_id: str, on_busy: str = "queue", actor: str = "local-user"
    ) -> str:
        if session_id not in self.sessions:
            raise ControlError("not_found", f"Unknown session: {session_id}.", 3)
        if on_busy not in ("queue", "steer", "reject"):
            raise ControlError("usage_error", f"unknown on_busy mode: {on_busy}", 2)
        texts = [b.get("text", "") for b in prompt if b.get("type") == "text"]
        brief = "\n".join(texts)
        self._counter += 1
        run_id = f"run_{self._counter:04d}"
        self.submitted.append(
            {
                "session_id": session_id,
                "blocks": list(prompt),
                "request_id": request_id,
                "on_busy": on_busy,
                "actor": actor,
                "run_id": run_id,
            }
        )
        self.runs[run_id] = {
            "session_id": session_id,
            "brief": brief,
            "settled": asyncio.Event(),
            "state": "running",
            "interrupted": False,
        }
        return run_id

    def watch(self, run_id: str, *, after: int = 0):  # AsyncIterator[ControlEvent]
        return self._watch_gen(run_id, after)

    def _mode(self, run: dict) -> str:
        brief = run["brief"]
        for mode in ("SLOW", "FAIL", "UNKNOWN", "TOOLFAIL", "TOOL", "LARGE", "SHORT", "DUP", "GEN", "WEIRD", "NOEVID"):
            if brief.startswith(mode):
                return mode
        return "normal"

    async def _watch_gen(self, run_id: str, after: int):
        run = self.runs.get(run_id)
        if run is None:
            raise ControlError("not_found", f"Unknown run: {run_id}.", 3)
        session_id = run["session_id"]
        brief = run["brief"]
        seq = after
        mode = self._mode(run)

        async def emit(kind: str, data: dict, evidence: str = "native", generation: str = "g1"):
            nonlocal seq
            seq += 1
            yield ControlEvent(
                seq=seq,
                kind=kind,
                session_id=session_id,
                run_id=run_id,
                data=data,
                evidence=evidence,
                generation=generation,
            )

        async def emit_text(native_seq: int, text: str, generation: str = "g1"):
            async for e in emit(
                "activity", {"type": "assistant_message", "native_seq": native_seq, "text": text}, "native", generation
            ):
                yield e

        if mode == "SLOW":
            try:
                secs = float(brief.split()[1])
            except (IndexError, ValueError):
                secs = 30.0
            steps = max(1, int(min(secs, 30.0) * 10))
            for _ in range(steps):
                await asyncio.sleep(0.1)
                if run["interrupted"]:
                    break
            if run["interrupted"]:
                async for e in emit("state", {"type": "state", "execution": "interrupted"}, "unknown"):
                    yield e
        elif mode in ("FAIL", "UNKNOWN"):
            async for e in emit("state", {"type": "state", "execution": "running"}, "unknown"):
                yield e
            if mode == "FAIL":
                async for e in emit_text(21, "FAILED scripted failure"):
                    yield e
        elif mode == "TOOLFAIL":
            async for e in emit(
                "activity", {"type": "tool_call", "native_seq": 71, "tool": "read", "call_id": f"{run_id}-c1"}
            ):
                yield e
            await asyncio.sleep(self.delay)
            async for e in emit(
                "activity",
                {
                    "type": "tool_result",
                    "native_seq": 72,
                    "tool": "read",
                    "call_id": f"{run_id}-c1",
                    "result": "error",
                    "text": "denied by policy",
                },
            ):
                yield e
        elif mode == "TOOL":
            async for e in emit(
                "activity", {"type": "tool_call", "native_seq": 31, "tool": "read", "call_id": f"{run_id}-c1"}
            ):
                yield e
            await asyncio.sleep(self.delay)
            async for e in emit(
                "activity",
                {
                    "type": "tool_result",
                    "native_seq": 32,
                    "tool": "read",
                    "call_id": f"{run_id}-c1",
                    "result": "ok",
                    "text": "file contents here",
                },
            ):
                yield e
            async for e in emit_text(33, f"done: {brief}"):
                yield e
        elif mode == "LARGE":
            async for e in emit_text(41, "LARGE " + ("\u6d4b\u8bd5\U0001f680" * 40000)):
                yield e
        elif mode == "SHORT":
            async for e in emit_text(51, "partial"):
                yield e
        elif mode == "DUP":
            # Identical native snapshots polled twice must surface once.
            async for e in emit_text(61, "same snapshot text"):
                yield e
            async for e in emit_text(61, "same snapshot text"):
                yield e
        elif mode == "GEN":
            async for e in emit_text(71, "same native message", "g1"):
                yield e
            async for e in emit_text(71, "same native message", "g2"):
                yield e
        elif mode == "NOEVID":
            async for e in emit("state", {"type": "state", "execution": "running"}, "unknown"):
                yield e
        else:
            await asyncio.sleep(self.delay)
            if run["interrupted"]:
                async for e in emit("state", {"type": "state", "execution": "interrupted"}, "unknown"):
                    yield e
            else:
                async for e in emit_text(91, f"done: {brief}"):
                    yield e
        if not run["settled"].is_set():
            if run["interrupted"]:
                run["state"] = "interrupted"
            elif mode in ("FAIL", "TOOLFAIL"):
                run["state"] = "failed"
            elif mode == "UNKNOWN":
                run["state"] = "unknown"
            else:
                run["state"] = "completed"
            run["settled"].set()

    async def wait(self, run_id: str, *, timeout: float | None = None) -> RunResult:
        run = self.runs.get(run_id)
        if run is None:
            raise ControlError("not_found", f"Unknown run: {run_id}.", 3)
        if timeout is None:
            await run["settled"].wait()
        else:
            try:
                await asyncio.wait_for(run["settled"].wait(), timeout)
            except asyncio.TimeoutError as exc:
                raise ControlError("wait_timeout", "the wait deadline passed", 6, details={"run_id": run_id}) from exc
        state = run["state"]
        brief = run["brief"]
        if state == "failed":
            return RunResult(
                run_id=run_id,
                state="failed",
                evidence="native",
                error={"code": "upstream_failure", "message": "scripted failure"},
            )
        if state == "unknown":
            return RunResult(run_id=run_id, state="unknown", evidence="unknown")
        if state == "interrupted":
            return RunResult(run_id=run_id, state="interrupted", evidence="unknown")
        final = f"partial + remainder tail text {brief}" if self._mode(run) == "SHORT" else f"done: {brief}"
        stop = "bogus_reason" if self._mode(run) == "WEIRD" else "end_turn"
        evidence = "unknown" if self._mode(run) == "NOEVID" else "native"
        return RunResult(run_id=run_id, state="completed", final_response=final, stop_reason=stop, evidence=evidence)

    async def interrupt(self, run_id: str, *, actor: str = "local-user") -> None:
        run = self.runs.get(run_id)
        if run is None:
            raise ControlError("not_found", f"Unknown run: {run_id}.", 3)
        run["interrupted"] = True
        if run["state"] == "running":
            run["state"] = "interrupted"
            run["settled"].set()

    async def close_session(self, session_id: str, *, actor: str = "local-user") -> None:
        if session_id not in self.sessions:
            raise ControlError("not_found", f"Unknown session: {session_id}.", 3)
        for _run_id, run in self.runs.items():
            if run["session_id"] == session_id and not run["settled"].is_set():
                run["interrupted"] = True
                run["state"] = "interrupted"
                run["settled"].set()
        del self.sessions[session_id]
        self.closed.append(session_id)

    async def list_sessions(self, *, cwd: str | None = None, cursor: str | None = None) -> dict:
        items = [
            {
                "session_id": h.id,
                "runtime": h.runtime,
                "cwd": h.cwd,
                "native_id": h.native_id,
                "instance_id": h.instance_id,
                "status": "hosted",
                "pane_liveness": "unknown",
            }
            for h in self.sessions.values()
            if cwd is None or h.cwd == cwd
        ]
        return {"sessions": items, "next_cursor": None}

    async def load_history(self, session_id: str) -> list[ControlEvent]:
        if session_id not in self.sessions:
            raise ControlError("not_found", f"Unknown session: {session_id}.", 3)
        return replay_events(session_id)

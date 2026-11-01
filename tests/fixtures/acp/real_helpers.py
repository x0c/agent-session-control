"""Host-side doubles for adapter tests against the real LocalController.

``ScriptedTerminal`` stands in for tmux hosting (no real panes, user
sessions or model calls are touched) while the production store, session
registry, runtime plans and supervision paths run for real. ``mcp_profile``
is a host-supplied runtime profile with a verified per-session MCP channel,
as the contract allows hosts to supply.

``CannedHistoryObserver`` returns one deterministic SessKit-shaped snapshot
(large assistant text, tool call/result/error, thinking) so baseline replay
tests never depend on real native histories, logins or model quota.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from agent_session_control.runtime import (
    MCP_CHANNEL_ENV,
    RuntimeProfile,
)

MCP_ENV_VAR = "ASC_TEST_MCP_CONFIG"
MCP_SECRET = "s3cr3t-mcp-token-not-a-real-credential"

#: Registry marker the canned observer answers for; bound by the fixture, not
#: by native discovery.
CANNED_HISTORY_PATH = "canned://acp-baseline-history"

#: Sentinels proving full delivery (never truncated, never lost).
LARGE_SENTINEL = "BASELINE_LARGE_SENTINEL_9f3c"
TOOL_OK_SENTINEL = "BASELINE_TOOL_OK_51ad"
TOOL_ERR_SENTINEL = "BASELINE_TOOL_ERR_d2e7"
RAW_IN_SENTINEL = "BASELINE_RAW_IN_7c1e"
RAW_OUT_SENTINEL = "BASELINE_RAW_OUT_3b9f"
RAW_ONLY_IN_SENTINEL = "BASELINE_RAW_ONLY_IN_6a2d"
RAW_ONLY_ERR_SENTINEL = "BASELINE_RAW_ONLY_ERR_8d2a"
LONG_RAW_SENTINEL = "BASELINE_LONG_RAW_4f6c"
DEEP_RAW_SENTINEL = "BASELINE_DEEP_RAW_1a2b"
FALSY_RAW_SENTINEL = "BASELINE_FALSY_RAW_9c4d"


def canned_large_text() -> str:
    """Deterministic >120 KiB assistant payload ending in a sentinel."""
    return "baseline replay payload " + ("0123456789abcdef=" * 8200) + LARGE_SENTINEL


def _encode_env(servers: tuple) -> str:
    import json as _json

    return _json.dumps(
        [
            {
                "name": s["name"],
                "command": s["command"],
                "args": list(s.get("args") or []),
                "env": dict(s.get("env") or {}),
            }
            for s in servers
        ],
        ensure_ascii=False,
    )


def mcp_profile() -> RuntimeProfile:
    return RuntimeProfile(
        id="t",
        display_name="Test",
        executable="true",
        interrupt_keys=("Escape",),
        supports_mcp=True,
        mcp_channel=MCP_CHANNEL_ENV,
        mcp_target=MCP_ENV_VAR,
        mcp_encode=_encode_env,
    )


def stdio_mcp_servers() -> list[dict]:
    return [
        {
            "name": "fs",
            "command": "/usr/bin/mcp-fs",
            "args": ["--stdio"],
            "env": [{"name": "MCP_TOKEN", "value": MCP_SECRET}],
        }
    ]


class ScriptedTerminal:
    """Records hosting calls; never touches tmux or user sessions.

    Readiness never arrives (models a pane that never reaches its composer),
    which parks the in-process supervisor before dispatch and keeps version
    checks deterministic. Threads are daemon-owned and die with the process.
    """

    def __init__(self) -> None:
        self.hosted: list[dict[str, Any]] = []
        self.closed_panes: list[str] = []
        self.ready_calls: list[str | None] = []

    def new_ident(self) -> str:
        return f"ident-{len(self.hosted)}"

    def host(self, argv: tuple, *, runtime: str, cwd: str, env: dict, ident: str) -> str:
        self.hosted.append({"argv": tuple(argv), "runtime": runtime, "cwd": cwd, "env": dict(env), "ident": ident})
        return f"pane-{len(self.hosted)}"

    def liveness(self, pane: str | None) -> str:
        # The double always considers its own recorded panes usable; it never
        # inspects real terminals.
        return "alive"

    def wait_ready(self, pane: str | None, markers: tuple = (), timeout: float | None = None) -> None:
        import threading as _threading

        self.ready_calls.append(pane)
        _threading.Event().wait(60.0)

    def capture(self, pane: str | None) -> str:
        return ""

    def close(self, pane: str | None) -> bool:
        self.closed_panes.append(str(pane))
        return True


class UnstoppableTerminal(ScriptedTerminal):
    """Refuses release while reporting the pane alive: close must stay uncertain."""

    def close(self, pane: str | None) -> bool:
        self.closed_panes.append(str(pane))
        return False


def _native(*, seq: int, type: str, **fields: Any) -> Any:
    """One genuine native SessKit event (frozen dataclass, like real histories)."""
    from sesskit.models import ActivityEvent, Evidence  # pyrefly: ignore[missing-import]

    evidence = fields.pop("evidence", None) or Evidence(origin="native")
    return ActivityEvent(seq=seq, type=type, evidence=evidence, **fields)


def _ok_result() -> Any:
    """Genuine native ok outcome (dataclass, losslessly projected to a dict)."""
    from sesskit.models import Evidence, ToolResultOutcome  # pyrefly: ignore[missing-import]

    return ToolResultOutcome(status="ok", evidence=Evidence(origin="native"))


def _native_error(*, kind: str, message: str) -> Any:
    """Genuine native error record (dataclass, never invented by the adapter)."""
    from sesskit.models import AgentError, Evidence  # pyrefly: ignore[missing-import]

    return AgentError(kind=kind, message=message, evidence=Evidence(origin="native"))


def canned_long_raw() -> dict:
    """Deterministic long nested raw payload (~8 KiB) proving no truncation."""
    return {
        "head": "nested raw payload",
        "levels": {"a": {"b": {"c": [f"item-{i:04d}" for i in range(200)]}}},
        "blob": "r=" * 4000 + LONG_RAW_SENTINEL,
    }


def canned_deep_raw(depth: int = 30) -> dict:
    """Valid JSON nested ``depth`` levels deep with a bottom sentinel.

    Proves the adapter imposes no depth cap: representable nesting of any
    depth passes through fully.
    """
    node: dict = {"leaf": DEEP_RAW_SENTINEL, "depth": 0}
    for level in range(1, depth + 1):
        node = {"depth": level, "next": node, "sibling": [level, str(level), None, True]}
    return node


def canned_falsy_raw() -> dict:
    """Valid JSON falsy values (false/zero/empty/nested) with a sentinel."""
    return {
        "flag": False,
        "count": 0,
        "name": "",
        "items": [],
        "opts": {},
        "nested": {"a": [0, False, "", None], "b": {"c": 0}},
        "marker": FALSY_RAW_SENTINEL,
    }


def canned_snapshot_events(large_text: str | None = None) -> tuple:
    """Deterministic native history: user, large assistant, tools, thinking.

    Tool events are genuine SessKit dataclasses carrying raw input/output
    payloads (including a raw-only call/result pair with no text) so replay
    and streaming tests prove full-fidelity delivery, not just text/status.
    """
    large = large_text if large_text is not None else canned_large_text()
    return (
        _native(seq=11, type="user_message", text="baseline original question"),
        _native(seq=12, type="assistant_message", text=large, message_id="m-base-12", turn_id="t-base"),
        _native(
            seq=13,
            type="tool_call",
            name="read",
            call_id="call_base_1",
            raw_input={"path": "/proj/notes.txt", "mode": "read", "marker": RAW_IN_SENTINEL},
        ),
        _native(
            seq=14,
            type="tool_result",
            name="read",
            call_id="call_base_1",
            result=_ok_result(),
            raw_output={"body": f"baseline file contents {RAW_OUT_SENTINEL}", "bytes": 41, "opts": canned_long_raw()},
            text=f"baseline file contents here {TOOL_OK_SENTINEL}",
        ),
        _native(
            seq=15,
            type="tool_result",
            name="exec",
            call_id="call_base_2",
            error=_native_error(kind="runtime_error", message="baseline denied by policy"),
            text=f"baseline denied by policy {TOOL_ERR_SENTINEL}",
        ),
        _native(seq=16, type="thinking", text="baseline quiet deliberation"),
        _native(
            seq=17, type="assistant_message", text="baseline closing line", message_id="m-base-17", turn_id="t-base"
        ),
        # Raw-only pair: no text anywhere, so only raw_input/raw_output and
        # the native error record can carry the evidence.
        _native(
            seq=18,
            type="tool_call",
            name="exec",
            call_id="call_base_3",
            raw_input={"argv": ["ls", "-la", "/proj"], "marker": RAW_ONLY_IN_SENTINEL},
        ),
        _native(
            seq=19,
            type="tool_result",
            name="exec",
            call_id="call_base_3",
            error=_native_error(kind="runtime_error", message=f"raw-only denial {RAW_ONLY_ERR_SENTINEL}"),
        ),
        # Deep + falsy pair: 30-level valid JSON input (no depth cap) and
        # falsy valid JSON output (false/zero/empty/nested preserved exactly).
        _native(
            seq=20,
            type="tool_call",
            name="read",
            call_id="call_base_4",
            raw_input=canned_deep_raw(30),
        ),
        _native(
            seq=21,
            type="tool_result",
            name="read",
            call_id="call_base_4",
            result=_ok_result(),
            raw_output=canned_falsy_raw(),
            text="falsy payload delivered",
        ),
    )


class CannedHistoryObserver:
    """Deterministic observer answering only the canned registry path.

    ``load_snapshot`` returns the fixed baseline snapshot for any session;
    the caller binds :data:`CANNED_HISTORY_PATH` explicitly (no native scan,
    no login, no quota). ``observe`` reports no recording so a parked
    supervisor never settles from this observer.
    """

    def __init__(self, large_text: str | None = None) -> None:
        self._large = large_text if large_text is not None else canned_large_text()
        self.snapshots = 0

    def load_snapshot(self, session: dict) -> Any:
        self.snapshots += 1
        return SimpleNamespace(
            state="available",
            events=tuple(canned_snapshot_events(self._large)),
            outcome=None,
            cursor="",
            generation="g9",
        )

    def baseline_seq(self, session: dict) -> int:
        return -1

    def open_reader(self, session: dict) -> Any:
        reader = SimpleNamespace(closed=False)
        reader.close = lambda: setattr(reader, "closed", True)
        return reader

    def observe(self, reader: Any, run_id: str, baseline_seq: int, events: dict, runtime: str = "") -> tuple:
        from agent_session_control.history import ActivityObservation

        return ActivityObservation(recorded=False), False


def bind_canned_history(controller: Any, session_id: str, *, actor: str = "acp-test-fixture") -> dict:
    """Bind the canned snapshot path onto one controlled session row."""
    row = controller.sessions.get(session_id)
    row = {**row, "history_path": CANNED_HISTORY_PATH}
    return controller.sessions.put(row, actor=actor, action="bind_canned_history")


def start_canned_binder(controller: Any, stop: Any, *, interval: float = 0.05) -> Any:
    """Background-bind the canned path onto every hosted session (stdio child).

    The stdio child cannot know session ids ahead of serving; the binder
    polls the production registry and binds rows created by ``session/new``
    so ``session/load`` replays deterministically. Daemon-owned, dies with
    the child.
    """
    import threading as _threading

    def loop() -> None:
        while not stop.is_set():
            try:
                for row in controller.sessions.list():
                    if not row.get("history_path"):
                        bind_canned_history(controller, row["id"])
            except Exception:
                pass
            stop.wait(interval)

    thread = _threading.Thread(target=loop, daemon=True, name="canned-binder")
    thread.start()
    return thread

"""Host-side doubles for adapter tests against the real LocalController.

``ScriptedTerminal`` stands in for tmux hosting (no real panes, user
sessions or model calls are touched) while the production store, session
registry, runtime plans and supervision paths run for real. ``mcp_profile``
is a host-supplied runtime profile with a verified per-session MCP channel,
as the contract allows hosts to supply.
"""

from __future__ import annotations

from typing import Any

from agent_session_control.runtime import (
    MCP_CHANNEL_ENV,
    RuntimeProfile,
)

MCP_ENV_VAR = "ASC_TEST_MCP_CONFIG"
MCP_SECRET = "s3cr3t-mcp-token-not-a-real-credential"


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

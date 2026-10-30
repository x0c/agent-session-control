"""Adapter tests against the real production controller and store.

``LocalController`` with its real session registry, operation store and
runtime plans runs here; only terminal hosting is doubled
(``ScriptedTerminal`` records host calls instead of touching tmux, user
sessions or model APIs). This exercises MCP pass-through/refusal, durable
acceptance, interrupt delivery, wait-timeout honesty and close/release
through production code paths. Run settlement streaming stays with the
deterministic fake in ``test_acp_agent.py``; driving a live terminal turn to
settle would spend quota and is engine-owned acceptance.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

FIX = Path(__file__).parent / "fixtures" / "acp"
sys.path.insert(0, str(FIX))

from real_helpers import (  # noqa: E402  # pyrefly: ignore[missing-import]
    MCP_ENV_VAR,
    MCP_SECRET,
    ScriptedTerminal,
    UnstoppableTerminal,
    mcp_profile,
    stdio_mcp_servers,
)

from agent_session_control.acp import ControlAgent  # noqa: E402
from agent_session_control.controller import LocalController  # noqa: E402


def make_real(tmp_path: Path, **kwargs: Any) -> tuple[LocalController, ScriptedTerminal]:
    terminal = ScriptedTerminal()
    controller = LocalController(
        root=tmp_path / "root",
        namespace="t",
        runtime_profiles={"t": mcp_profile()},
        terminal=terminal,
        spawn=False,
        **kwargs,
    )
    return controller, terminal


def test_real_create_passes_mcp_privately_to_host(tmp_path: Path) -> None:
    async def go() -> None:
        controller, terminal = make_real(tmp_path)
        agent = ControlAgent(controller, "t")
        resp = await agent.new_session(str(tmp_path), mcp_servers=stdio_mcp_servers())
        handle_id = resp.session_id
        assert handle_id.startswith("ses_")
        assert len(terminal.hosted) == 1
        env = terminal.hosted[0]["env"]
        assert MCP_ENV_VAR in env
        assert json.loads(env[MCP_ENV_VAR]) == [
            {"name": "fs", "command": "/usr/bin/mcp-fs", "args": ["--stdio"], "env": {"MCP_TOKEN": MCP_SECRET}}
        ]

    asyncio.run(go())


def test_real_mcp_strict_validation_no_secret_echo(tmp_path: Path) -> None:
    from acp import RequestError

    async def go() -> None:
        controller, _ = make_real(tmp_path)
        agent = ControlAgent(controller, "t")
        bad = [
            # Relative command, bad args, malformed env, unknown keys and
            # duplicates are the foundation boundary's authoritative refusal.
            [{"name": "fs", "command": "relative/mcp", "env": {"TOKEN": MCP_SECRET}}],
            [{"name": "fs", "command": "/usr/bin/mcp", "args": ["ok", 3], "env": {"TOKEN": MCP_SECRET}}],
            [{"name": "fs", "command": "/usr/bin/mcp", "env": [{"name": "TOKEN", "value": MCP_SECRET}, {"oops": 1}]}],
            [{"name": "fs", "command": "/usr/bin/mcp", "url": "https://x", "env": {"TOKEN": MCP_SECRET}}],
            [{"name": "dup", "command": "/a", "env": {}}, {"name": "dup", "command": "/b", "env": {}}],
        ]
        for entry in bad:
            with pytest.raises(RequestError) as exc:
                await agent.new_session(str(tmp_path), mcp_servers=entry)
            assert exc.value.code == -32602
            assert MCP_SECRET not in json.dumps(exc.value.data or {})

    asyncio.run(go())


def test_real_mcp_validation_never_echoes_secrets(tmp_path: Path) -> None:
    from acp import RequestError

    async def go() -> None:
        controller, _ = make_real(tmp_path)
        agent = ControlAgent(controller, "t")
        bad = [
            [{"name": "fs", "command": "relative/mcp", "env": [{"name": "T", "value": MCP_SECRET}]}],
            [
                {
                    "name": "fs",
                    "command": "/usr/bin/mcp",
                    "env": [{"name": "T", "value": MCP_SECRET}, {"name": "T", "value": "x"}],
                }
            ],
            [{"name": "a", "command": "/a", "env": []}, {"name": "a", "command": "/b", "env": []}],
        ]
        for entry in bad:
            with pytest.raises(RequestError) as exc:
                await agent.new_session(str(tmp_path), mcp_servers=entry)
            assert exc.value.code == -32602
            assert MCP_SECRET not in json.dumps(exc.value.data or {})

    asyncio.run(go())


def test_real_start_accepts_interrupt_delivers_wait_timeout_honest(tmp_path: Path) -> None:
    async def go() -> None:
        controller, terminal = make_real(tmp_path)
        agent = ControlAgent(controller, "t")
        resp = await agent.new_session(str(tmp_path), mcp_servers=[])
        handle_id = resp.session_id
        assert (await agent.resume_session(handle_id, str(tmp_path))) is not None
        run_id = await controller.start_turn(
            handle_id, [{"type": "text", "text": "hello"}], request_id="r1", actor="acp-test"
        )
        # Interrupt delivery goes through the real store; the message lands.
        for _ in range(100):
            if terminal.ready_calls:
                break
            await asyncio.sleep(0.05)
        assert terminal.ready_calls, "supervisor must park in readiness before interrupt"
        await controller.interrupt(run_id, actor="acp-test")
        queued = [m for m in controller.run_store.messages(run_id) if m.get("action") == "interrupt"]
        assert queued, "interrupt message must be durably recorded"
        from agent_session_control.errors import ControlError

        with pytest.raises(ControlError) as exc:
            await controller.wait(run_id, timeout=0.2)
        assert exc.value.code == "wait_timeout"

    asyncio.run(go())


def test_real_store_holds_no_mcp_secrets(tmp_path: Path) -> None:
    async def go() -> None:
        controller, terminal = make_real(tmp_path)
        agent = ControlAgent(controller, "t")
        resp = await agent.new_session(str(tmp_path), mcp_servers=stdio_mcp_servers())
        run_id = await controller.start_turn(
            resp.session_id, [{"type": "text", "text": "hi"}], request_id="r2", actor="acp-test"
        )
        record = controller.run_store.get(run_id, private=True)
        assert MCP_SECRET not in json.dumps(record)
        assert terminal.hosted  # the secret only ever reached the host env

    asyncio.run(go())


def test_real_failed_close_retains_attachment_and_live_work(tmp_path: Path) -> None:
    """F3: a failed/uncertain close keeps the binding and the turn state so
    cancel and prompt can still address live work."""
    from acp import RequestError

    async def go() -> None:
        terminal = UnstoppableTerminal()
        controller = LocalController(
            root=tmp_path / "root",
            namespace="t",
            runtime_profiles={"t": mcp_profile()},
            terminal=terminal,
            spawn=False,
        )
        agent = ControlAgent(controller, "t")
        handle = await agent.new_session(str(tmp_path), mcp_servers=[])
        with pytest.raises(RequestError) as exc:
            await agent.close_session(handle.session_id)
        assert exc.value.code == -32603
        row = controller.sessions.get(handle.session_id)
        assert row["status"] == "hosted" and row["pane"] is not None
        assert handle.session_id in agent._sessions
        # Live work is still addressable: cancel targets the actual run.
        task = asyncio.create_task(agent.prompt(handle.session_id, [{"type": "text", "text": "hi"}]))
        await asyncio.sleep(0.5)
        await agent.cancel(handle.session_id)
        run_id = agent._turns[handle.session_id]["run_id"]
        assert run_id is not None
        queued = [m for m in controller.run_store.messages(run_id) if m.get("action") == "interrupt"]
        assert queued, "interrupt message must be durably recorded"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(go())


def test_real_unknown_session_never_auto_resumes(tmp_path: Path) -> None:
    from acp import RequestError

    async def go() -> None:
        controller, _ = make_real(tmp_path)
        agent = ControlAgent(controller, "t")
        with pytest.raises(RequestError) as exc:
            await agent.prompt("ses_missing", [{"type": "text", "text": "hi"}])
        assert exc.value.code == -32002
        with pytest.raises(RequestError) as exc:
            await agent.load_session(str(tmp_path), "ses_missing")
        assert exc.value.code == -32002
        await agent.close_session("ses_missing")  # idempotent success

    asyncio.run(go())


def test_real_load_missing_history_is_resource_not_found(tmp_path: Path) -> None:
    from acp import RequestError

    async def go() -> None:
        controller, _ = make_real(tmp_path)
        agent = ControlAgent(controller, "t")
        resp = await agent.new_session(str(tmp_path), mcp_servers=[])
        with pytest.raises(RequestError) as exc:
            await agent.load_session(str(tmp_path), resp.session_id)
        assert exc.value.code == -32002

    asyncio.run(go())


def test_real_list_and_close_lifecycle(tmp_path: Path) -> None:
    async def go() -> None:
        controller, _ = make_real(tmp_path)
        agent = ControlAgent(controller, "t")
        resp = await agent.new_session(str(tmp_path), mcp_servers=[])
        handle_id = resp.session_id
        page = await agent.list_sessions()
        assert [s.session_id for s in page.sessions] == [handle_id]
        closed = await agent.close_session(handle_id)
        assert (closed.field_meta or {}).get("cancelConfirmed") is True
        assert controller.sessions.get(handle_id)["pane"] is None

    asyncio.run(go())

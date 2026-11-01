"""ACP stdio MCP baseline gate plus real-stdio production-controller tests.

Gate: the stable v1 baseline requires every served agent to support stdio
MCP (``session/new`` carries ``mcpServers``); HTTP/SSE are optional. The
serve entry (``serve_stdio`` and the ``acp`` CLI) refuses a selected
runtime/controller whose ``capabilities(runtime)["mcp"] != "supported"``
BEFORE the SDK owns stdout. Direct ``ControlAgent`` use stays available for
SDK/CLI control.

Real stdio: ``tests/fixtures/acp/run_real_agent.py`` serves the production
``LocalController`` (real session registry, operation store, runtime plans
on a disposable root) with only terminal hosting doubled
(``ScriptedTerminal``: no tmux, user sessions, logins, quota or model calls)
and driven by the official ``acp`` client SDK. Wire payloads are validated
against the pinned released schema ``schema-v1.27.0``, never the SDK draft.

The production load/live/final path projects losslessly
(``controller.py`` load_history, ``supervisor.py`` live publishing and the
terminal ``build_result`` all use ``project_event(text_limit=None)`` /
``build_result(text_limit=None)``), so the >120 KiB replay/stream assertions
below verify that path end to end.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

FIX = Path(__file__).parent / "fixtures" / "acp"
sys.path.insert(0, str(FIX))

from fake_controller import FakeController  # noqa: E402  # pyrefly: ignore[missing-import]
from real_helpers import (  # noqa: E402  # pyrefly: ignore[missing-import]
    DEEP_RAW_SENTINEL,
    FALSY_RAW_SENTINEL,
    LARGE_SENTINEL,
    LONG_RAW_SENTINEL,
    RAW_IN_SENTINEL,
    RAW_ONLY_ERR_SENTINEL,
    RAW_ONLY_IN_SENTINEL,
    RAW_OUT_SENTINEL,
    TOOL_ERR_SENTINEL,
    TOOL_OK_SENTINEL,
    canned_deep_raw,
    canned_falsy_raw,
    canned_large_text,
    mcp_profile,
)

from agent_session_control.acp import (  # noqa: E402
    ControlAgent,
    require_stdio_mcp_baseline,
    serve_stdio,
)
from agent_session_control.controller import LocalController  # noqa: E402
from agent_session_control.errors import ControlError  # noqa: E402

SCHEMA = json.loads((FIX / "schema-v1.27.0.json").read_text())


def validate(def_name: str, instance: object) -> None:
    """Validate one wire payload against the released stable schema."""
    import jsonschema  # pyrefly: ignore[untyped-import]
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    resource = Resource.from_contents(SCHEMA, default_specification=DRAFT202012)
    registry = Registry().with_resource("urn:acp-schema-v1-27-0", resource)
    validator = jsonschema.Draft202012Validator(
        {"$ref": f"urn:acp-schema-v1-27-0#/$defs/{def_name}"}, registry=registry
    )
    validator.validate(instance)


def dump(model: Any) -> dict:
    """Wire form: the SDK serializes with exclude_none/exclude_defaults."""
    return model.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_defaults=True)


class TClient:
    """Minimal official-SDK client: records session/update notifications."""

    def __init__(self) -> None:
        self.updates: list[tuple[str, object]] = []
        self.conn: object | None = None

    def on_connect(self, conn: object) -> None:
        self.conn = conn

    async def session_update(self, session_id: str, update: object, **kwargs: object) -> None:
        self.updates.append((session_id, update))


def any_client(client: TClient) -> Any:
    return client


class ConnStub:
    """In-process stand-in for AgentSideConnection.session_update."""

    def __init__(self) -> None:
        self.updates: list[tuple[str, object]] = []

    async def session_update(self, session_id: str, update: object, **kwargs: object) -> None:
        self.updates.append((session_id, update))


class RefusingPort(FakeController):
    """Backend without the stdio baseline: must never be served."""

    def capabilities(self, runtime: str) -> dict:
        return {**super().capabilities(runtime), "mcp": "unsupported"}


class ExplodingPort(FakeController):
    """Failed probe verifies nothing: serve must fail closed."""

    def capabilities(self, runtime: str) -> dict:
        raise RuntimeError("probe failed")


class SplitPort(FakeController):
    """One runtime baseline-conformant, another not: no union across runtimes."""

    def capabilities(self, runtime: str) -> dict:
        caps = super().capabilities(runtime)
        return {**caps, "mcp": "supported" if runtime == "good" else "unsupported"}


def child_env() -> dict:
    from acp import default_environment

    env = dict(default_environment())
    env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    src = str(Path(__file__).parent.parent / "src")
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


# -- serve entry gate -----------------------------------------------------------


def test_gate_accepts_baseline_backend() -> None:
    controller = FakeController()
    assert controller.capabilities("fake-test")["mcp"] == "supported"
    require_stdio_mcp_baseline(controller, "fake-test")
    asyncio.run(serve_stdio_gate_check_only(controller, "fake-test"))


async def serve_stdio_gate_check_only(controller: Any, runtime: str) -> None:
    """The gate itself, without starting the SDK transport."""
    require_stdio_mcp_baseline(controller, runtime)


def test_gate_refuses_unsupported_before_serving(capsys: pytest.CaptureFixture[str]) -> None:
    async def go() -> None:
        port = RefusingPort()
        with pytest.raises(ControlError) as exc:
            require_stdio_mcp_baseline(port, "fake-test")
        assert exc.value.code == "unsupported"
        assert "fake-test" in exc.value.message and "stdio" in exc.value.message.lower()
        # The serve entry fails the same way, before the SDK owns stdout.
        with pytest.raises(ControlError) as exc2:
            await serve_stdio(port, "fake-test")
        assert exc2.value.code == "unsupported"

    asyncio.run(go())
    assert capsys.readouterr().out == ""


def test_gate_refuses_failed_probe() -> None:
    with pytest.raises(ControlError) as exc:
        require_stdio_mcp_baseline(ExplodingPort(), "fake-test")
    assert exc.value.code == "unsupported"


def test_gate_refuses_unknown_runtime(tmp_path: Path) -> None:
    controller = LocalController(root=tmp_path / "root", namespace="g")
    with pytest.raises(ControlError) as exc:
        require_stdio_mcp_baseline(controller, "nope")
    assert exc.value.code == "unsupported"


def test_gate_uses_selected_backend_only() -> None:
    port = SplitPort()
    require_stdio_mcp_baseline(port, "good")
    with pytest.raises(ControlError) as exc:
        require_stdio_mcp_baseline(port, "bad")
    assert exc.value.code == "unsupported"


def test_control_agent_direct_use_ungated() -> None:
    """Gating the serve entry never removes SDK/CLI control use."""

    async def go() -> None:
        agent = ControlAgent(RefusingPort(), "fake-test")
        resp = await agent.initialize(1)
        assert resp.protocol_version == 1
        handle = await agent.new_session("/tmp/asc-proj")
        assert handle.session_id.startswith("ses_")

    asyncio.run(go())


def test_cli_gate_child_fails_cleanly(tmp_path: Path) -> None:
    """Exact CLI entry with a non-baseline runtime: exit 2, protocol-free stdout."""

    async def go() -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "agent_session_control",
            "acp",
            "--runtime",
            "cursor",
            "--root",
            str(tmp_path / "root"),
            "--namespace",
            "g",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env(),
        )
        assert process.stdin and process.stdout and process.stderr
        process.stdin.write(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1}}\n')
        await process.stdin.drain()
        process.stdin.close()
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
        except Exception:
            process.kill()
            raise
        assert process.returncode == 2
        assert stdout == b"", f"refused serve must emit no protocol bytes, got {stdout[:200]!r}"
        text = (stderr or b"").decode()
        assert "cursor" in text and "stdio" in text.lower()
        # Sanitized: the reported baseline value is never echoed.
        assert "mcp=" not in text

    asyncio.run(go())


class CredentialProbePort(FakeController):
    """Failed probe whose prose carries a synthetic credential (never real)."""

    def capabilities(self, runtime: str) -> dict:
        raise RuntimeError("SYNTHETIC_PROBE_CREDENTIAL_NOT_REAL")


class CredentialControlErrorPort(FakeController):
    """ControlError probe whose message carries a synthetic credential."""

    def capabilities(self, runtime: str) -> dict:
        raise ControlError("auth_required", "login token SYNTHETIC_CONTROL_CREDENTIAL_NOT_REAL", 4)


class CredentialValuePort(FakeController):
    """Baseline value itself carries a synthetic credential."""

    def capabilities(self, runtime: str) -> dict:
        return {**super().capabilities(runtime), "mcp": "SYNTHETIC_MCP_VALUE_NOT_REAL"}


class MalformedCapsPort(FakeController):
    """Non-mapping capabilities verify nothing: serve must fail closed."""

    def capabilities(self, runtime: str) -> Any:
        return ["mcp", "supported"]


def test_gate_probe_failures_carry_no_backend_prose(capsys: pytest.CaptureFixture[str]) -> None:
    """Sanitized gate errors: stable codes/types only, no untrusted values."""

    async def go() -> None:
        cases = [
            CredentialProbePort(),
            CredentialControlErrorPort(),
            CredentialValuePort(),
            MalformedCapsPort(),
        ]
        for port in cases:
            with pytest.raises(ControlError) as exc:
                require_stdio_mcp_baseline(port, "fake-test")
            assert exc.value.code == "unsupported"
            assert "fake-test" in exc.value.message  # safe caller-supplied identity kept
            assert "stdio" in exc.value.message.lower()
            with pytest.raises(ControlError) as exc2:
                await serve_stdio(port, "fake-test")
            assert exc2.value.code == "unsupported"

    asyncio.run(go())
    assert capsys.readouterr().out == ""
    # No synthetic credential travels outward through require or serve, and no
    # backend exception is chained outward (probe branches suppress context so
    # no backend traceback travels either).
    for port in (CredentialProbePort(), CredentialControlErrorPort(), CredentialValuePort(), MalformedCapsPort()):
        try:
            require_stdio_mcp_baseline(port, "fake-test")
        except ControlError as exc:
            assert "SYNTHETIC" not in exc.message
            assert exc.__cause__ is None
    for port in (CredentialProbePort(), CredentialControlErrorPort()):
        try:
            require_stdio_mcp_baseline(port, "fake-test")
        except ControlError as exc:
            assert exc.__suppress_context__
    # The generic-exception branch names the stable type, never the prose.
    try:
        require_stdio_mcp_baseline(CredentialProbePort(), "fake-test")
    except ControlError as exc:
        assert "RuntimeError" in exc.message
    # The ControlError branch names our stable code, never the prose.
    try:
        require_stdio_mcp_baseline(CredentialControlErrorPort(), "fake-test")
    except ControlError as exc:
        assert "auth_required" in exc.message


def test_cli_gate_unknown_runtime_child_sanitized(tmp_path: Path) -> None:
    """Exact CLI entry with an unknown runtime: exit 2, sanitized stderr."""

    async def go() -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "agent_session_control",
            "acp",
            "--runtime",
            "nope",
            "--root",
            str(tmp_path / "root"),
            "--namespace",
            "g",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env(),
        )
        assert process.stdin and process.stdout and process.stderr
        process.stdin.write(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1}}\n')
        await process.stdin.drain()
        process.stdin.close()
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
        except Exception:
            process.kill()
            raise
        assert process.returncode == 2
        assert stdout == b"", f"refused serve must emit no protocol bytes, got {stdout[:200]!r}"
        text = (stderr or b"").decode()
        assert "nope" in text and "stdio" in text.lower()
        assert "agentclientprotocol.com" in text
        # The engine's backend prose ("Unsupported runtime: ...") never travels.
        assert "Unsupported runtime" not in text

    asyncio.run(go())


# -- real stdio over the production controller ----------------------------------


def spawn_real_args(tmp_path: Path, fixture: str) -> list[str]:
    return [
        sys.executable,
        str(FIX / "run_real_agent.py"),
        "--runtime",
        "t",
        "--root",
        str(tmp_path / "root"),
        "--namespace",
        "base",
        "--fixture",
        fixture,
    ]


def test_real_stdio_handshake_lists_close_unknown_prompt(tmp_path: Path) -> None:
    from acp import PROTOCOL_VERSION, RequestError, spawn_agent_process

    async def go() -> None:
        client = TClient()
        env = child_env()
        async with spawn_agent_process(
            any_client(client), *spawn_real_args(tmp_path, "park"), env=env, use_unstable_protocol=True
        ) as (conn, process):
            resp = await asyncio.wait_for(conn.initialize(PROTOCOL_VERSION), 30)
            assert resp.protocol_version == 1
            caps = resp.agent_capabilities
            assert caps is not None
            assert caps.load_session is True
            assert caps.mcp_capabilities is not None
            assert caps.mcp_capabilities.http is False
            assert caps.mcp_capabilities.sse is False
            validate("InitializeResponse", dump(resp))
            page = await asyncio.wait_for(conn.list_sessions(), 30)
            assert page.sessions == []
            idle = await asyncio.wait_for(conn.close_session("ses_missing"), 30)
            assert idle is not None
            with pytest.raises(RequestError) as exc:
                await asyncio.wait_for(conn.prompt("ses_missing", [{"type": "text", "text": "hi"}]), 30)
            assert exc.value.code == -32002
            assert not (tmp_path / "root").exists(), "reads must never create storage"
        assert process.returncode == 0

    asyncio.run(go())


def test_real_stdio_mcp_accept_and_refuse_no_secret_echo(tmp_path: Path) -> None:
    from acp import PROTOCOL_VERSION, RequestError, spawn_agent_process

    async def go() -> None:
        client = TClient()
        proj = tmp_path / "proj"
        proj.mkdir()
        secret = "sekrit-live-not-a-real-credential"
        async with spawn_agent_process(
            any_client(client), *spawn_real_args(tmp_path, "park"), env=child_env(), use_unstable_protocol=True
        ) as (conn, _process):
            await asyncio.wait_for(conn.initialize(PROTOCOL_VERSION), 30)
            # Baseline stdio entry is accepted by the production controller.
            session = await asyncio.wait_for(
                conn.new_session(
                    str(proj),
                    mcp_servers=[
                        {
                            "name": "fs",
                            "command": "/usr/bin/mcp-fs",
                            "args": ["--stdio"],
                            "env": [{"name": "T", "value": secret}],
                        }
                    ],
                ),
                30,
            )
            assert session.session_id.startswith("ses_")
            validate("NewSessionResponse", dump(session))
            # Non-stdio transports are refused without secret echo.
            from acp.schema import HttpMcpServer

            with pytest.raises(RequestError) as exc:
                await asyncio.wait_for(
                    conn.new_session(
                        str(proj),
                        mcp_servers=[HttpMcpServer(type="http", name="h", url="https://example.invalid", headers=[])],
                    ),
                    30,
                )
            assert exc.value.code == -32602
            assert secret not in json.dumps(exc.value.data or {})

    asyncio.run(go())


def test_real_stdio_parallel_prompt_and_close_paths(tmp_path: Path) -> None:
    """One live turn per session: parallel prompt rejected, close frees, replay safe."""
    import contextlib

    from acp import PROTOCOL_VERSION, RequestError, spawn_agent_process, text_block

    async def go() -> None:
        client = TClient()
        proj = tmp_path / "proj"
        proj.mkdir()
        async with spawn_agent_process(
            any_client(client), *spawn_real_args(tmp_path, "park"), env=child_env(), use_unstable_protocol=True
        ) as (conn, _process):
            await asyncio.wait_for(conn.initialize(PROTOCOL_VERSION), 30)
            session = await asyncio.wait_for(conn.new_session(str(proj), mcp_servers=[]), 30)
            # The doubled terminal never settles: the turn stays live (parked).
            pending = asyncio.create_task(conn.prompt(session.session_id, [text_block("parked work")]))
            await asyncio.sleep(1.0)
            with pytest.raises(RequestError) as exc:
                await asyncio.wait_for(conn.prompt(session.session_id, [text_block("sibling")]), 30)
            assert exc.value.code == -32602
            assert (exc.value.data or {}).get("code") == "turn_active"
            # Cancel is accepted (notification); idle sessions are a no-op.
            await asyncio.wait_for(conn.cancel(session.session_id), 30)
            other = await asyncio.wait_for(conn.new_session(str(proj), mcp_servers=[]), 30)
            await asyncio.wait_for(conn.cancel(other.session_id), 30)
            # Close frees the session even with live work; the owned interrupt
            # settles the run, so cancellation confirms.
            closed = await asyncio.wait_for(conn.close_session(session.session_id), 60)
            assert dump(closed).get("_meta", {}).get("cancelConfirmed") is True
            with pytest.raises(RequestError) as exc2:
                await asyncio.wait_for(conn.prompt(session.session_id, [text_block("after close")]), 30)
            assert exc2.value.code == -32002
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending

    asyncio.run(go())


def test_real_stdio_load_replays_full_large_history(tmp_path: Path) -> None:
    """Production load path: full >120 KiB replay precedes the load response."""
    from acp import PROTOCOL_VERSION, spawn_agent_process

    async def go() -> None:
        client = TClient()
        proj = tmp_path / "proj"
        proj.mkdir()
        async with spawn_agent_process(
            any_client(client), *spawn_real_args(tmp_path, "history"), env=child_env(), use_unstable_protocol=True
        ) as (conn, _process):
            await asyncio.wait_for(conn.initialize(PROTOCOL_VERSION), 30)
            session = await asyncio.wait_for(conn.new_session(str(proj), mcp_servers=[]), 30)
            before = len(client.updates)
            # The binder needs a poll beat to attach the canned snapshot.
            await asyncio.sleep(0.5)
            await asyncio.wait_for(conn.load_session(str(proj), session.session_id, mcp_servers=[]), 60)
            replayed = client.updates[before:]
            assert replayed, "full history must stream before the load response returns"
            kinds = {getattr(u, "sessionUpdate", None) for _, u in replayed}
            assert "user_message_chunk" in kinds
            assert "agent_message_chunk" in kinds
            assert "agent_thought_chunk" in kinds
            assert "tool_call" in kinds and "tool_call_update" in kinds
            agent_text = "".join(
                dump(u).get("content", {}).get("text", "")
                for _, u in replayed
                if getattr(u, "sessionUpdate", None) == "agent_message_chunk"
            )
            assert LARGE_SENTINEL in agent_text
            assert len(agent_text) > 120 * 1024, f"lossless replay required, got {len(agent_text)} chars"
            blobs = [dump(u) for _, u in replayed]
            starts = [b for b in blobs if b.get("sessionUpdate") == "tool_call"]
            progresses = [b for b in blobs if b.get("sessionUpdate") == "tool_call_update"]
            assert {b.get("toolCallId") for b in starts} >= {"call_base_1", "call_base_2"}
            by_id = {}
            for b in progresses:
                by_id.setdefault(b.get("toolCallId"), []).append(b)
            assert by_id["call_base_1"] and all(b.get("status") == "completed" for b in by_id["call_base_1"])
            assert any(TOOL_OK_SENTINEL in json.dumps(b.get("content", [])) for b in by_id["call_base_1"])
            assert by_id["call_base_2"] and all(b.get("status") == "failed" for b in by_id["call_base_2"])
            assert any(TOOL_ERR_SENTINEL in json.dumps(b.get("content", [])) for b in by_id["call_base_2"])
            # Full-fidelity tool payloads: native raw input/output ride the
            # schema rawInput/rawOutput fields (genuine SessKit dataclass
            # records projected losslessly by the production controller).
            starts_by_id: dict[str, list] = {}
            for b in starts:
                starts_by_id.setdefault(b.get("toolCallId"), []).append(b)
            assert any(RAW_IN_SENTINEL in json.dumps(b.get("rawInput", {})) for b in starts_by_id["call_base_1"])
            assert any(RAW_OUT_SENTINEL in json.dumps(b.get("rawOutput", {})) for b in by_id["call_base_1"]), (
                "native raw output must ride tool_call_update.rawOutput"
            )
            assert any(LONG_RAW_SENTINEL in json.dumps(b.get("rawOutput", {})) for b in by_id["call_base_1"])
            # Raw-only pair (no text anywhere): input on the start, native
            # error detail in the progress content, input marker preserved.
            assert {b.get("toolCallId") for b in starts} >= {"call_base_3"}
            assert any(RAW_ONLY_IN_SENTINEL in json.dumps(b.get("rawInput", {})) for b in starts_by_id["call_base_3"])
            assert by_id["call_base_3"] and all(b.get("status") == "failed" for b in by_id["call_base_3"])
            assert any(RAW_ONLY_ERR_SENTINEL in json.dumps(b.get("content", [])) for b in by_id["call_base_3"])
            # Deep + falsy pair through production stdio replay: 30-level
            # valid JSON input preserved fully (no depth cap), falsy valid
            # JSON output preserved exactly (false/zero/empty/nested).
            assert {b.get("toolCallId") for b in starts} >= {"call_base_4"}
            expected_deep = canned_deep_raw(30)
            assert any(b.get("rawInput") == expected_deep for b in starts_by_id["call_base_4"]), (
                "30-level raw input must survive replay exactly"
            )
            assert DEEP_RAW_SENTINEL in json.dumps(starts_by_id["call_base_4"][0].get("rawInput", {}))
            expected_falsy = canned_falsy_raw()
            assert by_id["call_base_4"] and all(b.get("status") == "completed" for b in by_id["call_base_4"])
            assert any(b.get("rawOutput") == expected_falsy for b in by_id["call_base_4"]), (
                "falsy raw output must survive replay exactly"
            )
            assert any(FALSY_RAW_SENTINEL in json.dumps(b.get("rawOutput", {})) for b in by_id["call_base_4"])
            falsy_wire = next(b for b in by_id["call_base_4"] if b.get("rawOutput") == expected_falsy)["rawOutput"]
            assert falsy_wire["flag"] is False and falsy_wire["count"] == 0
            assert falsy_wire["name"] == "" and falsy_wire["items"] == [] and falsy_wire["opts"] == {}
            assert falsy_wire["nested"] == {"a": [0, False, "", None], "b": {"c": 0}}
            for _, update in replayed:
                validate("SessionNotification", {"sessionId": session.session_id, "update": dump(update)})
            # Resume reconnects with no replay.
            n = len(client.updates)
            await asyncio.wait_for(conn.resume_session(session.session_id, str(proj)), 60)
            assert len(client.updates) == n

    asyncio.run(go())


# -- production persistent path, in-process -------------------------------------


def make_production(tmp_path: Path, namespace: str = "stream") -> tuple:
    from real_helpers import ScriptedTerminal  # pyrefly: ignore[missing-import]

    terminal = ScriptedTerminal()
    controller = LocalController(
        root=tmp_path / "root",
        namespace=namespace,
        runtime_profiles={"t": mcp_profile()},
        terminal=terminal,
        spawn=False,
    )
    agent = ControlAgent(controller, "t")
    agent._conn = ConnStub()
    return controller, agent, agent._conn


async def settle_production_run(controller: Any, run_id: str, final_response: str) -> None:
    """Settle one parked run with lossless store content (post-B supervisor shape)."""
    controller.run_store.update(
        run_id,
        {
            "execution": "completed",
            "result": {"final_response": final_response},
            "stop_reason": "end_turn",
            "finality": "native",
        },
        actor="acp-test",
    )


def test_production_stream_cumulative_no_duplicate_no_loss(tmp_path: Path) -> None:
    """Cumulative deltas under one native id: prefix + tail, duplicates dropped."""

    async def go() -> None:
        controller, agent, conn = make_production(tmp_path)
        proj = tmp_path / "proj"
        proj.mkdir()
        handle = await agent.new_session(str(proj), mcp_servers=[])
        run_id = await controller.start_turn(
            handle.session_id, [{"type": "text", "text": "stream me"}], request_id="s1", actor="acp-test"
        )
        large = canned_large_text()
        prefix = large[:50_000]
        bodies = [
            {"kind": "activity", "type": "assistant_message", "native_seq": 5, "text": prefix},
            {"kind": "activity", "type": "assistant_message", "native_seq": 5, "text": large},
            {"kind": "activity", "type": "assistant_message", "native_seq": 5, "text": large},  # repeat poll
            {"kind": "activity", "type": "tool_call", "native_seq": 6, "tool": "read", "call_id": "c-stream-1"},
            {
                "kind": "activity",
                "type": "tool_result",
                "native_seq": 7,
                "tool": "read",
                "call_id": "c-stream-1",
                "result": {"status": "ok", "evidence": {"origin": "native"}},
                "text": f"contents here {TOOL_OK_SENTINEL}",
            },
        ]
        for index, body in enumerate(bodies):
            controller.run_store.append(run_id, f"test:{index}", body)
        await settle_production_run(controller, run_id, large)
        stats = await agent._stream_run(handle.session_id, run_id)
        result = await controller.wait(run_id, timeout=10)
        await agent._announce_terminal(handle.session_id, run_id, result, stats)
        resp = agent._prompt_response(run_id, result, stats)
        assert resp.stop_reason == "end_turn"
        texts = [
            dump(u)["content"]["text"]
            for _, u in conn.updates
            if getattr(u, "sessionUpdate", None) == "agent_message_chunk"
        ]
        assert "".join(texts) == large, "prefix + exactly the missing tail, never resend, never loss"
        assert len(texts) == 2, f"one prefix delta plus one tail, got {len(texts)}"
        assert LARGE_SENTINEL in texts[-1]
        progresses = [dump(u) for _, u in conn.updates if getattr(u, "sessionUpdate", None) == "tool_call_update"]
        assert progresses and all(p.get("toolCallId") == "c-stream-1" for p in progresses)
        assert all(p.get("status") == "completed" for p in progresses)
        assert any(TOOL_OK_SENTINEL in json.dumps(p.get("content", [])) for p in progresses)
        validate("PromptResponse", dump(resp))
        for _, update in conn.updates:
            validate("SessionNotification", {"sessionId": handle.session_id, "update": dump(update)})

    asyncio.run(go())


def test_production_tool_only_answer_delivers_final(tmp_path: Path) -> None:
    """Tool-only stream must not suppress the terminal assistant response."""

    async def go() -> None:
        controller, agent, conn = make_production(tmp_path, namespace="toolonly")
        proj = tmp_path / "proj"
        proj.mkdir()
        handle = await agent.new_session(str(proj), mcp_servers=[])
        run_id = await controller.start_turn(
            handle.session_id, [{"type": "text", "text": "tools please"}], request_id="s2", actor="acp-test"
        )
        controller.run_store.append(
            run_id,
            "test:0",
            {"kind": "activity", "type": "tool_call", "native_seq": 6, "tool": "read", "call_id": "c-only-1"},
        )
        controller.run_store.append(
            run_id,
            "test:1",
            {
                "kind": "activity",
                "type": "tool_result",
                "native_seq": 7,
                "tool": "read",
                "call_id": "c-only-1",
                "result": {"status": "ok", "evidence": {"origin": "native"}},
                "text": "read ok",
            },
        )
        await settle_production_run(controller, run_id, "TOOL_ONLY_FINAL_7e1a")
        stats = await agent._stream_run(handle.session_id, run_id)
        result = await controller.wait(run_id, timeout=10)
        await agent._announce_terminal(handle.session_id, run_id, result, stats)
        resp = agent._prompt_response(run_id, result, stats)
        assert resp.stop_reason == "end_turn"
        texts = [
            dump(u)["content"]["text"]
            for _, u in conn.updates
            if getattr(u, "sessionUpdate", None) == "agent_message_chunk"
        ]
        assert texts == ["TOOL_ONLY_FINAL_7e1a"]

    asyncio.run(go())


def test_production_stream_tool_raw_payloads_lossless(tmp_path: Path) -> None:
    """Live tool input/output/error details ride rawInput/rawOutput losslessly.

    Raw-only bodies (no text), a long nested payload, and lossless
    store-shaped records (the exact dicts the production supervisor projects
    from genuine native dataclasses before appending) through the production
    controller/store: the wire must carry every available native detail on
    the schema fields, inventing none. The dataclass-to-projection step itself
    is proven on the real stdio load path with genuine SessKit dataclasses.
    """

    async def go() -> None:
        controller, agent, conn = make_production(tmp_path, namespace="rawlive")
        proj = tmp_path / "proj"
        proj.mkdir()
        handle = await agent.new_session(str(proj), mcp_servers=[])
        run_id = await controller.start_turn(
            handle.session_id, [{"type": "text", "text": "raw tools"}], request_id="s3", actor="acp-test"
        )
        long_blob = "w=" * 4000 + LONG_RAW_SENTINEL
        bodies = [
            {
                "kind": "activity",
                "type": "tool_call",
                "native_seq": 6,
                "name": "read",
                "call_id": "c-raw-1",
                "raw_input": {"path": "/proj/live.txt", "marker": RAW_IN_SENTINEL},
            },
            {
                "kind": "activity",
                "type": "tool_result",
                "native_seq": 7,
                "name": "read",
                "call_id": "c-raw-1",
                "result": {"status": "ok", "evidence": {"origin": "native"}},
                "raw_output": {"body": f"live contents {RAW_OUT_SENTINEL}", "blob": long_blob},
            },
            {
                "kind": "activity",
                "type": "tool_call",
                "native_seq": 8,
                "name": "exec",
                "call_id": "c-raw-2",
                "raw_input": {"argv": ["live", "run"], "marker": RAW_ONLY_IN_SENTINEL},
            },
            {
                "kind": "activity",
                "type": "tool_result",
                "native_seq": 9,
                "name": "exec",
                "call_id": "c-raw-2",
                "error": {"kind": "runtime_error", "message": f"live raw-only denial {RAW_ONLY_ERR_SENTINEL}"},
            },
        ]
        for index, body in enumerate(bodies):
            controller.run_store.append(run_id, f"test:{index}", body)
        await settle_production_run(controller, run_id, "RAW_LIVE_FINAL_c4d9")
        stats = await agent._stream_run(handle.session_id, run_id)
        result = await controller.wait(run_id, timeout=10)
        await agent._announce_terminal(handle.session_id, run_id, result, stats)
        resp = agent._prompt_response(run_id, result, stats)
        assert resp.stop_reason == "end_turn"
        blobs = [dump(u) for _, u in conn.updates]
        starts = {b.get("toolCallId"): b for b in blobs if b.get("sessionUpdate") == "tool_call"}
        progresses: dict[str, list] = {}
        for b in blobs:
            if b.get("sessionUpdate") == "tool_call_update":
                progresses.setdefault(b.get("toolCallId"), []).append(b)
        assert RAW_IN_SENTINEL in json.dumps(starts["c-raw-1"].get("rawInput", {}))
        assert RAW_OUT_SENTINEL in json.dumps(progresses["c-raw-1"][-1].get("rawOutput", {}))
        assert LONG_RAW_SENTINEL in json.dumps(progresses["c-raw-1"][-1].get("rawOutput", {}))
        assert all(p.get("status") == "completed" for p in progresses["c-raw-1"])
        # Raw-only error pair: input marker on the start, native error detail
        # in the progress content, failed status.
        assert RAW_ONLY_IN_SENTINEL in json.dumps(starts["c-raw-2"].get("rawInput", {}))
        assert all(p.get("status") == "failed" for p in progresses["c-raw-2"])
        assert any(RAW_ONLY_ERR_SENTINEL in json.dumps(p.get("content", [])) for p in progresses["c-raw-2"])
        validate("PromptResponse", dump(resp))
        for _, update in conn.updates:
            validate("SessionNotification", {"sessionId": handle.session_id, "update": dump(update)})

    asyncio.run(go())


def test_no_fabricated_auth_and_registry_isolation(tmp_path: Path) -> None:
    """No auth bridge is invented; sessions isolate by root/namespace."""
    from acp import RequestError

    async def go() -> None:
        controller, agent, _ = make_production(tmp_path, namespace="iso")
        with pytest.raises(RequestError) as exc:
            await agent.authenticate("token")
        assert exc.value.code == -32601
        proj = tmp_path / "proj"
        proj.mkdir()
        handle = await agent.new_session(str(proj), mcp_servers=[])
        page = await agent.list_sessions()
        assert [s.session_id for s in page.sessions] == [handle.session_id]
        other_controller, other_agent, _ = make_production(tmp_path, namespace="other")
        other_page = await other_agent.list_sessions()
        assert other_page.sessions == []
        assert other_controller is not controller

    asyncio.run(go())


def test_documented_client_recipe_executable(tmp_path: Path) -> None:
    """The docs/ACP.md client recipe, executed verbatim (fixture driver only).

    Same interpreter, argument order, kwargs, calls and cleanup as documented;
    the only substitutions are the server argv (disposable ``history`` fixture
    driver instead of the real CLI, which would host real terminals) and the
    test-only ``env`` carrying ``PYTHONPATH=src``. Proves handshake +
    session create/list/close — protocol reachability only, never a live
    native turn (live prompts need the runtime login/quota).
    """
    import shutil
    import tempfile

    from acp import PROTOCOL_VERSION, spawn_agent_process

    class RecipeClient:
        def __init__(self) -> None:
            self.updates: list = []

        def on_connect(self, conn: object) -> None:
            self.conn = conn

        async def session_update(self, session_id: str, update: object) -> None:
            self.updates.append((session_id, update))

    async def go() -> None:
        tmp = Path(tempfile.mkdtemp(prefix="asc-acp-recipe-", dir=str(tmp_path)))
        try:
            proj = tmp / "proj"
            proj.mkdir()
            # Documented argument order; only the server argv points at the
            # disposable fixture driver (same positions as the real CLI).
            server_argv = [
                sys.executable,
                str(FIX / "run_real_agent.py"),
                "--runtime",
                "t",
                "--root",
                str(tmp / "root"),
                "--namespace",
                "recipe",
                "--fixture",
                "history",
            ]
            assert server_argv[0] == sys.executable
            async with spawn_agent_process(
                RecipeClient(),
                *server_argv,
                env=child_env(),  # test-only: PYTHONPATH=src; installed use inherits env
                use_unstable_protocol=True,
            ) as (conn, proc):
                resp = await asyncio.wait_for(conn.initialize(PROTOCOL_VERSION), 30)
                assert resp.protocol_version == 1
                session = await asyncio.wait_for(conn.new_session(str(proj), mcp_servers=[]), 30)
                page = await asyncio.wait_for(conn.list_sessions(), 30)
                assert [s.session_id for s in page.sessions] == [session.session_id]
                await asyncio.wait_for(conn.close_session(session.session_id), 30)
            assert proc.returncode == 0
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    asyncio.run(go())


def test_jsonable_preserves_deep_and_falsy_and_rejects_unmappable() -> None:
    """No depth cap on valid JSON; unmappable fails sanitized, never silent."""
    import dataclasses
    from types import SimpleNamespace

    from agent_session_control.acp import (
        _jsonable,
        _raw_input_of,
        _raw_output_of,
        _UnmappablePayloadError,
    )

    # 30-level nesting passes through exactly (the old depth>25 cap corrupted).
    deep: Any = {"leaf": "DEEP_UNIT_SENTINEL_5e7a", "depth": 0}
    for level in range(1, 31):
        deep = {"depth": level, "next": deep}
    assert _jsonable(deep) == deep
    # Falsy-but-valid JSON is preserved, never treated as absent.
    for falsy in (False, 0, "", [], {}):
        assert _jsonable({"v": falsy}) == {"v": falsy}
        assert _jsonable([falsy]) == [falsy]
    assert _jsonable((1, 2)) == [1, 2]
    assert _raw_input_of({"raw_input": False}) is False
    assert _raw_input_of({"raw_input": 0}) == 0
    assert _raw_input_of({"raw_input": ""}) == ""
    assert _raw_input_of({"raw_input": []}) == []
    assert _raw_input_of({"raw_input": {}}) == {}
    assert _raw_output_of({"raw_output": False}) is False
    assert _raw_output_of({"raw_output": 0}) == 0
    # Unmappable inputs fail explicitly with type-only identity (no values).
    secret = "sekrit-unit-not-a-real-credential"
    cyclic: dict = {"marker": secret}
    cyclic["self"] = cyclic
    cases: list[Any] = [
        {"s": {secret}},  # set
        b"bytes-payload",
        object(),
        SimpleNamespace(a=1),
        dataclasses.make_dataclass("Unit", [("a", int)])(1),
        {1: "non-str-key"},
        float("nan"),
        float("inf"),
        cyclic,
    ]
    for bad in cases:
        with pytest.raises(_UnmappablePayloadError) as exc:
            _jsonable(bad)
        assert secret not in str(exc.value)
    # Raw accessors surface sanitized -32603 (never SDK-salvaged None).
    from acp import RequestError

    for key, fn in (("raw_input", _raw_input_of), ("raw_output", _raw_output_of)):
        with pytest.raises(RequestError) as exc2:
            fn({key: {secret}})
        assert exc2.value.code == -32603
        assert secret not in json.dumps(exc2.value.data or {})
        with pytest.raises(RequestError):
            fn({key: cyclic})


def test_error_text_either_only_shapes() -> None:
    """Either-only error records render without unguarded attribute reads."""
    from types import SimpleNamespace

    from agent_session_control.acp import _error_text

    assert _error_text({"error": None}) is None
    assert _error_text({"error": "boom"}) == "boom"
    assert _error_text({"error": ""}) is None
    assert _error_text({"error": {"kind": "k-only"}}) == "k-only"
    assert _error_text({"error": {"message": "m-only"}}) == "m-only"
    assert _error_text({"error": {"kind": "k", "message": "m"}}) == "k: m"
    assert _error_text({"error": {"kind": "", "message": ""}}) is None
    assert _error_text({"error": {"kind": 1, "message": ["x"]}}) is None
    # Object shapes with only one attribute present (the old OR-then-read-both
    # raised AttributeError on exactly these).
    assert _error_text({"error": SimpleNamespace(kind="okind")}) == "okind"
    assert _error_text({"error": SimpleNamespace(message="omsg")}) == "omsg"
    assert _error_text({"error": SimpleNamespace(kind="k", message="m")}) == "k: m"
    assert _error_text({"error": SimpleNamespace()}) is None
    assert _error_text({"error": 42}) is None


def test_production_stream_deep_and_falsy_raw_lossless(tmp_path: Path) -> None:
    """Live production-store path: 30-deep + falsy raws ride exactly."""

    async def go() -> None:
        controller, agent, conn = make_production(tmp_path, namespace="rawdeep")
        proj = tmp_path / "proj"
        proj.mkdir()
        handle = await agent.new_session(str(proj), mcp_servers=[])
        run_id = await controller.start_turn(
            handle.session_id, [{"type": "text", "text": "deep tools"}], request_id="s4", actor="acp-test"
        )
        expected_deep = canned_deep_raw(30)
        expected_falsy = canned_falsy_raw()
        bodies = [
            {
                "kind": "activity",
                "type": "tool_call",
                "native_seq": 6,
                "name": "read",
                "call_id": "c-deep-1",
                "raw_input": expected_deep,
            },
            {
                "kind": "activity",
                "type": "tool_result",
                "native_seq": 7,
                "name": "read",
                "call_id": "c-deep-1",
                "result": {"status": "ok", "evidence": {"origin": "native"}},
                "raw_output": expected_falsy,
                "text": "deep payload delivered",
            },
        ]
        for index, body in enumerate(bodies):
            controller.run_store.append(run_id, f"test:{index}", body)
        await settle_production_run(controller, run_id, "DEEP_LIVE_FINAL_2b8e")
        stats = await agent._stream_run(handle.session_id, run_id)
        result = await controller.wait(run_id, timeout=10)
        await agent._announce_terminal(handle.session_id, run_id, result, stats)
        resp = agent._prompt_response(run_id, result, stats)
        assert resp.stop_reason == "end_turn"
        blobs = [dump(u) for _, u in conn.updates]
        starts = {b.get("toolCallId"): b for b in blobs if b.get("sessionUpdate") == "tool_call"}
        progresses = [b for b in blobs if b.get("sessionUpdate") == "tool_call_update"]
        assert starts["c-deep-1"]["rawInput"] == expected_deep
        assert DEEP_RAW_SENTINEL in json.dumps(starts["c-deep-1"]["rawInput"])
        assert progresses and progresses[0]["rawOutput"] == expected_falsy
        wire_falsy = progresses[0]["rawOutput"]
        assert wire_falsy["flag"] is False and wire_falsy["count"] == 0
        assert wire_falsy["name"] == "" and wire_falsy["items"] == [] and wire_falsy["opts"] == {}
        assert FALSY_RAW_SENTINEL in json.dumps(wire_falsy)
        validate("PromptResponse", dump(resp))
        for _, update in conn.updates:
            validate("SessionNotification", {"sessionId": handle.session_id, "update": dump(update)})

    asyncio.run(go())


def test_production_stream_unmappable_fails_never_succeeds(tmp_path: Path) -> None:
    """Mapping failures propagate: no swallowed success, no complete content."""
    from acp import RequestError

    async def go() -> None:
        controller, agent, conn = make_production(tmp_path, namespace="rawbad")
        secret = "sekrit-stream-not-a-real-credential"
        cyclic: dict = {"marker": secret}
        cyclic["self"] = cyclic
        # tool_call with a present-but-unmappable raw input.
        stats: dict = {"assistant": 0, "tool": 0, "seen": 0, "omitted": 0}
        with pytest.raises(RequestError) as exc:
            await agent._stream_tool_call(
                "ses_x", "run_x", ("g", 1), {"call_id": "c-bad-1", "tool": "read", "raw_input": cyclic}, set(), stats
            )
        assert exc.value.code == -32603
        assert secret not in json.dumps(exc.value.data or {})
        assert stats["tool"] == 0
        assert conn.updates == [], "no successful tool content may follow a mapping failure"
        # tool_result with a nonfinite raw output.
        stats2: dict = {"assistant": 0, "tool": 0, "seen": 0, "omitted": 0}
        with pytest.raises(RequestError) as exc2:
            await agent._stream_tool_result(
                "ses_x",
                "run_x",
                ("g", 2),
                {"call_id": "c-bad-2", "tool": "read", "raw_output": float("nan"), "result": {"status": "ok"}},
                set(),
                stats2,
            )
        assert exc2.value.code == -32603
        assert stats2["tool"] == 0
        assert conn.updates == []
        # _stream_event propagates (the live fan-out never swallows it).
        from types import SimpleNamespace as _NS

        with pytest.raises(RequestError):
            await agent._stream_event(
                "ses_x",
                "run_x",
                _NS(generation="g", seq=9, data={"type": "tool_call", "call_id": "c-bad-3", "raw_input": {secret}}),
                {},
                set(),
                {"assistant": 0, "tool": 0, "seen": 0, "omitted": 0},
            )
        assert conn.updates == []

    asyncio.run(go())


def test_replay_unmappable_fails_never_succeeds(tmp_path: Path) -> None:
    """Replay mapping failures fail the load, never succeed with loss."""
    from types import SimpleNamespace as _NS

    from acp import RequestError

    async def go() -> None:
        controller, agent, conn = make_production(tmp_path, namespace="replaybad")
        secret = "sekrit-replay-not-a-real-credential"
        bad_call = _NS(
            generation="g",
            seq=1,
            kind="activity",
            data={"type": "tool_call", "call_id": "c-rbad-1", "tool": "read", "raw_input": {secret}},
        )
        with pytest.raises(RequestError) as exc:
            await agent._replay("ses_r", [bad_call])
        assert exc.value.code == -32603
        assert secret not in json.dumps(exc.value.data or {})
        assert conn.updates == []
        bad_result = _NS(
            generation="g",
            seq=2,
            kind="activity",
            data={"type": "tool_result", "call_id": "c-rbad-2", "tool": "read", "raw_output": float("inf")},
        )
        with pytest.raises(RequestError):
            await agent._replay("ses_r", [bad_result])
        assert conn.updates == []

    asyncio.run(go())


def test_pkg_version_falls_back_to_canonical_without_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing distribution metadata resolves to the canonical version.

    Deterministic: ``importlib.metadata.version`` is forced to raise
    ``PackageNotFoundError`` so ``_pkg_version`` must take the fallback
    branch, which is bound to ``__version__`` (currently ``0.1.1``) rather
    than the stale ``0.1.0`` literal. Installed-metadata resolution is
    untouched: only the fallback value changed.
    """
    import importlib.metadata

    from agent_session_control import __version__ as canonical
    from agent_session_control.acp import AGENT_VERSION_FALLBACK, _pkg_version

    assert AGENT_VERSION_FALLBACK == canonical == "0.1.1"

    def _missing(_dist: str) -> str:
        raise importlib.metadata.PackageNotFoundError("no dist for test")

    monkeypatch.setattr(importlib.metadata, "version", _missing)
    assert _pkg_version() == "0.1.1"
    assert _pkg_version() == canonical == AGENT_VERSION_FALLBACK


def test_initialize_agent_info_version_without_distribution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Official-SDK ``initialize`` reports ``agentInfo`` version ``0.1.1``.

    Same missing-distribution condition as above, through the real SDK
    response models: ``agent_info.version`` and the ``agentInfo`` wire form
    (validated against the pinned schema) both carry the canonical version.
    """

    async def go() -> None:
        import importlib.metadata

        from agent_session_control.acp import ControlAgent

        def _missing(_dist: str) -> str:
            raise importlib.metadata.PackageNotFoundError("no dist for test")

        monkeypatch.setattr(importlib.metadata, "version", _missing)
        agent = ControlAgent(FakeController(), "fake-test")
        resp = await agent.initialize(1)
        assert resp.protocol_version == 1
        assert resp.agent_info.version == "0.1.1"
        wire = dump(resp)
        assert wire["agentInfo"]["version"] == "0.1.1"
        assert wire["agentInfo"]["name"] == "agent-session-control"
        validate("InitializeResponse", wire)

    asyncio.run(go())

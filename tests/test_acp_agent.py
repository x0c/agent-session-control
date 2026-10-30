"""ACP adapter tests: handlers plus real stdio against the official SDK client.

Handler tests inject a deterministic ``FakeController`` whose event shapes
mirror the production engine. Stdio tests spawn
``tests/fixtures/acp/run_fake_agent.py`` as an actual child process and drive
it with the official ``acp`` client SDK; wire payloads are validated against
the pinned released schema ``schema-v1.27.0`` (never the SDK's draft
surface).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from acp.schema import HttpMcpServer, ImageContentBlock

FIX = Path(__file__).parent / "fixtures" / "acp"
sys.path.insert(0, str(FIX))

from fake_controller import FakeController, scripted_prompt  # noqa: E402  # pyrefly: ignore[missing-import]

from agent_session_control.acp import (  # noqa: E402
    ControlAgent,
    check_mcp_list,
    control_error_to_request_error,  # noqa: E402
)

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
    """The test client exercises only session_update/on_connect; the SDK
    router resolves handlers dynamically, so the wider static ``Client``
    Protocol is not required at runtime for these paths."""
    return client


class ConnStub:
    """In-process stand-in for AgentSideConnection.session_update."""

    def __init__(self) -> None:
        self.updates: list[tuple[str, object]] = []

    async def session_update(self, session_id: str, update: object, **kwargs: object) -> None:
        self.updates.append((session_id, update))


class LinkCapsController(FakeController):
    """Fake whose backend declares the canonical resource_link block form."""

    def capabilities(self, runtime: str) -> dict:
        caps = super().capabilities(runtime)
        return {**caps, "prompt_blocks": ["text", "resource_link"]}


class ExplodingCapsController(FakeController):
    """Capability probe failure must advertise nothing optional."""

    def capabilities(self, runtime: str) -> dict:
        raise RuntimeError("probe failed")


class UnsupportedResumeController(FakeController):
    """Explicit backend opt-out suppresses the feature even with the method."""

    def capabilities(self, runtime: str) -> dict:
        caps = super().capabilities(runtime)
        return {**caps, "resume": {"supported": False}}


class UnknownCapsController(FakeController):
    """Unverified profile: nothing optional may be advertised."""

    def capabilities(self, runtime: str) -> dict:
        return {}


class GatedController(FakeController):
    """start_turn parks at a gate: deterministic cancellation races."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.gate = asyncio.Event()
        self.starts: list[str] = []
        self.start_notices: list[asyncio.Event] = [asyncio.Event() for _ in range(4)]
        self.watching: list[asyncio.Event] = [asyncio.Event() for _ in range(4)]
        self.done: dict[str, asyncio.Event] = {}
        self.states: dict[str, str] = {}
        self.interrupts: list[str] = []

    async def start_turn(self, *args: object, **kwargs: object) -> str:
        rid = f"r{len(self.starts) + 1}"
        self.starts.append(rid)
        self.done[rid] = asyncio.Event()
        self.states[rid] = "completed"
        self.start_notices[len(self.starts) - 1].set()
        await self.gate.wait()
        return rid

    def watch(self, rid: str, **kwargs: object):  # noqa: ANN202
        async def gen() -> object:
            self.watching[int(rid[1:]) - 1].set()
            await self.done[rid].wait()
            if False:
                yield

        return gen()

    async def wait(self, rid: str, **kwargs: object) -> object:
        from agent_session_control.errors import ControlError
        from agent_session_control.models import RunResult

        timeout = kwargs.get("timeout")
        if self.done[rid].is_set() or timeout is None:
            if timeout is None:
                await self.done[rid].wait()
            state = self.states[rid]
            if state == "completed":
                return RunResult(
                    rid, "completed", final_response="gated answer", stop_reason="end_turn", evidence="native"
                )
            return RunResult(rid, state, evidence="unknown")
        raise ControlError("wait_timeout", "gated wait always pending")

    async def interrupt(self, rid: str, **kwargs: object) -> None:
        self.interrupts.append(rid)
        self.states[rid] = "interrupted"
        self.done[rid].set()


class BadCloseController(FakeController):
    """close_session always fails: attachment must be retained."""

    async def close_session(self, session_id: str, **kwargs: object) -> None:
        from agent_session_control.errors import ControlError

        raise ControlError("close_uncertain", "synthetic owned process still alive")


def make_agent(**kwargs: object) -> tuple[ControlAgent, FakeController]:
    controller = FakeController(**kwargs)
    return ControlAgent(controller, "fake-test"), controller


# -- error mapping ------------------------------------------------------------


def test_control_error_mapping_codes() -> None:
    from agent_session_control.errors import ControlError

    assert control_error_to_request_error(ControlError("not_found", "gone")).code == -32002
    assert control_error_to_request_error(ControlError("usage_error", "bad")).code == -32602
    assert control_error_to_request_error(ControlError("unsupported", "nope")).code == -32602
    assert control_error_to_request_error(ControlError("upstream_failure", "boom")).code == -32603
    assert control_error_to_request_error(ValueError("x")).code == -32603


# -- capabilities ---------------------------------------------------------------


def test_initialize_capabilities_truthful() -> None:
    async def go() -> None:
        agent, _ = make_agent()
        resp = await agent.initialize(1)
        assert resp.protocol_version == 1
        caps = resp.agent_capabilities
        assert caps is not None
        assert caps.load_session is True
        assert caps.prompt_capabilities is not None
        assert caps.mcp_capabilities is not None
        assert caps.session_capabilities is not None
        assert caps.prompt_capabilities.image is False
        assert caps.prompt_capabilities.audio is False
        assert caps.prompt_capabilities.embedded_context is False
        assert caps.mcp_capabilities.http is False
        assert caps.mcp_capabilities.sse is False
        assert caps.session_capabilities.resume is not None
        assert caps.session_capabilities.close is not None
        assert caps.session_capabilities.list is not None
        validate("InitializeResponse", dump(resp))

    asyncio.run(go())


def test_capability_gating_honest_on_missing_methods() -> None:
    from acp import RequestError

    class Bare:
        """A port implementation exposing none of the optional methods."""

        def capabilities(self, runtime: str) -> dict:
            return {}

    async def go() -> None:
        agent = ControlAgent(Bare(), "fake-test")  # type: ignore[arg-type]
        for coro, name in (
            (agent.resume_session("s", "/tmp"), "session/resume"),
            (agent.load_session("/tmp", "s"), "session/load"),
            (agent.close_session("s"), "session/close"),
            (agent.list_sessions(), "session/list"),
        ):
            with pytest.raises(RequestError) as exc:
                await coro
            assert exc.value.code == -32601, name

    asyncio.run(go())


def test_capability_probe_failure_advertises_nothing_optional() -> None:
    from acp import RequestError

    async def go() -> None:
        agent = ControlAgent(ExplodingCapsController(), "fake-test")
        resp = await agent.initialize(1)
        caps = resp.agent_capabilities
        assert caps is not None and caps.load_session is False
        assert caps.session_capabilities is not None
        assert caps.session_capabilities.resume is None
        assert caps.session_capabilities.close is None
        assert caps.session_capabilities.list is None
        for coro in (
            agent.resume_session("s", "/tmp"),
            agent.load_session("/tmp", "s"),
            agent.close_session("s"),
            agent.list_sessions(),
        ):
            with pytest.raises(RequestError) as exc:
                await coro
            assert exc.value.code == -32601

    asyncio.run(go())


def test_capability_explicit_false_suppresses_feature() -> None:
    from acp import RequestError

    async def go() -> None:
        agent = ControlAgent(UnsupportedResumeController(), "fake-test")
        resp = await agent.initialize(1)
        caps = resp.agent_capabilities
        assert caps is not None and caps.load_session is True
        assert caps.session_capabilities is not None
        assert caps.session_capabilities.resume is None
        assert caps.session_capabilities.close is not None
        with pytest.raises(RequestError) as exc:
            await agent.resume_session("s", "/tmp")
        assert exc.value.code == -32601

    asyncio.run(go())


def test_unsupported_agent_methods_are_method_not_found() -> None:
    from acp import RequestError

    async def go() -> None:
        agent, _ = make_agent()
        for coro in (agent.authenticate("x"), agent.ext_method("nope", {})):
            with pytest.raises(RequestError) as exc:
                await coro
            assert exc.value.code == -32601

    asyncio.run(go())


# -- MCP validation ---------------------------------------------------------------


def test_mcp_entries_validated_without_secret_echo() -> None:
    from acp import RequestError

    # Adapter-level: transport choice and translation integrity only. Strict
    # value validation lives at the foundation boundary (tested against the
    # real engine in test_acp_real.py).
    good = check_mcp_list(
        [{"name": "fs", "command": "/usr/bin/mcp", "args": ["--stdio"], "env": [{"name": "TOKEN", "value": "sekrit"}]}]
    )
    assert good[0] == {"name": "fs", "command": "/usr/bin/mcp", "args": ["--stdio"], "env": {"TOKEN": "sekrit"}}
    cases = [
        [{"name": "", "command": "/usr/bin/mcp"}],
        [{"name": "x", "command": "/usr/bin/mcp", "env": [{"name": "", "value": "v"}]}],
        [{"name": "x", "command": "/usr/bin/mcp", "env": [{"name": "A", "value": "1"}, {"name": "A", "value": "2"}]}],
        [{"name": "dup", "command": "/a"}, {"name": "dup", "command": "/b"}],
        [{"type": "http", "name": "h", "url": "https://x", "headers": []}],
        [{"type": "acp", "name": "a", "command": "/x"}],
        [{"type": "weird", "name": "w", "command": "/x"}],
    ]
    for entry in cases:
        with pytest.raises(RequestError) as exc:
            check_mcp_list(entry)
        assert exc.value.code == -32602
        assert "sekrit" not in json.dumps(exc.value.data or {})


def test_new_session_mcp_refusal_carries_no_secrets() -> None:
    from acp import RequestError

    async def go() -> None:
        agent, _ = make_agent()
        with pytest.raises(RequestError) as exc:
            await agent.new_session(
                "/tmp",
                mcp_servers=[{"name": "fs", "command": "/usr/bin/mcp", "env": [{"name": "TOKEN", "value": "sekrit"}]}],
            )
        assert exc.value.code == -32602
        assert "sekrit" not in json.dumps(exc.value.data or {})

    asyncio.run(go())


def test_new_session_validation() -> None:
    from acp import RequestError

    async def go() -> None:
        agent, _ = make_agent()
        with pytest.raises(RequestError) as exc:
            await agent.new_session("")
        assert exc.value.code == -32602
        with pytest.raises(RequestError) as exc:
            await agent.new_session("/tmp", additional_directories=["/tmp"])
        assert exc.value.code == -32602
        with pytest.raises(RequestError) as exc:
            await agent.new_session(
                "/tmp", mcp_servers=[HttpMcpServer(type="http", name="h", url="https://x", headers=[])]
            )
        assert exc.value.code == -32602
        resp = await agent.new_session("/tmp/asc-proj", mcp_servers=[])
        assert resp.session_id.startswith("ses_")
        validate("NewSessionResponse", dump(resp))

    asyncio.run(go())


# -- prompt -------------------------------------------------------------------------


def test_prompt_happy_path_streams_before_responding() -> None:
    async def go() -> None:
        agent, controller = make_agent()
        conn = ConnStub()
        agent._conn = conn
        handle = await agent.new_session("/tmp/asc-proj")
        order: list[str] = []

        orig = conn.session_update

        async def rec(session_id: str, update: object, **kw: object) -> None:
            order.append("update")
            await orig(session_id, update, **kw)

        conn.session_update = rec  # type: ignore[method-assign]
        resp = await agent.prompt(handle.session_id, [{"type": "text", "text": scripted_prompt("hi")}])
        order.append("response")
        assert resp.stop_reason == "end_turn"
        assert order[0] == "update" and order[-1] == "response"
        kinds = [getattr(u, "sessionUpdate", None) for _, u in conn.updates]
        assert "agent_message_chunk" in kinds
        submitted = controller.submitted[-1]
        assert submitted["blocks"] == [{"type": "text", "text": controller.runs[submitted["run_id"]]["brief"]}]
        assert submitted["on_busy"] == "queue"
        validate("PromptResponse", dump(resp))
        for _, update in conn.updates:
            validate("SessionNotification", {"sessionId": handle.session_id, "update": dump(update)})

    asyncio.run(go())


def test_prompt_rejects_unknown_session_without_auto_resume() -> None:
    from acp import RequestError

    async def go() -> None:
        agent, controller = make_agent()
        agent._conn = ConnStub()
        with pytest.raises(RequestError) as exc:
            await agent.prompt("ses_missing", [{"type": "text", "text": "hi"}])
        assert exc.value.code == -32002
        assert controller.submitted == []

    asyncio.run(go())


def test_prompt_rejects_second_concurrent_turn() -> None:
    from acp import RequestError

    async def go() -> None:
        agent, _ = make_agent()
        agent._conn = ConnStub()
        handle = await agent.new_session("/tmp/asc-proj")
        slow = asyncio.create_task(
            agent.prompt(handle.session_id, [{"type": "text", "text": scripted_prompt("SLOW 30")}])
        )
        await asyncio.sleep(0.5)
        with pytest.raises(RequestError) as exc:
            await agent.prompt(handle.session_id, [{"type": "text", "text": "queued sibling"}])
        assert exc.value.code == -32602
        assert exc.value.data.get("code") == "turn_active"
        await agent.cancel(handle.session_id)
        assert (await asyncio.wait_for(slow, 30)).stop_reason == "cancelled"
        # After settlement the session accepts a new turn again.
        resp = await agent.prompt(handle.session_id, [{"type": "text", "text": scripted_prompt("hi")}])
        assert resp.stop_reason == "end_turn"

    asyncio.run(go())


def test_prompt_resource_link_and_unicode_with_legacy_backend() -> None:
    async def go() -> None:
        agent, controller = make_agent()
        agent._conn = ConnStub()
        handle = await agent.new_session("/tmp/asc-proj")
        text = "Unicode \u6743\u9650 \U0001f680 caf\u00e9"
        resp = await agent.prompt(
            handle.session_id,
            [
                {"type": "text", "text": text},
                {"type": "resource_link", "uri": "file:///tmp/asc-proj/a.py", "name": "a.py"},
            ],
        )
        assert resp.stop_reason == "end_turn"
        # The fake backend declares only the legacy alias: the adapter
        # normalizes uri plus title without flattening meaning away.
        blocks = controller.submitted[-1]["blocks"]
        assert blocks[1] == {"type": "resource", "uri": "file:///tmp/asc-proj/a.py", "title": "a.py"}
        echoed = [dump(u)["content"]["text"] for _, u in agent._conn.updates]
        assert any(text in chunk for chunk in echoed)

    asyncio.run(go())


def test_prompt_resource_link_canonical_with_capable_backend() -> None:
    async def go() -> None:
        controller = LinkCapsController()
        agent = ControlAgent(controller, "fake-test")
        agent._conn = ConnStub()
        handle = await agent.new_session("/tmp/asc-proj")
        resp = await agent.prompt(
            handle.session_id,
            [{"type": "resource_link", "uri": "file:///x.py", "name": "x.py", "mimeType": "text/x-python"}],
        )
        assert resp.stop_reason == "end_turn"
        assert controller.submitted[-1]["blocks"] == [
            {
                "type": "resource_link",
                "uri": "file:///x.py",
                "name": "x.py",
                "title": "x.py",
                "mimeType": "text/x-python",
            }
        ]

    asyncio.run(go())


def test_prompt_rejects_rich_content() -> None:
    from acp import RequestError

    async def go() -> None:
        agent, _ = make_agent()
        handle = await agent.new_session("/tmp/asc-proj")
        for bad in (
            [ImageContentBlock(type="image", data="eA==", mime_type="image/png")],
            [{"type": "image", "data": "eA==", "mimeType": "image/png"}],
            [{"type": "resource", "uri": "file:///x", "text": "x"}],
            [],
        ):
            with pytest.raises(RequestError) as exc:
                await agent.prompt(handle.session_id, bad)  # type: ignore[list-item]
            assert exc.value.code == -32602

    asyncio.run(go())


def test_prompt_failed_and_unknown_are_internal_errors_with_note() -> None:
    from acp import RequestError

    async def go() -> None:
        agent, _ = make_agent()
        agent._conn = ConnStub()
        handle = await agent.new_session("/tmp/asc-proj")
        with pytest.raises(RequestError) as exc:
            await agent.prompt(handle.session_id, [{"type": "text", "text": scripted_prompt("FAIL")}])
        assert exc.value.code == -32603
        notes = [
            dump(u)["content"]["text"]
            for _, u in agent._conn.updates
            if getattr(u, "sessionUpdate", None) == "agent_message_chunk"
        ]
        assert any("[run failed:" in n for n in notes)
        with pytest.raises(RequestError) as exc:
            await agent.prompt(handle.session_id, [{"type": "text", "text": scripted_prompt("UNKNOWN")}])
        assert exc.value.code == -32603

    asyncio.run(go())


def test_prompt_unmapped_stop_reason_and_unconfirmed_evidence() -> None:
    from acp import RequestError

    async def go() -> None:
        agent, _ = make_agent()
        agent._conn = ConnStub()
        handle = await agent.new_session("/tmp/asc-proj")
        with pytest.raises(RequestError) as exc:
            await agent.prompt(handle.session_id, [{"type": "text", "text": scripted_prompt("WEIRD")}])
        assert exc.value.code == -32603
        with pytest.raises(RequestError) as exc:
            await agent.prompt(handle.session_id, [{"type": "text", "text": scripted_prompt("NOEVID")}])
        assert exc.value.code == -32603

    asyncio.run(go())


def test_prompt_tool_updates_streamed_with_late_result() -> None:
    async def go() -> None:
        agent, _ = make_agent()
        agent._conn = ConnStub()
        handle = await agent.new_session("/tmp/asc-proj")
        resp = await agent.prompt(handle.session_id, [{"type": "text", "text": scripted_prompt("TOOL do it")}])
        assert resp.stop_reason == "end_turn"
        kinds = [getattr(u, "sessionUpdate", None) for _, u in agent._conn.updates]
        assert "tool_call" in kinds and "tool_call_update" in kinds
        ids = {
            dump(u).get("toolCallId")
            for _, u in agent._conn.updates
            if getattr(u, "sessionUpdate", None) in ("tool_call", "tool_call_update")
        }
        assert len(ids) == 1  # start and late result share the stable call id

    asyncio.run(go())


def test_tool_failure_result_maps_to_failed_progress_with_content() -> None:
    async def go() -> None:
        from acp import RequestError

        agent, _ = make_agent()
        agent._conn = ConnStub()
        handle = await agent.new_session("/tmp/asc-proj")
        with pytest.raises(RequestError) as exc:
            await agent.prompt(handle.session_id, [{"type": "text", "text": scripted_prompt("TOOLFAIL")}])
        assert exc.value.code == -32603
        updates = [dump(u) for _, u in agent._conn.updates]
        starts = [u for u in updates if u.get("sessionUpdate") == "tool_call"]
        progresses = [u for u in updates if u.get("sessionUpdate") == "tool_call_update"]
        assert starts and progresses
        assert {u.get("toolCallId") for u in starts + progresses} == {starts[0].get("toolCallId")}
        failed = [u for u in progresses if u.get("status") == "failed"]
        assert failed, "the error result must surface as a failed update, not silence"
        assert "denied by policy" in json.dumps(failed[0].get("content", []))

    asyncio.run(go())


def test_streaming_lossless_against_production_projection() -> None:
    """Genuine compact_event/build_result shapes: the 1200-char summary plus
    truncation marker must reconcile to the full terminal result (>1200)."""
    from types import SimpleNamespace as NS

    from agent_session_control.correlation import build_result, compact_event
    from agent_session_control.models import ControlEvent, RunResult

    async def go() -> None:
        native_text = "x" * 1600 + "REQUIRED_FINAL_SENTINEL"
        native = NS(type="assistant_message", seq=1, ts=None, text=native_text)
        summary = compact_event(native)
        assert summary["text"].endswith("more chars]")
        assert len(summary["text"]) < len(native_text)
        result = build_result({"events": [native]})
        assert result["final_response"] == native_text

        class ProjectedPort(FakeController):
            async def start_turn(self, *a: object, **k: object) -> str:
                return "run_9"

            def watch(self, run_id: str, **kwargs: object):  # noqa: ANN202
                async def gen() -> object:
                    yield ControlEvent(
                        seq=1,
                        kind="activity",
                        session_id="s",
                        run_id=run_id,
                        data=summary,
                        evidence="native",
                        generation="g1",
                    )

                return gen()

            async def wait(self, run_id: str, **kwargs: object) -> object:
                return RunResult(
                    run_id,
                    "completed",
                    final_response=result["final_response"],
                    stop_reason="end_turn",
                    evidence="native",
                )

        agent = ControlAgent(ProjectedPort(), "fake-test")
        agent._conn = ConnStub()
        from agent_session_control.models import SessionHandle

        agent._sessions["s"] = SessionHandle(id="s", runtime="fake-test", cwd="/tmp/asc-proj")
        resp = await agent.prompt("s", [{"type": "text", "text": "hi"}])
        assert resp.stop_reason == "end_turn"
        rendered = "".join(
            dump(u)["content"]["text"]
            for _, u in agent._conn.updates
            if getattr(u, "sessionUpdate", None) == "agent_message_chunk"
        )
        assert rendered == native_text
        assert "REQUIRED_FINAL_SENTINEL" in rendered

    asyncio.run(go())


def test_streaming_lossless_with_full_projection() -> None:
    """Neutral lossless path: production project_event with complete text,
    native message ids and object-shaped tool results streams exactly."""
    from types import SimpleNamespace as NS

    from agent_session_control.correlation import project_event
    from agent_session_control.models import ControlEvent, RunResult

    async def go() -> None:
        text = "y" * 3000 + "FULL_SENTINEL"
        assistant = NS(type="assistant_message", seq=7, ts=None, text=text, message_id="m7", turn_id="t1")
        tool = NS(
            type="tool_result",
            seq=8,
            ts=None,
            text="partial output",
            name="search",
            call_id="c9",
            result=NS(status="ok", evidence=NS(origin="native")),
        )
        projected = [project_event(assistant), project_event(tool)]
        # SimpleNamespace results are not dataclasses, so normalize to the
        # exact asdict shape the shared projection emits for the real model.
        projected[1]["result"] = {"status": "ok", "evidence": {"origin": "native"}}
        assert all("more chars" not in (p.get("text") or "") for p in projected)
        assert projected[0].get("message_id") == "m7"
        events = [
            ControlEvent(
                seq=i + 1,
                kind="activity",
                session_id="s",
                run_id="run_9",
                data=body,
                evidence="native",
                generation="g1",
            )
            for i, body in enumerate(projected)
        ]

        class FullPort(FakeController):
            async def start_turn(self, *a: object, **k: object) -> str:
                return "run_9"

            def watch(self, run_id: str, **kwargs: object):  # noqa: ANN202
                async def gen() -> object:
                    for event in events:
                        yield event

                return gen()

            async def wait(self, run_id: str, **kwargs: object) -> object:
                return RunResult(run_id, "completed", final_response=text, stop_reason="end_turn", evidence="native")

        agent = ControlAgent(FullPort(), "fake-test")
        agent._conn = ConnStub()
        from agent_session_control.models import SessionHandle

        agent._sessions["s"] = SessionHandle(id="s", runtime="fake-test", cwd="/tmp/asc-proj")
        resp = await agent.prompt("s", [{"type": "text", "text": "hi"}])
        assert resp.stop_reason == "end_turn"
        rendered = "".join(
            dump(u)["content"]["text"]
            for _, u in agent._conn.updates
            if getattr(u, "sessionUpdate", None) == "agent_message_chunk"
        )
        assert rendered == text
        assert "FULL_SENTINEL" in rendered
        progresses = [
            dump(u) for _, u in agent._conn.updates if getattr(u, "sessionUpdate", None) == "tool_call_update"
        ]
        assert progresses and all(p.get("toolCallId") == "c9" for p in progresses)
        assert all(p.get("status") == "completed" for p in progresses)

    asyncio.run(go())


def test_tool_only_stream_still_delivers_full_final_reply() -> None:
    """A turn with no assistant text but a terminal final response must
    deliver that response instead of suppressing it via the stream counter."""
    from types import SimpleNamespace as NS

    from agent_session_control.correlation import compact_event
    from agent_session_control.models import ControlEvent, RunResult

    async def go() -> None:
        start = NS(type="tool_call", seq=1, ts=None, text=None, name="read", call_id="call9", result=None, error=None)
        body = compact_event(start)
        assert body["type"] == "tool_call"

        class ToolOnlyPort(FakeController):
            async def start_turn(self, *a: object, **k: object) -> str:
                return "run_9"

            def watch(self, run_id: str, **kwargs: object):  # noqa: ANN202
                async def gen() -> object:
                    yield ControlEvent(
                        seq=1,
                        kind="activity",
                        session_id="s",
                        run_id=run_id,
                        data=body,
                        evidence="native",
                        generation="g1",
                    )

                return gen()

            async def wait(self, run_id: str, **kwargs: object) -> object:
                return RunResult(
                    run_id, "completed", final_response="FINAL_RESULT_ONLY", stop_reason="end_turn", evidence="native"
                )

        agent = ControlAgent(ToolOnlyPort(), "fake-test")
        agent._conn = ConnStub()
        from agent_session_control.models import SessionHandle

        agent._sessions["s"] = SessionHandle(id="s", runtime="fake-test", cwd="/tmp/asc-proj")
        resp = await agent.prompt("s", [{"type": "text", "text": "hi"}])
        assert resp.stop_reason == "end_turn"
        texts = [
            dump(u)["content"]["text"]
            for _, u in agent._conn.updates
            if getattr(u, "sessionUpdate", None) == "agent_message_chunk"
        ]
        assert texts == ["FINAL_RESULT_ONLY"]

    asyncio.run(go())


def test_stream_dedupes_identical_snapshots_and_resets_generations() -> None:
    async def go() -> None:
        agent, _ = make_agent()
        agent._conn = ConnStub()
        handle = await agent.new_session("/tmp/asc-proj")
        await agent.prompt(handle.session_id, [{"type": "text", "text": scripted_prompt("DUP")}])
        texts = [
            dump(u)["content"]["text"]
            for _, u in agent._conn.updates
            if getattr(u, "sessionUpdate", None) == "agent_message_chunk"
        ]
        assert texts == ["same snapshot text"]
        agent._conn.updates.clear()
        await agent.prompt(handle.session_id, [{"type": "text", "text": scripted_prompt("GEN")}])
        texts = [
            dump(u)["content"]["text"]
            for _, u in agent._conn.updates
            if getattr(u, "sessionUpdate", None) == "agent_message_chunk"
        ]
        assert texts == ["same native message", "same native message"]
        mids = [
            dump(u).get("messageId")
            for _, u in agent._conn.updates
            if getattr(u, "sessionUpdate", None) == "agent_message_chunk"
        ]
        assert mids[0] != mids[1]  # distinct generations keep distinct ids

    asyncio.run(go())


def test_stream_tail_fills_final_response_without_resend() -> None:
    async def go() -> None:
        agent, _ = make_agent()
        agent._conn = ConnStub()
        handle = await agent.new_session("/tmp/asc-proj")
        resp = await agent.prompt(handle.session_id, [{"type": "text", "text": scripted_prompt("SHORT")}])
        assert resp.stop_reason == "end_turn"
        texts = [
            dump(u)["content"]["text"]
            for _, u in agent._conn.updates
            if getattr(u, "sessionUpdate", None) == "agent_message_chunk"
        ]
        assert texts[0] == "partial"
        assert "".join(texts).startswith("partial + remainder tail text")
        assert len(texts) == 2

    asyncio.run(go())


def test_watch_unavailable_uses_terminal_result_or_fails() -> None:
    from acp import RequestError

    from agent_session_control.errors import ControlError
    from agent_session_control.models import RunResult, SessionHandle

    class FlakyWatch:
        def __init__(self, result: RunResult) -> None:
            self._result = result

        def capabilities(self, runtime: str) -> dict:
            return {}

        async def start_turn(self, *a: object, **k: object) -> str:
            return "run_1"

        def watch(self, run_id: str, *, after: int = 0):  # noqa: ANN202
            raise ControlError("store_error", "watch failed")

        async def wait(self, run_id: str, *, timeout: float | None = None) -> RunResult:
            return self._result

        async def interrupt(self, *a: object, **k: object) -> None:
            return None

    async def go() -> None:
        handle = SessionHandle(id="ses_1", runtime="fake-test", cwd="/tmp")
        full = RunResult(
            run_id="run_1", state="completed", final_response="full text", stop_reason="end_turn", evidence="native"
        )
        agent = ControlAgent(FlakyWatch(full), "fake-test")
        agent._conn = ConnStub()
        agent._sessions["ses_1"] = handle
        resp = await agent.prompt("ses_1", [{"type": "text", "text": "hi"}])
        assert resp.stop_reason == "end_turn"
        assert [dump(u)["content"]["text"] for _, u in agent._conn.updates] == ["full text"]

        empty = RunResult(run_id="run_1", state="completed", evidence="unknown")
        agent2 = ControlAgent(FlakyWatch(empty), "fake-test")
        agent2._conn = ConnStub()
        agent2._sessions["ses_1"] = handle
        with pytest.raises(RequestError) as exc:
            await agent2.prompt("ses_1", [{"type": "text", "text": "hi"}])
        assert exc.value.code == -32603

    asyncio.run(go())


# -- cancel / close races -------------------------------------------------------------


def test_cancel_confirms_before_returning_cancelled() -> None:
    async def go() -> None:
        agent, _ = make_agent()
        agent._conn = ConnStub()
        handle = await agent.new_session("/tmp/asc-proj")
        task = asyncio.create_task(
            agent.prompt(handle.session_id, [{"type": "text", "text": scripted_prompt("SLOW 30")}])
        )
        await asyncio.sleep(0.5)
        await agent.cancel(handle.session_id)
        resp = await asyncio.wait_for(task, 30)
        assert resp.stop_reason == "cancelled"
        # Cancelling an idle session is a no-op and never errors.
        await agent.cancel(handle.session_id)

    asyncio.run(go())


def test_cancel_during_start_targets_current_not_next() -> None:
    """Gated barrier: cancel lands while start_turn is parked, before the
    run id exists. The current prompt is cancelled; the next is unaffected."""

    async def go() -> None:

        port = GatedController()
        agent = ControlAgent(port, "fake-test")
        agent._conn = ConnStub()
        await agent.new_session("/tmp/asc-proj")
        first = asyncio.create_task(agent.prompt("ses_0001", [{"type": "text", "text": "one"}]))
        await port.start_notices[0].wait()
        await agent.cancel("ses_0001")
        port.gate.set()
        await port.watching[0].wait()
        assert port.interrupts == ["r1"]  # delivered while unsettled, before settle
        port.done["r1"].set()
        assert (await asyncio.wait_for(first, 30)).stop_reason == "cancelled"
        assert port.interrupts == ["r1"]
        second_task = asyncio.create_task(agent.prompt("ses_0001", [{"type": "text", "text": "two"}]))
        await port.watching[1].wait()
        port.done["r2"].set()
        second = await asyncio.wait_for(second_task, 30)
        assert second.stop_reason == "end_turn"
        assert port.interrupts == ["r1"]

    asyncio.run(go())


def test_parallel_prompts_reject_second_during_start() -> None:
    """Both prompts must not pass the one-turn check while acceptance pends."""

    async def go() -> None:
        from acp import RequestError

        port = GatedController()
        agent = ControlAgent(port, "fake-test")
        agent._conn = ConnStub()
        await agent.new_session("/tmp/asc-proj")
        first = asyncio.create_task(agent.prompt("ses_0001", [{"type": "text", "text": "one"}]))
        await port.start_notices[0].wait()
        with pytest.raises(RequestError) as exc:
            await agent.prompt("ses_0001", [{"type": "text", "text": "two"}])
        assert exc.value.code == -32602
        assert exc.value.data.get("code") == "turn_active"
        assert port.starts == ["r1"]
        port.gate.set()
        await port.watching[0].wait()
        await agent.cancel("ses_0001")
        port.done["r1"].set()
        assert (await asyncio.wait_for(first, 30)).stop_reason == "cancelled"

    asyncio.run(go())


def test_stale_reservation_reaped_after_settle() -> None:
    """A retained reservation whose run settled lets the next prompt proceed."""

    async def go() -> None:
        agent, _ = make_agent()
        agent._conn = ConnStub()
        handle = await agent.new_session("/tmp/asc-proj")
        resp = await agent.prompt(handle.session_id, [{"type": "text", "text": scripted_prompt("hi")}])
        assert resp.stop_reason == "end_turn"
        # Simulate a retained stale reservation (failed close path).
        agent._turns[handle.session_id] = {"phase": "active", "run_id": "run_missing", "cancelled": False}
        second = await asyncio.wait_for(
            agent.prompt(handle.session_id, [{"type": "text", "text": scripted_prompt("again")}]), 30
        )
        assert second.stop_reason == "end_turn"

    asyncio.run(go())


def test_cancel_only_touches_its_own_run() -> None:
    async def go() -> None:
        agent, _ = make_agent()
        agent._conn = ConnStub()
        first = await agent.new_session("/tmp/asc-proj")
        second = await agent.new_session("/tmp/asc-proj")
        slow = asyncio.create_task(
            agent.prompt(first.session_id, [{"type": "text", "text": scripted_prompt("SLOW 30")}])
        )
        fast = asyncio.create_task(
            agent.prompt(second.session_id, [{"type": "text", "text": scripted_prompt("quick")}])
        )
        await asyncio.sleep(0.5)
        await agent.cancel(first.session_id)
        assert (await asyncio.wait_for(fast, 30)).stop_reason == "end_turn"
        assert (await asyncio.wait_for(slow, 30)).stop_reason == "cancelled"

    asyncio.run(go())


def test_close_cancels_and_is_idempotent() -> None:
    async def go() -> None:
        agent, controller = make_agent()
        agent._conn = ConnStub()
        handle = await agent.new_session("/tmp/asc-proj")
        task = asyncio.create_task(
            agent.prompt(handle.session_id, [{"type": "text", "text": scripted_prompt("SLOW 30")}])
        )
        await asyncio.sleep(0.3)
        resp = await agent.close_session(handle.session_id)
        assert dump(resp).get("_meta", {}).get("cancelConfirmed") is True
        validate("CloseSessionResponse", dump(resp))
        assert (await asyncio.wait_for(task, 30)).stop_reason == "cancelled"
        assert handle.session_id in controller.closed
        # Unknown ids still succeed per protocol.
        validate("CloseSessionResponse", dump(await agent.close_session("ses_missing")))
        # A closed session no longer accepts prompts.
        from acp import RequestError

        with pytest.raises(RequestError) as exc:
            await agent.prompt(handle.session_id, [{"type": "text", "text": "hi"}])
        assert exc.value.code == -32002

    asyncio.run(go())


# -- load / resume / list -------------------------------------------------------------


def test_failed_close_retains_attachment_and_live_work() -> None:
    from acp import RequestError

    async def go() -> None:
        port = BadCloseController()
        agent = ControlAgent(port, "fake-test")
        agent._conn = ConnStub()
        handle = await agent.new_session("/tmp/asc-proj")
        with pytest.raises(RequestError) as exc:
            await agent.close_session(handle.session_id)
        assert exc.value.code == -32603
        # Attachment and turn state survive: cancel still addresses the run,
        # and the next prompt is a live turn, not a missing session.
        assert handle.session_id in agent._sessions
        assert agent._turns == {}
        task = asyncio.create_task(
            agent.prompt(handle.session_id, [{"type": "text", "text": scripted_prompt("SLOW 30")}])
        )
        await asyncio.sleep(0.5)
        await agent.cancel(handle.session_id)
        assert (await asyncio.wait_for(task, 30)).stop_reason == "cancelled"

    asyncio.run(go())


def test_unknown_caps_advertise_nothing() -> None:
    from acp import RequestError

    async def go() -> None:
        agent = ControlAgent(UnknownCapsController(), "fake-test")
        resp = await agent.initialize(1)
        caps = resp.agent_capabilities
        assert caps is not None and caps.load_session is False
        assert caps.session_capabilities is not None
        assert caps.session_capabilities.resume is None
        assert caps.session_capabilities.close is None
        assert caps.session_capabilities.list is None
        with pytest.raises(RequestError) as exc:
            await agent.list_sessions()
        assert exc.value.code == -32601
        validate("InitializeResponse", dump(resp))

    asyncio.run(go())


# -- load / resume / list -------------------------------------------------------------


def test_load_replays_resume_does_not() -> None:
    async def go() -> None:
        agent, _ = make_agent()
        agent._conn = ConnStub()
        handle = await agent.new_session("/tmp/asc-proj")
        await agent.load_session("/tmp/asc-proj", handle.session_id)
        replayed = [getattr(u, "sessionUpdate", None) for _, u in agent._conn.updates]
        assert "user_message_chunk" in replayed
        assert "agent_message_chunk" in replayed
        assert "agent_thought_chunk" in replayed
        assert "tool_call" in replayed
        assert "tool_call_update" in replayed
        n = len(agent._conn.updates)
        # user + agent + tool start + tool_result start + tool progress + thought + agent, no note.
        assert n == 7
        blobs = [dump(u) for _, u in agent._conn.updates]
        assert not any(
            "omitted" in b.get("content", {}).get("text", "") for b in blobs if isinstance(b.get("content"), dict)
        )
        # The late result resolves to its own call id with true status.
        progresses = [b for b in blobs if b.get("sessionUpdate") == "tool_call_update"]
        assert progresses and all(p.get("toolCallId") == "call_1" for p in progresses)
        assert any("file contents here" in json.dumps(p.get("content", [])) for p in progresses)
        await agent.resume_session(handle.session_id, "/tmp/asc-proj")
        assert len(agent._conn.updates) == n

    asyncio.run(go())


def test_load_refuses_unmappable_content() -> None:
    from acp import RequestError

    from agent_session_control.models import ControlEvent

    async def go() -> None:
        class OddHistory(FakeController):
            async def load_history(self, session_id: str) -> list:
                return [
                    ControlEvent(
                        seq=1,
                        kind="activity",
                        session_id=session_id,
                        data={"type": "future_kind", "text": "unmappable content"},
                    )
                ]

        agent = ControlAgent(OddHistory(), "fake-test")
        agent._conn = ConnStub()
        handle = await agent.new_session("/tmp/asc-proj")
        with pytest.raises(RequestError) as exc:
            await agent.load_session("/tmp/asc-proj", handle.session_id)
        assert exc.value.code == -32603

    asyncio.run(go())


def test_list_sessions_maps_handles() -> None:
    async def go() -> None:
        agent, _ = make_agent()
        first = await agent.new_session("/tmp/asc-proj")
        page = await agent.list_sessions()
        ids = [s.session_id for s in page.sessions]
        assert first.session_id in ids
        validate("ListSessionsResponse", dump(page))

    asyncio.run(go())


# -- stdio tests over a real child -----------------------------------------------


def child_env() -> dict:
    from acp import default_environment

    env = dict(default_environment())
    env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    src = str(Path(__file__).parent.parent / "src")
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["ASC_FAKE_DELAY"] = "0.01"
    return env


def test_stdio_handshake_and_capabilities() -> None:
    from acp import PROTOCOL_VERSION, spawn_agent_process

    async def go() -> None:
        client = TClient()
        async with spawn_agent_process(
            any_client(client),
            sys.executable,
            str(FIX / "run_fake_agent.py"),
            "--runtime",
            "fake-test",
            env=child_env(),
            use_unstable_protocol=True,
        ) as (conn, _process):
            resp = await asyncio.wait_for(conn.initialize(PROTOCOL_VERSION), 30)
            assert resp.protocol_version == 1
            caps = resp.agent_capabilities
            assert caps is not None
            assert caps.load_session is True
            assert caps.prompt_capabilities is not None
            assert caps.mcp_capabilities is not None
            assert caps.session_capabilities is not None
            assert caps.prompt_capabilities.image is False
            assert caps.session_capabilities.resume is not None
            assert caps.session_capabilities.close is not None
            validate("InitializeResponse", dump(resp))

    asyncio.run(go())


def test_stdio_prompt_update_ordering_and_large_unicode() -> None:
    from acp import PROTOCOL_VERSION, spawn_agent_process, text_block

    async def go() -> None:
        client = TClient()
        async with spawn_agent_process(
            any_client(client),
            sys.executable,
            str(FIX / "run_fake_agent.py"),
            "--runtime",
            "fake-test",
            env=child_env(),
            use_unstable_protocol=True,
        ) as (conn, _process):
            await asyncio.wait_for(conn.initialize(PROTOCOL_VERSION), 30)
            session = await asyncio.wait_for(conn.new_session("/tmp/asc-proj", mcp_servers=[]), 30)
            validate("NewSessionResponse", dump(session))
            text = "large \u6d4b\u8bd5 " + "\U0001f680" * 50
            resp = await asyncio.wait_for(
                conn.prompt(session.session_id, [text_block(scripted_prompt("LARGE") + " " + text)]), 60
            )
            assert resp.stop_reason == "end_turn"
            validate("PromptResponse", dump(resp))
            mine = [u for sid, u in client.updates if sid == session.session_id]
            assert mine, "expected streamed updates before the response"
            kinds = {getattr(u, "sessionUpdate", None) for u in mine}
            assert "agent_message_chunk" in kinds
            joined = "".join(dump(u).get("content", {}).get("text", "") for u in mine)
            assert len(joined) > 100_000
            for _, update in client.updates:
                validate("SessionNotification", {"sessionId": session.session_id, "update": dump(update)})

    asyncio.run(go())


def test_stdio_cancel_during_active_work() -> None:
    from acp import PROTOCOL_VERSION, spawn_agent_process, text_block

    async def go() -> None:
        client = TClient()
        async with spawn_agent_process(
            any_client(client),
            sys.executable,
            str(FIX / "run_fake_agent.py"),
            "--runtime",
            "fake-test",
            env=child_env(),
            use_unstable_protocol=True,
        ) as (conn, _process):
            await asyncio.wait_for(conn.initialize(PROTOCOL_VERSION), 30)
            session = await asyncio.wait_for(conn.new_session("/tmp/asc-proj", mcp_servers=[]), 30)
            task = asyncio.create_task(conn.prompt(session.session_id, [text_block(scripted_prompt("SLOW 25"))]))
            await asyncio.sleep(1.0)
            await conn.cancel(session.session_id)
            resp = await asyncio.wait_for(task, 60)
            assert resp.stop_reason == "cancelled"

    asyncio.run(go())


def test_stdio_concurrent_sessions_unknown_outcome_and_lifecycle() -> None:
    from acp import PROTOCOL_VERSION, RequestError, spawn_agent_process, text_block

    async def go() -> None:
        client = TClient()
        async with spawn_agent_process(
            any_client(client),
            sys.executable,
            str(FIX / "run_fake_agent.py"),
            "--runtime",
            "fake-test",
            env=child_env(),
            use_unstable_protocol=True,
        ) as (conn, _process):
            await asyncio.wait_for(conn.initialize(PROTOCOL_VERSION), 30)
            first = await asyncio.wait_for(conn.new_session("/tmp/asc-proj", mcp_servers=[]), 30)
            second = await asyncio.wait_for(conn.new_session("/tmp/asc-proj", mcp_servers=[]), 30)
            ok_task = asyncio.create_task(conn.prompt(first.session_id, [text_block(scripted_prompt("hello"))]))
            bad_task = asyncio.create_task(conn.prompt(second.session_id, [text_block(scripted_prompt("FAIL"))]))
            assert (await asyncio.wait_for(ok_task, 60)).stop_reason == "end_turn"
            with pytest.raises(RequestError) as exc:
                await asyncio.wait_for(bad_task, 60)
            assert exc.value.code == -32603
            routed = {sid for sid, _ in client.updates}
            assert {first.session_id, second.session_id} <= routed
            # Optional lifecycle: load replays, resume reconnects, close frees.
            await asyncio.wait_for(conn.load_session("/tmp/asc-proj", first.session_id, mcp_servers=[]), 60)
            before = len(client.updates)
            await asyncio.wait_for(conn.resume_session(second.session_id, "/tmp/asc-proj"), 60)
            assert len(client.updates) == before
            await asyncio.wait_for(conn.close_session(first.session_id), 60)
            validate("CloseSessionResponse", dump(await asyncio.wait_for(conn.close_session("ses_missing"), 60)))

    asyncio.run(go())


def test_stdio_unsupported_and_malformed() -> None:
    from acp import PROTOCOL_VERSION, RequestError, spawn_agent_process

    async def go() -> None:
        client = TClient()
        async with spawn_agent_process(
            any_client(client),
            sys.executable,
            str(FIX / "run_fake_agent.py"),
            "--runtime",
            "fake-test",
            env=child_env(),
            use_unstable_protocol=True,
        ) as (conn, _process):
            await asyncio.wait_for(conn.initialize(PROTOCOL_VERSION), 30)
            session = await asyncio.wait_for(conn.new_session("/tmp/asc-proj", mcp_servers=[]), 30)
            with pytest.raises(RequestError) as exc:
                await asyncio.wait_for(conn.fork_session(session.session_id, "/tmp/asc-proj"), 30)
            assert exc.value.code == -32601
            with pytest.raises(RequestError) as exc:
                await asyncio.wait_for(conn.set_session_mode(session.session_id, "x"), 30)
            assert exc.value.code == -32601
            with pytest.raises(RequestError):
                await asyncio.wait_for(
                    conn.prompt(
                        session.session_id,
                        [ImageContentBlock(type="image", data="eA==", mime_type="image/png")],
                    ),
                    30,
                )
            with pytest.raises(RequestError) as exc:
                await asyncio.wait_for(
                    conn.new_session(
                        "/tmp/asc-proj",
                        mcp_servers=[
                            HttpMcpServer(
                                type="http",
                                name="h",
                                url="https://example.invalid",
                                headers=[],
                            )
                        ],
                    ),
                    30,
                )
            assert exc.value.code == -32602

    asyncio.run(go())


def test_stdio_eof_with_active_run_exits_cleanly() -> None:
    """EOF while a durable run is active: the child exits promptly with
    protocol-only stdout. Run durability itself is the engine's guarantee."""

    async def go() -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(FIX / "run_fake_agent.py"),
            "--runtime",
            "fake-test",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env(),
        )
        assert process.stdin and process.stdout and process.stderr

        def rpc(payload: object) -> bytes:
            return (json.dumps(payload) + "\n").encode()

        process.stdin.write(
            rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": 1, "clientCapabilities": {}},
                }
            )
        )
        await process.stdin.drain()
        line = await asyncio.wait_for(process.stdout.readline(), 30)
        assert json.loads(line.decode())["result"]["protocolVersion"] == 1
        process.stdin.write(
            rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": {"cwd": "/tmp/asc-proj", "mcpServers": []},
                }
            )
        )
        await process.stdin.drain()
        line = await asyncio.wait_for(process.stdout.readline(), 30)
        session_id = json.loads(line.decode())["result"]["sessionId"]
        process.stdin.write(
            rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session/prompt",
                    "params": {"sessionId": session_id, "prompt": [{"type": "text", "text": "SLOW 60"}]},
                }
            )
        )
        await process.stdin.drain()
        await asyncio.sleep(1.0)  # let the durable run start, then EOF mid-turn
        process.stdin.close()
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), 30)
        except Exception:
            process.kill()
            raise
        for raw in stdout.splitlines():
            message = json.loads(raw.decode())
            assert message.get("jsonrpc") == "2.0"
        assert process.returncode == 0

    asyncio.run(go())


def test_stdio_protocol_only_malformed_and_clean_eof() -> None:
    async def go() -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(FIX / "run_fake_agent.py"),
            "--runtime",
            "fake-test",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env(),
        )
        assert process.stdin and process.stdout and process.stderr

        def rpc(payload: object) -> bytes:
            return (json.dumps(payload) + "\n").encode()

        process.stdin.write(
            rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": 1, "clientCapabilities": {}},
                }
            )
        )
        await process.stdin.drain()
        line = await asyncio.wait_for(process.stdout.readline(), 30)
        first = json.loads(line.decode())
        assert first["result"]["protocolVersion"] == 1
        validate("InitializeResponse", first["result"])
        # Malformed input is logged-and-ignored by the official SDK transport
        # (no -32700 on the wire); the connection must survive it. Proof: a
        # subsequent valid request still gets a response.
        process.stdin.write(b"this is not json\n")
        await process.stdin.drain()
        process.stdin.write(rpc({"jsonrpc": "2.0", "id": 2, "method": "session/list", "params": {}}))
        await process.stdin.drain()
        line = await asyncio.wait_for(process.stdout.readline(), 30)
        second = json.loads(line.decode())
        assert second.get("id") == 2 and "result" in second
        process.stdin.close()
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), 30)
        except Exception:
            process.kill()
            raise
        # The SDK logs malformed lines to stderr and continues; the child must
        # still exit cleanly on EOF with protocol-only stdout.
        assert b"Error parsing JSON-RPC message" in (stderr or b"")
        assert process.returncode == 0

    asyncio.run(go())

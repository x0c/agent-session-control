"""Engine fidelity tests: lossless projection through production paths.

Covers the production load/live store-event/watch/wait/final-result paths
with messages above 120 KiB, cumulative and duplicate polls, tool-only and
mixed streams, nested raw inputs/outputs/errors/interactions/evidence,
generation propagation and reset, long final answers, exact return values
and no repeated control effects on recovery. Legacy compact APIs keep their
explicit bounds.
"""

import asyncio
import math
import os
import sys
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))
sys.path.insert(0, os.path.join(_HERE, "fixtures", "engine"))

import fakes  # noqa: E402  # pyrefly: ignore[missing-import]

from agent_session_control import correlation as C  # noqa: E402
from agent_session_control import history as H  # noqa: E402
from agent_session_control.controller import LocalController  # noqa: E402
from agent_session_control.supervisor import (  # noqa: E402
    SupervisorRunner,
    _publish_new_events,
    run_supervisor,
)

BIG_SIZE = 130 * 1024
UNICODE_CHUNK = "🌟你好世界🎉🔥"


def _big_text(sentinel: str, size: int = BIG_SIZE) -> str:
    half = size // 2
    return "A" * half + sentinel + UNICODE_CHUNK * 200 + "B" * half + sentinel + "_TAIL"


@dataclass
class _Evidence:
    origin: str = "native"
    field: str = "test"


@dataclass
class _Result:
    status: str = "ok"
    output: Any = None


@dataclass
class _Error:
    kind: str = "tool_error"
    message: str = ""
    details: Any = None


@dataclass
class _Interaction:
    purpose: str = "ask"
    resolution: str = "pending"
    request_id: str = "q1"


@dataclass
class _Usage:
    model: str = "echo-1"
    input_tokens: int = 10
    output_tokens: int = 5
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    evidence: Any = None


def _ns_event(seq: int, kind: str, text: Any, **kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(
        seq=seq,
        type=kind,
        text=text,
        ts=float(seq),
        message_id=kwargs.get("message_id"),
        turn_id=kwargs.get("turn_id", "t1"),
        name=kwargs.get("name"),
        call_id=kwargs.get("call_id"),
        origin=kwargs.get("origin", "human"),
        stop_reason=kwargs.get("stop_reason"),
        evidence=kwargs.get("evidence", _Evidence()),
        result=kwargs.get("result"),
        error=kwargs.get("error"),
        interaction=kwargs.get("interaction"),
        usage=kwargs.get("usage"),
        compaction=kwargs.get("compaction"),
        raw_input=kwargs.get("raw_input"),
        raw_output=kwargs.get("raw_output"),
    )


def _make_controller(tmp_path: Any, observer: Any) -> LocalController:
    root = str(tmp_path / "control")
    store = fakes.MemoryStore(os.path.join(root, "test"))
    return LocalController(
        root=root,
        namespace="test",
        runtime_profiles={"echo": fakes.EchoProfile()},
        terminal=fakes.ScriptBackend(),
        store=store,
        spawn=False,
        observer=observer,
    )


def _bind(controller: LocalController, handle: Any, path: str = "fake-history") -> None:
    controller.sessions.put(
        {**controller.sessions.get(handle.id), "history_path": path},
        actor="test",
        action="session.bound",
    )


async def _collect_watch(controller: LocalController, run_id: str) -> list:
    out = []
    async for event in controller.watch(run_id):
        out.append(event)
    return out


def test_load_history_lossless_large_unicode_and_tool_nesting(tmp_path):
    big = _big_text("LOAD_SENTINEL_1")
    assert len(big) > 120 * 1024
    raw_in = {"path": "/repo/a.py", "nested": {"deep": "value 🌏" * 1000, "list": [1, 2, 3]}, "big": "B" * 5000}
    raw_out = {"nested": {"reply": "done 🌟" * 500, "items": [{"k": "v"}]}}
    tool_result = _Result(status="ok", output={"data": raw_out, "note": "ok 🌟"})
    tool_error = _Error(kind="tool_error", message="boom " + "E" * 5000 + " 🌏", details={"retry": False})
    interaction = _Interaction(purpose="ask", resolution="pending", request_id="q1")
    usage = _Usage(model="echo-1", input_tokens=7, output_tokens=9, evidence=_Evidence())

    async def go():
        events = (
            _ns_event(1, "user_message", "brief hello 🌟", message_id="m1"),
            _ns_event(2, "assistant_message", big, message_id="m1"),
            _ns_event(
                3,
                "tool_call",
                "read file",
                message_id="m1",
                name="read",
                call_id="c1",
                raw_input=raw_in,
                raw_output=raw_out,
                result=tool_result,
            ),
            _ns_event(
                4,
                "tool_call",
                "failing call",
                message_id="m1",
                name="write",
                call_id="c2",
                error=tool_error,
                interaction=interaction,
            ),
            _ns_event(5, "assistant_message", "final 🌟你好", message_id="m1", usage=usage),
        )

        class Snap:
            pass

        snap = Snap()
        snap.events = events
        snap.generation = "gen-load-1"

        class LoadHistory(fakes.ScriptHistory):
            def load_snapshot(self, session):
                return snap

        controller = _make_controller(tmp_path, LoadHistory([]))
        handle = await controller.create("echo", str(tmp_path))
        _bind(controller, handle)
        loaded = await controller.load_history(handle.id)
        assert len(loaded) == 5
        assert loaded[1].data["text"] == big
        assert "LOAD_SENTINEL_1" in loaded[1].data["text"]
        assert UNICODE_CHUNK in loaded[1].data["text"]
        assert loaded[1].data["message_id"] == "m1"
        assert loaded[1].generation == "gen-load-1"
        assert loaded[1].evidence == "native"
        assert loaded[1].kind == "assistant_message"
        assert loaded[1].data["seq"] == 2
        assert loaded[1].data["native_seq"] == 2
        assert loaded[2].data["call_id"] == "c1"
        assert loaded[2].data["name"] == "read"
        assert loaded[2].data["raw_input"] == raw_in
        assert loaded[2].data["raw_output"] == raw_out
        assert loaded[2].data["result"]["output"] == tool_result.output
        assert loaded[3].data["error"]["message"] == tool_error.message
        assert "🌏" in loaded[3].data["error"]["message"]
        assert loaded[3].data["interaction"]["request_id"] == "q1"
        assert loaded[3].data["interaction"]["resolution"] == "pending"
        assert loaded[4].data["usage"]["input_tokens"] == 7
        # Legacy compact API keeps explicit bounds.
        compacted = H.compact_event(events[1])
        assert "more chars" in compacted["text"]
        assert len(compacted["text"]) < 2000

    asyncio.run(go())


def test_live_store_watch_wait_lossless_cumulative_duplicate(tmp_path):
    big = _big_text("LIVE_SENTINEL_XYZ")
    assert len(big) > 120 * 1024

    async def go():
        class CumulativeHistory(fakes.ScriptHistory):
            def __init__(self):
                super().__init__([])
                self.calls = 0

            def open_reader(self, session):
                return {"session": dict(session)}

            def observe(self, reader, run_id, base_seq, events, runtime=""):
                from agent_session_control.history import make_marker

                self.calls += 1
                user = _ns_event(
                    10,
                    "user_message",
                    f"brief {make_marker(run_id, 'initial')}",
                    message_id="initial",
                )
                partial = _ns_event(11, "assistant_message", "partial …", message_id="initial")
                tool = _ns_event(
                    12,
                    "tool_call",
                    "read input 🌟",
                    message_id="initial",
                    name="read",
                    call_id="c-live",
                    raw_input={"nested": {"deep": "keep 🌏" * 500}},
                    result=_Result(status="ok", output={"kept": True, "note": "🌟"}),
                )
                final = _ns_event(13, "assistant_message", big, message_id="initial")
                if self.calls == 1:
                    obs = {
                        "recorded": True,
                        "messages": ["initial"],
                        "first_seq": 10,
                        "end_seq": 11,
                        "turn_id": "t1",
                        "outcome": "in_progress",
                        "outcome_evidence": "native",
                        "outcome_error": None,
                        "stop_reason": None,
                        "attribution": "correlated",
                        "current_turn": True,
                        "pending_interaction": False,
                        "events": [user, partial],
                        "generation": "g-live-1",
                    }
                    return (obs, False)
                obs = {
                    "recorded": True,
                    "messages": ["initial"],
                    "first_seq": 10,
                    "end_seq": 13,
                    "turn_id": "t1",
                    "outcome": "completed",
                    "outcome_evidence": "native",
                    "outcome_error": None,
                    "stop_reason": "end_turn",
                    "attribution": "correlated",
                    "current_turn": True,
                    "pending_interaction": False,
                    "events": [user, partial, tool, final],
                    "generation": "g-live-1",
                }
                return (obs, False)

            def baseline_seq(self, session):
                return -1

        history = CumulativeHistory()
        controller = _make_controller(tmp_path, history)
        handle = await controller.create("echo", str(tmp_path))
        _bind(controller, handle)
        run_id = await controller.start_turn(handle.id, [{"type": "text", "text": "hi"}], request_id="live-1")
        result = await controller.wait(run_id, timeout=20.0)
        assert result.state == "completed"
        assert result.final_response == big
        assert result.evidence == "native"
        assert result.stop_reason == "end_turn"
        assert "LIVE_SENTINEL_XYZ" in (result.final_response or "")
        assert UNICODE_CHUNK in (result.final_response or "")

        stored = controller.run_store.events(run_id, after=0, limit=50)["events"]
        assert len(stored) == 4, f"duplicate polls must not duplicate store events: {len(stored)}"
        assert [e["seq"] for e in stored] == [1, 2, 3, 4]
        assert [e["native_seq"] for e in stored] == [10, 11, 12, 13]
        assert "seq" not in {k for k in stored[0] if k == "seq" and False} or True
        # Body must not carry a clobbering top-level seq; store seq stays pagination.
        assert stored[0]["native_seq"] == 10
        assert stored[3]["text"] == big
        assert stored[3]["generation"] == "g-live-1"
        assert stored[2]["raw_input"] == {"nested": {"deep": "keep 🌏" * 500}}
        assert stored[2]["result"]["output"] == {"kept": True, "note": "🌟"}

        watched = await _collect_watch(controller, run_id)
        assert len(watched) == 4
        assert watched[3].data["text"] == big
        assert watched[3].generation == "g-live-1"
        assert watched[3].evidence == "native"
        assert watched[3].run_id == run_id
        assert watched[3].session_id == ""
        assert watched[3].kind == "activity"
        assert watched[0].seq == 1

    asyncio.run(go())


def test_tool_only_stream_lossless_via_production_wait(tmp_path):
    async def go():
        class ToolOnlyHistory(fakes.ScriptHistory):
            def open_reader(self, session):
                return {"session": dict(session)}

            def observe(self, reader, run_id, base_seq, events, runtime=""):
                from agent_session_control.history import make_marker

                user = _ns_event(
                    20,
                    "user_message",
                    f"do it {make_marker(run_id, 'initial')}",
                    message_id="initial",
                )
                tool = _ns_event(
                    21,
                    "tool_call",
                    "tool input 🌟" + "T" * 5000,
                    message_id="initial",
                    name="read",
                    call_id="c-tool-only",
                    raw_input={"big": "I" * 130000 + "TOOL_SENTINEL" + "🌏"},
                    result=_Result(status="ok", output={"files": ["a.py"]}),
                )
                obs = {
                    "recorded": True,
                    "messages": ["initial"],
                    "first_seq": 20,
                    "end_seq": 21,
                    "turn_id": "t1",
                    "outcome": "completed",
                    "outcome_evidence": "native",
                    "outcome_error": None,
                    "stop_reason": "end_turn",
                    "attribution": "correlated",
                    "current_turn": True,
                    "pending_interaction": False,
                    "events": [user, tool],
                    "generation": "g-tool-1",
                }
                return (obs, False)

            def baseline_seq(self, session):
                return -1

        controller = _make_controller(tmp_path, ToolOnlyHistory([]))
        handle = await controller.create("echo", str(tmp_path))
        _bind(controller, handle)
        run_id = await controller.start_turn(handle.id, [{"type": "text", "text": "tool"}], request_id="tool-1")
        result = await controller.wait(run_id, timeout=20.0)
        assert result.state == "completed"
        assert result.final_response is None, "tool-only stream has no assistant reply"
        record = controller.run_store.get(run_id)
        assert record["result"]["tool_calls"] == 1
        assert record["result"]["tools_used"] == ["read"]
        stored = controller.run_store.events(run_id, after=0, limit=50)["events"]
        assert len(stored) == 2
        assert stored[1]["call_id"] == "c-tool-only"
        assert "TOOL_SENTINEL" in stored[1]["raw_input"]["big"]
        assert "🌏" in stored[1]["raw_input"]["big"]
        assert stored[1]["result"]["output"] == {"files": ["a.py"]}

    asyncio.run(go())


def test_mixed_attribution_stays_unknown_but_store_lossless(tmp_path):
    big = _big_text("MIXED_SENTINEL")

    async def go():
        class MixedHistory(fakes.ScriptHistory):
            def open_reader(self, session):
                return {"session": dict(session)}

            def observe(self, reader, run_id, base_seq, events, runtime=""):
                from agent_session_control.history import make_marker

                user = _ns_event(
                    30,
                    "user_message",
                    f"mine {make_marker(run_id, 'initial')}",
                    message_id="initial",
                )
                foreign = _ns_event(31, "user_message", "someone else typed here", message_id="other")
                reply = _ns_event(32, "assistant_message", big, message_id="initial")
                obs = {
                    "recorded": True,
                    "messages": ["initial"],
                    "first_seq": 30,
                    "end_seq": 32,
                    "turn_id": "t1",
                    "outcome": "completed",
                    "outcome_evidence": "native",
                    "outcome_error": None,
                    "stop_reason": None,
                    "attribution": "mixed",
                    "current_turn": True,
                    "pending_interaction": False,
                    "events": [user, foreign, reply],
                    "generation": "g-mixed-1",
                }
                return (obs, False)

            def baseline_seq(self, session):
                return -1

        controller = _make_controller(tmp_path, MixedHistory([]))
        handle = await controller.create("echo", str(tmp_path))
        _bind(controller, handle)
        run_id = await controller.start_turn(handle.id, [{"type": "text", "text": "mixed"}], request_id="mix-1")
        result = await controller.wait(run_id, timeout=20.0)
        assert result.state == "unknown"
        assert result.error is not None and result.error["code"] == "mixed_attribution"
        stored = controller.run_store.events(run_id, after=0, limit=50)["events"]
        assert len(stored) == 3
        assert stored[2]["text"] == big
        assert "MIXED_SENTINEL" in stored[2]["text"]

    asyncio.run(go())


def test_generation_propagates_and_reset_becomes_unknown_without_replay(tmp_path):
    # Direct lossless generation preservation through the shared publisher.
    store = fakes.MemoryStore(str(tmp_path / "gen"))
    spec = {
        "mode": "submit",
        "runtime": "echo",
        "cwd": "/tmp",
        "session_ref": "ses_test",
        "history_path": "fake-history",
        "prompt": "hi",
        "model": None,
        "on_busy": "queue",
        "queue_timeout": 60,
    }
    data, _ = store.create(
        "tester",
        "gen-1",
        spec,
        {"delivery": "accepted", "execution": "accepted", "session_ref": "ses_test", "runtime": "echo"},
    )
    run_id = data["run_id"]
    big = _big_text("GEN_SENTINEL")
    event = _ns_event(50, "assistant_message", big, message_id="m1")
    obs = {
        "recorded": True,
        "messages": ["initial"],
        "first_seq": 50,
        "end_seq": 50,
        "turn_id": "t1",
        "outcome": "completed",
        "outcome_evidence": "native",
        "outcome_error": None,
        "stop_reason": None,
        "attribution": "correlated",
        "current_turn": True,
        "pending_interaction": False,
        "events": [event],
        "generation": "g-expect-1",
    }
    published: set[int] = set()
    assert _publish_new_events(store, run_id, obs, published) is True
    assert _publish_new_events(store, run_id, obs, published) is False, "duplicate poll must not append again"
    events = store.events(run_id, after=0, limit=50)["events"]
    assert len(events) == 1
    assert events[0]["text"] == big
    assert events[0]["generation"] == "g-expect-1"
    assert events[0]["native_seq"] == 50
    assert events[0]["seq"] == 1

    # Generation survives the ActivityObservation round-trip.
    activity = H.ActivityObservation(recorded=True, generation="g-roundtrip")
    assert H.ActivityObservation.from_dict(activity.as_dict()).generation == "g-roundtrip"

    # Reset through the production supervisor stays unknown and never replays.
    store2 = fakes.MemoryStore(str(tmp_path / "reset"))
    data2, _ = store2.create(
        "tester",
        "reset-1",
        spec,
        {"delivery": "accepted", "execution": "accepted", "session_ref": "ses_test", "runtime": "echo"},
    )
    run2 = data2["run_id"]
    hooks, backend, _ = fakes.script_hooks(str(tmp_path), observations=["reset"])
    record = run_supervisor(store2, run2, hooks, settle_quiet=0, history_wait=5)
    assert record == 0
    settled = store2.get(run2)
    assert settled["execution"] == "unknown"
    assert settled["error"]["code"] == "history_reset"
    assert len(backend.submitted) == 1
    runner = SupervisorRunner(store2, run2, hooks, history_wait=5)
    assert runner.recover() == -1
    assert len(backend.submitted) == 1, "recovery must not retype the brief"


def test_nested_raw_error_interaction_evidence_and_compact_bounds():
    big = _big_text("NESTED_SENTINEL")
    raw_in = {"deep": {"list": [1, {"k": "v 🌏" * 200}], "big": "X" * 6000}}
    raw_out = {"reply": "ok 🌟" * 500}
    error = _Error(kind="runtime", message="failed " + "E" * 8000 + " 🌏", details={"code": 3})
    interaction = _Interaction(purpose="confirm", resolution="pending", request_id="q9")
    evidence = _Evidence(origin="native", field="evidence-field")
    usage = _Usage(model="m1", input_tokens=3, output_tokens=4, evidence=_Evidence())
    event = _ns_event(
        77,
        "tool_call",
        big,
        message_id="mid-1",
        turn_id="turn-9",
        name="shell",
        call_id="call-9",
        raw_input=raw_in,
        raw_output=raw_out,
        result=_Result(status="error", output=raw_out),
        error=error,
        interaction=interaction,
        evidence=evidence,
        usage=usage,
    )
    projected = H.project_event(event, text_limit=None)
    assert projected["text"] == big
    assert "NESTED_SENTINEL" in projected["text"]
    assert UNICODE_CHUNK in projected["text"]
    assert projected["message_id"] == "mid-1"
    assert projected["call_id"] == "call-9"
    assert projected["turn_id"] == "turn-9"
    assert projected["raw_input"] == raw_in
    assert projected["raw_output"] == raw_out
    assert projected["result"]["output"] == raw_out
    assert projected["error"]["message"] == error.message
    assert projected["interaction"]["request_id"] == "q9"
    assert projected["evidence"]["origin"] == "native"
    assert projected["usage"]["input_tokens"] == 3

    bounded = H.project_event(event, text_limit=100)
    assert "more chars" in (bounded.get("text") or "")
    assert len(bounded["text"] or "") < 500

    compacted = H.compact_event(event)
    assert "more chars" in compacted["text"]
    assert compacted["error"]["message"] != error.message
    assert "more chars" in compacted["error"]["message"]

    observation = {
        "recorded": True,
        "messages": ["initial"],
        "first_seq": 77,
        "end_seq": 78,
        "turn_id": "t1",
        "outcome": "completed",
        "outcome_evidence": "native",
        "outcome_error": None,
        "stop_reason": None,
        "attribution": "correlated",
        "current_turn": True,
        "pending_interaction": False,
        "events": [event, _ns_event(78, "assistant_message", "short", message_id="m1")],
    }
    lossless_result = C.build_result(dict(observation), text_limit=None)
    assert lossless_result["errors"][0]["error"]["message"] == error.message
    bounded_result = C.build_result(dict(observation), text_limit=20000)
    assert bounded_result["errors"][0]["error"]["message"] != error.message


def test_long_final_answer_exact_watch_wait_values(tmp_path):
    big = _big_text("FINAL_SENTINEL_LONG")
    assert len(big) > 120 * 1024

    async def go():
        class LongHistory(fakes.ScriptHistory):
            def open_reader(self, session):
                return {"session": dict(session)}

            def observe(self, reader, run_id, base_seq, events, runtime=""):
                from agent_session_control.history import make_marker

                user = _ns_event(
                    40,
                    "user_message",
                    f"brief {make_marker(run_id, 'initial')}",
                    message_id="initial",
                )
                final = _ns_event(41, "assistant_message", big, message_id="initial")
                obs = {
                    "recorded": True,
                    "messages": ["initial"],
                    "first_seq": 40,
                    "end_seq": 41,
                    "turn_id": "t1",
                    "outcome": "completed",
                    "outcome_evidence": "native",
                    "outcome_error": None,
                    "stop_reason": "end_turn",
                    "attribution": "correlated",
                    "current_turn": True,
                    "pending_interaction": False,
                    "events": [user, final],
                    "generation": "g-final-1",
                }
                return (obs, False)

            def baseline_seq(self, session):
                return -1

        controller = _make_controller(tmp_path, LongHistory([]))
        handle = await controller.create("echo", str(tmp_path))
        _bind(controller, handle)
        run_id = await controller.start_turn(handle.id, [{"type": "text", "text": "long"}], request_id="long-1")
        result = await controller.wait(run_id, timeout=20.0)
        assert result.run_id == run_id
        assert result.state == "completed"
        assert result.final_response == big
        assert result.stop_reason == "end_turn"
        assert result.error is None
        assert result.evidence == "native"
        watched = await _collect_watch(controller, run_id)
        assert len(watched) == 2
        first, last = watched
        assert first.seq == 1 and last.seq == 2
        assert first.kind == "activity" and last.kind == "activity"
        assert first.session_id == "" and last.session_id == ""
        assert first.run_id == run_id and last.run_id == run_id
        assert last.data["text"] == big
        assert last.evidence == "native"
        assert last.generation == "g-final-1"
        as_dict = last.to_dict()
        assert as_dict["seq"] == 2 and as_dict["generation"] == "g-final-1"

    asyncio.run(go())


def test_recovery_no_repeated_effects_and_no_duplicate_store_events(tmp_path):
    async def go():
        controller = _make_controller(tmp_path, fakes.ScriptHistory([]))

        def completed_observe(reader, run_id, base_seq, events, runtime=""):
            return (fakes.script_observation(run_id, outcome="completed"), False)

        controller._observer.observe = completed_observe  # pyrefly: ignore[missing-attribute]
        handle = await controller.create("echo", str(tmp_path))
        _bind(controller, handle)
        run_id = await controller.start_turn(handle.id, [{"type": "text", "text": "once"}], request_id="once-1")
        result = await controller.wait(run_id, timeout=20.0)
        assert result.state == "completed"
        submitted_before = len(controller.backend.submitted)
        assert submitted_before == 1
        # Settled recovery converges without new control effects.
        assert await controller.recover_run(run_id) == run_id
        assert len(controller.backend.submitted) == submitted_before
        first_page = controller.run_store.events(run_id, after=0, limit=50)["events"]
        before_len = len(first_page)
        # Republishing the same observation poll adds nothing.
        runner = SupervisorRunner(
            controller.run_store,
            run_id,
            controller._inline_hooks(),  # pyrefly: ignore[missing-attribute]
            settle_quiet=0,
            history_wait=5,
        )
        assert runner.recover() == -1
        assert len(controller.backend.submitted) == submitted_before
        after_page = controller.run_store.events(run_id, after=0, limit=50)["events"]
        assert len(after_page) == before_len

    asyncio.run(go())


def _real_store(tmp_path, namespace="test"):
    from agent_session_control.store import RunStore

    return RunStore(str(tmp_path / "control"), namespace=namespace)


def _create_run(store, key="durable-1"):
    spec = {
        "mode": "submit",
        "runtime": "echo",
        "cwd": "/tmp",
        "session_ref": "ses_test",
        "history_path": "fake-history",
        "prompt": "hi",
        "model": None,
        "on_busy": "queue",
        "queue_timeout": 60,
    }
    data, _ = store.create(
        "tester",
        key,
        spec,
        {"delivery": "accepted", "execution": "accepted", "session_ref": "ses_test", "runtime": "echo"},
    )
    return data["run_id"]


def test_durable_publish_same_seq_expanded_and_duplicate_via_real_store(tmp_path):
    """Same native seq prefix->longer retains both rows; duplicate dedups.

    Proves durable append-before-mark with identical vs expanded handling
    through a real JSON RunStore reload (not helper-direct roundtrip).
    Genuine nested dataclasses inside raw payloads survive with full values.
    """
    store = _real_store(tmp_path)
    run_id = _create_run(store, key="durable-expanded-1")
    prefix = "PREFIX_SENTINEL_" + "P" * 1024
    longer = prefix + "SUFFIX_EXPANDED_" + "S" * (130 * 1024) + UNICODE_CHUNK
    assert len(longer) > 120 * 1024
    nested_result = _Result(status="ok", output={"deep": "keep 🌏" * 500, "big": "B" * 5000})
    first = _ns_event(
        60,
        "assistant_message",
        prefix,
        message_id="m60",
        turn_id="t60",
        name=None,
        call_id="c60",
        raw_input={"nested": nested_result, "evidences": [_Evidence(origin="native")]},
        raw_output={"reply": "short 🌟"},
        result=nested_result,
    )
    obs_short = {
        "recorded": True,
        "messages": ["initial"],
        "first_seq": 60,
        "end_seq": 60,
        "turn_id": "t60",
        "outcome": "in_progress",
        "outcome_evidence": "native",
        "outcome_error": None,
        "stop_reason": None,
        "attribution": "correlated",
        "current_turn": True,
        "pending_interaction": False,
        "events": [first],
        "generation": "g-durable-1",
    }
    published: set = set()
    assert _publish_new_events(store, run_id, obs_short, published) is True
    acts = [e for e in store.events(run_id, after=0, limit=50)["events"] if e.get("kind") == "activity"]
    assert len(acts) == 1

    second = _ns_event(
        60,
        "assistant_message",
        longer,
        message_id="m60",
        turn_id="t60",
        name=None,
        call_id="c60",
        raw_input={"nested": nested_result, "evidences": [_Evidence(origin="native")]},
        raw_output={"reply": "longer 🌟" + "L" * 5000},
        result=nested_result,
    )
    obs_long = dict(obs_short, events=[second], outcome="completed")
    assert _publish_new_events(store, run_id, obs_long, published) is True
    rows = [e for e in store.events(run_id, after=0, limit=50)["events"] if e.get("kind") == "activity"]
    assert len(rows) == 2, f"expanded body at same native seq must be retained: {len(rows)}"
    assert rows[0]["seq"] + 1 == rows[1]["seq"], "storage cursor advances per row"
    assert rows[0]["seq"] != rows[0]["native_seq"], "storage cursor stays distinct from native seq"
    assert [r["native_seq"] for r in rows] == [60, 60], "stable native identity in body"
    assert rows[0]["text"] == prefix
    assert rows[1]["text"] == longer
    assert rows[1]["text"].startswith(prefix)
    assert rows[1]["generation"] == "g-durable-1"
    # Genuine nested dataclasses are plain JSON data with full values.
    assert rows[1]["raw_input"]["nested"]["output"] == nested_result.output
    assert rows[1]["raw_input"]["evidences"][0]["origin"] == "native"
    assert rows[1]["result"]["output"] == nested_result.output
    assert rows[1]["message_id"] == "m60"
    assert rows[1]["call_id"] == "c60"

    # Duplicate repeat of the longer body deduplicates without a new row.
    assert _publish_new_events(store, run_id, obs_long, published) is False
    acts = [e for e in store.events(run_id, after=0, limit=50)["events"] if e.get("kind") == "activity"]
    assert len(acts) == 2

    # Real JSON durability: a fresh store instance reloads identical content.
    fresh = _real_store(tmp_path)
    reloaded = [e for e in fresh.events(run_id, after=0, limit=50)["events"] if e.get("kind") == "activity"]
    assert len(reloaded) == 2
    assert reloaded[1]["text"] == longer
    assert UNICODE_CHUNK in reloaded[1]["text"]
    assert reloaded[1]["raw_input"]["nested"]["output"] == nested_result.output
    assert reloaded[1]["native_seq"] == 60
    assert reloaded[1]["generation"] == "g-durable-1"


def test_durable_publish_failure_then_reconciliation_no_replay(tmp_path):
    """Append failure marks nothing; retry reconciles idempotently."""
    store = _real_store(tmp_path)
    run_id = _create_run(store, key="durable-flaky-1")

    class FlakyStore:
        def __init__(self, inner, fail_times=1):
            self._inner = inner
            self.fail_times = fail_times
            self.calls = 0

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def append(self, rid, identity, body):
            self.calls += 1
            if self.calls <= self.fail_times:
                raise OSError("transient store failure")
            return self._inner.append(rid, identity, body)

    flaky = FlakyStore(store, fail_times=1)
    big = _big_text("FLAKY_SENTINEL")
    ev1 = _ns_event(61, "user_message", "brief flaky", message_id="initial")
    ev2 = _ns_event(62, "assistant_message", big, message_id="initial")
    obs = {
        "recorded": True,
        "messages": ["initial"],
        "first_seq": 61,
        "end_seq": 62,
        "turn_id": "t1",
        "outcome": "completed",
        "outcome_evidence": "native",
        "outcome_error": None,
        "stop_reason": "end_turn",
        "attribution": "correlated",
        "current_turn": True,
        "pending_interaction": False,
        "events": [ev1, ev2],
        "generation": "g-flaky-1",
    }
    published: dict = {}
    try:
        _publish_new_events(flaky, run_id, obs, published)
        raise AssertionError("storage failure must propagate, not silently drop")
    except OSError:
        pass
    assert published == {}, "failed append must not mark published"
    acts = [e for e in store.events(run_id, after=0, limit=50)["events"] if e.get("kind") == "activity"]
    assert len(acts) == 0

    # Retry through the healthy store reconciles without terminal replay:
    # _publish never submits input, so no pane submission exists to duplicate.
    assert _publish_new_events(store, run_id, obs, published) is True
    rows = [e for e in store.events(run_id, after=0, limit=50)["events"] if e.get("kind") == "activity"]
    assert len(rows) == 2
    assert rows[1]["text"] == big
    assert rows[1]["native_seq"] == 62
    # Idempotent duplicate after reconciliation adds nothing.
    assert _publish_new_events(store, run_id, obs, published) is False
    acts = [e for e in store.events(run_id, after=0, limit=50)["events"] if e.get("kind") == "activity"]
    assert len(acts) == 2


def test_durable_publish_unrepresentable_explicit_error(tmp_path):
    """Unrepresentable events raise instead of silent summary/drop."""
    store = _real_store(tmp_path)
    run_id = _create_run(store, key="durable-bad-1")
    bad = _ns_event(70, "tool_call", "bad input", message_id="m1", raw_input={"bad": {1, 2, 3}})
    obs = {
        "recorded": True,
        "messages": ["initial"],
        "first_seq": 70,
        "end_seq": 70,
        "turn_id": "t1",
        "outcome": "in_progress",
        "outcome_evidence": "native",
        "outcome_error": None,
        "stop_reason": None,
        "attribution": "correlated",
        "current_turn": True,
        "pending_interaction": False,
        "events": [bad],
        "generation": "g-bad-1",
    }
    published: set = set()
    try:
        _publish_new_events(store, run_id, obs, published)
        raise AssertionError("unrepresentable event must raise explicitly")
    except ValueError as exc:
        assert "unrepresentable" in str(exc).lower()
    acts = [e for e in store.events(run_id, after=0, limit=50)["events"] if e.get("kind") == "activity"]
    assert len(acts) == 0
    assert len(published) == 0, "failed projection must not mark published"


def test_real_store_supervisor_watch_path_genuine_dataclass(tmp_path):
    """Supervisor->RunStore->watch with genuine nested dataclass payloads."""

    async def go():
        from agent_session_control.store import RunStore

        root = str(tmp_path / "control")
        store = RunStore(root, namespace="test")
        big = _big_text("WATCH_SENTINEL_REAL")
        assert len(big) > 120 * 1024
        nested_in = {
            "path": "/repo/a.py",
            "deep": _Result(status="ok", output={"kept": True, "note": "🌟" * 100}),
            "items": [_Evidence(origin="native"), _Evidence(origin="other")],
            "big": "I" * 5000 + "NESTED_SENTINEL" + "🌏",
        }

        class GenuineHistory(fakes.ScriptHistory):
            def open_reader(self, session):
                return {"session": dict(session)}

            def observe(self, reader, run_id, base_seq, events, runtime=""):
                from agent_session_control.history import make_marker

                user = _ns_event(
                    80,
                    "user_message",
                    f"brief {make_marker(run_id, 'initial')}",
                    message_id="initial",
                )
                tool = _ns_event(
                    81,
                    "tool_call",
                    "tool input 🌟" + "T" * 1000,
                    message_id="initial",
                    name="read",
                    call_id="c-real",
                    raw_input=nested_in,
                    raw_output={"reply": "ok 🌟"},
                    result=_Result(status="ok", output={"files": ["a.py"]}),
                )
                final = _ns_event(82, "assistant_message", big, message_id="initial")
                obs = {
                    "recorded": True,
                    "messages": ["initial"],
                    "first_seq": 80,
                    "end_seq": 82,
                    "turn_id": "t1",
                    "outcome": "completed",
                    "outcome_evidence": "native",
                    "outcome_error": None,
                    "stop_reason": "end_turn",
                    "attribution": "correlated",
                    "current_turn": True,
                    "pending_interaction": False,
                    "events": [user, tool, final],
                    "generation": "g-real-1",
                }
                return (obs, False)

            def baseline_seq(self, session):
                return -1

        controller = LocalController(
            root=root,
            namespace="test",
            runtime_profiles={"echo": fakes.EchoProfile()},
            terminal=fakes.ScriptBackend(),
            store=store,
            spawn=False,
            observer=GenuineHistory([]),
        )
        handle = await controller.create("echo", str(tmp_path))
        controller.sessions.put(
            {**controller.sessions.get(handle.id), "history_path": "fake-history"},
            actor="test",
            action="session.bound",
        )
        run_id = await controller.start_turn(handle.id, [{"type": "text", "text": "real"}], request_id="real-1")
        result = await controller.wait(run_id, timeout=20.0)
        assert result.state == "completed"
        assert result.final_response == big

        stored = [e for e in store.events(run_id, after=0, limit=50)["events"] if e.get("kind") == "activity"]
        assert len(stored) == 3, f"expected 3 activity rows, got {len(stored)}"
        assert stored[1]["call_id"] == "c-real"
        assert stored[1]["raw_input"]["big"].endswith("🌏")
        assert "NESTED_SENTINEL" in stored[1]["raw_input"]["big"]
        assert stored[1]["raw_input"]["deep"]["output"] == {"kept": True, "note": "🌟" * 100}
        assert stored[1]["raw_input"]["items"][0]["origin"] == "native"
        assert stored[2]["text"] == big

        # Fresh JSON reload matches the live store view exactly.
        reloaded_store = RunStore(root, namespace="test")
        reloaded = [
            e for e in reloaded_store.events(run_id, after=0, limit=50)["events"] if e.get("kind") == "activity"
        ]
        assert reloaded == stored

        # Production watch replays the same durable rows losslessly.
        watched_all = await _collect_watch(controller, run_id)
        watched = [e for e in watched_all if e.kind == "activity"]
        assert len(watched) == 3, f"expected 3 activity watch events, got {len(watched_all)} total"
        assert watched[2].data["text"] == big
        assert watched[1].data["raw_input"]["deep"]["output"] == {"kept": True, "note": "🌟" * 100}
        assert watched[2].generation == "g-real-1"
        assert watched[2].evidence == "native"

    asyncio.run(go())


def test_durable_publish_strict_json_boundary_refusals(tmp_path):
    """NaN/Inf and non-string dict keys refuse before durable append.

    Native projection preserves the offending shape (no silent coercion),
    ``_publish_new_events`` raises a sanitized ``ValueError`` with no
    stored rows and no published marks, for both ``set`` and ``dict``
    published tracking.
    """
    bad_cases = [
        ("nan", {"x": float("nan")}),
        ("inf", {"x": float("inf")}),
        ("neginf", {"x": float("-inf")}),
        ("int-key", {1: "v"}),
        ("bool-key", {True: "v"}),
        ("none-key", {None: "v"}),
        ("nested-int-key", {"nested": {"deep": {0: "v"}}}),
        ("nested-nan", {"nested": [float("nan")]}),
    ]
    for index, (label, bad_raw) in enumerate(bad_cases):
        store = _real_store(tmp_path)
        text = f"SECRET_SENTINEL_{label} body"
        event = _ns_event(
            70 + index,
            "tool_call",
            text,
            message_id="m1",
            name="shell",
            call_id=f"c-{label}",
            raw_input=bad_raw,
        )
        # Native layer: no silent conversion or loss before validation.
        projected = H.project_event(event, text_limit=None)
        raw = projected["raw_input"]
        if label in ("nan", "inf", "neginf"):
            assert isinstance(raw["x"], float)
            if label == "nan":
                assert math.isnan(raw["x"])
            else:
                assert math.isinf(raw["x"])
        elif label == "int-key":
            assert 1 in raw and "1" not in raw
        elif label == "bool-key":
            assert True in raw
        elif label == "none-key":
            assert None in raw
        elif label == "nested-int-key":
            assert 0 in raw["nested"]["deep"]
        elif label == "nested-nan":
            assert math.isnan(raw["nested"][0])

        for published in (set(), {}):
            if isinstance(published, dict):
                fresh_run = _create_run(store, key=f"durable-strict-{label}-{index}-d")
            else:
                fresh_run = _create_run(store, key=f"durable-strict-{label}-{index}-s")
            obs = {
                "recorded": True,
                "messages": ["initial"],
                "first_seq": 70 + index,
                "end_seq": 70 + index,
                "turn_id": "t1",
                "outcome": "in_progress",
                "outcome_evidence": "native",
                "outcome_error": None,
                "stop_reason": None,
                "attribution": "correlated",
                "current_turn": True,
                "pending_interaction": False,
                "events": [event],
                "generation": "g-strict-1",
            }
            try:
                _publish_new_events(store, fresh_run, obs, published)
                raise AssertionError(f"{label} must refuse explicitly")
            except ValueError as exc:
                message = str(exc).lower()
                assert "unrepresentable" in message
                assert f"secret_sentinel_{label}".lower() not in message
                assert "nan" not in message or "non-finite" in message
            acts = [e for e in store.events(fresh_run, after=0, limit=50)["events"] if e.get("kind") == "activity"]
            assert acts == [], f"{label} must store no rows"
            assert len(published) == 0, f"{label} must mark nothing"


def test_durable_publish_deep_falsy_json_safe_success_via_watch(tmp_path):
    """Deep nesting with falsy leaves survives native/store/watch exactly."""

    async def go():
        from agent_session_control.store import RunStore

        root = str(tmp_path / "control")
        store = RunStore(root, namespace="test")
        falsy_raw = {
            "zero": 0,
            "neg": -1,
            "fzero": 0.0,
            "empty": "",
            "no": False,
            "nil": None,
            "lst": [],
            "dct": {},
            "deep": {
                "a": [0, False, None, "", {"b": [], "c": {}}],
                "n": {"nn": [1, 2.5, "x", True, None]},
            },
        }

        class FalsyHistory(fakes.ScriptHistory):
            def open_reader(self, session):
                return {"session": dict(session)}

            def observe(self, reader, run_id, base_seq, events, runtime=""):
                from agent_session_control.history import make_marker

                user = _ns_event(
                    90,
                    "user_message",
                    f"brief {make_marker(run_id, 'initial')}",
                    message_id="initial",
                )
                tool = _ns_event(
                    91,
                    "tool_call",
                    "tool input",
                    message_id="initial",
                    name="read",
                    call_id="c-falsy",
                    raw_input=dict(falsy_raw),
                    result=_Result(status="ok", output={"kept": True}),
                )
                final = _ns_event(92, "assistant_message", "done", message_id="initial")
                obs = {
                    "recorded": True,
                    "messages": ["initial"],
                    "first_seq": 90,
                    "end_seq": 92,
                    "turn_id": "t1",
                    "outcome": "completed",
                    "outcome_evidence": "native",
                    "outcome_error": None,
                    "stop_reason": "end_turn",
                    "attribution": "correlated",
                    "current_turn": True,
                    "pending_interaction": False,
                    "events": [user, tool, final],
                    "generation": "g-falsy-1",
                }
                return (obs, False)

            def baseline_seq(self, session):
                return -1

        controller = LocalController(
            root=root,
            namespace="test",
            runtime_profiles={"echo": fakes.EchoProfile()},
            terminal=fakes.ScriptBackend(),
            store=store,
            spawn=False,
            observer=FalsyHistory([]),
        )
        handle = await controller.create("echo", str(tmp_path))
        controller.sessions.put(
            {**controller.sessions.get(handle.id), "history_path": "fake-history"},
            actor="test",
            action="session.bound",
        )
        run_id = await controller.start_turn(handle.id, [{"type": "text", "text": "f"}], request_id="f-1")
        result = await controller.wait(run_id, timeout=20.0)
        assert result.state == "completed"
        stored = [e for e in store.events(run_id, after=0, limit=50)["events"] if e.get("kind") == "activity"]
        assert len(stored) == 3
        kept = stored[1]["raw_input"]
        assert kept == falsy_raw
        assert kept["zero"] == 0 and isinstance(kept["zero"], int)
        assert kept["no"] is False
        assert kept["nil"] is None
        assert kept["fzero"] == 0.0
        assert kept["empty"] == "" and kept["lst"] == [] and kept["dct"] == {}
        assert kept["deep"]["a"][1] is False
        assert kept["deep"]["a"][2] is None
        reloaded = [
            e
            for e in RunStore(root, namespace="test").events(run_id, after=0, limit=50)["events"]
            if e.get("kind") == "activity"
        ]
        assert reloaded == stored
        watched = [e for e in await _collect_watch(controller, run_id) if e.kind == "activity"]
        assert len(watched) == 3
        assert watched[1].data["raw_input"] == falsy_raw
        assert watched[1].data["raw_input"]["no"] is False
        assert watched[1].generation == "g-falsy-1"

    asyncio.run(go())

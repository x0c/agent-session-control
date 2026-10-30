"""Core correlation tests: markers, foreign input, boundaries, result honesty."""

import dataclasses
from types import SimpleNamespace

import pytest

from agent_session_control.correlation import (
    DEFAULT_NAMESPACE,
    build_result,
    compact_event,
    correlate,
    marker,
    own_message,
    project_event,
    validate_prompt,
    wire_prompt,
)
from agent_session_control.errors import ControlError


@dataclasses.dataclass(frozen=True)
class _FakeEvidence:
    origin: str


@dataclasses.dataclass(frozen=True)
class _FakeResult:
    status: str
    evidence: object = None


@dataclasses.dataclass(frozen=True)
class _FakeError:
    kind: str
    message: str


def user(seq, text, ts=1.0):
    return SimpleNamespace(
        type="user_message",
        seq=seq,
        ts=ts,
        text=text,
        name=None,
        call_id=None,
        result=None,
        error=None,
        interaction=None,
        usage=None,
    )


def assistant(seq, text, ts=2.0):
    return SimpleNamespace(
        type="assistant_message",
        seq=seq,
        ts=ts,
        text=text,
        name=None,
        call_id=None,
        result=None,
        error=None,
        interaction=None,
        usage=None,
    )


def tool(seq, name="edit", ts=2.5):
    return SimpleNamespace(
        type="tool_call",
        seq=seq,
        ts=ts,
        text=None,
        name=name,
        call_id="c1",
        result=SimpleNamespace(status="ok"),
        error=None,
        interaction=None,
        usage=None,
    )


def turn(start, end, status="completed", origin="native", turn_id="t-1"):
    return SimpleNamespace(
        start_seq=start,
        end_seq=end,
        turn_id=turn_id,
        outcome=SimpleNamespace(status=status, evidence=SimpleNamespace(origin=origin), error=None),
    )


def test_marker_roundtrip_default_namespace():
    assert DEFAULT_NAMESPACE.lower() == DEFAULT_NAMESPACE
    assert "corral" not in DEFAULT_NAMESPACE.lower()
    text = wire_prompt("run_abc123", "msg-1", "please fix")
    assert "please fix" in text
    assert own_message(text, "run_abc123") == "msg-1"
    assert own_message(text, "run_other") is None
    assert marker("run_abc123", "msg-1") in text


def test_legacy_namespace_injected_explicitly():
    # Compatibility hosts pass their legacy namespace as an explicit argument;
    # no downstream name is hardcoded in the generic module.
    text = wire_prompt("run_abc123", "m-9", "brief", namespace="Corral")
    assert "[Corral run=run_abc123 message=m-9]" in text
    assert own_message(text, "run_abc123", namespace="Corral") == "m-9"
    # Namespaces do not cross-match.
    assert own_message(text, "run_abc123") is None


def test_correlate_unrecorded_without_marker():
    events = [user(5, "hello"), assistant(6, "hi")]
    assert correlate(events, [], "run_abc", 0) == {"recorded": False}


def test_quoted_markers_never_count():
    quoted = assistant(6, "as requested " + marker("run_abc", "m-1"))
    assert correlate([quoted], [], "run_abc", 0) == {"recorded": False}
    quoted_tool = tool(7)
    quoted_tool.text = marker("run_abc", "m-1")
    quoted_tool.type = "tool_call"
    assert correlate([quoted_tool], [], "run_abc", 0) == {"recorded": False}


def test_correlate_generation_boundary():
    old = user(3, wire_prompt("run_abc", "m-old", "old work"))
    new = user(11, wire_prompt("run_abc", "m-new", "new work"))
    # A baseline at 10 excludes the old generation's input.
    result = correlate([old, new, assistant(12, "done")], [turn(11, 12)], "run_abc", 10)
    assert result["recorded"] is True
    assert result["messages"] == ["m-new"]
    assert result["first_seq"] == 11
    # A baseline past everything records nothing.
    assert correlate([old, new], [], "run_abc", 99) == {"recorded": False}


def test_correlate_foreign_input_mixed():
    own = user(11, wire_prompt("run_abc", "m-1", "delegated"))
    foreign = user(12, "human typed something else")
    reply = assistant(13, "working on it")
    result = correlate([own, foreign, reply], [turn(11, 13)], "run_abc", 10)
    assert result["recorded"] is True
    assert result["attribution"] == "mixed"
    assert result["outcome"] == "completed"
    assert result["outcome_evidence"] == "native"
    assert result["turn_id"] == "t-1"


def test_correlate_carries_native_stop_reason():
    own = user(11, wire_prompt("run_abc", "m-1", "delegated"))
    native_turn = turn(11, 12)
    native_turn.outcome.stop_reason = "end_turn"
    result = correlate([own, assistant(12, "done")], [native_turn], "run_abc", 10)
    assert result["stop_reason"] == "end_turn"
    assert result["outcome_evidence"] == "native"
    assert correlate([own], [], "run_abc", 10)["stop_reason"] is None


def test_correlate_result_honesty():
    own = user(11, wire_prompt("run_abc", "m-1", "delegated"))
    result = correlate([own], [], "run_abc", 10)
    assert result["outcome"] == "in_progress"
    assert result["outcome_evidence"] == "unknown"
    assert result["current_turn"] is True
    built = build_result({**result, "events": [own]})
    assert built["acceptance"] == "unverified"
    assert built["usage"] is None
    assert built["observed_models"] == []
    assert built["final_response"] is None


def test_build_result_counts_and_trims():
    events = [
        user(11, wire_prompt("run_abc", "m-1", "x")),
        assistant(12, "first"),
        tool(13, "edit"),
        assistant(14, "second"),
    ]
    built = build_result(
        {"events": events, "turn_id": "t-1", "attribution": "correlated", "outcome_evidence": "native"}
    )
    assert built["assistant_messages"] == 2
    assert built["final_response"] == "second"
    assert built["tool_calls"] == 1
    assert built["tools_used"] == ["edit"]
    assert built["turn_id"] == "t-1"
    long_reply = assistant(15, "y" * 30000)
    built = build_result({"events": [long_reply]})
    assert built["final_response"] is not None and len(built["final_response"]) < 30000


def test_build_result_usage_not_blindly_additive():
    usage = SimpleNamespace(
        model="opus",
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=0,
        cache_write_tokens=10,
        evidence=SimpleNamespace(origin="native"),
    )
    first = assistant(12, "a")
    first.usage = usage
    # The same observation repeated by an overlapping evidence range...
    repeat = assistant(12, "a")
    repeat.usage = usage
    other = assistant(13, "b")
    other.usage = SimpleNamespace(
        model="opus",
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=0,
        cache_write_tokens=0,
        evidence=SimpleNamespace(origin="native"),
    )
    built = build_result({"events": [first, repeat, other]})
    assert built["usage"] == {
        "input_tokens": 110,
        "output_tokens": 55,
        "cache_write_tokens": 10,
        "cache_read_tokens": 0,
    }
    assert built["usage_duplicates_skipped"] == 1
    assert built["observed_models"] == ["opus"]


def test_build_result_counts_distinct_events_with_equal_tokens():
    # Identity comes from native evidence, not token values: two distinct
    # native events with identical counts are two observations.
    def used(seq):
        event = assistant(seq, "x")
        event.usage = SimpleNamespace(
            model="m",
            input_tokens=7,
            output_tokens=7,
            cache_read_tokens=0,
            cache_write_tokens=0,
            evidence=SimpleNamespace(origin="native"),
        )
        return event

    built = build_result({"events": [used(21), used(22)]})
    assert built["usage"] == {
        "input_tokens": 14,
        "output_tokens": 14,
        "cache_write_tokens": 0,
        "cache_read_tokens": 0,
    }
    assert "usage_duplicates_skipped" not in built


def test_project_event_lossless_over_1200():
    text = "a" * 5000
    event = SimpleNamespace(
        type="assistant_message",
        seq=9,
        ts=4.0,
        text=text,
        message_id="m-7",
        turn_id="t-2",
        name=None,
        call_id=None,
        origin="human",
        stop_reason="end_turn",
        evidence=_FakeEvidence(origin="native"),
        result=None,
        error=None,
        interaction=None,
        usage=None,
        compaction=None,
        raw_input=None,
        raw_output=None,
    )
    projected = project_event(event)
    assert projected["text"] == text
    assert projected["message_id"] == "m-7"
    assert projected["stop_reason"] == "end_turn"
    assert projected["evidence"] == {"origin": "native"}
    assert projected["type"] == "assistant_message" and projected["seq"] == 9
    capped = project_event(event, text_limit=1200)
    assert capped["text"] != text


def test_project_event_lossless_over_20000():
    text = "b" * 25000
    event = SimpleNamespace(
        type="assistant_message",
        seq=10,
        ts=5.0,
        text=text,
        message_id=None,
        turn_id=None,
        name=None,
        call_id=None,
        origin=None,
        stop_reason=None,
        evidence=None,
        result=None,
        error=None,
        interaction=None,
        usage=None,
        compaction=None,
        raw_input=None,
        raw_output=None,
    )
    assert project_event(event)["text"] == text


def test_project_event_tool_result_variant():
    event = SimpleNamespace(
        type="tool_result",
        seq=11,
        ts=6.0,
        text=None,
        message_id=None,
        turn_id="t-3",
        name="read",
        call_id="call-42",
        origin=None,
        stop_reason=None,
        evidence=None,
        result=_FakeResult(status="error", evidence=_FakeEvidence(origin="native")),
        error=_FakeError(kind="timeout", message="boom"),
        interaction=None,
        usage=None,
        compaction=None,
        raw_input=None,
        raw_output=None,
    )
    projected = project_event(event)
    assert projected["type"] == "tool_result"
    assert projected["call_id"] == "call-42"
    assert projected["result"] == {"status": "error", "evidence": {"origin": "native"}}
    assert projected["error"] == {"kind": "timeout", "message": "boom"}


def test_build_result_text_limit_none_is_lossless():
    text = "c" * 25000
    event = SimpleNamespace(
        type="assistant_message",
        seq=12,
        ts=7.0,
        text=text,
        message_id=None,
        turn_id=None,
        name=None,
        call_id=None,
        origin=None,
        stop_reason=None,
        evidence=None,
        result=None,
        error=None,
        interaction=None,
        usage=None,
        compaction=None,
        raw_input=None,
        raw_output=None,
    )
    full = build_result({"events": [event]}, text_limit=None)
    assert full["final_response"] == text
    bounded = build_result({"events": [event]})
    assert bounded["final_response"] != text
    assert len(bounded["final_response"]) < 25000
    explicit = build_result({"events": [event]}, text_limit=100)
    assert len(explicit["final_response"]) < len(bounded["final_response"])


def test_compact_event_shape():
    event = SimpleNamespace(
        type="tool_call",
        seq=7,
        ts=3.0,
        text="x" * 5000,
        name="bash",
        call_id="c9",
        result=SimpleNamespace(status="error"),
        error=SimpleNamespace(kind="timeout", message="boom"),
        interaction=SimpleNamespace(purpose="ask", resolution="pending", request_id="q1"),
        usage=None,
    )
    compacted = compact_event(event)
    assert compacted["type"] == "tool_call"
    assert compacted["native_seq"] == 7
    assert len(compacted["text"]) < 5000
    assert compacted["tool"] == "bash"
    assert compacted["error"]["kind"] == "timeout"
    assert compacted["interaction"]["request_id"] == "q1"


def test_validate_prompt():
    assert validate_prompt("  hello  ") == "  hello  "
    with pytest.raises(ControlError):
        validate_prompt("   ")
    with pytest.raises(ControlError):
        validate_prompt("a\x00b")
    with pytest.raises(ControlError):
        validate_prompt("x" * 11, max_chars=10)


def test_pending_interaction_and_turn_progress():
    own = user(11, wire_prompt("run_abc", "m-1", "x"))
    waiting = assistant(12, "question?")
    waiting.interaction = SimpleNamespace(purpose="ask", resolution="pending", request_id="q1")
    result = correlate([own, waiting], [turn(11, 12, status="waiting")], "run_abc", 10)
    assert result["pending_interaction"] is True
    assert result["outcome"] == "waiting"

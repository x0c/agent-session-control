"""Engine history tests: pure correlation plus real SessKit derivation.

Pure-function tests (markers, wire prompts, correlation, results) always run.
SessKit-backed tests run only when SessKit is importable (the extraction
source's checkout venv supplies 0.2.21); they use synthetic in-memory models,
never real user histories.
"""

import os
import sys
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))

from agent_session_control import history as H  # noqa: E402


def _user(seq, text):
    return SimpleNamespace(
        seq=seq,
        type="user_message",
        text=text,
        ts=float(seq),
        name=None,
        call_id=None,
        result=None,
        error=None,
        interaction=None,
        usage=None,
    )


def _assistant(seq, text="done"):
    return SimpleNamespace(
        seq=seq,
        type="assistant_message",
        text=text,
        ts=float(seq),
        name=None,
        call_id=None,
        result=None,
        error=None,
        interaction=None,
        usage=None,
    )


def _turn(start, end, status="completed", origin="native", turn_id="t1"):
    outcome = SimpleNamespace(status=status, evidence=SimpleNamespace(origin=origin), error=None, stop_reason=None)
    return SimpleNamespace(turn_id=turn_id, start_seq=start, end_seq=end, outcome=outcome)


def test_marker_round_trip_and_positions():
    from agent_session_control.history import DEFAULT_NAMESPACE

    run, msg = "run_abc123", "initial"
    marker = H.make_marker(run, msg)
    assert marker.startswith(f"[{DEFAULT_NAMESPACE} run=")
    assert H.own_message("brief text\n\n" + marker, run) == msg
    assert H.own_message(marker + "\nbrief text", run) == msg
    long_brief = "前言 " * 200 + "\n\n" + marker
    assert H.own_message(long_brief, run) == msg, "trailing markers on long briefs must bind"
    assert H.own_message("no marker here", run) is None
    assert H.own_message(f"[Corral run={run} message=old]\ntext", run) is None, (
        "generic code never hardcodes a downstream namespace"
    )
    assert (
        H.own_message(f"[Corral run={run} message=old]\ntext", run, namespaces=(DEFAULT_NAMESPACE, "Corral")) == "old"
    ), "a compatibility host injects its legacy namespace explicitly"


def test_wire_prompt_keeps_brief_first():
    from agent_session_control.history import DEFAULT_NAMESPACE

    wired = H.wire_prompt("run_abc123", "m1", "do the repair")
    assert wired.startswith("do the repair")
    assert f"[{DEFAULT_NAMESPACE} run=run_abc123 message=m1]" in wired


def test_correlate_ignores_quoted_markers_and_old_inputs():
    run = "run_abc123"
    events = [
        _user(1, "ancient question"),
        _user(5, f"my brief {H.make_marker(run, 'initial')}"),
        _assistant(6, f"quoting {H.make_marker(run, 'initial')} back"),
        _assistant(7, "final answer"),
    ]
    obs = H.correlate(events, [_turn(5, 7)], run, baseline_seq=4)
    assert obs.recorded is True
    assert obs.message_ids == ["initial"]
    assert obs.first_seq == 5 and obs.end_seq == 7
    assert obs.attribution == "correlated"
    assert obs.outcome == "completed" and obs.outcome_evidence == "native"


def test_correlate_marks_foreign_input_mixed():
    run = "run_abc123"
    events = [
        _user(5, f"my brief {H.make_marker(run, 'initial')}"),
        _user(6, "someone else typed here"),
        _assistant(7, "answer"),
    ]
    obs = H.correlate(events, [_turn(5, 7)], run, baseline_seq=4)
    assert obs.recorded is True
    assert obs.attribution == "mixed"


def test_correlate_unrecorded_without_marker():
    events = [_user(5, "plain human input"), _assistant(6, "answer")]
    obs = H.correlate(events, [_turn(5, 6)], "run_abc123", baseline_seq=4)
    assert obs.recorded is False


def test_correlate_baseline_excludes_older_runs():
    run = "run_abc123"
    events = [_user(2, f"older {H.make_marker(run, 'initial')}"), _user(9, f"newer {H.make_marker(run, 'followup')}")]
    obs = H.correlate(events, [_turn(9, 10)], run, baseline_seq=8)
    assert obs.recorded is True
    assert obs.message_ids == ["followup"] and obs.first_seq == 9


def test_build_result_counts_but_never_bills():
    run = "run_abc123"
    usage = SimpleNamespace(model="echo-1", input_tokens=10, output_tokens=5, cache_read_tokens=0, cache_write_tokens=0)
    ask = _assistant(6, "answer one")
    ask.usage = usage
    tool = SimpleNamespace(
        seq=7,
        type="tool_call",
        text=None,
        ts=7.0,
        name="read",
        call_id="c1",
        result=SimpleNamespace(status="ok"),
        error=None,
        interaction=None,
        usage=None,
    )
    events = [_user(5, f"b {H.make_marker(run, 'initial')}"), ask, tool, _assistant(8, "final")]
    obs = H.correlate(events, [_turn(5, 8)], run, baseline_seq=4)
    result = H.build_result(obs)
    assert result["assistant_messages"] == 2
    assert result["final_response"] == "final"
    assert result["tools_used"] == ["read"]
    assert result["usage"] == {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0, "cache_write_tokens": 0}
    assert result["acceptance"] == "unverified"


def test_correlate_carries_native_stop_reason_without_inventing():
    run = "run_abc123"
    events = [_user(5, f"b {H.make_marker(run, 'initial')}"), _assistant(6, "done")]
    turn = _turn(5, 6)
    turn.outcome.stop_reason = "end_turn"
    obs = H.correlate(events, [turn], run, baseline_seq=4)
    assert obs.recorded is True
    assert obs.stop_reason == "end_turn"
    bare = H.correlate(events, [_turn(5, 6)], run, baseline_seq=4)
    assert bare.stop_reason is None, "missing evidence stays missing, never fabricated"


def test_compact_event_trims_long_text():
    event = _user(1, "x" * 5000)
    compacted = H.compact_event(event)
    assert len(compacted["text"]) < 2000 and "more chars" in compacted["text"]


def test_history_observer_reports_missing_history():
    observer = H.HistoryObserver(runtime="echo")
    with pytest.raises(H.HistoryError) as excinfo:
        observer.load_snapshot({"path": "/nonexistent/history.jsonl", "source": "echo"})
    assert excinfo.value.code == "history_unavailable"


def test_real_derive_turns_with_synthetic_models():
    sesskit = pytest.importorskip("sesskit", reason="SessKit not importable in this env")
    from sesskit.models import ActivityEvent, ActivitySnapshot, Evidence  # pyrefly: ignore[missing-import]

    run = "run_abc123"
    events = [
        ActivityEvent(
            seq=1,
            type="user_message",
            evidence=Evidence(origin="native", field="test"),
            text=f"brief {H.make_marker(run, 'initial')}",
        ),
        ActivityEvent(seq=2, type="assistant_message", evidence=Evidence(origin="native", field="test"), text="done"),
    ]
    snapshot = ActivitySnapshot(state="available", events=tuple(events), outcome=None, cursor="", generation="g1")
    turns = list(sesskit.derive_turns(snapshot, runtime=""))
    assert turns, "a user message plus reply must derive at least one turn"
    obs = H.correlate(list(events), turns, run, baseline_seq=0)
    assert obs.recorded is True
    assert obs.message_ids == ["initial"]
    result = H.build_result(obs)
    assert result["final_response"] == "done"


def test_project_event_delegates_or_falls_back_without_new_algorithm():
    from agent_session_control import correlation

    event = _user(1, "hello " + "y" * 5000)
    projected = H.project_event(event)
    assert projected["type"] == "user_message"
    if hasattr(correlation, "project_event"):
        full = H.project_event(event, text_limit=None)
        assert "hello" in (full.get("text") or "")
    else:
        assert projected == H.compact_event(event), "fallback is the shared bounded projection"


def test_build_result_accepts_text_limit_option():
    run = "run_abc123"
    events = [_user(5, f"b {H.make_marker(run, 'initial')}"), _assistant(6, "done")]
    obs = H.correlate(events, [_turn(5, 6)], run, baseline_seq=4)
    result = H.build_result(obs, text_limit=None)
    assert result["final_response"] == "done"
    limited = H.build_result(obs, text_limit=2)
    assert limited["final_response"] is not None

"""Engine supervisor tests: fake observer/backend, no tmux, no SessKit.

Covers dispatch-once semantics, idempotent create/conflicting payloads,
crash recovery without replay, missing-history and generation-reset bounds,
mixed attribution, queued versus active cancellation, and cross-process
target/input locks.
"""

import os
import subprocess
import sys
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))
sys.path.insert(0, os.path.join(_HERE, "fixtures", "engine"))

import fakes  # noqa: E402  # pyrefly: ignore[missing-import]

from agent_session_control.supervisor import (  # noqa: E402
    SupervisorRunner,
    file_lock,
    lock_held,
    run_supervisor,
    settle_crash,
)


def make_run(
    store,
    session_ref="ses_test",
    prompt="do the thing",
    key="k1",
    on_busy="queue",
    mode="submit",
    history_path="fake-history",
):
    spec = {
        "mode": mode,
        "runtime": "echo",
        "cwd": "/tmp",
        "session_ref": session_ref,
        "history_path": history_path,
        "prompt": prompt,
        "model": None,
        "on_busy": on_busy,
        "queue_timeout": 60,
    }
    data, created = store.create(
        "tester",
        key,
        spec,
        {"delivery": "accepted", "execution": "accepted", "session_ref": session_ref, "runtime": "echo"},
    )
    assert created
    return data["run_id"], spec


def run_to_end(store, run_id, hooks, **kwargs):
    kwargs.setdefault("settle_quiet", 0)
    kwargs.setdefault("history_wait", 30)
    assert run_supervisor(store, run_id, hooks, **kwargs) == 0
    return store.get(run_id)


def test_successful_run_settles_completed(tmp_path):
    store = fakes.MemoryStore(str(tmp_path))
    run_id, _ = make_run(store)
    hooks, backend, _ = fakes.script_hooks(
        str(tmp_path), observations=[fakes.script_observation(run_id, outcome="completed")]
    )
    record = run_to_end(store, run_id, hooks)
    assert record["execution"] == "completed"
    assert record["delivery"] == "native_recorded"
    assert backend.submitted, "the brief must reach the backend exactly once"
    assert len(backend.submitted) == 1


def test_repeated_create_returns_same_run_and_conflict_on_changed_payload(tmp_path):
    store = fakes.MemoryStore(str(tmp_path))
    spec = {"mode": "submit", "prompt": "same"}
    first, created = store.create("tester", "idem-1", spec, {"delivery": "accepted"})
    assert created
    second, created_again = store.create("tester", "idem-1", dict(spec), {"delivery": "accepted"})
    assert not created_again and second["run_id"] == first["run_id"]
    with pytest.raises(Exception) as excinfo:
        store.create("tester", "idem-1", {"mode": "submit", "prompt": "different"}, {"delivery": "accepted"})
    assert getattr(excinfo.value, "code", None) == "idempotency_conflict"


def test_certain_submit_failure_rejects_without_retry(tmp_path):
    from agent_session_control.supervisor import CertainFailure

    store = fakes.MemoryStore(str(tmp_path))
    run_id, _ = make_run(store)

    def boom(ref, text):
        raise CertainFailure("input_rejected", "composer is blocked")

    hooks, backend, _ = fakes.script_hooks(str(tmp_path))
    hooks.submit = boom
    record = run_to_end(store, run_id, hooks)
    assert record["delivery"] == "rejected"
    assert record["execution"] == "failed"
    assert record["error"]["code"] == "input_rejected"
    assert backend.submitted == []


def test_uncertain_submit_becomes_unknown_and_is_never_replayed(tmp_path):
    from agent_session_control.supervisor import UncertainDelivery

    store = fakes.MemoryStore(str(tmp_path))
    run_id, _ = make_run(store)
    calls = []

    def flaky(ref, text):
        calls.append(text)
        raise UncertainDelivery("paste timed out")

    hooks, _, _ = fakes.script_hooks(str(tmp_path), observations=["unrecorded", "unrecorded"])
    hooks.submit = flaky
    record = run_to_end(store, run_id, hooks, history_wait=1)
    assert record["delivery"] == "unknown"
    assert record["execution"] == "unknown"
    assert len(calls) == 1, "uncertain input must not be retried"
    # Recovery of the same run must not retype either.
    runner = SupervisorRunner(store, run_id, hooks, history_wait=1)
    assert runner.recover() == -1
    assert len(calls) == 1


def test_crash_before_dispatch_resumes_safely(tmp_path):
    store = fakes.MemoryStore(str(tmp_path))
    run_id, _ = make_run(store)
    # Crash leaves delivery accepted: nothing was typed, so supervising again
    # admits and dispatches normally.
    record = store.get(run_id)
    assert record["delivery"] == "accepted"
    hooks, backend, _ = fakes.script_hooks(
        str(tmp_path), observations=[fakes.script_observation(run_id, outcome="completed")]
    )
    record = run_to_end(store, run_id, hooks)
    assert record["execution"] == "completed"
    assert len(backend.submitted) == 1


def test_crash_settle_marks_unknown_after_input(tmp_path):
    store = fakes.MemoryStore(str(tmp_path))
    run_id, _ = make_run(store)
    store.update(run_id, {"delivery": "dispatching", "execution": "running"})
    settle_crash(store, run_id, RuntimeError("boom"))
    record = store.get(run_id)
    assert record["execution"] == "unknown"
    assert record["delivery"] == "dispatching"


def test_missing_history_stays_unknown_and_recoverable(tmp_path):
    store = fakes.MemoryStore(str(tmp_path))
    run_id, _ = make_run(store)
    hooks, backend, _ = fakes.script_hooks(str(tmp_path), observations=["unrecorded"])
    record = run_to_end(store, run_id, hooks, history_wait=1)
    assert record["execution"] == "unknown"
    assert record["error"]["code"] == "native_history_missing"
    assert len(backend.submitted) == 1, "the brief was sent once; the failure is observation-only"


def test_generation_reset_becomes_unknown(tmp_path):
    store = fakes.MemoryStore(str(tmp_path))
    run_id, _ = make_run(store)
    hooks, _, _ = fakes.script_hooks(str(tmp_path), observations=["reset"])
    record = run_to_end(store, run_id, hooks)
    assert record["execution"] == "unknown"
    assert record["error"]["code"] == "history_reset"


def test_mixed_attribution_never_becomes_exclusive_result(tmp_path):
    store = fakes.MemoryStore(str(tmp_path))
    run_id, _ = make_run(store)
    hooks, _, _ = fakes.script_hooks(
        str(tmp_path), observations=[fakes.script_observation(run_id, outcome="completed", attribution="mixed")]
    )
    record = run_to_end(store, run_id, hooks)
    assert record["execution"] == "unknown"
    assert record["error"]["code"] == "mixed_attribution"


def test_interrupt_before_dispatch_cancels_locally(tmp_path):
    store = fakes.MemoryStore(str(tmp_path))
    run_id, _ = make_run(store)
    record = store.get(run_id)
    store.enqueue(run_id, "tester", "msg-cancel", {"action": "interrupt"}, record["version"])
    hooks, backend, _ = fakes.script_hooks(
        str(tmp_path), observations=[fakes.script_observation(run_id, outcome="completed")]
    )
    record = run_to_end(store, run_id, hooks)
    assert record["execution"] == "interrupted"
    assert record["delivery"] == "rejected"
    assert backend.submitted == [], "cancelled-before-dispatch sends nothing"
    assert backend.interrupts == []


def test_interrupt_active_run_requests_exact_turn(tmp_path):
    store = fakes.MemoryStore(str(tmp_path))
    run_id, _ = make_run(store)

    def submit_then_interrupt(pane, text):
        record = store.get(run_id)
        store.enqueue(run_id, "tester", "msg-stop", {"action": "interrupt"}, record["version"])

    in_progress = fakes.script_observation(run_id, outcome="in_progress")
    done = fakes.script_observation(run_id, outcome="interrupted", evidence="inferred")
    hooks, backend, _ = fakes.script_hooks(
        str(tmp_path), observations=[in_progress, done], submit_hook=submit_then_interrupt
    )
    record = run_to_end(store, run_id, hooks)
    assert len(backend.interrupts) == 1
    assert record["execution"] == "interrupted"


def test_interrupt_without_correlated_turn_is_rejected(tmp_path):
    store = fakes.MemoryStore(str(tmp_path))
    run_id, _ = make_run(store)
    holder = {}

    def submit_then_interrupt(pane, text):
        record = store.get(run_id)
        (msg, _) = store.enqueue(run_id, "tester", "msg-stop", {"action": "interrupt"}, record["version"])
        holder["msg"] = msg

    unrecorded = {"recorded": False}
    done = fakes.script_observation(run_id, outcome="completed")
    hooks, backend, _ = fakes.script_hooks(
        str(tmp_path), observations=[unrecorded, done], submit_hook=submit_then_interrupt
    )
    record = run_to_end(store, run_id, hooks)
    assert record["execution"] == "completed"
    assert backend.interrupts == [], "no correlated turn: no interrupt may go out"
    states = [m["state"] for m in store.messages(run_id) if m["message_id"] == holder["msg"]["message_id"]]
    assert states == ["rejected"]


def test_steer_without_backend_support_rejects_message(tmp_path):
    store = fakes.MemoryStore(str(tmp_path))
    run_id, _ = make_run(store)
    record = store.get(run_id)
    (msg, _) = store.enqueue(run_id, "tester", "msg-steer", {"action": "steer", "prompt": "go left"}, record["version"])
    done = fakes.script_observation(run_id, message_ids=("initial",), outcome="completed")
    hooks, _, _ = fakes.script_hooks(str(tmp_path), observations=[done], steer_supported=False)
    record = run_to_end(store, run_id, hooks)
    assert record["execution"] == "completed"
    states = [m["state"] for m in store.messages(run_id) if m["message_id"] == msg["message_id"]]
    assert states == ["rejected"]


def test_waiting_input_is_not_terminal(tmp_path):
    store = fakes.MemoryStore(str(tmp_path))
    run_id, _ = make_run(store)
    waiting = fakes.script_observation(run_id, outcome="awaiting_input", pending=True)
    done = fakes.script_observation(run_id, outcome="completed")
    hooks, _, _ = fakes.script_hooks(
        str(tmp_path), observations=[waiting, waiting, done], questions=[{"request_id": "q1", "prompt": "which one?"}]
    )
    record = run_to_end(store, run_id, hooks)
    assert record["execution"] == "completed"


def test_file_lock_is_cross_process(tmp_path):
    root = str(tmp_path)
    src_dir = os.path.join(_HERE, "..", "src")
    env = dict(os.environ)
    env["PYTHONPATH"] = src_dir + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    child_script = (
        "from agent_session_control.supervisor import file_lock\n"
        "import time\n"
        "with file_lock(r'''" + root + "''', 'shared-identity', blocking=True) as owned:\n"
        "    assert owned\n"
        "    time.sleep(8)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", child_script], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env
    )
    try:
        deadline = time.monotonic() + 10.0
        seen_held = False
        while time.monotonic() < deadline:
            if lock_held(root, "shared-identity"):
                seen_held = True
                break
            time.sleep(0.1)
        assert seen_held, "the child process must visibly hold the shared lock"
        with file_lock(root, "shared-identity") as owned:
            assert owned is False, "a second process must not acquire a held target lock"
    finally:
        holder.terminate()
        _, err = holder.communicate(timeout=10)
        assert holder.returncode is not None
        assert b"Traceback" not in (err or b"")


def test_unbound_run_binds_then_settles(tmp_path):
    store = fakes.MemoryStore(str(tmp_path))
    run_id, _ = make_run(store, history_path=None)
    calls = []

    def bind(binding):
        calls.append(dict(binding))
        if len(calls) < 2:
            return None
        return {**binding, "history_path": "fake-history", "native_id": "native-1"}

    hooks, backend, _ = fakes.script_hooks(
        str(tmp_path),
        observations=[fakes.script_observation(run_id, outcome="completed")],
        bind_native=bind,
        resolve_path=None,
    )
    record = run_to_end(store, run_id, hooks)
    assert record["execution"] == "completed"
    assert len(calls) >= 2, "binding retries until the exact session appears"
    assert store.get(run_id, private=True)["history_path"] == "fake-history"


def test_unbound_run_gives_up_bounded_without_resend(tmp_path):
    store = fakes.MemoryStore(str(tmp_path))
    run_id, _ = make_run(store, history_path=None)
    hooks, backend, _ = fakes.script_hooks(
        str(tmp_path), observations=["unrecorded"], bind_native=lambda b: None, resolve_path=None
    )
    record = run_to_end(store, run_id, hooks, history_wait=1)
    assert record["execution"] == "unknown"
    assert record["error"]["code"] == "native_binding_missing"
    assert len(backend.submitted) == 1, "the brief went out once; the failure is binding-only"


def test_queued_run_expires_failed_not_rejected(tmp_path):
    from agent_session_control.supervisor import file_lock

    store = fakes.MemoryStore(str(tmp_path))
    spec = {
        "mode": "submit",
        "runtime": "echo",
        "cwd": "/tmp",
        "session_ref": "ses_test",
        "history_path": "fake-history",
        "prompt": "queued",
        "model": None,
        "on_busy": "queue",
        "queue_timeout": 1,
    }
    data, _ = store.create(
        "tester",
        "expiring",
        spec,
        {"delivery": "accepted", "execution": "accepted", "session_ref": "ses_test", "runtime": "echo"},
    )
    run_id = data["run_id"]
    time.sleep(1.2)
    import threading

    outcomes = {}

    def supervise():
        outcomes["rc"] = run_supervisor(
            store,
            run_id,
            fakes.script_hooks(str(tmp_path), observations=["unrecorded"])[0],
            settle_quiet=0,
            history_wait=10,
        )

    with file_lock(str(tmp_path), "target:ses_test") as owned:
        assert owned
        worker = threading.Thread(target=supervise)
        worker.start()
        worker.join(timeout=30)
    record = store.get(run_id)
    assert record["delivery"] == "rejected"
    assert record["execution"] == "failed", "expiry is failed work, never a 'rejected' execution state"
    assert record["error"]["code"] == "queue_expired"


def test_target_lock_serializes_two_runs(tmp_path):
    store = fakes.MemoryStore(str(tmp_path))
    first, _ = make_run(store, key="first")
    second, _ = make_run(store, key="second")
    slow = fakes.script_observation(first, outcome="in_progress")
    done_first = fakes.script_observation(first, outcome="completed")
    done_second = fakes.script_observation(second, outcome="completed")
    hooks1, _, _ = fakes.script_hooks(str(tmp_path), observations=[slow, slow, done_first])
    hooks2, backend2, _ = fakes.script_hooks(str(tmp_path), observations=[done_second])

    import threading

    results = {}

    def supervise(run_id, hooks):
        results[run_id] = run_supervisor(store, run_id, hooks, settle_quiet=0, history_wait=10)

    first_thread = threading.Thread(target=supervise, args=(first, hooks1))
    first_thread.start()
    time.sleep(0.5)
    second_thread = threading.Thread(target=supervise, args=(second, hooks2))
    second_thread.start()
    first_thread.join(timeout=60)
    second_thread.join(timeout=60)
    assert store.get(first)["execution"] == "completed"
    assert store.get(second)["execution"] == "completed"

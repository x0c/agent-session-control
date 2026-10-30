"""Core store tests: read-only behavior, audit atomics, idempotency, isolation."""

import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading

import pytest

from agent_session_control.errors import ControlError
from agent_session_control.store import (
    SETTLED,
    TERMINAL,
    RunStore,
    default_home,
    digest,
    run_version,
)


def make_store(tmp_path, **kwargs):
    return RunStore(root=tmp_path / "control", **kwargs)


def spec(target="claude:abc", text="fix"):
    return {"target": target, "text": text, "mode": "queue"}


def initial(execution="queued", delivery="accepted"):
    return {"execution": execution, "delivery": delivery, "session_ref": "claude:abc"}


def test_default_home_neutral():
    home = default_home()
    assert "agent-session-control" in str(home)
    assert "corral" not in str(home)


def test_read_only_access_creates_nothing(tmp_path):
    store = make_store(tmp_path)
    assert not store.exists()
    with pytest.raises(ControlError) as exc:
        store.get("run_missing")
    assert exc.value.code == "not_found"
    with pytest.raises(ControlError):
        store.recent(5)
    with pytest.raises(ControlError):
        store.events("run_missing")
    with pytest.raises(ControlError):
        store.audit_trail()
    assert store.find("nope") is None
    assert store.unsettled() == []
    assert store.active_hosts() == set()
    # No files, directories or side effects from any read path.
    assert list(tmp_path.iterdir()) == []


def test_digest_stable_and_version_shape():
    assert digest({"b": 1, "a": 2}) == digest({"a": 2, "b": 1})
    version = run_version({"execution": "running", "delivery": "accepted", "questions": []})
    assert version.startswith("v2:")
    other = run_version({"execution": "running", "delivery": "accepted", "questions": [{"request_id": "q1"}]})
    assert other != version


def test_create_idempotent_same_spec(tmp_path):
    store = make_store(tmp_path)
    first, created = store.create("alice", "key-1", spec(), initial())
    assert created is True
    assert first["run_id"].startswith("run_")
    assert first["version"].startswith("v2:")
    second, created_again = store.create("alice", "key-1", spec(), initial())
    assert created_again is False
    assert second["run_id"] == first["run_id"]
    found = store.find("key-1")
    assert found is not None and found["run_id"] == first["run_id"]
    assert found["_digest"] == digest(spec())


def test_create_conflict_changed_payload(tmp_path):
    store = make_store(tmp_path)
    store.create("alice", "key-1", spec(), initial())
    before = store.audit_trail(limit=100)
    with pytest.raises(ControlError) as exc:
        store.create("alice", "key-1", spec(text="different"), initial())
    assert exc.value.code == "idempotency_conflict"
    assert exc.value.exit_code == 5
    # The failed write leaves no partial run and no extra audit row.
    assert store.audit_trail(limit=100) == before
    assert len(store.recent(10)) == 1


def test_audit_same_transaction(tmp_path):
    store = make_store(tmp_path)
    record, _ = store.create("alice", "key-1", spec(), initial())
    trail = store.audit_trail(target=record["run_id"])
    assert len(trail) == 1
    assert trail[0]["actor"] == "alice"
    assert trail[0]["action"] == "run.create"
    updated = store.update(record["run_id"], {"execution": "running"}, actor="alice", expected=record["version"])
    actions = [row["action"] for row in store.audit_trail(target=record["run_id"])]
    assert sorted(actions) == ["run.create", "run.update"]
    # audit_trail reads newest-first.
    assert actions == ["run.update", "run.create"]
    assert updated["revision"] == 2


def test_update_version_conflict_and_terminal_immutable(tmp_path):
    store = make_store(tmp_path)
    record, _ = store.create("alice", "key-1", spec(), initial())
    with pytest.raises(ControlError) as exc:
        store.update(record["run_id"], {"execution": "running"}, expected="v2:stale")
    assert exc.value.code == "conflict"
    record = store.update(record["run_id"], {"execution": "completed"})
    with pytest.raises(ControlError) as exc:
        store.update(record["run_id"], {"execution": "failed"})
    assert exc.value.code == "terminal_run"
    assert TERMINAL == frozenset({"completed", "failed", "interrupted"})
    assert "unknown" in SETTLED


def test_concurrent_threads_converge(tmp_path):
    store = make_store(tmp_path)
    outcomes = []

    def worker():
        try:
            record, created = store.create("alice", "shared-key", spec(), initial())
            outcomes.append((record["run_id"], created))
        except ControlError as exc:  # pragma: no cover - convergence expected instead
            outcomes.append(("error:" + exc.code, False))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(outcomes) == 8
    run_ids = {run_id for run_id, _ in outcomes}
    assert len(run_ids) == 1
    assert sorted(created for _, created in outcomes) == [False] * 7 + [True]
    assert len(store.recent(10)) == 1


def _race_child_script(src_root, store_root):
    lines = [
        "import json, sys",
        f"sys.path.insert(0, {src_root!r})",
        "from agent_session_control.store import RunStore",
        f"s = RunStore(root={store_root!r})",
        "rec, created = s.create('op', 'race-key', {'t': 1}, {'execution': 'queued'})",
        "print(json.dumps({'run_id': rec['run_id'], 'created': created}))",
    ]
    return "; ".join(lines)


def test_cross_process_admission(tmp_path):
    src_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
    store_root = str(tmp_path / "control")
    script = _race_child_script(src_root, store_root)
    procs = [subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True) for _ in range(2)]
    outputs = [json.loads(proc.communicate()[0]) for proc in procs]
    assert [proc.returncode for proc in procs] == [0, 0]
    assert outputs[0]["run_id"] == outputs[1]["run_id"]
    assert sorted(o["created"] for o in outputs) == [False, True]
    store = make_store(tmp_path)
    assert len(store.recent(10)) == 1


def test_root_and_namespace_separation(tmp_path):
    first = make_store(tmp_path / "a")
    second = make_store(tmp_path / "b")
    rec_a, _ = first.create("alice", "key", spec(), initial())
    assert second.find("key") is None
    assert second.unsettled() == []

    ns_default = make_store(tmp_path)
    ns_other = RunStore(root=tmp_path / "control", namespace="team-x")
    assert ns_other.path != ns_default.path
    rec_default, _ = ns_default.create("alice", "key", spec(), initial())
    rec_other, _ = ns_other.create("alice", "key", spec(), initial())
    assert rec_default["run_id"] != rec_other["run_id"]
    found_default = ns_default.find("key")
    found_other = ns_other.find("key")
    assert found_default is not None and found_other is not None
    assert found_default["run_id"] == rec_default["run_id"]
    assert found_other["run_id"] == rec_other["run_id"]
    assert rec_a["run_id"] != rec_default["run_id"]
    with pytest.raises(ValueError):
        RunStore(root=tmp_path / "control", namespace="../evil")


def test_supervisor_identity_configurable(tmp_path):
    store = RunStore(root=tmp_path / "control", supervisor="host-supervisor")
    record, _ = store.create("alice", "key-1", spec(), initial())
    store.append(record["run_id"], "note:1", {"kind": "note"})
    actions = [(row["actor"], row["action"]) for row in store.audit_trail(target=record["run_id"])]
    assert ("host-supervisor", "event.append") in actions
    supervisor_default = make_store(tmp_path / "other")
    rec2, _ = supervisor_default.create("alice", "key-1", spec(), initial())
    actors = {row["actor"] for row in supervisor_default.audit_trail(target=rec2["run_id"])}
    assert actors == {"alice"}


def test_events_paging_and_dedupe(tmp_path):
    store = make_store(tmp_path)
    record, _ = store.create("alice", "key-1", spec(), initial())
    assert store.append(record["run_id"], "a", {"kind": "note"}) is True
    assert store.append(record["run_id"], "a", {"kind": "note"}) is False
    assert store.append(record["run_id"], "b", {"kind": "note"}) is True
    page = store.events(record["run_id"], after=0, limit=1)
    assert len(page["events"]) == 1
    assert page["has_more"] is True
    rest = store.events(record["run_id"], after=page["next_cursor"])
    assert len(rest["events"]) == 2  # seeded state event + second note
    assert rest["has_more"] is False


def test_messages_idempotent_and_settled(tmp_path):
    store = make_store(tmp_path)
    record, _ = store.create("alice", "key-1", spec(), initial())
    first, created = store.enqueue(
        record["run_id"], "alice", "m-1", {"action": "steer", "text": "go"}, record["version"]
    )
    assert created is True
    again, created_again = store.enqueue(
        record["run_id"], "alice", "m-1", {"action": "steer", "text": "go"}, "v2:stale"
    )
    assert created_again is False and again["message_id"] == first["message_id"]
    with pytest.raises(ControlError) as exc:
        store.enqueue(record["run_id"], "alice", "m-1", {"action": "steer", "text": "other"}, record["version"])
    assert exc.value.code == "idempotency_conflict"
    assert len(store.pending_messages(record["run_id"])) == 1
    store.message_state(first["message_id"], "submitted")
    assert store.pending_messages(record["run_id"]) == []
    record = store.update(record["run_id"], {"execution": "completed"})
    with pytest.raises(ControlError) as exc:
        store.enqueue(record["run_id"], "alice", "m-2", {"action": "steer", "text": "late"}, record["version"])
    assert exc.value.code == "terminal_run"


def test_for_target_unsettled_and_hosts(tmp_path):
    store = make_store(tmp_path)
    rec, _ = store.create("alice", "key-1", spec(), {"execution": "running", "session_ref": "claude:abc", "host": "h1"})
    assert [r["run_id"] for r in store.for_target("claude:abc")] == [rec["run_id"]]
    assert store.active_hosts() == {"h1"}
    store.update(rec["run_id"], {"execution": "completed"})
    assert store.for_target("claude:abc") == []
    assert store.active_hosts() == set()


def test_private_spec_and_file_permissions(tmp_path):
    store = make_store(tmp_path)
    record, created = store.create("alice", "key-1", spec(), initial())
    assert created
    assert "spec" not in store.get(record["run_id"])
    assert store.get(record["run_id"], private=True)["spec"] == spec()
    mode = stat.S_IMODE(os.stat(store.path).st_mode)
    assert mode == 0o600
    assert stat.S_IMODE(os.stat(store.path.parent).st_mode) == 0o700


def test_symlink_storage_rejected(tmp_path):
    root = tmp_path / "control"
    root.mkdir()
    (root / "runs.sqlite3").symlink_to(tmp_path / "elsewhere.sqlite3")
    store = RunStore(root=root)
    with pytest.raises(ControlError) as exc:
        store.create("alice", "key-1", spec(), initial())
    # The symlink guard refuses; no silent write through a link.
    assert exc.value.code == "conflict"
    assert not (tmp_path / "elsewhere.sqlite3").exists()


def test_error_envelope_shape():
    err = ControlError("not_found", "missing", 3, hint="h", next_commands=["corral run status x"])
    envelope = err.to_envelope(meta={"run_id": "r"})
    assert envelope == {
        "ok": False,
        "data": None,
        "error": {
            "code": "not_found",
            "message": "missing",
            "hint": "h",
            "next_commands": ["corral run status x"],
            "details": None,
        },
        "meta": {"run_id": "r"},
    }


def test_audit_trail_read_only_missing(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ControlError):
        store.audit_trail()
    assert list(tmp_path.iterdir()) == []
    store.create("alice", "k", spec(), initial())
    direct = sqlite3.connect(store.path.as_uri() + "?mode=ro", uri=True)
    try:
        count = direct.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
    finally:
        direct.close()
    assert count == len(store.audit_trail(limit=100)) == 1

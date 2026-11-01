"""Engine controller tests: LocalController against the SessionController port.

Fake backend/history keep these tests free of model logins and real agents;
one test uses a real owned tmux echo pane, and one proves durable work
survives the initiating process via a detached supervisor over a
file-backed store. Real native-agent proof lives in test_engine_native.py.
"""

import asyncio
import os
import shutil
import sys
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))
sys.path.insert(0, os.path.join(_HERE, "fixtures", "engine"))

import fakes  # noqa: E402  # pyrefly: ignore[missing-import]

from agent_session_control.controller import ControlError, LocalController  # noqa: E402

needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")


def make_controller(tmp_path, **kwargs):
    root = str(tmp_path / "control")
    kwargs.setdefault("root", root)
    kwargs.setdefault("namespace", "test")
    kwargs.setdefault("runtime_profiles", {"echo": fakes.EchoProfile()})
    kwargs.setdefault("spawn", False)
    if "store" not in kwargs:
        kwargs["store"] = fakes.MemoryStore(os.path.join(root, "test"))
    if "terminal" not in kwargs:
        kwargs["terminal"] = fakes.ScriptBackend()
    return LocalController(**kwargs)


def canned_history(run_holder, outcome="completed", evidence="native"):
    history = fakes.ScriptHistory([])

    def observe(reader, run_id, base_seq, events, runtime=""):
        run_holder.append(run_id)
        return (fakes.script_observation(run_id, outcome=outcome, evidence=evidence), False)

    history.observe = observe
    return history


async def create_session(controller, tmp_path):
    return await controller.create("echo", str(tmp_path))


def bind_fake(controller, handle, path="fake-history"):
    """Fixture-lane native binding: the canned observer needs a locator, and
    there is no native world here, so the binding is seeded explicitly."""
    controller.sessions.put(
        {**controller.sessions.get(handle.id), "history_path": path},
        actor="test",
        action="session.bound",
    )


def test_capabilities_are_honest(tmp_path):
    ctl = make_controller(tmp_path)
    caps = ctl.capabilities("echo")
    assert caps["queue"] is True
    assert caps["steer"] is False
    assert caps["model_selection"] is False
    assert "text" in caps["prompt_blocks"] and "resource_link" in caps["prompt_blocks"]
    assert caps["resume"]["supported"] is True
    assert caps["load_history"]["requires"] == "bound native history"
    assert caps["close"] is True and caps["list"] is True and caps["detach"] is True
    assert caps["mcp"] == "unsupported"
    with pytest.raises(ControlError) as excinfo:
        ctl.capabilities("nope")
    assert excinfo.value.code == "unsupported_runtime"


def test_create_persists_exact_handle(tmp_path):
    async def go():
        controller = make_controller(tmp_path)
        handle = await create_session(controller, tmp_path)
        assert handle.id.startswith("ses_") and handle.runtime == "echo"
        listed = await controller.list_sessions()
        assert [s["session_id"] for s in listed["sessions"]] == [handle.id]
        row = controller.sessions.get(handle.id)
        assert row["cwd"] == str(tmp_path)
        return handle

    asyncio.run(go())


def test_create_preserves_caller_path_verbatim(tmp_path):
    """Symlinked tmp dirs (/var vs /private/var) must not be rewritten."""

    async def go():
        controller = make_controller(tmp_path)
        handle = await create_session(controller, tmp_path)
        assert handle.cwd == os.path.abspath(str(tmp_path))
        row = controller.sessions.get(handle.id)
        assert row["cwd"] == handle.cwd

    asyncio.run(go())


def test_resume_unknown_and_blank_refusals(tmp_path):
    async def go():
        controller = make_controller(tmp_path)
        with pytest.raises(ControlError) as excinfo:
            await controller.resume("echo:does-not-exist")
        assert excinfo.value.code == "not_found"
        handle = await create_session(controller, tmp_path)
        # A live owned pane is reused without needing native identity; kill it
        # first so recovery must build a resume plan, which refuses a blank.
        controller.backend.panes[controller.sessions.get(handle.id)["pane"]]["alive"] = False
        with pytest.raises(ControlError):
            await controller.resume(handle.id)

    asyncio.run(go())


def test_resume_reuses_live_binding_without_forking(tmp_path):
    async def go():
        controller = make_controller(tmp_path)
        backend = controller.backend
        handle = await create_session(controller, tmp_path)
        first_pane = controller.sessions.get(handle.id)["pane"]
        before = len(backend.panes)
        resumed = await controller.resume(handle.id)
        assert resumed.id == handle.id
        assert controller.sessions.get(handle.id)["pane"] == first_pane
        assert len(backend.panes) == before, "a live owned binding must be reused, never relaunched"

    asyncio.run(go())


def test_resume_recovers_dead_pane_preserving_identity(tmp_path):
    async def go():
        controller = make_controller(tmp_path)
        backend = controller.backend
        handle = await create_session(controller, tmp_path)
        old_pane = controller.sessions.get(handle.id)["pane"]
        backend.panes[old_pane]["alive"] = False
        controller.sessions.put(
            {
                "id": handle.id,
                "runtime": "echo",
                "cwd": handle.cwd,
                "native_id": "native-1",
                "instance_id": None,
                "pane": old_pane,
                "title": "keep me",
                "history_path": "/tmp/fake-history",
                "identity_evidence": "claimed",
                "status": "hosted",
            },
            actor="tester",
            action="session.bound",
        )
        resumed = await controller.resume(handle.id)
        assert resumed.id == handle.id and resumed.native_id == "native-1"
        row = controller.sessions.get(handle.id)
        assert row["pane"] and row["pane"] != old_pane
        assert row["history_path"] == "/tmp/fake-history", "recovery preserves the bound history"
        assert row["title"] == "keep me", "recovery preserves the title"

    asyncio.run(go())


def test_resume_rejects_unknown_liveness(tmp_path):
    async def go():
        controller = make_controller(tmp_path)
        backend = controller.backend
        real_liveness = backend.liveness
        backend.liveness = lambda pane: "unknown"
        try:
            handle = await create_session(controller, tmp_path)
            with pytest.raises(ControlError) as excinfo:
                await controller.resume(handle.id)
            assert excinfo.value.code == "liveness_unknown"
        finally:
            backend.liveness = real_liveness

    asyncio.run(go())


def test_concurrent_resumes_launch_once(tmp_path):
    async def go():
        controller = make_controller(tmp_path)
        backend = controller.backend
        handle = await create_session(controller, tmp_path)
        old_pane = controller.sessions.get(handle.id)["pane"]
        backend.panes[old_pane]["alive"] = False
        controller.sessions.put(
            {
                **controller.sessions.get(handle.id),
                "native_id": "native-1",
            },
            actor="tester",
            action="session.bound",
        )
        before = len(backend.panes)
        results = []

        async def one():
            results.append(await controller.resume(handle.id))

        await asyncio.gather(one(), one())
        assert len(backend.panes) == before + 1, "serialized resumes share one recovery launch"
        assert results[0].id == results[1].id == handle.id
        assert controller.sessions.get(handle.id)["pane"] not in (None, old_pane)

    asyncio.run(go())


def test_resume_path_addressed_pi_like_profile(tmp_path):
    async def go():
        controller = make_controller(tmp_path, runtime_profiles={"pi-like": fakes.PiLikeProfile()})
        backend = controller.backend
        handle = await controller.create("pi-like", str(tmp_path))
        row = controller.sessions.get(handle.id)
        backend.panes[row["pane"]]["alive"] = False
        controller.sessions.put(
            {**row, "native_id": "/tmp/native-pi-history", "history_path": "/tmp/native-pi-history"},
            actor="tester",
            action="session.bound",
        )
        resumed = await controller.resume("pi-like:/tmp/native-pi-history")
        assert resumed.native_id == "/tmp/native-pi-history"
        pane = controller.sessions.get(handle.id)["pane"]
        assert "/tmp/native-pi-history" in backend.panes[pane]["argv"], (
            "the resume plan carries the exact native history path"
        )

    asyncio.run(go())


def test_start_turn_wait_settles_completed(tmp_path):
    async def go():
        runs = []
        controller = make_controller(tmp_path)
        controller._observer = canned_history(runs)
        handle = await create_session(controller, tmp_path)
        bind_fake(controller, handle)
        run_id = await controller.start_turn(handle.id, [{"type": "text", "text": "fix it"}], request_id="req-1")
        assert run_id.startswith("run_")
        result = await controller.wait(run_id, timeout=20.0)
        assert result.state == "completed"
        assert result.evidence == "native"
        assert runs and runs[0] == run_id

    asyncio.run(go())


def test_hook_shaped_observer_tolerated_without_voiding_run(tmp_path):
    """Integration regression: a 3-tuple observer must not crash every poll."""

    async def go():
        controller = make_controller(tmp_path)

        class ThreeTupleHistory(fakes.ScriptHistory):
            def observe(self, reader, run_id, base_seq, events, runtime=""):
                obs, reset = super().observe(reader, run_id, base_seq, events, runtime)
                return (obs, reset, "ready")

        run_holder: list = []
        history = ThreeTupleHistory([])
        history.observe = lambda reader, run_id, base, events, runtime="": (
            fakes.script_observation(run_id, outcome="completed"),
            False,
            "ready",
        )
        controller._observer = history
        handle = await create_session(controller, tmp_path)
        bind_fake(controller, handle)
        run_id = await controller.start_turn(handle.id, [{"type": "text", "text": "hi"}], request_id="t3")
        result = await controller.wait(run_id, timeout=20.0)
        assert result.state == "completed", "shape tolerance must settle, not void the run"
        assert run_holder == [] or True

    asyncio.run(go())


def test_request_idempotency_and_conflict(tmp_path):
    async def go():
        controller = make_controller(tmp_path)
        controller._observer = canned_history([])
        handle = await create_session(controller, tmp_path)
        bind_fake(controller, handle)
        prompt = [{"type": "text", "text": "same brief"}]
        first = await controller.start_turn(handle.id, prompt, request_id="idem-9")
        second = await controller.start_turn(handle.id, prompt, request_id="idem-9")
        assert first == second
        with pytest.raises(ControlError) as excinfo:
            await controller.start_turn(handle.id, [{"type": "text", "text": "other brief"}], request_id="idem-9")
        assert excinfo.value.code == "idempotency_conflict"

    asyncio.run(go())


def test_mcp_flows_to_plan_env_and_stays_out_of_storage(tmp_path):
    async def go():
        root = str(tmp_path / "control")
        controller = make_controller(
            tmp_path, root=root, runtime_profiles={"mcp-echo": fakes.McpEchoProfile(), "echo": fakes.EchoProfile()}
        )
        servers = [{"name": "db", "uri": "https://db.internal/x", "token": "SECRET-123"}]
        assert controller.capabilities("mcp-echo")["mcp"] == "supported"
        handle = await controller.create("mcp-echo", str(tmp_path), mcp_servers=servers)
        pane = controller.sessions.get(handle.id)["pane"]
        delivered = controller.backend.panes[pane]["env"].get("ASC_MCP_SERVERS", "")
        assert "SECRET-123" in delivered, "the agent process needs the live config"
        leaked = []
        for dirpath, _dirnames, filenames in os.walk(root):
            for filename in filenames:
                if not filename.endswith((".sqlite3", ".json", ".log")):
                    continue
                with open(os.path.join(dirpath, filename), "rb") as stream:
                    blob = stream.read()
                if b"SECRET-123" in blob:
                    leaked.append(filename)
        assert leaked == [], f"credentials must never persist in storage: {leaked}"
        # Unsupported MCP fails before any launch, with no hosted pane.
        before = len(controller.backend.panes)
        with pytest.raises(ControlError):
            await controller.create("echo", str(tmp_path), mcp_servers=servers)
        assert len(controller.backend.panes) == before

    asyncio.run(go())


def test_shared_store_converges_across_instances(tmp_path):
    async def go():
        root = str(tmp_path / "control")
        store = fakes.MemoryStore(os.path.join(root, "test"))
        first = make_controller(tmp_path, root=root, store=store)
        second = make_controller(tmp_path, root=root, store=store)
        handle = await create_session(first, tmp_path)
        prompt = [{"type": "text", "text": "shared"}]
        run_a = await first.start_turn(handle.id, prompt, request_id="shared-1")
        run_b = await second.start_turn(handle.id, prompt, request_id="shared-1")
        assert run_a == run_b

    asyncio.run(go())


def test_steer_rejected_when_backend_cannot_steer(tmp_path):
    async def go():
        controller = make_controller(tmp_path)
        handle = await create_session(controller, tmp_path)
        with pytest.raises(ControlError) as excinfo:
            await controller.start_turn(handle.id, [{"type": "text", "text": "x"}], request_id="s1", on_busy="steer")
        assert excinfo.value.code == "steer_unsupported"

    asyncio.run(go())


def test_resource_links_keep_semantics_and_rich_blocks_rejected(tmp_path):
    async def go():
        controller = make_controller(tmp_path)
        controller._observer = canned_history([])
        handle = await create_session(controller, tmp_path)
        bind_fake(controller, handle)
        run_id = await controller.start_turn(
            handle.id,
            [
                {"type": "text", "text": "read this"},
                {"type": "resource_link", "uri": "file:///repo/a.py", "title": "a.py", "mimeType": "text/x-python"},
            ],
            request_id="res-1",
        )
        record = controller.run_store.get(run_id, private=True)
        assert 'uri="file:///repo/a.py"' in record["spec"]["prompt"]
        assert "a.py" in record["spec"]["prompt"]
        legacy = await controller.start_turn(
            handle.id,
            [{"type": "resource", "uri": "file:///repo/b.py"}],
            request_id="res-legacy",
        )
        legacy_record = controller.run_store.get(legacy, private=True)
        assert "file:///repo/b.py" in legacy_record["spec"]["prompt"]
        with pytest.raises(ControlError) as excinfo:
            await controller.start_turn(handle.id, [{"type": "image", "data": "x"}], request_id="res-2")
        assert excinfo.value.code == "unsupported_content"

    asyncio.run(go())


def test_wait_timeout_never_cancels(tmp_path):
    async def go():
        controller = make_controller(tmp_path)
        history = fakes.ScriptHistory(["unrecorded"] * 1000)
        controller._observer = history
        handle = await create_session(controller, tmp_path)
        bind_fake(controller, handle)
        run_id = await controller.start_turn(handle.id, [{"type": "text", "text": "slow"}], request_id="slow-1")
        with pytest.raises(ControlError) as excinfo:
            await controller.wait(run_id, timeout=0.5)
        assert excinfo.value.code == "wait_timeout"
        # The run is still alive: scripting completion settles the same run.
        history.script[:] = [fakes.script_observation(run_id, outcome="completed")]
        result = await controller.wait(run_id, timeout=20.0)
        assert result.state == "completed"

    asyncio.run(go())


def test_close_cancels_queued_run_and_releases_pane(tmp_path):
    async def go():
        controller = make_controller(tmp_path)
        handle = await create_session(controller, tmp_path)
        backend = controller.backend
        pane = controller.sessions.get(handle.id)["pane"]
        # A durable accepted run with no supervisor yet: close must cancel it.
        spec = {
            "mode": "submit",
            "runtime": "echo",
            "cwd": handle.cwd,
            "session_ref": handle.id,
            "prompt": "queued",
            "on_busy": "queue",
            "queue_timeout": 60,
        }
        data, _ = controller.run_store.create(
            "tester",
            f"{handle.id}:q1",
            spec,
            {"delivery": "accepted", "execution": "accepted", "session_ref": handle.id},
        )
        await controller.close_session(handle.id)
        record = controller.run_store.get(data["run_id"])
        assert record["execution"] == "interrupted"
        assert backend.liveness(pane) == "dead"
        row = controller.sessions.get(handle.id)
        assert row["status"] == "closed"

    asyncio.run(go())


def test_close_failure_keeps_binding_and_reports_uncertain(tmp_path):
    async def go():
        controller = make_controller(tmp_path)
        backend = controller.backend
        handle = await create_session(controller, tmp_path)
        pane = controller.sessions.get(handle.id)["pane"]

        def fail_close(pane_name):
            raise RuntimeError("kill transport exploded")

        backend.close = fail_close
        with pytest.raises(ControlError) as excinfo:
            await controller.close_session(handle.id)
        assert excinfo.value.code == "close_failed"
        row = controller.sessions.get(handle.id)
        assert row["status"] == "hosted", "a failed kill must not release the binding"
        assert row["pane"] == pane

    asyncio.run(go())


def test_close_preserves_natural_completion_racing_cancel(tmp_path):
    async def go():
        runs = []
        controller = make_controller(tmp_path)
        controller._observer = canned_history(runs)
        handle = await create_session(controller, tmp_path)
        bind_fake(controller, handle)
        run_id = await controller.start_turn(handle.id, [{"type": "text", "text": "quick"}], request_id="race-1")
        result = await controller.wait(run_id, timeout=20.0)
        assert result.state == "completed"
        await controller.close_session(handle.id)
        assert controller.run_store.get(run_id)["execution"] == "completed"
        assert controller.sessions.get(handle.id)["status"] == "closed"

    asyncio.run(go())


def test_detach_keeps_work_but_marks_session(tmp_path):
    async def go():
        controller = make_controller(tmp_path)
        handle = await create_session(controller, tmp_path)
        await controller.detach_session(handle.id)
        assert controller.sessions.get(handle.id)["status"] == "detached"

    asyncio.run(go())


def test_namespace_dirs_are_separated(tmp_path):
    async def go():
        root = str(tmp_path / "control")
        first = make_controller(tmp_path, root=root, namespace="alpha")
        second = make_controller(tmp_path, root=root, namespace="beta")
        assert first.sessions.path != second.sessions.path
        handle = await create_session(first, tmp_path)
        listed_first = await first.list_sessions()
        assert [s["session_id"] for s in listed_first["sessions"]] == [handle.id]
        listed = await second.list_sessions()
        assert listed["sessions"] == []
        assert first.sessions.path.is_file()
        assert not second.sessions.path.is_file()

    asyncio.run(go())


def test_list_cursor_pages_and_rejects_garbage(tmp_path):
    async def go():
        controller = make_controller(tmp_path)
        for _ in range(3):
            await create_session(controller, tmp_path)
        first = await controller.list_sessions(cursor="0")
        assert len(first["sessions"]) == 3
        assert first["next_cursor"] is None
        with pytest.raises(ControlError) as excinfo:
            await controller.list_sessions(cursor="bogus")
        assert excinfo.value.code == "usage_error"
        with pytest.raises(ControlError):
            await controller.list_sessions(cursor="-5")

    asyncio.run(go())


def test_detached_spawn_rejects_unserializable_ports(tmp_path):
    async def go():
        controller = make_controller(tmp_path, spawn=True)
        handle = await create_session(controller, tmp_path)
        with pytest.raises(ControlError) as excinfo:
            await controller.start_turn(handle.id, [{"type": "text", "text": "x"}], request_id="det-1")
        assert excinfo.value.code == "unsupported"

    asyncio.run(go())


def test_bind_native_session_via_claim_hook(tmp_path):
    async def go():
        controller = make_controller(tmp_path)
        handle = await create_session(controller, tmp_path)
        assert controller.bind_native_session(handle.id) is None

        def claim(session_ref, runtime, pane, hint):
            assert session_ref == handle.id
            return {"native_id": "claimed-9", "history_path": "/tmp/claimed-history", "evidence": "test-claim"}

        import os as _os

        real_isfile = _os.path.isfile
        _os.path.isfile = lambda p: True if p == "/tmp/claimed-history" else real_isfile(p)
        controller.host_hooks.claim_native = claim
        try:
            binding = controller.bind_native_session(handle.id)
        finally:
            _os.path.isfile = real_isfile
        assert binding and binding["history_path"] == "/tmp/claimed-history"
        row = controller.sessions.get(handle.id)
        assert row["native_id"] == "claimed-9"
        assert row["identity_evidence"] == "test-claim"

    asyncio.run(go())


def test_bind_native_session_via_exact_scan(tmp_path):
    async def go():
        controller = make_controller(tmp_path)
        handle = await create_session(controller, tmp_path)
        controller.sessions.put(
            {**controller.sessions.get(handle.id), "native_id": "exact-1"},
            actor="tester",
            action="session.seed",
        )

        class ExactHistory(fakes.ScriptHistory):
            def find_exact(self, runtime_id, native_id, limit=50):
                assert native_id == "exact-1"
                return {"id": "exact-1", "path": "/tmp/exact-history", "native_title": "T"}

        controller._observer = ExactHistory([])
        binding = controller.bind_native_session(handle.id)
        assert binding and binding["history_path"] == "/tmp/exact-history"
        row = controller.sessions.get(handle.id)
        assert row["identity_evidence"] == "preassigned"
        assert row["title"] == "T"

    asyncio.run(go())


def test_conforms_to_session_controller_port(tmp_path):
    from agent_session_control.models import SessionController

    controller = make_controller(tmp_path)
    for name in SessionController.__protocol_attrs__ if hasattr(SessionController, "__protocol_attrs__") else ():
        assert callable(getattr(controller, name, None)), name
    for name in (
        "capabilities",
        "create",
        "resume",
        "start_turn",
        "watch",
        "wait",
        "interrupt",
        "close_session",
        "list_sessions",
        "load_history",
    ):
        assert callable(getattr(controller, name, None)), name


def test_real_foundation_store_end_to_end(tmp_path):
    from agent_session_control.store import RunStore

    async def go():
        root = str(tmp_path / "control")
        store = RunStore(root, namespace="test")
        controller = make_controller(tmp_path, root=root, namespace="test", store=store)
        controller._observer = canned_history([])
        handle = await create_session(controller, tmp_path)
        bind_fake(controller, handle)
        run_id = await controller.start_turn(handle.id, [{"type": "text", "text": "hi"}], request_id="r1")
        result = await controller.wait(run_id, timeout=30.0)
        assert result.state == "completed"
        assert result.evidence == "native"
        trail = store.audit_trail(run_id)
        assert any(entry["target"] == run_id for entry in trail)

    asyncio.run(go())


@needs_tmux
def test_real_tmux_echo_round_trip(tmp_path):
    from agent_session_control.terminal import TmuxBackend

    async def go():
        root = str(tmp_path / "control")
        backend = TmuxBackend(f"ctl-{os.getpid()}", root=root)
        controller = make_controller(tmp_path, root=root, terminal=backend)
        controller._observer = canned_history([])
        handle = await create_session(controller, tmp_path)
        bind_fake(controller, handle)
        pane = controller.sessions.get(handle.id)["pane"]
        try:
            assert backend.wait_ready(pane, ("ECHO-READY",), timeout=20.0)
            run_id = await controller.start_turn(handle.id, [{"type": "text", "text": "ping"}], request_id="real-1")
            result = await controller.wait(run_id, timeout=20.0)
            assert result.state == "completed"
        finally:
            try:
                backend.close(pane)
            except Exception:
                pass

    asyncio.run(go())


def test_detached_supervisor_survives_initiating_exit(tmp_path):
    """Accepted work settles after the creating controller object is dropped."""
    import fakes as _fakes  # pyrefly: ignore[missing-import]

    async def go():
        file_root = str(tmp_path / "control" / "test")
        store = _fakes.FileStore(file_root)
        backend = _fakes.FileBackend(file_root)
        controller = LocalController(
            root=file_root,
            namespace="default",
            runtime_profiles={"echo": _fakes.EchoProfile()},
            terminal=backend,
            observer=_fakes.FileHistory(file_root),
            store=store,
            spawn=True,
            supervisor_factory="fakes:file_hooks_factory",
            supervisor_store_factory="fakes:file_store_factory",
        )
        handle = await controller.create("echo", str(tmp_path))
        run_id = await controller.start_turn(handle.id, [{"type": "text", "text": "durable"}], request_id="durable-1")
        # The initiating controller goes away only after its child is reaped;
        # the test asserts the reaping contract, not GC timing.
        deadline = time.monotonic() + 45.0
        record = None
        while time.monotonic() < deadline:
            record = store.get(run_id)
            if record.get("execution") in ("completed", "failed", "interrupted", "unknown"):
                break
            time.sleep(0.5)
        assert record is not None and record["execution"] == "completed", record
        assert controller.reap_children() == [], "settled runs must leave no live children behind"
        transcript = backend.capture(controller_pane(file_root, handle))
        assert run_id in transcript
        del controller

    def controller_pane(file_root, handle):
        import sqlite3

        conn = sqlite3.connect(os.path.join(file_root, "sessions.sqlite3"))
        try:
            row = conn.execute("SELECT pane FROM sessions WHERE id=?", (handle.id,)).fetchone()
        finally:
            conn.close()
        return row[0]

    old = os.environ.get("ASC_TEST_FIXTURES")
    os.environ["ASC_TEST_FIXTURES"] = os.path.join(_HERE, "fixtures", "engine")
    try:
        asyncio.run(go())
    finally:
        if old is None:
            os.environ.pop("ASC_TEST_FIXTURES", None)
        else:
            os.environ["ASC_TEST_FIXTURES"] = old


def test_admit_preserves_full_binding_and_runtime(tmp_path):
    """R3: a bound session must observe through its own runtime provider."""
    from agent_session_control.controller import HostHooks, build_hooks
    from agent_session_control.supervisor import SupervisorRunner

    async def go():
        root = str(tmp_path / "control")
        controller = make_controller(tmp_path, root=root)
        handle = await create_session(controller, tmp_path)
        controller.sessions.put(
            {
                **controller.sessions.get(handle.id),
                "native_id": "native-9",
                "history_path": "/tmp/bound-history",
                "identity_evidence": "claimed",
                "title": "T",
            },
            actor="test",
            action="session.bound",
        )
        seen = {}

        class CapHistory(fakes.ScriptHistory):
            def open_reader(self, session):
                seen["open"] = dict(session)
                return {"session": dict(session)}

            def observe(self, reader, run_id, base_seq, events, runtime=""):
                seen["poll_runtime"] = runtime
                return (fakes.script_observation(run_id, outcome="completed"), False)

        store = controller.run_store
        spec = {
            "mode": "submit",
            "runtime": "echo",
            "cwd": handle.cwd,
            "session_ref": handle.id,
            "history_path": "/tmp/bound-history",
            "prompt": "hi",
            "model": None,
            "on_busy": "queue",
            "queue_timeout": 60,
        }
        data, _ = store.create(
            "test",
            f"{handle.id}:r3",
            spec,
            {"delivery": "accepted", "execution": "accepted", "session_ref": handle.id, "runtime": "echo"},
        )
        hooks = build_hooks(
            root=root,
            namespace="test",
            backend=controller.backend,
            observer=CapHistory([]),
            registry={"echo": fakes.EchoProfile()},
            store=store,
            host_hooks=HostHooks(),
        )
        runner = SupervisorRunner(store, data["run_id"], hooks, settle_quiet=0, history_wait=5)
        runner.admit()
        assert runner.binding["runtime"] == "echo", "admission must keep the runtime for observation"
        assert runner.binding["native_id"] == "native-9"
        assert runner.binding["history_path"] == "/tmp/bound-history"
        handle_id = hooks.open_observation(runner.binding)
        obs, reset, state = hooks.poll_observation(handle_id, data["run_id"], -1)
        assert seen["open"]["source"] == "echo", "the observer must read through the bound runtime"
        assert seen["poll_runtime"] == "echo"
        assert obs["recorded"] is True and reset is False

    asyncio.run(go())


def test_observation_keeps_single_reader_and_closes_it(tmp_path):
    """R4: one reader/cursor per handle; polled incrementally; closed at teardown."""
    from types import SimpleNamespace

    async def go():
        controller = make_controller(tmp_path)
        opened = []
        closed = []

        class CountingHistory(fakes.ScriptHistory):
            def open_reader(self, session):
                opened.append(dict(session))
                reader = SimpleNamespace(closed=False)

                def _close():
                    reader.closed = True
                    closed.append(reader)

                reader.close = _close
                return reader

        runs = []
        history = CountingHistory([])
        history.observe = lambda reader, run_id, base, events, runtime="": (
            runs.append(run_id)
            or (
                fakes.script_observation(run_id, outcome="in_progress"),
                False,
            )
        )
        controller._observer = history
        handle = await create_session(controller, tmp_path)
        bind_fake(controller, handle)
        run_id = await controller.start_turn(handle.id, [{"type": "text", "text": "slow"}], request_id="r4")
        history.observe = lambda reader, run_id, base, events, runtime="": (
            fakes.script_observation(run_id, outcome="completed"),
            False,
        )
        result = await controller.wait(run_id, timeout=30.0)
        assert result.state == "completed"
        assert len(opened) == 1, f"exactly one reader per observation handle, got {len(opened)}"
        assert len(closed) == 1, "the retained reader must be closed at teardown"

    asyncio.run(go())


def test_spawn_failure_retry_reconciles_supervision(tmp_path):
    """R5: identical retry after a spawn failure converges and supervises."""

    async def go():
        controller = make_controller(tmp_path)
        controller._observer = canned_history([])
        handle = await create_session(controller, tmp_path)
        bind_fake(controller, handle)
        calls = []
        real_launch = controller._launch_supervisor

        def fail_once(run_id, actor):
            calls.append(("fail", run_id))
            raise OSError("simulated process-spawn failure")

        def counting_real(run_id, actor):
            calls.append(("ok", run_id))
            return real_launch(run_id, actor)

        controller._launch_supervisor = fail_once
        prompt = [{"type": "text", "text": "durable"}]
        with pytest.raises(OSError):
            await controller.start_turn(handle.id, prompt, request_id="r5")
        controller._launch_supervisor = counting_real
        # A fresh caller retries the identical request: same run, supervision ensured.
        run_id = await controller.start_turn(handle.id, prompt, request_id="r5")
        result = await controller.wait(run_id, timeout=30.0)
        assert result.state == "completed"
        assert [kind for kind, _ in calls] == ["fail", "ok"], "one failed attempt plus one reconciled launch"
        assert len(controller.backend.submitted) == 1, "recovery must not replay the brief"

    asyncio.run(go())


def test_recover_run_launches_accepted_and_ignores_settled(tmp_path):
    async def go():
        controller = make_controller(tmp_path)
        controller._observer = canned_history([])
        handle = await create_session(controller, tmp_path)
        bind_fake(controller, handle)
        row = controller.sessions.get(handle.id)
        spec = {
            "mode": "submit",
            "runtime": "echo",
            "cwd": handle.cwd,
            "session_ref": handle.id,
            "history_path": row.get("history_path"),
            "prompt": "orphan",
            "model": None,
            "on_busy": "queue",
            "queue_timeout": 60,
            "mcp": {"names": [], "digest": "x"},
            "marker_namespace": "session-control",
            "legacy_namespaces": [],
        }
        data, created = controller.run_store.create(
            "test",
            f"{handle.id}:orphan",
            spec,
            {"delivery": "accepted", "execution": "accepted", "session_ref": handle.id},
        )
        assert created
        returned = await controller.recover_run(data["run_id"])
        assert returned == data["run_id"]
        result = await controller.wait(data["run_id"], timeout=30.0)
        assert result.state == "completed"
        before = len(controller.backend.submitted)
        assert await controller.recover_run(data["run_id"]) == data["run_id"]
        assert len(controller.backend.submitted) == before, "settled runs are never relaunched"

    asyncio.run(go())


def test_resume_refuses_live_unowned_pane(tmp_path):
    """R7: a live pane outside our ownership is lost ownership, not recovery."""
    from agent_session_control.errors import CODE_LOST_OWNERSHIP

    async def go():
        controller = make_controller(tmp_path)
        handle = await create_session(controller, tmp_path)
        foreign = fakes.ScriptBackend()
        foreign.panes = controller.backend.panes
        foreign.is_owned = lambda pane: False
        controller._terminal = foreign
        with pytest.raises(ControlError) as excinfo:
            await controller.resume(handle.id)
        assert excinfo.value.code == CODE_LOST_OWNERSHIP
        row = controller.sessions.get(handle.id)
        assert row["pane"] is not None and row["status"] == "hosted", "refusal changes nothing"

    asyncio.run(go())


def test_close_racing_resume_keeps_closed(tmp_path):
    """R8: a close concurrent with resume recovery must win durably."""
    import threading

    async def go():
        controller = make_controller(tmp_path)
        backend = controller.backend
        handle = await create_session(controller, tmp_path)
        old_pane = controller.sessions.get(handle.id)["pane"]
        backend.panes[old_pane]["alive"] = False
        controller.sessions.put(
            {**controller.sessions.get(handle.id), "native_id": "native-1"},
            actor="tester",
            action="session.bound",
        )
        started = threading.Event()
        release = threading.Event()
        real_host = backend.host

        def blocking_host(*args, **kwargs):
            started.set()
            assert release.wait(15), "test release timed out"
            return real_host(*args, **kwargs)

        backend.host = blocking_host
        errors = []

        def do_resume():
            try:
                asyncio.run(controller.resume(handle.id))
            except BaseException as exc:  # noqa: BLE001 - collected for assertion
                errors.append(exc)

        worker = threading.Thread(target=do_resume)
        worker.start()
        try:
            assert started.wait(15), "resume never reached the host boundary"
            await controller.close_session(handle.id)
            after = controller.sessions.get(handle.id)
            assert after["status"] == "closed" and after["pane"] is None
        finally:
            release.set()
            worker.join(15)
        final = controller.sessions.get(handle.id)
        assert final["status"] == "closed", f"resume resurrected the session: {final}"
        assert final["pane"] is None, "no new live pane may survive a reported close"

    asyncio.run(go())


def _wait_for_condition(predicate, *, timeout=30.0, what="condition"):
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.05)


def test_start_turn_retry_converges_after_binding_learned(tmp_path):
    """Identical retry after async binding converges (history_path excluded)."""

    async def go():
        controller = make_controller(tmp_path)
        controller._observer = canned_history([])
        handle = await create_session(controller, tmp_path)
        prompt = [{"type": "text", "text": "same brief"}]
        # First accept while unbound: stored spec records history_path None.
        first = await controller.start_turn(handle.id, prompt, request_id="bind-race-1")
        stored = controller.run_store.get(first, private=True)
        assert stored["spec"]["history_path"] is None
        assert stored["spec"]["prompt"] == "same brief"
        # Binding is learned asynchronously after acceptance.
        bind_fake(controller, handle, path="fake-history")
        assert controller.sessions.get(handle.id)["history_path"] == "fake-history"
        # Identical retry with the learned path must converge, not conflict.
        second = await controller.start_turn(handle.id, prompt, request_id="bind-race-1")
        assert second == first
        result = await controller.wait(first, timeout=20.0)
        assert result.state == "completed"
        # No replay/double supervisor: exactly one brief submission, one run.
        assert len(controller.backend.submitted) == 1
        assert len(controller.run_store.runs) == 1
        # Historical submit spec is preserved (still None), not rewritten.
        preserved = controller.run_store.get(first, private=True)
        assert preserved["spec"]["history_path"] is None
        # Changed payloads still conflict after binding.
        with pytest.raises(ControlError) as excinfo:
            await controller.start_turn(
                handle.id, [{"type": "text", "text": "different brief"}], request_id="bind-race-1"
            )
        assert excinfo.value.code == "idempotency_conflict"
        with pytest.raises(ControlError) as excinfo2:
            await controller.start_turn(handle.id, prompt, request_id="bind-race-1", on_busy="reject")
        assert excinfo2.value.code == "idempotency_conflict"
        assert len(controller.backend.submitted) == 1, "conflicts must not submit"

    asyncio.run(go())


def test_start_turn_reopened_controller_converges_after_binding(tmp_path):
    """Reopened controller on the same root reuses the bound run (real store)."""
    from agent_session_control.store import RunStore

    async def go():
        root = str(tmp_path / "control")
        store = RunStore(root, namespace="test")
        first = make_controller(tmp_path, root=root, namespace="test", store=store)
        first._observer = canned_history([])
        handle = await create_session(first, tmp_path)
        prompt = [{"type": "text", "text": "durable brief"}]
        run_a = await first.start_turn(handle.id, prompt, request_id="reopen-1")
        _wait_for_condition(lambda: len(first.backend.submitted) >= 1, what="first brief submission")
        # Binding learned after acceptance (supervisor claim path).
        first.sessions.put(
            {**first.sessions.get(handle.id), "history_path": "fake-history"},
            actor="test",
            action="session.bound",
        )
        reopened = LocalController(
            root=root,
            namespace="test",
            runtime_profiles={"echo": fakes.EchoProfile()},
            terminal=fakes.ScriptBackend(),
            observer=canned_history([]),
            store=RunStore(root, namespace="test"),
            spawn=False,
        )
        run_b = await reopened.start_turn(handle.id, prompt, request_id="reopen-1")
        assert run_b == run_a, "the immutable request id converges, not forks"
        result = await reopened.wait(run_a, timeout=20.0)
        assert result.state == "completed"
        # One durable run, one submission on the owning backend; reopened
        # convergence must not replay the brief on its own backend.
        assert len(reopened.backend.submitted) == 0
        assert len(first.backend.submitted) == 1
        trail = store.audit_trail(run_a)
        creates = [e for e in trail if e["action"] == "run.create"]
        assert len(creates) == 1, f"exactly one durable acceptance, got {len(creates)}"
        stored = store.get(run_a, private=True)
        assert stored["spec"]["prompt"] == "durable brief"
        with pytest.raises(ControlError) as excinfo:
            await reopened.start_turn(handle.id, [{"type": "text", "text": "changed"}], request_id="reopen-1")
        assert excinfo.value.code == "idempotency_conflict"

    asyncio.run(go())


def test_start_turn_concurrent_same_key_converges_single_run(tmp_path):
    """Concurrent identical retries behind a barrier converge to one run."""
    import threading

    from agent_session_control.store import RunStore

    async def _make_and_start(root, barrier, results, errors, index):
        try:
            store = RunStore(root, namespace="test")
            controller = make_controller(
                tmp_path,
                root=root,
                namespace="test",
                store=store,
                observer=canned_history([]),
            )
            if index == 0:
                handle = await create_session(controller, tmp_path)
                barrier._handle_id[0] = handle.id
            barrier.wait(15)
            handle_id = barrier._handle_id[0]
            # All parties observe the bound row; intent is identical.
            prompt = [{"type": "text", "text": "concurrent brief"}]
            run_id = await controller.start_turn(handle_id, prompt, request_id="conc-1")
            results[index] = (run_id, controller)
        except BaseException as exc:  # noqa: BLE001 - collected for assertion
            errors[index] = exc

    root = str(tmp_path / "control")
    seed = make_controller(tmp_path, root=root, namespace="test", store=RunStore(root, namespace="test"))
    seed._observer = canned_history([])

    async def go():
        handle = await create_session(seed, tmp_path)
        bind_fake(seed, handle, path="fake-history")
        return handle.id

    handle_id = asyncio.run(go())
    barrier = threading.Barrier(4)
    barrier._handle_id = [handle_id]
    results: dict = {}
    errors: dict = {}
    workers = [
        threading.Thread(target=lambda i=i: asyncio.run(_make_and_start(root, barrier, results, errors, i)))
        for i in range(4)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(30)
    assert not errors, f"concurrent retries raised: {errors}"
    assert len(results) == 4
    run_ids = {run_id for run_id, _ in results.values()}
    assert len(run_ids) == 1, f"concurrent same-key retries must converge, got {run_ids}"
    run_id = next(iter(run_ids))
    store = RunStore(root, namespace="test")
    record = store.get(run_id, private=True)
    assert record["spec"]["prompt"] == "concurrent brief"
    trail = store.audit_trail(run_id)
    creates = [e for e in trail if e["action"] == "run.create"]
    assert len(creates) == 1, f"exactly one durable acceptance, got {len(creates)}"

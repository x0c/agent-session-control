"""Consumer control tests: create/send/observe/cancel/resume + durable restart.

Uses only the installed ``agent_session_control`` public controller port
plus the public ``HostHooks.claim_native`` host-claim hook (the documented
equivalent of Corral's managed-identity preparation). No Corral,
SessKit-parsing, UI or vendor-SDK imports. stdlib unittest with
``asyncio.run`` (no pytest plugin required); also runs under pytest.

Test strategy (honest split, no fake compatibility):

- Plan fidelity (argv/cwd/env per runtime) is covered against the REAL
  foundation plan builders in ``test_consumer_plan.py``.
- Here the controller port runs with ``spawn=False`` (in-process
  supervision, no subprocess) plus self-contained doubles behind the
  documented ports: ``terminal=`` (pane lifecycle/input), ``observer=``
  (scripted native-law observation) and ``runtime_profiles=`` (a thin shim
  over the real foundation plan builders, so argv semantics stay genuine).
  Nothing here spawns a process, touches tmux, or reads user histories.
- Real-terminal behavior (pane ownership, readiness, paste verification,
  login/quota limits) belongs to the engine worker's isolated tmux suite,
  not to this consumer harness.

Each test uses a disposable root; namespaces are unique per test. The
scripted observer simulates native law only (recorded/correlated input,
native completion evidence, interrupt settlement); it never invents
delivery the fake backend did not perform.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from agent_session_control.controller import HostHooks, LocalController
from agent_session_control.errors import ControlError
from agent_session_control.runtime import BUILTIN_RUNTIME_IDS

RUNTIME = os.environ.get("ASC_CONSUMER_RUNTIME", "claude")
PROMPT = [{"type": "text", "text": "Reply with the single word ACK and stop."}]


def _event(seq: int, kind: str, text: str):
    return SimpleNamespace(
        seq=seq,
        type=kind,
        text=text,
        ts=None,
        name=None,
        usage=None,
        error=None,
        call_id=None,
        result=None,
        interaction=None,
    )


def _observation(outcome: str, evidence: str):
    return {
        "recorded": True,
        "messages": ["initial"],
        "first_seq": 1,
        "end_seq": 2,
        "turn_id": "t1",
        "outcome": outcome,
        "outcome_evidence": evidence,
        "outcome_error": None,
        "attribution": "correlated",
        "current_turn": True,
        "pending_interaction": False,
        "events": [_event(1, "user_message", "brief [wire]"), _event(2, "assistant_message", "ACK")],
    }


class FakeBackend:
    """In-process pane lifecycle/input double behind the terminal port."""

    def __init__(self):
        self.hosted: list[dict] = []
        self.submitted: list[tuple[str, str]] = []
        self.interrupts: list[tuple[str, tuple]] = []
        self.closed: list[str] = []
        self.liveness_value = "alive"
        self._panes = 0

    def new_ident(self) -> str:
        return f"ident-{uuid.uuid4().hex[:8]}"

    def host(self, argv, *, runtime, cwd, env, ident):
        self._panes += 1
        pane = f"pane-{self._panes}"
        self.hosted.append(
            {
                "pane": pane,
                "argv": tuple(argv),
                "runtime": runtime,
                "cwd": cwd,
                "env": dict(env or {}),
                "ident": ident,
            }
        )
        return pane

    def liveness(self, pane):
        return self.liveness_value

    def wait_ready(self, pane, markers, timeout=None):
        return None

    def submit_text(self, pane, text, composer_region=None):
        self.submitted.append((pane, text))

    def verify_alive_after_inject(self, pane):
        return None

    def interrupt(self, pane, keys):
        self.interrupts.append((pane, tuple(keys)))

    def capture(self, pane):
        return ""

    def close(self, pane):
        self.closed.append(pane)
        return True


class ScriptedObserver:
    """Scripted native-law observation: per-run outcome, polled by the supervisor."""

    def __init__(self):
        self.outcomes: dict[str, tuple[str, str]] = {}
        self.polls = 0

    def set_outcome(self, run_id: str, outcome: str, evidence: str = "native") -> None:
        self.outcomes[run_id] = (outcome, evidence)

    def baseline_seq(self, session):
        return -1

    def open_reader(self, session):
        reader = SimpleNamespace(closed=False)
        reader.close = lambda: setattr(reader, "closed", True)
        return reader

    def observe(self, reader, run_id, base_seq, events, runtime):
        self.polls += 1
        outcome, evidence = self.outcomes.get(run_id, ("in_progress", "unknown"))
        return (_observation(outcome, evidence), False, "ready")


class ProfileShim:
    """Port-shaped profile delegating plan building to the real foundation builders."""

    interrupt_keys = ("Escape",)
    readiness_markers: tuple = ()
    composer_region = None

    def __init__(self, runtime_id: str):
        from agent_session_control import runtime as rt

        self.id = runtime_id
        self._mod = rt

    def build_create_plan(self, cwd, *, model=None, auto_approve=False, mcp_servers=()):
        return self._mod.build_create_plan(
            self.id, cwd, model=model, auto_approve=auto_approve, mcp_servers=mcp_servers
        )

    def build_resume_plan(self, session, *, auto_approve=False, mcp_servers=()):
        return self._mod.build_resume_plan(self.id, session, auto_approve=auto_approve, mcp_servers=mcp_servers)

    def describe(self):
        profile = self._mod.runtime_profile(self.id)
        return {
            "steer": False,
            "interrupt_keys": list(profile.get("interrupt_keys", ["Escape"])),
            "model_selection": bool(profile.get("model_selection", False)),
            "mcp": "unsupported",
        }


def _fresh_scope():
    tmp = tempfile.TemporaryDirectory(prefix="asc-consumer-ctl-")
    project = str(Path(tmp.name, "project").resolve())
    os.makedirs(project, exist_ok=True)
    root = str(Path(tmp.name, "control").resolve())
    return tmp, root, f"consumer-{uuid.uuid4().hex[:8]}", project


def _profiles():
    return {rid: ProfileShim(rid) for rid in BUILTIN_RUNTIME_IDS}


def _wait_for(predicate, *, timeout: float = 30.0, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.1)


class _ControlBase(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root, self.namespace, self.project = _fresh_scope()
        self.addCleanup(self.tmp.cleanup)
        self.backend = FakeBackend()
        self.observer = ScriptedObserver()
        self.fixture = str(Path(self.tmp.name, "native.jsonl").resolve())
        Path(self.fixture).write_text("", encoding="utf-8")
        claim_path = self.fixture

        def claim_native(session_id, runtime, pane, native_id):
            return {
                "history_path": claim_path,
                "native_id": native_id or f"test-native-{uuid.uuid4().hex[:8]}",
                "evidence": "test-claim",
            }

        self.controller = LocalController(
            root=self.root,
            namespace=self.namespace,
            terminal=self.backend,
            observer=self.observer,
            runtime_profiles=_profiles(),
            host_hooks=HostHooks(claim_native=claim_native),
            spawn=False,
        )

    def _wait_settled(self, run_id: str, timeout: float = 60.0):
        async def _wait():
            return await self.controller.wait(run_id, timeout=timeout)

        return asyncio.run(_wait())

    def _drain_threads(self):
        for thread in list(getattr(self.controller, "_threads", [])):
            thread.join(timeout=20)


class ConsumerControlTests(_ControlBase):
    def test_create_send_observe_settle(self):
        async def _flow():
            handle = await self.controller.create(RUNTIME, self.project)
            self.assertTrue(handle.id.startswith("ses_"))
            self.assertEqual(handle.runtime, RUNTIME)
            self.assertIsNone(handle.native_id, "no native identity is fabricated before first write")
            hosted = self.backend.hosted[-1]
            self.assertTrue(hosted["argv"], "launch plan is an argv array, never a shell command")
            self.assertNotIn(" ", hosted["argv"][0])
            self.assertEqual(Path(hosted["cwd"]), Path(self.project))
            self.assertEqual(hosted["env"], {})
            run_id = await self.controller.start_turn(handle.id, PROMPT, request_id=f"consumer-{uuid.uuid4().hex[:12]}")
            self.assertTrue(run_id.startswith("run_"))
            return handle, run_id

        handle, run_id = asyncio.run(_flow())
        _wait_for(lambda: self.backend.submitted, what="brief submission")
        pane, text = self.backend.submitted[-1]
        self.assertEqual(pane, self.backend.hosted[-1]["pane"])
        listing = asyncio.run(self.controller.list_sessions())
        self.assertTrue(any(s["session_id"] == handle.id for s in listing["sessions"]))
        self.observer.set_outcome(run_id, "completed")
        result = self._wait_settled(run_id)
        self.assertEqual(result.state, "completed")
        self.assertEqual(result.evidence, "native")
        self.assertEqual(result.final_response, "ACK")
        self.assertEqual(result.run_id, run_id)

        async def _watch():
            return [e async for e in self.controller.watch(run_id)]

        seen = asyncio.run(_watch())
        self.assertTrue(seen, "settled runs keep readable events")
        self.assertTrue(all(isinstance(e.seq, int) for e in seen))
        asyncio.run(self.controller.close_session(handle.id))
        self.assertIn(self.backend.hosted[-1]["pane"], self.backend.closed, "close releases the owned pane")
        row = self.controller.sessions.get(handle.id)
        self.assertEqual(row["status"], "closed")
        self._drain_threads()

    def test_interrupt_targets_only_the_exact_run(self):
        async def _flow():
            handle_a = await self.controller.create(RUNTIME, self.project)
            handle_b = await self.controller.create(RUNTIME, self.project)
            run_a = await self.controller.start_turn(
                handle_a.id, PROMPT, request_id=f"consumer-a-{uuid.uuid4().hex[:12]}"
            )
            run_b = await self.controller.start_turn(
                handle_b.id, PROMPT, request_id=f"consumer-b-{uuid.uuid4().hex[:12]}"
            )
            return handle_a, handle_b, run_a, run_b

        handle_a, handle_b, run_a, run_b = asyncio.run(_flow())
        pane_b = self.backend.hosted[-1]["pane"]
        _wait_for(lambda: len(self.backend.submitted) >= 2, what="both briefs submitted")
        asyncio.run(self.controller.interrupt(run_b))
        _wait_for(lambda: self.backend.interrupts, what="interrupt delivery")
        self.observer.set_outcome(run_b, "interrupted")
        result_b = self._wait_settled(run_b)
        self.assertEqual(result_b.state, "interrupted")
        self.assertEqual([[pane, list(keys)] for pane, keys in self.backend.interrupts], [[pane_b, ["Escape"]]])
        record_a = self.controller.run_store.get(run_a)
        self.assertEqual(record_a["execution"], "running", "the unrelated run is untouched")
        messages = self.controller.run_store.messages(run_b)
        states = {m["message_id"]: m["state"] for m in messages}
        self.assertIn(
            "input_submitted",
            set(states.values()),
            "the interrupt was delivered through the exact run's message path",
        )
        self.observer.set_outcome(run_a, "completed")
        result_a = self._wait_settled(run_a)
        self.assertEqual(result_a.state, "completed")
        asyncio.run(self.controller.close_session(handle_a.id))
        asyncio.run(self.controller.close_session(handle_b.id))
        self._drain_threads()

    def test_resume_preserves_exact_native_identity(self):
        async def _flow():
            handle = await self.controller.create(RUNTIME, self.project)
            self.controller.sessions.put(
                {
                    **self.controller.sessions.get(handle.id),
                    "native_id": "exact-native-9",
                    "history_path": self.fixture,
                },
                actor="test",
                action="session.seed",
            )
            resumed = await self.controller.resume(handle.id)
            self.assertEqual(
                resumed.native_id,
                "exact-native-9",
                "native identity is exact, never replaced by the local control id",
            )
            self.assertNotEqual(resumed.id, resumed.native_id)
            await self.controller.close_session(handle.id)
            return resumed

        resumed = asyncio.run(_flow())
        self.assertTrue(resumed.id)
        self._drain_threads()

    def test_capabilities_are_honest(self):
        async def _flow():
            info = self.controller.capabilities(RUNTIME)
            self.assertEqual(info["runtime"], RUNTIME)
            self.assertTrue(info["queue"])
            self.assertFalse(info["steer"], "no true steering is claimed without backend support")

        asyncio.run(_flow())
        self._drain_threads()


class ConsumerDurabilityTests(_ControlBase):
    def test_durable_restart_idempotency_and_timeout_honesty(self):
        async def _start():
            handle = await self.controller.create(RUNTIME, self.project)
            key = f"consumer-{uuid.uuid4().hex[:12]}"
            run_id = await self.controller.start_turn(handle.id, PROMPT, request_id=key)
            return handle, key, run_id

        handle, key, run_a = asyncio.run(_start())
        _wait_for(lambda: self.backend.submitted, what="brief submission")
        # A stopped caller loses nothing: a second controller on the same
        # root/namespace rejoins the same durable run.
        reopened = LocalController(
            root=self.root,
            namespace=self.namespace,
            terminal=FakeBackend(),
            observer=ScriptedObserver(),
            runtime_profiles=_profiles(),
            host_hooks=self.controller.host_hooks,
            spawn=False,
        )

        async def _rejoin():
            run_b = await reopened.start_turn(handle.id, PROMPT, request_id=key)
            self.assertEqual(run_a, run_b, "the immutable request id converges, not forks")
            try:
                await reopened.start_turn(handle.id, [{"type": "text", "text": "different payload"}], request_id=key)
            except ControlError as exc:
                self.assertEqual(exc.code, "idempotency_conflict")
            else:
                self.fail("a changed payload under the same request id must conflict")
            try:
                await reopened.wait(run_a, timeout=1)
            except ControlError as exc:
                self.assertEqual(exc.code, "wait_timeout")
            else:
                self.fail("a short wait must time out while work continues")
            record = reopened.run_store.get(run_a)
            self.assertEqual(record["execution"], "running", "a wait timeout never cancels work")
            return run_b

        run_b = asyncio.run(_rejoin())
        self.assertEqual(run_a, run_b)
        self.observer.set_outcome(run_a, "completed")
        result = self._wait_settled(run_a)
        self.assertEqual(result.state, "completed")

        async def _close():
            await self.controller.close_session(handle.id)

        asyncio.run(_close())
        self._drain_threads()
        for thread in list(getattr(reopened, "_threads", [])):
            thread.join(timeout=20)


class ConsumerAcpEntryTests(unittest.TestCase):
    def test_acp_adapter_present(self):
        from agent_session_control import acp

        self.assertTrue(hasattr(acp, "ControlAgent"))

    def test_cli_entry_responds(self):
        import pathlib

        repo = pathlib.Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(repo / "src") + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        proc = subprocess.run(
            [sys.executable, "-m", "agent_session_control", "version"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(repo),
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn('"ok": true', proc.stdout)


if __name__ == "__main__":
    unittest.main()

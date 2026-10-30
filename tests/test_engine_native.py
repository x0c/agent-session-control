"""Real native-agent acceptance: production-default detached codex turn.

No fixture hooks, factories, backends, observers or profiles anywhere on this
path: a separate exiting caller process drives a production-default
``LocalController`` (real RunStore, owned TmuxBackend, foundation registry,
SessKit observer, default supervisor factories) to submit one tiny turn to a
real codex session, then exits. The detached supervisor binds the exact
native session by wire-marker evidence, observes the real turn through
SessKit, and settles with the returned response.

Codex has no preassignable native id, so binding proves the marker-evidence
path: the candidate search is bounded to the disposable cwd, and only a
marker whose run belongs to this session (resolved through the operation
store) binds. Recency, title and mtime alone never bind.

Skips (with reasons, never silent passes) when SessKit or the codex CLI is
unavailable, or codex is not logged in. Spends exactly one tiny model turn
in a disposable workspace. Claude coverage is recorded as an explicit gap:
its account quota is exhausted until Nov 1, so no real Claude turn can
complete in this environment (verified: the pane reports the weekly limit
and the run stays honestly unsettled).
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(_HERE)
CALLER = os.path.join(_HERE, "fixtures", "engine", "detached_caller.py")

sesskit = pytest.importorskip("sesskit", reason="SessKit is required for native observation")
needs_codex = pytest.mark.skipif(shutil.which("codex") is None, reason="codex CLI is not installed")

PROMPT = "Reply with exactly the single word PONG and nothing else. Do not use any tools."


def codex_logged_in() -> bool:
    try:
        proc = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and "not logged in" not in (proc.stdout + proc.stderr).lower()


@needs_codex
def test_production_default_detached_codex_turn(tmp_path):
    if not codex_logged_in():
        pytest.skip("codex is not logged in on this machine")
    from agent_session_control.controller import LocalController
    from agent_session_control.store import RunStore
    from agent_session_control.terminal import TmuxBackend

    root = str(tmp_path / "control")
    namespace = "native"
    workspace = str(tmp_path / "workspace")
    os.makedirs(workspace, exist_ok=True)

    controller = LocalController(root=root, namespace=namespace)
    backend = controller.backend
    assert isinstance(backend, TmuxBackend)

    async def _create():
        return await controller.create("codex", workspace)

    handle = asyncio.run(_create())
    assert handle.native_id is None, "codex has no preassignable id; nothing is fabricated before binding"
    pane = controller.sessions.get(handle.id)["pane"]
    try:
        assert backend.wait_ready(pane, ("OpenAI Codex",), timeout=180.0), "the codex TUI never became ready"

        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.join(REPO, "src")
        if os.environ.get("PYTHONPATH"):
            env["PYTHONPATH"] += os.pathsep + os.environ["PYTHONPATH"]
        caller = subprocess.run(
            [sys.executable, CALLER, root, namespace, handle.id, PROMPT, f"native-{uuid_suffix()}"],
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
        assert caller.returncode == 0, f"the exiting caller failed: {caller.stderr[-2000:]}"
        created = json.loads(caller.stdout.strip().splitlines()[-1])
        assert created["session_id"] == handle.id
        run_id = created["run_id"]
        assert run_id.startswith("run_")

        store = RunStore(root, namespace=namespace)
        deadline = time.monotonic() + 600.0
        record = None
        while time.monotonic() < deadline:
            record = store.get(run_id)
            if record.get("execution") in ("completed", "failed", "interrupted", "unknown"):
                break
            time.sleep(2.0)
        assert record is not None, "the detached run never settled"
        if record["execution"] != "completed":
            # Report environment limits distinctly: a quota/auth stop is a
            # limit, never a pass — but it must read as what it is, not as a
            # product defect. Anything else is a genuine acceptance failure.
            diagnosis = {
                "execution": record.get("execution"),
                "delivery": record.get("delivery"),
                "attribution": record.get("attribution"),
                "finality": record.get("finality"),
                "error": record.get("error"),
            }
            err_text = str((record.get("error") or {}).get("message", "")).lower()
            limit_marks = (
                "weekly limit",
                "rate limit",
                "usage limit",
                "quota",
                "billing",
                "insufficient credit",
                "payment",
                "unauthorized",
                "forbidden",
                "not logged in",
                "no auth",
                "authenticate",
            )
            if any(mark in err_text for mark in limit_marks):
                pytest.skip(f"codex refused on login/quota/capacity; limit, not a pass: {diagnosis}")
            raise AssertionError(f"native turn did not complete; run diagnosis: {diagnosis}")
        assert record["delivery"] == "native_recorded"
        assert record["attribution"] == "correlated"
        result = record.get("result") or {}
        assert result.get("final_response") and "pong" in result["final_response"].lower(), result
        assert record.get("finality") in ("native", "inferred")
        row = controller.sessions.get(handle.id)
        assert row.get("native_id"), "marker evidence bound the exact native thread id"
        assert row.get("history_path"), "marker evidence bound the exact native history"
        assert row.get("identity_evidence") == "marker"
    finally:
        try:

            async def _close():
                await controller.close_session(handle.id)

            asyncio.run(_close())
        except Exception:
            pass


def uuid_suffix():
    import uuid

    return uuid.uuid4().hex[:12]

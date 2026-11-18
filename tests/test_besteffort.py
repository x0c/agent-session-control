"""Best-effort session creation policy: candidates, backoff, fallback.

Everything runs against a scripted controller double implementing the
SessionController port surface this feature consumes — no terminal hosting,
native history or model quota is involved. CLI handler tests inject the same
double as the controller factory, so ``cmd_session_create`` auto mode is
covered without touching real runtimes.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from agent_session_control import besteffort  # noqa: E402
from agent_session_control import runtime as runtime_mod
from agent_session_control.besteffort import create_session_best_effort  # noqa: E402
from agent_session_control.cli import cmd_session_create  # noqa: E402
from agent_session_control.errors import ControlError, unavailable  # noqa: E402
from agent_session_control.models import RunResult, SessionHandle  # noqa: E402


def launch_failed() -> ControlError:
    return ControlError("launch_failed", "tmux hiccup")


class Scripted:
    """Controller double with scriptable failures and call recording."""

    def __init__(
        self,
        *,
        available=("claude",),
        create_errors=None,
        submit_errors=None,
        turn_results=None,
        wait_errors=None,
        registry_ids=None,
        declare_availability=True,
    ):
        self.available = set(available)
        self.declare_availability = declare_availability
        self.create_errors = list(create_errors or [])
        self.submit_errors = list(submit_errors or [])
        self.turn_results = list(turn_results or [])
        self.wait_errors = list(wait_errors or [])
        self.creates: list[tuple[str, object]] = []
        self.submits: list[tuple[str, str]] = []
        self.waits: list[tuple[str, object]] = []
        self.closes: list[str] = []
        self.profiles = SimpleNamespace(ids=tuple(registry_ids)) if registry_ids is not None else None

    def capabilities(self, runtime):
        if not self.declare_availability:
            return {}  # the stock controller declares no availability key
        return {"available": runtime in self.available}

    async def create(self, runtime, cwd, *, model=None, mcp_servers=(), actor="local-user"):
        self.creates.append((runtime, model))
        error = self.create_errors.pop(0) if self.create_errors else None
        if error is not None:
            raise error
        return SessionHandle(id=f"ses_{runtime}_{len(self.creates)}", runtime=runtime, cwd=cwd)

    async def start_turn(self, session_id, prompt, *, request_id, on_busy="queue", actor="local-user"):
        self.submits.append((session_id, request_id))
        error = self.submit_errors.pop(0) if self.submit_errors else None
        if error is not None:
            raise error
        return f"run_{session_id}"

    async def wait(self, run_id, *, timeout=None):
        self.waits.append((run_id, timeout))
        error = self.wait_errors.pop(0) if self.wait_errors else None
        if error is not None:
            raise error
        result = self.turn_results.pop(0) if self.turn_results else None
        if result is not None:
            return result
        return RunResult(run_id=run_id, state="completed", final_response="done")

    async def close_session(self, session_id, *, actor="local-user"):
        self.closes.append(session_id)


def recording_sleep(log: list[float]):
    async def _sleep(seconds: float) -> None:
        log.append(seconds)

    return _sleep


def run(coro):
    return asyncio.run(coro)


def cli_args(tmp_path: Path, **overrides) -> SimpleNamespace:
    base = {
        "cwd": str(tmp_path),
        "runtime": None,
        "model": None,
        "prompt": None,
        "prompt_file": None,
        "dry_run": False,
        "mcp_servers": None,
        "mcp_servers_file": None,
        "json": True,
        "root": None,
        "namespace": "default",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# -- unit: candidate selection ------------------------------------------------------


def test_first_candidate_hosts_without_prompt():
    controller = Scripted(available=("claude",))
    result = run(create_session_best_effort(controller, "/tmp/proj"))
    assert result.session.runtime == "claude"
    assert result.run_id is None
    assert [entry.outcome for entry in result.attempts] == ["started"]
    assert controller.submits == []
    assert controller.closes == []


def test_unavailable_candidate_skipped_and_logged():
    controller = Scripted(available=("codex",))
    result = run(create_session_best_effort(controller, "/tmp/proj"))
    assert result.session.runtime == "codex"
    assert [entry.outcome for entry in result.attempts] == ["skipped_unavailable", "started"]
    assert result.attempts[0].runtime == "claude"
    assert result.attempts[0].code == "unavailable"
    assert controller.creates == [("codex", None)]


def test_registry_profiles_drive_candidate_order():
    controller = Scripted(available=("custom", "claude"), registry_ids=("custom", "claude"))
    result = run(create_session_best_effort(controller, "/tmp/proj"))
    assert result.session.runtime == "custom"
    assert controller.creates[0][0] == "custom"


def test_availability_probe_used_when_controller_does_not_declare(monkeypatch):
    """The stock controller's capabilities carry no ``available`` key."""

    def probe(runtime, registry=None):
        return runtime == "codex"

    monkeypatch.setattr(besteffort, "is_available", probe)
    controller = Scripted(available=("claude", "codex"), declare_availability=False)
    result = run(create_session_best_effort(controller, "/tmp/proj"))
    assert result.session.runtime == "codex"
    assert [entry.outcome for entry in result.attempts] == ["skipped_unavailable", "started"]
    assert controller.creates == [("codex", None)]


def test_runtimes_explicit_order_and_dedupe():
    controller = Scripted(available=("codex", "claude"), registry_ids=("claude",))
    result = run(create_session_best_effort(controller, "/tmp/proj", runtimes=("codex", " codex ", "claude", "")))
    assert [entry.runtime for entry in result.attempts] == ["codex"]
    assert result.session.runtime == "codex"


def test_runtimes_empty_is_usage_error():
    controller = Scripted()
    with pytest.raises(ControlError) as raised:
        run(create_session_best_effort(controller, "/tmp/proj", runtimes=(" ",)))
    assert raised.value.code == "usage_error"


def test_prompt_must_be_nonempty():
    controller = Scripted()
    with pytest.raises(ControlError) as raised:
        run(create_session_best_effort(controller, "/tmp/proj", prompt="   "))
    assert raised.value.code == "usage_error"
    assert controller.creates == []


# -- unit: retry and fallback -------------------------------------------------------


def test_transient_create_failure_retries_with_backoff():
    controller = Scripted(available=("claude",), create_errors=[launch_failed()])
    sleeps: list[float] = []
    result = run(create_session_best_effort(controller, "/tmp/proj", sleep=recording_sleep(sleeps)))
    assert result.session.runtime == "claude"
    assert sleeps == [besteffort.DEFAULT_BACKOFF_BASE]
    assert [entry.outcome for entry in result.attempts] == ["create_failed", "started"]
    assert result.attempts[1].backoff_seconds == besteffort.DEFAULT_BACKOFF_BASE
    assert len(controller.creates) == 2


def test_deterministic_unavailable_is_not_retried():
    controller = Scripted(available=("claude",), create_errors=[unavailable("CLI not installed")])
    sleeps: list[float] = []
    with pytest.raises(ControlError) as raised:
        run(create_session_best_effort(controller, "/tmp/proj", attempts_per_runtime=3, sleep=recording_sleep(sleeps)))
    assert raised.value.code == "create_exhausted"
    assert len(controller.creates) == 1
    assert sleeps == []


def test_backoff_capped():
    controller = Scripted(available=("claude",), create_errors=[launch_failed()] * 3)
    sleeps: list[float] = []
    with pytest.raises(ControlError):
        run(
            create_session_best_effort(
                controller,
                "/tmp/proj",
                attempts_per_runtime=3,
                backoff_base=3.0,
                backoff_cap=4.0,
                sleep=recording_sleep(sleeps),
            )
        )
    assert sleeps == [3.0, 4.0]


def test_turn_failure_closes_session_and_falls_back():
    controller = Scripted(
        available=("claude", "codex"),
        turn_results=[
            RunResult(run_id="r1", state="failed", error={"code": "not_authorized", "message": "login required"})
        ],
    )
    result = run(create_session_best_effort(controller, "/tmp/proj", prompt="fix the login retry"))
    assert result.session.runtime == "codex"
    assert result.run_state == "completed"
    assert [entry.outcome for entry in result.attempts] == ["turn_failed", "started"]
    assert result.attempts[0].code == "not_authorized"
    assert result.attempts[0].cleanup == "closed"
    assert controller.closes == [result.attempts[0].session_id]
    assert controller.submits and controller.submits[0][1] == result.request_id


def test_turn_failure_is_not_retried_on_same_runtime():
    controller = Scripted(
        available=("claude",),
        turn_results=[RunResult(run_id="r1", state="failed", error={"code": "quota", "message": "spent"})],
    )
    with pytest.raises(ControlError) as raised:
        run(create_session_best_effort(controller, "/tmp/proj", prompt="brief", attempts_per_runtime=3))
    assert raised.value.code == "create_exhausted"
    assert len(controller.creates) == 1


def test_running_turn_reports_started_without_close():
    controller = Scripted(
        available=("claude",), wait_errors=[ControlError("wait_timeout", "the wait deadline passed", 6)]
    )
    result = run(create_session_best_effort(controller, "/tmp/proj", prompt="brief"))
    assert result.run_state == "running"
    assert result.final_response is None
    assert controller.closes == []
    assert controller.waits[0][1] == besteffort.DEFAULT_CONFIRM_WINDOW


def test_unknown_state_is_never_retried():
    controller = Scripted(available=("claude", "codex"), turn_results=[RunResult(run_id="r1", state="unknown")])
    result = run(create_session_best_effort(controller, "/tmp/proj", prompt="brief"))
    assert result.run_state == "unknown"
    assert len(controller.creates) == 1
    assert controller.closes == []


def test_completed_turn_returns_final_response():
    controller = Scripted(
        available=("claude",),
        turn_results=[RunResult(run_id="r1", state="completed", final_response="all good")],
    )
    result = run(create_session_best_effort(controller, "/tmp/proj", prompt="brief"))
    assert result.run_state == "completed"
    assert result.final_response == "all good"


def test_confirm_window_passthrough():
    controller = Scripted(available=("claude",))
    result = run(create_session_best_effort(controller, "/tmp/proj", prompt="brief", confirm_window=7.5))
    assert controller.waits == [(result.run_id, 7.5)]


def test_exhausted_raises_with_full_attempt_log():
    controller = Scripted(available=("claude", "codex"), create_errors=[launch_failed()] * 4)
    sleeps: list[float] = []
    with pytest.raises(ControlError) as raised:
        run(create_session_best_effort(controller, "/tmp/proj", prompt="brief", sleep=recording_sleep(sleeps)))
    assert raised.value.code == "create_exhausted"
    assert raised.value.exit_code == 1
    log = raised.value.details["attempts"]
    assert [entry["outcome"] for entry in log] == [
        "create_failed",
    ] * 4 + ["skipped_unavailable"] * (len(runtime_mod.BUILTIN_RUNTIME_IDS) - 2)
    assert raised.value.details["candidates"] == list(runtime_mod.BUILTIN_RUNTIME_IDS)
    assert sleeps == [besteffort.DEFAULT_BACKOFF_BASE, besteffort.DEFAULT_BACKOFF_BASE]


def test_all_skipped_reports_install_hint():
    controller = Scripted(available=())
    with pytest.raises(ControlError) as raised:
        run(create_session_best_effort(controller, "/tmp/proj"))
    assert raised.value.code == "create_exhausted"
    assert "installed" in raised.value.message
    assert controller.creates == []


def test_usage_error_aborts_without_fallback():
    controller = Scripted(available=("claude", "codex"), create_errors=[ControlError("usage_error", "bad cwd", 2)])
    with pytest.raises(ControlError) as raised:
        run(create_session_best_effort(controller, "/tmp/proj"))
    assert raised.value.code == "usage_error"
    assert len(controller.creates) == 1


# -- CLI handler --------------------------------------------------------------------


def test_cli_auto_mode_with_prompt(tmp_path):
    controller = Scripted(available=("claude",))
    data = cmd_session_create(cli_args(tmp_path, prompt="fix the login retry"), factory=lambda _args: controller)
    assert data["status"] == "created"
    assert data["session"]["runtime"] == "claude"
    assert data["run"]["run_id"] == "run_ses_claude_1"
    assert data["run"]["state"] == "completed"
    assert [entry["outcome"] for entry in data["attempts"]] == ["started"]
    assert any("run events" in command for command in data["next_commands"])


def test_cli_explicit_runtime_keeps_single_attempt_semantics(tmp_path):
    controller = Scripted(available=("claude",), create_errors=[launch_failed()])
    with pytest.raises(ControlError) as raised:
        cmd_session_create(cli_args(tmp_path, runtime="claude"), factory=lambda _args: controller)
    assert raised.value.code == "launch_failed"
    assert len(controller.creates) == 1  # no retry loop on the explicit path


def test_cli_explicit_runtime_without_prompt_is_shape_compatible(tmp_path):
    controller = Scripted(available=("claude",))
    data = cmd_session_create(cli_args(tmp_path, runtime="claude"), factory=lambda _args: controller)
    assert data["status"] == "created"
    assert "run" not in data
    assert "attempts" not in data
    assert controller.submits == []


def test_cli_explicit_runtime_with_prompt_submits_without_waiting(tmp_path):
    controller = Scripted(available=("claude",))
    data = cmd_session_create(cli_args(tmp_path, runtime="claude", prompt="brief"), factory=lambda _args: controller)
    assert data["run"]["state"] == "accepted"
    assert controller.waits == []
    assert controller.submits == [(data["session"]["id"], data["run"]["request_id"])]


def test_cli_dry_run_auto_never_builds_controller(tmp_path):
    def forbidden(_args):
        raise AssertionError("dry run must not construct a controller")

    data = cmd_session_create(cli_args(tmp_path, dry_run=True), factory=forbidden)
    assert data["dry_run"] is True
    assert [candidate["runtime"] for candidate in data["candidates"]] == list(runtime_mod.default_registry().ids)
    for candidate in data["candidates"]:
        assert set(candidate) == {"runtime", "available"}
    assert set(data["policy"]) == {"attempts_per_runtime", "backoff_base", "backoff_cap", "confirm_window"}
    assert data["prompt_chars"] is None


def test_cli_dry_run_auto_with_prompt_chars(tmp_path):
    data = cmd_session_create(cli_args(tmp_path, dry_run=True, prompt="fix it"), factory=lambda _args: None)
    assert data["prompt_chars"] == len("fix it")


def test_cli_dry_run_explicit_with_prompt_stays_pure(tmp_path, monkeypatch):
    plan = SimpleNamespace(argv=("claude",), cwd=str(tmp_path), to_dict=lambda: {"argv": ["claude"]})
    monkeypatch.setattr(runtime_mod, "build_create_plan", lambda *args, **kwargs: plan)
    data = cmd_session_create(
        cli_args(tmp_path, runtime="claude", prompt="brief", dry_run=True), factory=lambda _args: None
    )
    assert data["launch"] == {"argv": ["claude"]}
    assert data["prompt_chars"] == len("brief")


def test_cli_auto_exhaustion_surfaces_attempt_log(tmp_path):
    controller = Scripted(available=("claude",), create_errors=[launch_failed(), launch_failed()])
    with pytest.raises(ControlError) as raised:
        cmd_session_create(cli_args(tmp_path, prompt="brief"), factory=lambda _args: controller)
    assert raised.value.code == "create_exhausted"
    assert [entry["outcome"] for entry in raised.value.details["attempts"]] == ["create_failed"] * 2 + [
        "skipped_unavailable"
    ] * (len(runtime_mod.BUILTIN_RUNTIME_IDS) - 1)

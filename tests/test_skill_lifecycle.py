"""Skill lifecycle semantics: durable keys, timeout, interrupt, unknown, close.

Deterministic operational coverage beyond dry-runs, without terminals,
model sessions, or network:

- durable same-key convergence and changed-payload conflict through the
  public ``RunStore`` (the mechanism behind CLI ``--request-id``);
- wait-timeout continuation, interrupt request vs settlement, unknown
  settlement with no replay, and close-without-history-deletion through
  the public JSON CLI with an injected deterministic controller.

The injected controller is the shared ACP-test fake, reused read-only
(no peer-file edits): scripted ``SLOW``/``UNKNOWN`` prompts drive
timeout/interrupt/unknown paths deterministically.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
SRC = REPO / "src"
FIX = Path(__file__).parent / "fixtures" / "acp"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(FIX))

from fake_controller import FakeController  # noqa: E402  # pyrefly: ignore[missing-import]

from agent_session_control import cli as cli_mod  # noqa: E402
from agent_session_control.errors import ControlError  # noqa: E402
from agent_session_control.store import RunStore  # noqa: E402


def main_with_fake(argv: list[str], controller: FakeController, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
    code = cli_mod.main(argv, controller_factory=lambda _args: controller)
    out = capsys.readouterr().out
    assert out.strip()
    return code, json.loads(out.strip().splitlines()[-1])


def make_session(controller: FakeController, capsys: pytest.CaptureFixture, cwd: str) -> str:
    code, body = main_with_fake(
        ["session", "create", "--runtime", "fake-test", "--cwd", cwd, "--json"], controller, capsys
    )
    assert code == 0 and body["ok"] is True
    return body["data"]["session"]["id"]


def submit(
    controller: FakeController, capsys: pytest.CaptureFixture, session_id: str, prompt: str, request_id: str
) -> str:
    code, body = main_with_fake(
        ["run", "submit", "--session-id", session_id, "--prompt", prompt, "--request-id", request_id, "--json"],
        controller,
        capsys,
    )
    assert code == 0, body
    assert body["data"]["status"] == "submitted"
    assert body["data"]["run"]["request_id"] == request_id
    return body["data"]["run"]["run_id"]


def test_durable_same_key_converges_changed_payload_conflicts(tmp_path: Path) -> None:
    """The durable mechanism behind ``--request-id``: identical retries
    converge on the original run; a changed payload conflicts."""
    store = RunStore(root=tmp_path / "control", namespace="skill-test", supervisor="skill-test")
    spec = {"target": "fake:sess", "text": "fix the login retry"}
    first, created = store.create("local-user", "req-stable-1", spec, {"execution": "accepted"})
    assert created is True
    assert first["run_id"].startswith("run_")

    second, created_again = store.create("local-user", "req-stable-1", dict(spec), {"execution": "accepted"})
    assert created_again is False
    assert second["run_id"] == first["run_id"]

    with pytest.raises(ControlError) as exc:
        store.create(
            "local-user", "req-stable-1", {"target": "fake:sess", "text": "different work"}, {"execution": "a"}
        )
    assert exc.value.code == "idempotency_conflict"

    trail = store.audit_trail()
    assert any(row.get("target") == first["run_id"] for row in trail)


def test_wait_timeout_continues_work(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """A wait timeout never cancels: re-observe via events, then settle.

    ``run wait`` only blocks on settlement while ``run events`` pumps
    the controller's event stream to completion, so the honest
    observe-again path is wait-timeout, events, wait-settled.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    controller = FakeController()
    session_id = make_session(controller, capsys, str(proj))
    run_id = submit(controller, capsys, session_id, "SLOW 1 settle-late", "req-timeout-1")

    code, body = main_with_fake(["run", "wait", "--run-id", run_id, "--timeout", "0.2", "--json"], controller, capsys)
    assert code == 6
    assert body["ok"] is False
    assert body["error"]["code"] == "wait_timeout"

    code, body = main_with_fake(["run", "events", "--run-id", run_id, "--json"], controller, capsys)
    assert code == 0
    assert body["data"]["run_id"] == run_id

    code, body = main_with_fake(["run", "wait", "--run-id", run_id, "--timeout", "30", "--json"], controller, capsys)
    assert code == 0
    assert body["data"]["run"]["state"] == "completed"
    assert body["data"]["run"]["run_id"] == run_id


def test_interrupt_request_vs_confirmed_settlement(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    controller = FakeController()
    session_id = make_session(controller, capsys, str(proj))
    run_id = submit(controller, capsys, session_id, "SLOW 30 cancellable", "req-interrupt-1")

    code, body = main_with_fake(["run", "interrupt", "--run-id", run_id, "--json"], controller, capsys)
    assert code == 0
    assert body["data"]["status"] == "interrupt_requested"
    assert "confirmed" in body["data"]["note"]

    code, body = main_with_fake(["run", "wait", "--run-id", run_id, "--timeout", "30", "--json"], controller, capsys)
    assert code == 0
    assert body["data"]["run"]["state"] == "interrupted"


def test_unknown_settles_without_replay(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Unknown delivery/settlement authorizes observation, never a new key."""
    proj = tmp_path / "proj"
    proj.mkdir()
    controller = FakeController()
    session_id = make_session(controller, capsys, str(proj))
    run_id = submit(controller, capsys, session_id, "UNKNOWN ambiguous-outcome", "req-unknown-1")

    code, body = main_with_fake(["run", "events", "--run-id", run_id, "--json"], controller, capsys)
    assert code == 0
    assert body["data"]["run_id"] == run_id

    code, body = main_with_fake(["run", "wait", "--run-id", run_id, "--timeout", "30", "--json"], controller, capsys)
    assert code == 0
    assert body["data"]["run"]["state"] == "unknown"
    assert body["data"]["run"]["evidence"] == "unknown"

    code, again = main_with_fake(["run", "wait", "--run-id", run_id, "--timeout", "30", "--json"], controller, capsys)
    assert code == 0 and again["data"]["run"]["state"] == "unknown"
    assert len(controller.submitted) == 1


def test_close_releases_without_history_deletion(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    controller = FakeController()
    session_id = make_session(controller, capsys, str(proj))

    code, body = main_with_fake(["session", "close", "--session-id", session_id, "--json"], controller, capsys)
    assert code == 0
    assert body["data"]["status"] == "closed"

    code, body = main_with_fake(["session", "close", "--session-id", session_id, "--json"], controller, capsys)
    assert code == 3
    assert body["error"]["code"] == "not_found"

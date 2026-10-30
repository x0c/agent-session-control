"""Core model, prompt-block and controller-port contract tests."""

import json

import pytest

from agent_session_control.errors import ControlError
from agent_session_control.models import (
    ControlEvent,
    RunResult,
    SessionController,
    SessionHandle,
    normalize_prompt,
    prompt_text,
    resource_link,
    text_block,
)


def test_session_handle_native_identity_preserved():
    handle = SessionHandle(id="local-1", runtime="claude", cwd="/repo", native_id="abc-123", instance_id="tmux-1")
    assert handle.native_id == "abc-123"
    assert handle.native_id != handle.id
    assert SessionHandle.from_dict(handle.to_dict()) == handle
    assert json.dumps(handle.to_dict())


def test_session_handle_optional_identity_defaults():
    handle = SessionHandle(id="local-1", runtime="pi", cwd="/repo")
    assert handle.native_id is None
    assert handle.instance_id is None


def test_control_event_defaults():
    event = ControlEvent(seq=3, kind="state", session_id="s-1")
    assert event.run_id is None
    assert event.data == {}
    assert event.evidence == "unknown"
    assert event.generation == ""
    assert ControlEvent.from_dict(event.to_dict()) == event


def test_run_result_states_and_honesty():
    for state in ("completed", "failed", "interrupted", "unknown"):
        result = RunResult(run_id="run_x", state=state)
        assert result.evidence == "unknown"
        assert RunResult.from_dict(result.to_dict()) == result
    with pytest.raises(ValueError):
        RunResult(run_id="run_x", state="end_turn")


def test_run_result_unknown_never_masquerades():
    result = RunResult(run_id="run_x", state="unknown", stop_reason=None)
    assert result.stop_reason is None
    assert result.to_dict()["stop_reason"] is None


def test_prompt_blocks_baseline():
    prompt = [text_block("fix it"), resource_link("file:///repo/a.py", name="a.py")]
    normalized = normalize_prompt(prompt)
    assert normalized[0] == {"type": "text", "text": "fix it"}
    assert normalized[1] == {"type": "resource_link", "uri": "file:///repo/a.py", "title": "a.py"}
    text = prompt_text(prompt)
    assert "fix it" in text
    # Resource links stay visible; they are never silently flattened away.
    assert "file:///repo/a.py" in text
    assert "a.py" in text


def test_prompt_blocks_legacy_resource_alias():
    normalized = normalize_prompt([{"type": "resource", "uri": "file:///repo/a.py", "title": "a.py"}])
    assert normalized == [{"type": "resource_link", "uri": "file:///repo/a.py", "title": "a.py"}]
    bare = normalize_prompt([{"type": "resource", "uri": "file:///repo/a.py"}])
    assert bare == [{"type": "resource_link", "uri": "file:///repo/a.py"}]


def test_prompt_blocks_reject_embedded_data():
    for bad in (
        {"type": "resource_link", "uri": "file:///x", "data": "eA=="},
        {"type": "resource_link", "uri": "file:///x", "text": "inline"},
        {"type": "resource", "uri": "file:///x", "text": "x"},
        {"type": "resource", "uri": "file:///x", "content": [{"type": "text"}]},
    ):
        with pytest.raises(ControlError) as exc:
            normalize_prompt([bad])
        assert exc.value.code == "unsupported"


def test_prompt_blocks_reject_rich_content():
    with pytest.raises(ControlError) as exc:
        normalize_prompt([{"type": "image", "data": "..."}])
    assert exc.value.code == "unsupported"
    with pytest.raises(ControlError):
        normalize_prompt([])
    with pytest.raises(ControlError):
        normalize_prompt([{"type": "text"}])
    with pytest.raises(ControlError):
        normalize_prompt([{"type": "resource"}])


def test_controller_port_shape():
    assert issubclass(SessionController, object)

    class FakeController:
        def capabilities(self, runtime):
            return {}

        async def create(self, runtime, cwd, *, model=None, mcp_servers=(), actor="local-user"):
            raise NotImplementedError

        async def resume(self, session_id, *, cwd=None, mcp_servers=(), actor="local-user"):
            raise NotImplementedError

        async def start_turn(self, session_id, prompt, *, request_id, on_busy="queue", actor="local-user"):
            raise NotImplementedError

        def watch(self, run_id, *, after=0):
            raise NotImplementedError

        async def wait(self, run_id, *, timeout=None):
            raise NotImplementedError

        async def interrupt(self, run_id, *, actor="local-user"):
            raise NotImplementedError

        async def close_session(self, session_id, *, actor="local-user"):
            raise NotImplementedError

        async def list_sessions(self, *, cwd=None, cursor=None):
            raise NotImplementedError

        async def load_history(self, session_id):
            raise NotImplementedError

    assert isinstance(FakeController(), SessionController)

    class PartialController:
        def capabilities(self, runtime):
            return {}

    assert not isinstance(PartialController(), SessionController)

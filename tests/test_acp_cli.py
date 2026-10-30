"""JSON CLI acceptance: envelopes, exits, dry-run isolation, controller paths.

Offline/pure paths run the packaged entry in a subprocess. Controller-backed
commands run in-process with an injected deterministic ``FakeController`` so
no terminal, storage side effect or model call can occur. One packaged-entry
stdio handshake proves the production ``acp`` wiring without launching.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

FIX = Path(__file__).parent / "fixtures" / "acp"
sys.path.insert(0, str(FIX))

from fake_controller import FakeController  # noqa: E402  # pyrefly: ignore[missing-import]

from agent_session_control import cli as cli_mod  # noqa: E402

EXE = [sys.executable, "-m", "agent_session_control"]


class TClient:
    """Minimal official-SDK client: records session/update notifications."""

    def __init__(self) -> None:
        self.updates: list[tuple[str, object]] = []
        self.conn: object | None = None

    def on_connect(self, conn: object) -> None:
        self.conn = conn

    async def session_update(self, session_id: str, update: object, **kwargs: object) -> None:
        self.updates.append((session_id, update))


def child_env() -> dict:
    from acp import default_environment

    env = dict(default_environment())
    env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    src = str(Path(__file__).parent.parent / "src")
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["ASC_FAKE_DELAY"] = "0.01"
    return env


def any_client(client: TClient) -> Any:
    """Only session_update/on_connect are exercised; the SDK router resolves
    client handlers dynamically."""
    return client


def run_cli(
    *argv: str, cwd: Path | None = None, env_extra: dict | None = None, stdin_text: str | None = None
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = str(Path(os.environ.get("XDG_CONFIG_HOME_TMP", "/tmp")) / "asc-cli-test")
    env["NO_COLOR"] = "1"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [*EXE, *argv],
        capture_output=True,
        text=True,
        input=stdin_text,
        cwd=str(cwd) if cwd else None,
        env=env,
        timeout=60,
    )


def envelope(proc: subprocess.CompletedProcess) -> dict:
    assert proc.stdout, f"empty stdout; stderr={proc.stderr[-2000:]}"
    return json.loads(proc.stdout)


def test_version_envelope_offline() -> None:
    proc = run_cli("version", "--json")
    assert proc.returncode == 0
    body = envelope(proc)
    assert body["ok"] is True and body["error"] is None
    assert body["data"]["package"] == "agent-session-control"
    assert set(body) == {"ok", "data", "error", "meta"}


def test_failure_envelope_and_usage_exit() -> None:
    proc = run_cli("run", "submit", "--session-id", "x", "--json")
    assert proc.returncode == 2
    body = envelope(proc)
    assert body["ok"] is False and body["data"] is None
    assert body["error"]["code"] == "usage_error"


def test_unknown_flag_is_usage_not_silent_ignore() -> None:
    proc = run_cli("run", "wait", "--run-id", "r", "--prompt", "x", "--json")
    assert proc.returncode == 2


def test_describe_single_source_lists_everything() -> None:
    proc = run_cli("describe", "--json")
    assert proc.returncode == 0
    body = envelope(proc)
    names = [c["command"] for c in body["data"]["commands"]]
    assert names == cli_mod.COMMAND_ORDER
    for command in body["data"]["commands"]:
        assert command["risk"] in ("read", "write_local", "serve")
        assert command["params"] and command["examples"]
    bad = run_cli("describe", "--command", "nope", "--json")
    assert bad.returncode == 2


def test_doctor_offline_and_never_writes(tmp_path: Path) -> None:
    before = list(tmp_path.iterdir())
    proc = run_cli("doctor", "--json", "--root", str(tmp_path / "root"), "--namespace", "n")
    assert proc.returncode == 0
    body = envelope(proc)
    assert body["data"]["engine"] == "available"
    assert body["data"]["extras"]["acp"] == "installed"
    assert len(body["data"]["runtimes"]) == 5
    assert list(tmp_path.iterdir()) == before


def test_plan_create_pure_and_backend_gap_visible(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    proc = run_cli("plan", "create", "--runtime", "codex", "--cwd", str(proj), "--json")
    assert proc.returncode == 0
    assert envelope(proc)["data"]["launch"]["argv"][0] == "codex"
    # opencode has no verified per-launch model flag: must fail, never fall back.
    bad = run_cli("plan", "create", "--runtime", "opencode", "--cwd", str(proj), "--model", "m", "--json")
    assert bad.returncode == 2
    assert envelope(bad)["error"]["code"] == "model_selection_unsupported"
    rel = run_cli("plan", "create", "--runtime", "codex", "--cwd", "relative", "--json")
    assert rel.returncode == 2
    assert (tmp_path / "root").exists() is False


def test_dry_run_never_emits_mcp_secrets(tmp_path: Path) -> None:
    """F1: dry-run and plan output use the redacted projection only.

    Synthetic credentials must appear in neither stdout nor stderr for any
    profile, whether secrets travel on argv (claude/codex) or in the
    environment (opencode)."""
    import json as _json

    secret = "SYNTHETIC_MCP_SECRET_NOT_A_CREDENTIAL"
    proj = tmp_path / "proj"
    proj.mkdir()
    mcp = _json.dumps([{"name": "fixture", "command": "/usr/bin/true", "args": [], "env": {"TOKEN": secret}}])
    for runtime in ("claude", "codex", "opencode"):
        proc = run_cli(
            "session",
            "create",
            "--runtime",
            runtime,
            "--cwd",
            str(proj),
            "--dry-run",
            "--json",
            "--root",
            str(tmp_path / "root"),
            "--mcp-servers",
            mcp,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        assert secret not in proc.stdout, runtime
        assert secret not in proc.stderr, runtime
        body = json.loads(proc.stdout)
        assert body["ok"] is True
        launch = body["data"]["launch"]
        assert "env" not in launch or secret not in json.dumps(launch["env"])
        assert secret not in json.dumps(launch.get("argv", []))
    planned = run_cli("plan", "create", "--runtime", "codex", "--cwd", str(proj), "--json")
    assert planned.returncode == 0
    assert secret not in planned.stdout + planned.stderr


def test_dry_run_truly_read_only(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    root = tmp_path / "root"
    before = sorted(p.name for p in tmp_path.iterdir())
    one = run_cli(
        "session", "create", "--runtime", "codex", "--cwd", str(proj), "--dry-run", "--json", "--root", str(root)
    )
    assert one.returncode == 0
    planned = envelope(one)["data"]
    assert planned["dry_run"] is True and planned["status"] == "planned"
    assert planned["session"]["id"] is None
    assert planned["session"]["runtime"] == "codex"
    assert planned["session"]["cwd"] == str(proj.resolve())
    assert planned["launch"]["argv"][0] == "codex"
    assert planned["target"] == {"runtime": "codex", "cwd": str(proj.resolve())}
    two = run_cli(
        "run", "submit", "--session-id", "ses_x", "--prompt", "hi", "--dry-run", "--json", "--root", str(root)
    )
    assert two.returncode == 0
    assert envelope(two)["data"]["status"] == "planned"
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert not root.exists()


def test_prompt_file_stdin_and_validation(tmp_path: Path) -> None:
    brief = tmp_path / "brief.txt"
    brief.write_text("fix the login retry")
    proc = run_cli("run", "submit", "--session-id", "ses_x", "--prompt-file", str(brief), "--dry-run", "--json")
    assert proc.returncode == 0
    assert envelope(proc)["data"]["run"]["prompt_chars"] == len("fix the login retry")
    both = run_cli("run", "submit", "--session-id", "ses_x", "--prompt", "a", "--prompt-file", str(brief), "--json")
    assert both.returncode == 2
    piped = run_cli(
        "run", "submit", "--session-id", "ses_x", "--prompt-file", "-", "--dry-run", "--json", stdin_text="piped brief"
    )
    assert piped.returncode == 0
    assert envelope(piped)["data"]["run"]["prompt_chars"] == len("piped brief")


def test_stdout_stderr_separation() -> None:
    proc = subprocess.run([*EXE, "version", "--json"], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0
    json.loads(proc.stdout)  # parses with stderr discarded
    assert proc.stderr == ""


def test_human_readable_default_without_json() -> None:
    import pty

    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = "/tmp/asc-cli-test"
    env.pop("NO_COLOR", None)
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen([*EXE, "version"], stdout=slave, stderr=subprocess.DEVNULL, env=env)
        os.close(slave)
        chunks: list[bytes] = []
        while True:
            try:
                chunk = os.read(master, 1024)
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        assert proc.wait(timeout=60) == 0
    finally:
        os.close(master)
    text = b"".join(chunks).decode().replace("\r", "")
    assert '"ok"' not in text
    assert "agent-session-control" in text


# -- in-process controller paths (injected fake; nothing launched) ---------------


def main_with_fake(argv: list[str], controller: FakeController, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
    code = cli_mod.main(argv, controller_factory=lambda _args: controller)
    out = capsys.readouterr().out
    assert out.strip()
    return code, json.loads(out.strip().splitlines()[-1])


def test_session_and_run_lifecycle_with_fake(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    controller = FakeController()
    code, body = main_with_fake(
        [
            "session",
            "create",
            "--runtime",
            "fake-test",
            "--cwd",
            str(proj),
            "--json",
            "--root",
            str(tmp_path),
            "--namespace",
            "t",
        ],
        controller,
        capsys,
    )
    assert code == 0 and body["ok"] is True
    session_id = body["data"]["session"]["id"]
    assert session_id.startswith("ses_")

    code, body = main_with_fake(
        ["run", "submit", "--session-id", session_id, "--prompt", "do it", "--request-id", "k1", "--json"],
        controller,
        capsys,
    )
    assert code == 0
    run_id = body["data"]["run"]["run_id"]
    assert body["data"]["next_commands"]

    code, body = main_with_fake(["run", "events", "--run-id", run_id, "--json"], controller, capsys)
    assert code == 0 and body["data"]["events"]
    assert body["data"]["next_cursor"] >= 0

    code, body = main_with_fake(["run", "wait", "--run-id", run_id, "--timeout", "30", "--json"], controller, capsys)
    assert code == 0
    assert body["data"]["run"]["state"] == "completed"
    assert "review the work" in body["data"]["note"]

    code, body = main_with_fake(["run", "interrupt", "--run-id", run_id, "--json"], controller, capsys)
    assert code == 0 and body["data"]["status"] == "interrupt_requested"

    code, body = main_with_fake(["session", "list", "--json"], controller, capsys)
    assert code == 0 and body["data"]["returned"] == 1

    code, body = main_with_fake(["session", "close", "--session-id", session_id, "--json"], controller, capsys)
    assert code == 0 and body["data"]["status"] == "closed"


def test_run_wait_timeout_is_exit_6_and_work_continues(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    controller = FakeController()
    code, body = main_with_fake(
        ["session", "create", "--runtime", "fake-test", "--cwd", str(proj), "--json"], controller, capsys
    )
    session_id = body["data"]["session"]["id"]
    code, body = main_with_fake(
        ["run", "submit", "--session-id", session_id, "--prompt", "SLOW 25 x", "--json"], controller, capsys
    )
    run_id = body["data"]["run"]["run_id"]
    code, body = main_with_fake(["run", "wait", "--run-id", run_id, "--timeout", "0.2", "--json"], controller, capsys)
    assert code == 6
    assert body["ok"] is False and body["error"]["code"] == "wait_timeout"


def test_follow_jsonl_event_protocol(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    controller = FakeController()
    code, body = main_with_fake(
        ["session", "create", "--runtime", "fake-test", "--cwd", str(proj), "--json"], controller, capsys
    )
    session_id = body["data"]["session"]["id"]
    code, body = main_with_fake(
        ["run", "submit", "--session-id", session_id, "--prompt", "hello x", "--json"], controller, capsys
    )
    run_id = body["data"]["run"]["run_id"]
    capsys.readouterr()
    code = cli_mod.main(
        ["run", "events", "--run-id", run_id, "--follow", "--timeout", "30"],
        controller_factory=lambda _args: controller,
    )
    assert code == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines[0]["type"] == "start" and lines[0]["run_id"] == run_id
    assert any(line["type"] == "record" for line in lines)
    assert lines[-1]["type"] == "end" and lines[-1]["reason"] == "settled"


def test_submit_unknown_session_is_not_found_exit_3(capsys: pytest.CaptureFixture) -> None:
    controller = FakeController()
    code, body = main_with_fake(
        ["run", "submit", "--session-id", "ses_missing", "--prompt", "hi", "--json"], controller, capsys
    )
    assert code == 3
    assert body["error"]["code"] == "not_found"


def test_packaged_acp_entry_handshake_only(tmp_path: Path) -> None:
    """Production ``acp`` entry over real engine: initialize + list only.

    Never creates a session (that would host a terminal). Proves the packaged
    wiring, protocol-only stdout and offline capability truthfulness.
    """

    async def go() -> None:
        from acp import PROTOCOL_VERSION, RequestError, spawn_agent_process

        proj = tmp_path / "proj"
        proj.mkdir()
        client = TClient()
        env = child_env()
        async with spawn_agent_process(
            any_client(client),
            sys.executable,
            "-m",
            "agent_session_control",
            "acp",
            "--runtime",
            "cursor",
            "--root",
            str(tmp_path / "root"),
            "--namespace",
            "t",
            env=env,
            use_unstable_protocol=True,
        ) as (conn, process):
            resp = await asyncio.wait_for(conn.initialize(PROTOCOL_VERSION), 30)
            assert resp.protocol_version == 1
            assert resp.agent_capabilities is not None
            assert resp.agent_capabilities.load_session is True
            page = await asyncio.wait_for(conn.list_sessions(), 30)
            assert page.sessions == []
            idle = await asyncio.wait_for(conn.close_session("ses_missing"), 30)
            assert idle is not None
            # Prompting an unknown session never auto-resumes: create/load first.
            with pytest.raises(RequestError) as exc:
                await asyncio.wait_for(conn.prompt("ses_missing", [{"type": "text", "text": "hi"}]), 30)
            assert exc.value.code == -32002
            # Reads never create storage.
            assert not (tmp_path / "root").exists()
            # The cursor profile has no per-session MCP support: honest
            # refusal that leaks no server values, before anything launches.
            with pytest.raises(RequestError) as exc:
                await asyncio.wait_for(
                    conn.new_session(
                        str(proj),
                        mcp_servers=[
                            {
                                "name": "fs",
                                "command": "/usr/bin/mcp",
                                "args": [],
                                "env": [{"name": "TOKEN", "value": "sekrit"}],
                            }
                        ],
                    ),
                    30,
                )
            assert exc.value.code == -32602
            assert "sekrit" not in json.dumps(exc.value.data or {})
            # The refused create records nothing hosted: any registry row left
            # behind must have no pane. (Engine internals; guards "refuse
            # before launching", not the registry format itself.)
            import sqlite3 as _sqlite3

            for db in (tmp_path / "root").rglob("*.sqlite3"):
                conn = _sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                try:
                    panes = conn.execute("SELECT pane, status FROM sessions").fetchall()
                finally:
                    conn.close()
                assert all(pane is None for pane, _ in panes), panes
        assert process.returncode == 0

    asyncio.run(go())

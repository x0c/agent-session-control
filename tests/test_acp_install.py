"""Clean installed-wheel validation: no repo checkout, no PYTHONPATH.

Builds the wheel, installs it with only its declared surface plus the
optional ACP SDK into a throwaway venv, and drives the packaged console
entry: read-only commands, dry runs, real store error paths and one stdio
handshake against the production engine. Nothing here launches a terminal,
spends quota or touches user sessions.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent


def run(
    *argv: str, env: dict | None = None, cwd: Path | None = None, stdin_text: str | None = None, timeout: int = 120
) -> subprocess.CompletedProcess:
    merged = dict(os.environ)
    merged.pop("PYTHONPATH", None)  # the wheel must be self-sufficient
    merged["XDG_CONFIG_HOME"] = str(Path("/tmp") / "asc-install-test")
    if env:
        merged.update(env)
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        input=stdin_text,
        cwd=str(cwd) if cwd else None,
        env=merged,
        timeout=timeout,
    )


@pytest.fixture(scope="module")
def wheel_venv() -> Path:
    """Build once per module. Preparation failures fail loudly: a skipped
    build/install would prove nothing about the installed artifact."""
    if shutil.which("uv") is None:
        pytest.fail("uv is required for the isolated install check")
    work = Path(os.environ.get("ASC_INSTALL_TMP", "/tmp")) / "asc-install-wheel"
    if work.exists():
        shutil.rmtree(work)
    (work / "dist").mkdir(parents=True)
    venv = work / "iw"
    build = run(sys.executable, "-m", "build", "--wheel", "--outdir", str(work / "dist"), cwd=REPO, timeout=300)
    assert build.returncode == 0, f"wheel build failed:\n{build.stderr[-2000:]}"
    wheels = sorted((work / "dist").glob("agent_session_control-*.whl"))
    assert wheels, "no wheel produced"
    venv_proc = run("uv", "venv", str(venv), timeout=120)
    assert venv_proc.returncode == 0, f"uv venv failed:\n{venv_proc.stderr[-2000:]}"
    install = run(
        "uv",
        "pip",
        "install",
        "--python",
        str(venv / "bin" / "python"),
        str(wheels[0]),
        "agent-client-protocol==0.12.1",
        timeout=300,
    )
    assert install.returncode == 0, f"isolated install failed:\n{install.stderr[-2000:]}"
    return venv


def entry(venv: Path) -> list[str]:
    exe = venv / "bin" / "agent-session-control"
    assert exe.exists()
    return [str(exe)]


def test_installed_version_describe_doctor(wheel_venv: Path, tmp_path: Path) -> None:
    env: dict = {}  # run() strips PYTHONPATH: the wheel must be self-sufficient
    proc = run(*entry(wheel_venv), "version", "--json", env=env, cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout)
    assert body["ok"] is True and body["data"]["package"] == "agent-session-control"
    describe = run(*entry(wheel_venv), "describe", "--json", env=env, cwd=tmp_path)
    assert describe.returncode == 0
    assert [c["command"] for c in json.loads(describe.stdout)["data"]["commands"]]
    doctor = run(*entry(wheel_venv), "doctor", "--json", env=env, cwd=tmp_path)
    assert doctor.returncode == 0
    assert json.loads(doctor.stdout)["data"]["engine"] == "available"


def test_installed_dry_runs_create_no_state(wheel_venv: Path, tmp_path: Path) -> None:
    env: dict = {}
    proj = tmp_path / "proj"
    proj.mkdir()
    before = sorted(p.name for p in tmp_path.iterdir())
    one = run(
        *entry(wheel_venv), "plan", "create", "--runtime", "codex", "--cwd", str(proj), "--json", env=env, cwd=tmp_path
    )
    assert one.returncode == 0, one.stderr
    two = run(
        *entry(wheel_venv),
        "session",
        "create",
        "--runtime",
        "codex",
        "--cwd",
        str(proj),
        "--dry-run",
        "--json",
        "--root",
        str(tmp_path / "root"),
        env=env,
    )
    assert two.returncode == 0, two.stderr
    assert envelope(two)["data"]["status"] == "planned"
    three = run(
        *entry(wheel_venv),
        "run",
        "submit",
        "--session-id",
        "ses_x",
        "--prompt",
        "hi",
        "--dry-run",
        "--json",
        "--root",
        str(tmp_path / "root"),
        env=env,
    )
    assert three.returncode == 0, three.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def envelope(proc: subprocess.CompletedProcess) -> dict:
    assert proc.stdout, f"empty stdout; stderr={proc.stderr[-2000:]}"
    return json.loads(proc.stdout)


def test_installed_real_store_error_paths(wheel_venv: Path, tmp_path: Path) -> None:
    env: dict = {}
    root = tmp_path / "root"
    listed = run(*entry(wheel_venv), "session", "list", "--json", "--root", str(root), env=env, cwd=tmp_path)
    assert listed.returncode == 0, listed.stderr
    assert envelope(listed)["data"]["sessions"] == []
    submit = run(
        *entry(wheel_venv),
        "run",
        "submit",
        "--session-id",
        "ses_missing",
        "--prompt",
        "hi",
        "--json",
        "--root",
        str(root),
        env=env,
        cwd=tmp_path,
    )
    assert submit.returncode == 3  # real store not_found through the packaged entry
    assert envelope(submit)["error"]["code"] == "not_found"
    waited = run(
        *entry(wheel_venv),
        "run",
        "wait",
        "--run-id",
        "run_missing",
        "--timeout",
        "1",
        "--json",
        "--root",
        str(root),
        env=env,
        cwd=tmp_path,
    )
    assert waited.returncode == 3
    bad = run(*entry(wheel_venv), "run", "wait", "--run-id", "x", "--prompt", "y", "--json", env=env, cwd=tmp_path)
    assert bad.returncode == 2  # unknown flags are usage errors, never ignored


def test_installed_stdio_handshake_production_engine(wheel_venv: Path, tmp_path: Path) -> None:
    """Packaged ``acp`` entry from the wheel, driven by the official client."""

    async def go() -> None:
        from acp import PROTOCOL_VERSION, RequestError, spawn_agent_process
        from test_acp_agent import TClient, any_client  # noqa: PLC0415  # pyrefly: ignore[missing-import]

        client = TClient()
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        async with spawn_agent_process(
            any_client(client),
            *entry(wheel_venv),
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
            resp = await asyncio.wait_for(conn.initialize(PROTOCOL_VERSION), 60)
            assert resp.protocol_version == 1
            assert resp.agent_capabilities is not None
            assert resp.agent_capabilities.load_session is True
            page = await asyncio.wait_for(conn.list_sessions(), 60)
            assert page.sessions == []
            with pytest.raises(RequestError) as exc:
                await asyncio.wait_for(
                    conn.new_session(
                        "/tmp/asc-proj",
                        # Complete SDK entries: the official SDK
                        # silently drops incomplete ones (gap G7).
                        mcp_servers=[
                            {
                                "name": "fs",
                                "command": "/usr/bin/mcp",
                                "args": [],
                                "env": [{"name": "TOKEN", "value": "sekrit"}],
                            }
                        ],
                    ),
                    60,
                )
            # cursor has no per-session MCP support: honest refusal, no hosting.
            assert exc.value.code == -32602
            assert "sekrit" not in json.dumps(exc.value.data or {})
        assert process.returncode == 0

    asyncio.run(go())

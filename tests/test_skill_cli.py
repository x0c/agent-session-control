"""Skill CLI behavior against the deterministic checkout source.

Every subprocess runs with ``PYTHONPATH`` set to exactly this
checkout's ``src/`` so results describe the in-flight source, never a
stale installed copy. The intended package version is read from the
project metadata (``pyproject.toml``), never hardcoded. Isolated
``--root`` fixtures keep state off the real store. No model session,
terminal hosting, or network use occurs here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
SRC = REPO / "src"
sys.path.insert(0, str(SRC))
SKILL_FIX = Path(__file__).parent / "fixtures" / "skill"
SYNTHETIC_SECRET = "SYNTHETIC_SKILL_SECRET_NOT_A_CREDENTIAL"


def intended_version() -> str:
    for line in (REPO / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("version ="):
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    raise AssertionError("version not found in pyproject.toml")


def src_env(tmp_path: Path) -> dict:
    """Deterministic source environment: this checkout's src only."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env["NO_COLOR"] = "1"
    env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")
    return env


def run_src(*argv: str, tmp_path: Path, stdin_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "agent_session_control", *argv],
        capture_output=True,
        text=True,
        input=stdin_text,
        cwd=str(REPO),
        env=src_env(tmp_path),
        timeout=60,
    )


def body(proc: subprocess.CompletedProcess) -> dict:
    assert proc.stdout, f"empty stdout; stderr={proc.stderr[-2000:]}"
    return json.loads(proc.stdout)


def test_source_identity_matches_checkout(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", "import agent_session_control as a; print(a.__file__); print(a.__version__)"],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        env=src_env(tmp_path),
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    path, _, version = proc.stdout.strip().partition("\n")
    assert path.startswith(str(SRC)), path
    assert version.strip() == intended_version()


def test_version_envelope_and_readback(tmp_path: Path) -> None:
    proc = run_src("version", "--json", tmp_path=tmp_path)
    assert proc.returncode == 0
    data = body(proc)
    assert set(data) == {"ok", "data", "error", "meta"}
    assert data["ok"] is True and data["error"] is None
    assert data["data"]["package"] == "agent-session-control"
    assert data["data"]["version"] == intended_version()
    assert data["data"]["python"].startswith("3.")
    assert data["meta"]["package"] == "agent-session-control"


def test_describe_matches_live_parser(tmp_path: Path) -> None:
    from agent_session_control import cli as cli_mod

    proc = run_src("describe", "--json", tmp_path=tmp_path)
    assert proc.returncode == 0
    names = [c["command"] for c in body(proc)["data"]["commands"]]
    assert names == cli_mod.COMMAND_ORDER
    for command in body(proc)["data"]["commands"]:
        assert command["risk"] in ("read", "write_local", "serve")
        assert command["summary"] and command["params"] and command["examples"]
    one = run_src("describe", "--command", "run submit", "--json", tmp_path=tmp_path)
    assert one.returncode == 0
    assert body(one)["data"]["commands"]["command"] == "run submit"
    bad = run_src("describe", "--command", "nope", "--json", tmp_path=tmp_path)
    assert bad.returncode == 2
    assert body(bad)["error"]["code"] == "usage_error"


def test_plan_create_pure_and_refusals(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    proc = run_src("plan", "create", "--runtime", "codex", "--cwd", str(proj), "--json", tmp_path=tmp_path)
    assert proc.returncode == 0
    data = body(proc)["data"]
    assert data["dry_run"] is True and data["status"] == "planned"
    assert data["launch"]["argv"][0] == "codex"
    assert (tmp_path / "root").exists() is False

    rel = run_src("plan", "create", "--runtime", "codex", "--cwd", "relative", "--json", tmp_path=tmp_path)
    assert rel.returncode == 2
    assert body(rel)["error"]["code"] == "usage_error"

    bogus = run_src("plan", "create", "--runtime", "bogus", "--cwd", str(proj), "--json", tmp_path=tmp_path)
    assert bogus.returncode == 2
    err = body(bogus)["error"]
    assert err["code"] == "unsupported"
    assert "claude" in err["hint"]

    model = run_src(
        "plan", "create", "--runtime", "opencode", "--cwd", str(proj), "--model", "m", "--json", tmp_path=tmp_path
    )
    assert model.returncode == 0
    assert body(model)["data"]["launch"]["argv"][-2:] == ["--model", "m"]


def test_session_create_dryrun_no_state(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    root = tmp_path / "root"
    mcp = (SKILL_FIX / "mcp-no-secret.json").read_text(encoding="utf-8")
    proc = run_src(
        "session",
        "create",
        "--runtime",
        "codex",
        "--cwd",
        str(proj),
        "--dry-run",
        "--json",
        "--root",
        str(root),
        "--mcp-servers",
        mcp,
        tmp_path=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    data = body(proc)["data"]
    assert data["dry_run"] is True and data["status"] == "planned"
    assert data["session"]["id"] is None
    assert data["session"]["runtime"] == "codex"
    assert data["launch"]["argv"][0] == "codex"
    assert not root.exists()

    refused = run_src(
        "session",
        "create",
        "--runtime",
        "cursor",
        "--cwd",
        str(proj),
        "--dry-run",
        "--json",
        "--root",
        str(root),
        "--mcp-servers",
        mcp,
        tmp_path=tmp_path,
    )
    assert refused.returncode == 2
    assert body(refused)["error"]["code"] == "unsupported"


def test_mcp_servers_file_channel_real(tmp_path: Path) -> None:
    """A's safe file channel exercised for real: file, stdin, conflict."""
    probe = run_src("describe", "--command", "session create", "--json", tmp_path=tmp_path)
    assert probe.returncode == 0
    flags = [flag for p in body(probe)["data"]["commands"]["params"] for flag in p["flags"]]
    assert "--mcp-servers-file" in flags
    proj = tmp_path / "proj"
    proj.mkdir()
    root = tmp_path / "root"
    mcp_file = SKILL_FIX / "mcp-no-secret.json"
    mcp_text = mcp_file.read_text(encoding="utf-8")

    via_file = run_src(
        "session",
        "create",
        "--runtime",
        "codex",
        "--cwd",
        str(proj),
        "--dry-run",
        "--json",
        "--root",
        str(root),
        "--mcp-servers-file",
        str(mcp_file),
        tmp_path=tmp_path,
    )
    assert via_file.returncode == 0, via_file.stderr[-2000:]
    assert body(via_file)["data"]["status"] == "planned"
    assert not root.exists()

    via_stdin = run_src(
        "session",
        "create",
        "--runtime",
        "codex",
        "--cwd",
        str(proj),
        "--dry-run",
        "--json",
        "--root",
        str(root),
        "--mcp-servers-file",
        "-",
        tmp_path=tmp_path,
        stdin_text=mcp_text,
    )
    assert via_stdin.returncode == 0, via_stdin.stderr[-2000:]
    assert body(via_stdin)["data"]["status"] == "planned"
    assert not root.exists()

    conflict = run_src(
        "session",
        "create",
        "--runtime",
        "codex",
        "--cwd",
        str(proj),
        "--dry-run",
        "--json",
        "--root",
        str(root),
        "--mcp-servers",
        mcp_text,
        "--mcp-servers-file",
        str(mcp_file),
        tmp_path=tmp_path,
    )
    assert conflict.returncode == 2
    assert body(conflict)["error"]["code"] == "usage_error"

    missing = run_src(
        "session",
        "create",
        "--runtime",
        "codex",
        "--cwd",
        str(proj),
        "--dry-run",
        "--json",
        "--root",
        str(root),
        "--mcp-servers-file",
        str(tmp_path / "absent.json"),
        tmp_path=tmp_path,
    )
    assert missing.returncode == 2
    assert body(missing)["error"]["code"] == "usage_error"
    assert not root.exists()


def test_mcp_secret_redaction_regression(tmp_path: Path) -> None:
    """Explicit redaction regression: the only inline-secret use in tests.

    Synthetic credentials must appear in neither stdout nor stderr for
    any secret-carrying channel, and the dry-run projection must stay
    redacted. All other operational tests use the non-secret fixture.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    root = tmp_path / "root"
    secret_entry = {"name": "fx", "command": "/usr/bin/true", "args": [], "env": {"TOKEN": SYNTHETIC_SECRET}}
    secret_mcp = json.dumps([secret_entry])
    for runtime in ("claude", "codex", "opencode"):
        red = run_src(
            "session",
            "create",
            "--runtime",
            runtime,
            "--cwd",
            str(proj),
            "--dry-run",
            "--json",
            "--root",
            str(root),
            "--mcp-servers",
            secret_mcp,
            tmp_path=tmp_path,
        )
        assert red.returncode == 0, red.stderr[-2000:]
        assert SYNTHETIC_SECRET not in red.stdout, runtime
        assert SYNTHETIC_SECRET not in red.stderr, runtime
    secret_file = tmp_path / "mcp-secret.json"
    secret_file.write_text(secret_mcp, encoding="utf-8")
    via_file = run_src(
        "session",
        "create",
        "--runtime",
        "codex",
        "--cwd",
        str(proj),
        "--dry-run",
        "--json",
        "--root",
        str(root),
        "--mcp-servers-file",
        str(secret_file),
        tmp_path=tmp_path,
    )
    assert via_file.returncode == 0, via_file.stderr[-2000:]
    assert SYNTHETIC_SECRET not in via_file.stdout
    assert SYNTHETIC_SECRET not in via_file.stderr
    via_stdin = run_src(
        "session",
        "create",
        "--runtime",
        "codex",
        "--cwd",
        str(proj),
        "--dry-run",
        "--json",
        "--root",
        str(root),
        "--mcp-servers-file",
        "-",
        tmp_path=tmp_path,
        stdin_text=secret_mcp,
    )
    assert via_stdin.returncode == 0, via_stdin.stderr[-2000:]
    assert SYNTHETIC_SECRET not in via_stdin.stdout
    assert SYNTHETIC_SECRET not in via_stdin.stderr
    assert not root.exists()


def test_mcp_file_preparation_without_literals(tmp_path: Path, monkeypatch) -> None:
    """Input-preparation regression for the native incident: the value
    travels environment → file → CLI with no literal in any argv or
    output; only keys are reported. Identical handling to the
    allowlisted fixture value."""
    monkeypatch.setenv("ASC_SKILL_TEST_TOKEN", SYNTHETIC_SECRET)
    proj = tmp_path / "proj"
    proj.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    resolved = os.environ["ASC_SKILL_TEST_TOKEN"]
    secret_file = root / "mcp.json"
    secret_file.write_text(
        json.dumps([{"name": "fx", "command": "/usr/bin/true", "args": [], "env": {"TOKEN": resolved}}]),
        encoding="utf-8",
    )
    argv = [
        "session",
        "create",
        "--runtime",
        "codex",
        "--cwd",
        str(proj),
        "--dry-run",
        "--json",
        "--root",
        str(root),
        "--mcp-servers-file",
        str(secret_file),
    ]
    assert all(SYNTHETIC_SECRET not in part for part in argv)
    proc = run_src(*argv, tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert SYNTHETIC_SECRET not in proc.stdout
    assert SYNTHETIC_SECRET not in proc.stderr
    data = body(proc)["data"]
    assert data["launch"]["env_keys"] == ["TOKEN"]
    assert SYNTHETIC_SECRET not in json.dumps(data["launch"]["argv"])


def test_run_submit_dryrun_key_and_prompt_file(tmp_path: Path) -> None:
    root = tmp_path / "root"
    brief = SKILL_FIX / "brief.txt"
    expected_chars = len(brief.read_text(encoding="utf-8"))
    proc = run_src(
        "run",
        "submit",
        "--session-id",
        "ses_abc",
        "--prompt-file",
        str(brief),
        "--request-id",
        "req-stable-001",
        "--dry-run",
        "--json",
        "--root",
        str(root),
        tmp_path=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    run = body(proc)["data"]["run"]
    assert run["session_id"] == "ses_abc"
    assert run["request_id"] == "req-stable-001"
    assert run["prompt_chars"] == expected_chars
    assert run["run_id"] is None
    assert not root.exists()

    piped = run_src(
        "run",
        "submit",
        "--session-id",
        "ses_abc",
        "--prompt-file",
        "-",
        "--request-id",
        "req-stable-002",
        "--dry-run",
        "--json",
        "--root",
        str(root),
        tmp_path=tmp_path,
        stdin_text="piped brief",
    )
    assert piped.returncode == 0
    assert body(piped)["data"]["run"]["prompt_chars"] == len("piped brief")
    assert not root.exists()

    both = run_src(
        "run",
        "submit",
        "--session-id",
        "ses_abc",
        "--prompt",
        "a",
        "--prompt-file",
        str(brief),
        "--json",
        "--root",
        str(root),
        tmp_path=tmp_path,
    )
    assert both.returncode == 2
    assert body(both)["error"]["code"] == "usage_error"

    bad_key = run_src(
        "run",
        "submit",
        "--session-id",
        "ses_abc",
        "--prompt",
        "hi",
        "--request-id",
        "bad key",
        "--dry-run",
        "--json",
        "--root",
        str(root),
        tmp_path=tmp_path,
    )
    assert bad_key.returncode == 2
    assert "request-id" in body(bad_key)["error"]["message"]


def test_failure_envelope_and_stdout_separation(tmp_path: Path) -> None:
    proc = run_src("run", "submit", "--session-id", "x", "--json", "--root", str(tmp_path / "r"), tmp_path=tmp_path)
    assert proc.returncode == 2
    data = body(proc)
    assert data["ok"] is False and data["data"] is None
    assert data["error"]["code"] == "usage_error"

    direct = subprocess.run(
        [sys.executable, "-m", "agent_session_control", "version", "--json"],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        env=src_env(tmp_path),
        timeout=60,
    )
    assert direct.returncode == 0
    json.loads(direct.stdout)
    assert direct.stderr == ""


def test_doctor_readonly(tmp_path: Path) -> None:
    before = sorted(p.name for p in tmp_path.iterdir())
    proc = run_src("doctor", "--json", "--root", str(tmp_path / "root"), "--namespace", "n", tmp_path=tmp_path)
    assert proc.returncode == 0
    data = body(proc)["data"]
    assert data["engine"] == "available"
    assert len(data["runtimes"]) == 6
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_acp_baseline_refusal_is_stderr_not_protocol(tmp_path: Path) -> None:
    """Honest serving restriction: cursor/pi/antigravity fail before the ACP connection.

    SDK/CLI control covers six runtimes; ACP serving requires the stdio
    MCP baseline (claude/codex/opencode). A refused serve entry keeps
    stdout protocol-clean and reports on stderr with exit 2.
    """
    proc = run_src(
        "acp", "--runtime", "cursor", "--root", str(tmp_path / "root"), "--namespace", "t", tmp_path=tmp_path
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "cannot be served over ACP" in proc.stderr
    assert "hint:" in proc.stderr


def test_argparse_usage_error_prints_envelope(tmp_path: Path) -> None:
    """P0 契约：用法错误 stderr 保留 argparse usage、stdout 补 usage_error 信封。"""
    proc = run_src("version", "--bogus", tmp_path=tmp_path)
    assert proc.returncode == 2
    assert "usage:" in proc.stderr
    data = body(proc)
    assert data["ok"] is False and data["data"] is None
    assert data["error"]["code"] == "usage_error"
    assert data["error"]["hint"]
    joined = " ".join(data["error"]["next_commands"])
    assert "describe" in joined and "--json" in joined


def test_argparse_usage_error_targets_describe_at_the_command(tmp_path: Path) -> None:
    """信封的 next_commands 指向出错命令自己的 describe 条目。"""
    proc = run_src("run", "wait", "--run-id", "r", "--timeout", "abc", tmp_path=tmp_path)
    assert proc.returncode == 2
    data = body(proc)
    assert data["error"]["code"] == "usage_error"
    assert "run wait" in data["error"]["message"]
    joined = " ".join(data["error"]["next_commands"])
    assert 'describe --command "run wait"' in joined


def test_acp_argparse_error_keeps_stdout_empty(tmp_path: Path) -> None:
    """ACP 入口 stdout 归协议独占：argparse 用法错误只走 stderr。"""
    proc = run_src("acp", tmp_path=tmp_path)
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "usage:" in proc.stderr


def test_not_found_envelope_carries_recovery(tmp_path: Path) -> None:
    """P1 错误出路：not_found 带同 scope 的 session list 真实命令。"""
    root = tmp_path / "root"
    proc = run_src(
        "session",
        "resume",
        "--session-id",
        "ses_missing",
        "--root",
        str(root),
        "--namespace",
        "demo",
        "--json",
        tmp_path=tmp_path,
    )
    assert proc.returncode == 3
    err = body(proc)["error"]
    assert err["code"] == "not_found"
    assert err["hint"]
    joined = " ".join(err["next_commands"])
    assert "session list" in joined and str(root) in joined and "--namespace demo" in joined and "--json" in joined


def test_unknown_run_not_found_explains_no_listing(tmp_path: Path) -> None:
    """未知 run id：hint 明说没有 run 列举命令（说清不可建议，而非留空）。"""
    proc = run_src("run", "wait", "--run-id", "run_missing", "--root", str(tmp_path / "r"), "--json", tmp_path=tmp_path)
    assert proc.returncode == 3
    err = body(proc)["error"]
    assert err["code"] == "not_found"
    assert "no run listing" in (err["hint"] or "")


def test_wait_timeout_error_carries_events_recovery() -> None:
    """wait_timeout 的 next_commands 指向同一 run 的 run events（单测装饰逻辑）。"""
    from types import SimpleNamespace

    from agent_session_control.cli import _decorate_error
    from agent_session_control.errors import ControlError

    args = SimpleNamespace(command_name="run wait", run_id="run_x", root="/tmp/asc-demo", namespace="demo")
    exc = _decorate_error(ControlError("wait_timeout", "the wait deadline passed", 6), args)
    joined = " ".join(exc.next_commands)
    assert "run events" in joined and "run_x" in joined and "/tmp/asc-demo" in joined


def test_help_mirrors_risk_and_examples(tmp_path: Path) -> None:
    """P1 自解释：--help 镜像风险等级与可复制示例。"""
    proc = run_src("session", "create", "--help", tmp_path=tmp_path)
    assert proc.returncode == 0
    assert "risk: write_local" in proc.stdout
    assert "examples:" in proc.stdout
    assert "session create --runtime pi --cwd /tmp/proj" in proc.stdout
    leaf = run_src("--help", tmp_path=tmp_path)
    assert "Print package and interpreter versions" in leaf.stdout


def test_dry_run_next_commands_replay_the_real_call(tmp_path: Path) -> None:
    """dry-run 形状对齐：next_commands 复述真实调用（去掉 --dry-run）。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    root = tmp_path / "root"
    proc = run_src(
        "session",
        "create",
        "--runtime",
        "codex",
        "--cwd",
        str(proj),
        "--prompt",
        "do it",
        "--dry-run",
        "--root",
        str(root),
        "--json",
        tmp_path=tmp_path,
    )
    assert proc.returncode == 0
    joined = " ".join(body(proc)["data"]["next_commands"])
    assert "--runtime codex" in joined and "--cwd" in joined and str(proj) in joined
    assert "--prompt do it" in joined and str(root) in joined
    assert "--dry-run" not in joined


def test_dry_run_stdin_input_suggests_nothing(tmp_path: Path) -> None:
    """stdin 管道输入无法逐字回放：next_commands 为空而不是丢参数的建议。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    proc = run_src(
        "session",
        "create",
        "--runtime",
        "codex",
        "--cwd",
        str(proj),
        "--prompt-file",
        "-",
        "--dry-run",
        "--json",
        stdin_text="piped brief",
        tmp_path=tmp_path,
    )
    assert proc.returncode == 0
    assert body(proc)["data"]["next_commands"] == []

    submit = run_src(
        "run",
        "submit",
        "--session-id",
        "ses_x",
        "--prompt-file",
        "-",
        "--request-id",
        "k-1",
        "--dry-run",
        "--json",
        stdin_text="piped brief",
        tmp_path=tmp_path,
    )
    assert submit.returncode == 0
    assert body(submit)["data"]["next_commands"] == []


def test_run_submit_dry_run_reuses_generated_request_id(tmp_path: Path) -> None:
    """dry-run 生成的幂等键进入 next_commands：真调用复用同一个键。"""
    proc = run_src(
        "run",
        "submit",
        "--session-id",
        "ses_x",
        "--prompt",
        "do it",
        "--dry-run",
        "--root",
        str(tmp_path / "r"),
        "--json",
        tmp_path=tmp_path,
    )
    assert proc.returncode == 0
    data = body(proc)["data"]
    joined = " ".join(data["next_commands"])
    assert f"--request-id {data['run']['request_id']}" in joined
    assert "--dry-run" not in joined

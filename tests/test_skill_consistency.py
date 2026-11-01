"""Skill source consistency: frontmatter, live-syntax agreement, links, corpus.

Asserts the maintained skill source describes only the real CLI surface
(no invented run status/result/recover), links resolve, no secrets or
private paths leak, and the evaluation corpus covers positive plus
nearby-negative cases. In-process imports and ``describe`` probes use
this checkout's ``src/`` deterministically. Packaged-artifact coverage
lives in ``test_skill_package.py`` (isolated build, no skips).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
SRC = REPO / "src"
sys.path.insert(0, str(SRC))

SKILL_DIR = REPO / "skills" / "agent-session-control"
SKILL_MD = SKILL_DIR / "SKILL.md"
MCP_MD = SKILL_DIR / "references" / "mcp.md"
EVAL_MD = REPO / "tests" / "fixtures" / "skill" / "evaluation.md"

INVENTED = ("run status", "run result", "run recover")
REAL_COMMANDS = (
    "version",
    "describe",
    "doctor",
    "plan create",
    "plan resume",
    "session create",
    "session resume",
    "session close",
    "session list",
    "run submit",
    "run wait",
    "run events",
    "run interrupt",
    "acp",
)


def _read_skill() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def _frontmatter(text: str) -> dict:
    match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    assert match, "SKILL.md needs YAML frontmatter"
    raw: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        cleaned = value.strip()
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in ('"', "'"):
            cleaned = cleaned[1:-1]
        raw[key.strip()] = cleaned
    return raw


def _describe_objects() -> list[dict]:
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    proc = subprocess.run(
        [sys.executable, "-m", "agent_session_control", "describe", "--json"],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout)["data"]["commands"]


def _describe_params(command: str) -> list[str]:
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    proc = subprocess.run(
        [sys.executable, "-m", "agent_session_control", "describe", "--command", command, "--json"],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    params = json.loads(proc.stdout)["data"]["commands"]["params"]
    return [flag for p in params for flag in p["flags"]]


def test_skill_source_and_frontmatter() -> None:
    assert SKILL_MD.is_file()
    text = _read_skill()
    fm = _frontmatter(text)
    assert fm.get("name") == "agent-session-control"
    desc = fm.get("description", "")
    assert len(desc) >= 20 and len(desc) <= 1024
    assert "<" not in desc and ">" not in desc
    assert "TODO" not in text
    assert "Structuring This Skill" not in text
    assert "Not every skill requires" not in text
    assert "/Users/" not in text
    assert "SYNTHETIC" not in text
    assert "permission bypass" in text.lower() or "permission-bypass" in text.lower()
    assert "scheduler" in text.lower()


def test_no_invented_run_commands() -> None:
    text = _read_skill()
    for phrase in INVENTED:
        for match in re.finditer(re.escape(phrase), text):
            window = text[max(0, match.start() - 120) : match.end() + 40].lower()
            assert "there is no" in window or "never invent" in window, phrase
    assert '"$ABS" run status' not in text
    assert '"$ABS" run result' not in text
    assert "There is no" in text


def test_documented_commands_match_describe() -> None:
    text = _read_skill()
    live = [c["command"] for c in _describe_objects()]
    for command in REAL_COMMANDS:
        assert command in text, command
        assert command in live, command
    create_flags = _describe_params("session create")
    submit_flags = _describe_params("run submit")
    assert "--mcp-servers" in create_flags
    assert "--prompt-file" in submit_flags
    assert "--request-id" in submit_flags
    assert "--prompt-file" in text and "--request-id" in text


def test_mcp_file_channel_matches_live_help() -> None:
    """Secret file/stdin input agrees with the actual CLI surface.

    A's flag has landed: ``describe`` and ``--help`` list
    ``--mcp-servers-file`` (``-`` for stdin), and the skill teaches
    that real path with an executable example. The inline
    ``--mcp-servers`` form stays documented for non-secret entries
    only.
    """
    import os

    text = _read_skill()
    create_flags = _describe_params("session create")
    assert "--mcp-servers-file" in create_flags
    assert "--mcp-servers-file" in text
    assert "--mcp-servers-file -" in text or "--mcp-servers-file `-`" in text or "mcp-servers-file -" in text
    assert any(
        "--mcp-servers-file" in line and ("$ABS" in line or "$ROOT" in line or "/path/mcp.json" in line)
        for line in text.splitlines()
    ), "skill must show an executable --mcp-servers-file example"
    assert "without secrets only" in text or "non-secret" in text or "non-secrets" in text
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    proc = subprocess.run(
        [sys.executable, "-m", "agent_session_control", "session", "create", "--help"],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0
    assert ("--mcp-servers-file" in proc.stdout) == ("--mcp-servers-file" in create_flags)


def test_bootstrap_venv_entry_direct() -> None:
    """Missing-CLI bootstrap binds the venv entry directly: a
    caller-selected 3.10–3.14 interpreter, a concrete user-directory
    venv, ``python -m pip`` only, then ``ABS="$VENV/bin/...`` with an
    executable + version check — never a global-PATH re-probe."""
    text = _read_skill()
    assert "3.10" in text and "3.14" in text
    assert "python3.12 -m venv" not in text
    assert "this machine uses the selected 3.12" not in text
    pip_lines = [line for line in text.splitlines() if "pip install" in line]
    assert pip_lines, "install block must exist"
    assert all("-m pip" in line for line in pip_lines), "every install goes through python -m pip"
    assert "$VENV/bin/python" in text
    bind = 'ABS="$VENV/bin/agent-session-control"'
    assert bind in text
    assert 'test -x "$ABS"' in text
    bootstrap = text.split("If the CLI is missing")[1]
    assert "command -v agent-session-control" not in bootstrap
    first_install = text.index("-m pip install")
    abs_bind = text.index(bind)
    readback = text.index('"$ABS" version --json', abs_bind)
    assert first_install < abs_bind < readback


def test_mcp_reference_operational_no_rubric() -> None:
    """The runtime reference documents the real channel; the assessment
    corpus no longer ships with the skill and nothing invites a
    grading-rubric read."""
    text = _read_skill()
    assert "evaluation.md" not in text
    assert "(references/mcp.md)" in text
    assert MCP_MD.is_file()
    mcp = MCP_MD.read_text(encoding="utf-8")
    for token in ('"name"', '"command"', '"args"', '"env"', "--mcp-servers-file", "unsupported", "env_keys"):
        assert token in mcp, token
    assert "Expect:" not in mcp
    assert "Nearby-negative" not in mcp
    for token in ("heredoc", "$ROOT/mcp.json", "secret store", "0600", "0700", "umask 077", "trap"):
        assert token in mcp, token


def _sh_blocks(path: Path) -> list[str]:
    return re.findall(r"```sh\n(.*?)```", path.read_text(encoding="utf-8"), re.DOTALL)


def test_doc_examples_model_safe_preparation() -> None:
    """Secret-context examples must model the safe channel end to end:
    no heredocs, no cat-backs, no inline python, no echo pipes, and
    secret file args reference the task-owned root or stdin — never
    generic /tmp. File-target blocks carry a trap removing only the
    owned temp config."""
    blocks = _sh_blocks(MCP_MD) + [b for b in _sh_blocks(SKILL_MD) if "mcp-servers" in b.lower()]
    assert len(blocks) >= 2
    for block in blocks:
        for line in block.splitlines():
            stripped = line.strip()
            assert not stripped.startswith("cat "), f"never cat a secret config: {stripped}"
            assert not stripped.startswith("echo "), f"printf, not echo, for secret stdin: {stripped}"
            assert "<<" not in line, f"no heredocs in examples: {stripped}"
            assert "python -c" not in line, f"no inline python in examples: {stripped}"
            if re.search(r"\brm\b", stripped):
                assert '"$CFG"' in stripped, f"only the owned temp config is ever removed: {stripped}"
                assert "/tmp/" not in stripped
    for block in blocks:
        for match in re.finditer(r"--mcp-servers-file\s+(\S+)", block):
            target = match.group(1)
            assert target == "-" or target.startswith('"$ROOT'), f"task-owned path or stdin: {target}"
            assert "/tmp/" not in target, f"no generic /tmp secret paths: {target}"
        if re.search(r"--mcp-servers-file\s+\"\$ROOT", block):
            assert "trap" in block and "rm -f" in block, "file-target examples clean up the owned temp config"


def test_unique_task_scope_and_temp_hygiene() -> None:
    """No fixed shared state: the scope is a caller-selected unique
    directory (mktemp + 077), temp secrets are 0600 in 0700 with
    xtrace off, and only the owned temp config is ever removed."""
    text = _read_skill()
    assert "/tmp/demo-root" not in text
    assert "mktemp -d" in text and "umask 077" in text
    assert "set +x" in text
    assert "0600" in text and "0700" in text
    assert "trap 'rm -f \"$CFG\"'" in text


def test_links_and_corpus() -> None:
    text = _read_skill()
    targets = re.findall(r"\[[^\]]+\]\(([^)]+)\)", text)
    assert targets, "expected at least one skill-relative link"
    for target in targets:
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        assert (SKILL_DIR / target).exists(), target
    assert EVAL_MD.is_file()
    corpus = EVAL_MD.read_text(encoding="utf-8")
    assert "Positive" in corpus and "Nearby-negative" in corpus
    numbered = re.findall(r"^(\d+)\.", corpus, re.MULTILINE)
    assert len(numbered) >= 7
    positive, _, negative = corpus.partition("Nearby-negative")
    # The invented-status question is in-scope recovery: the skill loads
    # and redirects to the real commands — never a nearby-negative.
    assert "Redirect an invented command" in positive
    assert "never invent" in positive.lower()
    # The genuine unrelated negative is retrospective parsing with no
    # control action.
    assert "History parsing only" in negative
    # Delete/global-approval cases explain tool scope; they never state
    # a universal prohibition on otherwise-authorized actions.
    assert "refuse" not in corpus.lower()
    task_lines = [line for line in corpus.splitlines() if re.match(r"^\d+\.", line)]
    assert any("bypass" in line.lower() or "approval" in line.lower() for line in corpus.splitlines())
    for line in task_lines:
        assert "--request-id" not in line and "$ABS" not in line

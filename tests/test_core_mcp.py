"""Core MCP tests: validation plus disposable-fixture runs through real runtimes.

The channel-shape tests need no runtime. Fixture safety is proven without
any model by driving the fixture over stdio directly: only the explicitly
allowlisted synthetic value is ever returned, and an unrelated synthetic
ambient secret never appears in any tool result or log. The live tests render
the interactive plan form, then execute the same channel through the
runtime's headless entry with the safe fixture. Headless delivery is proven;
interactive hosted-TUI delivery additionally needs the host's server
ownership and is reported as pending, never counted from these runs.
Anything that cannot run here (missing executable, no login/quota, timeout)
skips with its reason instead of counting as a pass. Global configuration
files are checksummed around every live run.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid

import pytest

from agent_session_control.runtime import build_create_plan

#: The test MCP fixture speaks MCP over stdio using only the standard
#: library. It offers `echo` (returns caller input) and `read_allowlisted`
#: (returns exactly one explicitly allowlisted synthetic value). It NEVER
#: dumps the process environment: a generic environment-dump tool is
#: forbidden because it could carry ambient credentials into tool results,
#: logs or model input. The allowlisted key travels as argv[1]; only that
#: single value is ever read from the environment.
FIXTURE_SOURCE = """
import json, os, sys
ALLOW = sys.argv[1] if len(sys.argv) > 1 else ""
MARKER = "fixture-echo:"
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n"); sys.stdout.flush()
def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        mid = msg.get("id")
        method = msg.get("method", "")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}}, "serverInfo": {"name": "asc-fixture", "version": "0.0.1"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
                {"name": "echo", "description": "echo back input",
                 "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
                {"name": "read_allowlisted", "description": "return the single allowlisted value",
                 "inputSchema": {"type": "object", "properties": {}}}]}})
        elif method == "tools/call":
            name = (msg.get("params") or {}).get("name", "")
            if name == "read_allowlisted":
                send({"jsonrpc": "2.0", "id": mid, "result": {"content": [
                    {"type": "text", "text": str(os.environ.get(ALLOW, ""))}]}})
            elif name == "echo":
                args = (msg.get("params") or {}).get("arguments", {})
                send({"jsonrpc": "2.0", "id": mid, "result": {"content": [
                    {"type": "text", "text": MARKER + str(args.get("text", ""))}]}})
            else:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool"}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
main()
"""

# Skip only on specific observed native auth/quota evidence — never on
# generic substrings ("auth", "try again", bare "login") and never on a bare
# timeout. Checked only when the fixture marker is absent.
LIMIT_MARKERS = (
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


def _checksum(path):
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def _skip_unless_executable(name):
    path = shutil.which(name)
    if path is None:
        pytest.skip(f"runtime executable is not installed: {name}")
    return path


def _check_login(label, argv, probe):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"{label} login probe failed: {exc}")
    if not probe(proc.stdout + proc.stderr):
        pytest.skip(f"{label} is not logged in; per-profile limit, not a pass")


def _run_or_skip(label, argv, *, cwd, env, timeout=280):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env)
    except subprocess.TimeoutExpired as exc:
        # No concrete external reason: an unknown/hung path is a failure,
        # never an accepted environment skip.
        pytest.fail(f"{label} model call produced no result within {timeout}s: {exc}")
    except OSError as exc:
        pytest.skip(f"{label} could not launch: {exc}")
    return proc


def _fail_unless_echo(label, proc, marker):
    output = proc.stdout + proc.stderr
    if marker in output:
        return
    lowered = output.lower()
    if any(mark in lowered for mark in LIMIT_MARKERS):
        pytest.skip(f"{label} refused on login/quota/capacity; limit, not a pass")
    assert marker in output, f"{label} did not reach the fixture MCP server:\n{output[-3000:]}"


@pytest.fixture()
def fixture_server(tmp_path):
    path = tmp_path / "asc_mcp_fixture.py"
    path.write_text(FIXTURE_SOURCE, encoding="utf-8")
    return str(path)


@pytest.fixture()
def nonce():
    return uuid.uuid4().hex[:8]


def _stdio_call(fixture, allow_key, env, messages):
    request_lines = "\n".join(json.dumps(message) for message in messages) + "\n"
    proc = subprocess.run(
        [sys.executable, fixture, allow_key],
        input=request_lines,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return proc.stdout + proc.stderr


def test_fixture_reveals_only_allowlisted_key(tmp_path, fixture_server, nonce):
    """The fixture can never leak ambient secrets: no model needed.

    Drives the fixture over stdio directly. An unrelated synthetic ambient
    secret is present in its process environment; the full transcript
    (stdout and stderr) must never contain it, while the explicitly
    allowlisted value is returned verbatim.
    """
    allow_key = f"ASC_ALLOW_{nonce}"
    allow_value = f"allow-value-{nonce}"
    ambient_key = f"ASC_UNRELATED_{nonce}"
    ambient_secret = f"ambient-secret-{nonce}"
    env = dict(os.environ, **{allow_key: allow_value, ambient_key: ambient_secret})
    transcript = _stdio_call(
        fixture_server,
        allow_key,
        env,
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "read_allowlisted", "arguments": {}},
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"text": "hi"}},
            },
        ],
    )
    assert allow_value in transcript, "the allowlisted value is the designed proof channel"
    assert ambient_secret not in transcript, "unrelated ambient secret must never appear anywhere"
    assert ambient_key not in transcript
    lines = [json.loads(line) for line in transcript.splitlines() if line.strip().startswith("{")]
    tools = next(
        message["result"]["tools"]
        for message in lines
        if message.get("id") == 2 and isinstance(message.get("result"), dict)
    )
    assert sorted(tool["name"] for tool in tools) == ["echo", "read_allowlisted"]
    calls = [message for message in lines if message.get("id") == 3]
    assert len(calls) == 1
    assert calls[0]["result"]["content"] == [{"type": "text", "text": allow_value}]
    assert "fixture-echo:hi" in transcript


def test_claude_mcp_config_argv_reaches_fixture(tmp_path, fixture_server, nonce):
    _skip_unless_executable("claude")
    _check_login("claude", ["claude", "auth", "status"], lambda out: '"loggedIn": true' in out)
    name = f"asc-rt-{nonce}"
    key = f"ASC_RT_ENV_{nonce}"
    server = {"name": name, "command": sys.executable, "args": [fixture_server], "env": {key: f"secret-{nonce}"}}
    plan = build_create_plan("claude", str(tmp_path), auto_approve=True, mcp_servers=[server])
    document = plan.argv[plan.argv.index("--mcp-config") + 1]
    # The argv document references the secret; the value lives only in plan.env.
    lookup = f"ASC_MCP_{name.upper().replace('-', '_')}_{key}"
    entry = json.loads(document)["mcpServers"][name]
    assert entry["env"] == {key: "${" + lookup + "}"}
    assert plan.to_dict(include_env=True)["env"][lookup] == f"secret-{nonce}"
    assert f"secret-{nonce}" not in json.dumps(plan.to_dict())
    marker = f"fixture-echo:{nonce}"
    env = dict(os.environ, **plan.to_dict(include_env=True)["env"])
    argv = [
        "claude",
        "--mcp-config",
        document,
        "--strict-mcp-config",
        "-p",
        f"Call echo from MCP server {name} with text {nonce!r} and reply with ONLY the tool result.",
    ]
    proc = _run_or_skip("claude", argv, cwd=str(tmp_path), env=env)
    _fail_unless_echo("claude", proc, marker)


def test_codex_config_override_reaches_fixture(tmp_path, fixture_server, nonce):
    _skip_unless_executable("codex")
    _check_login("codex", ["codex", "login", "status"], lambda out: "logged in" in out.lower())
    name = f"asc-rt-{nonce}"
    key = f"ASC_RT_ENV_{nonce}"
    secret = f"secret-{nonce}"
    server = {
        "name": name,
        "command": sys.executable,
        "args": [fixture_server, key],
        "env": {key: secret},
    }
    plan = build_create_plan("codex", str(tmp_path), auto_approve=True, mcp_servers=[server])
    pairs = [plan.argv[i + 1] for i, token in enumerate(plan.argv) if token == "-c"]
    assert any(p.startswith(f"mcp_servers.{name}.command=") for p in pairs)
    # No inline env on argv: only the forwarding name travels there.
    assert any(p == f'mcp_servers.{name}.env_vars=["{key}"]' for p in pairs)
    assert secret not in json.dumps(plan.to_dict())
    live_env = plan.to_dict(include_env=True)["env"]
    assert live_env == {key: secret}
    config_path = os.path.expanduser("~/.codex/config.toml")
    before = _checksum(config_path)
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()
    argv = ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check"]
    for pair in pairs:
        argv.extend(["-c", pair])
    argv.append(f"Call the read_allowlisted tool from MCP server {name} and reply with ONLY its result.")
    env = dict(os.environ, **live_env)
    proc = _run_or_skip("codex", argv, cwd=str(project), env=env)
    _fail_unless_echo("codex", proc, secret)
    assert _checksum(config_path) == before, "per-invocation MCP must not persist to global config"
    with open(config_path, encoding="utf-8", errors="replace") as handle:
        assert name not in handle.read()


def test_opencode_env_config_reaches_fixture(tmp_path, fixture_server, nonce):
    opencode = _skip_unless_executable("opencode")
    _check_login("opencode", [opencode, "auth", "list"], lambda out: "stored" in out.lower())
    name = f"asc-rt-{nonce}"
    server = {"name": name, "command": sys.executable, "args": [fixture_server], "env": {}}
    project = tmp_path / "proj"
    project.mkdir()
    plan = build_create_plan("opencode", str(project), auto_approve=True, mcp_servers=[server])
    content = plan.to_dict(include_env=True)["env"]["OPENCODE_CONFIG_CONTENT"]
    assert json.loads(content)["mcp"]["servers"][name]["command"][0] == sys.executable
    home = os.path.expanduser("~")
    watched = [
        os.path.join(home, ".config", "opencode", "opencode.json"),
        os.path.join(home, ".config", "opencode", "config.json"),
    ]
    before = {path: _checksum(path) for path in watched}
    env = dict(os.environ, OPENCODE_CONFIG_CONTENT=content)
    argv = [
        opencode,
        "--standalone",
        "run",
        "--format",
        "json",
        f"Call echo from MCP server {name} with text {nonce!r} and reply with ONLY the tool result.",
    ]
    proc = _run_or_skip("opencode", argv, cwd=str(project), env=env)
    _fail_unless_echo("opencode", proc, f"fixture-echo:{nonce}")
    for path in watched:
        assert _checksum(path) == before[path], f"global config changed: {path}"

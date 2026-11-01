"""Security: secret-safe tmux transport, MCP file channel, opencode standalone.

Proves the 0.1.0 defect is closed: ``plan.env`` values never appear in any
intermediate tmux argv (no ``-e NAME=value``), travel instead through a
single-use private socket rendezvous to the pane, and never reach
server-global state, sibling sessions, diagnostics or stored records.
Covers hostile characters, per-session scope, failure cleanup, CLI file
input and launcher-owned opencode MCP.
"""

import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_session_control.runtime import build_create_plan, build_resume_plan  # noqa: E402
from agent_session_control.terminal import TmuxBackend, TmuxError  # noqa: E402

needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")

SECRET = "SYNTHETIC-SECRET-abc123-NOT-A-CREDENTIAL"
HOSTILE = "x`$'\"\nnewline-é-🎉\ttab\\backslash${HOME};|&*?~#"


def _server(name="s", secret=SECRET):
    return {"name": name, "command": "/bin/echo", "args": ["--x"], "env": {"TOKEN": secret}}


def test_plan_argv_never_carries_env_secrets(tmp_path):
    cwd = str(tmp_path)
    for rid in ("claude", "codex", "opencode"):
        plan = build_create_plan(rid, cwd, auto_approve=True, mcp_servers=[_server()])
        assert SECRET not in " ".join(plan.argv)
        assert SECRET not in repr(plan)
        assert SECRET not in json.dumps(plan.to_dict())
        assert SECRET not in json.dumps(plan.to_dict(include_env=True)["argv"])
        live = plan.to_dict(include_env=True)["env"]
        assert SECRET in json.dumps(live)
        resumed = build_resume_plan(
            rid,
            {"id": "abc", "path": "/tmp/h", "cwd": cwd},
            auto_approve=True,
            mcp_servers=[_server()],
        )
        assert SECRET not in " ".join(resumed.argv)
        assert SECRET not in repr(resumed)


def test_opencode_uses_launcher_owned_standalone(tmp_path):
    cwd = str(tmp_path)
    plain = build_create_plan("opencode", cwd, auto_approve=True)
    assert plain.argv == ("opencode", "--auto")
    with_mcp = build_create_plan("opencode", cwd, auto_approve=True, mcp_servers=[_server()])
    assert with_mcp.argv == ("opencode", "--standalone", "--auto")
    assert "--standalone" in " ".join(with_mcp.argv)
    assert SECRET not in " ".join(with_mcp.argv)
    resume_plain = build_resume_plan("opencode", {"id": "abc", "cwd": cwd}, auto_approve=True)
    assert resume_plain.argv == ("opencode", "--auto", "-s", "abc")
    resume_mcp = build_resume_plan("opencode", {"id": "abc", "cwd": cwd}, auto_approve=True, mcp_servers=[_server()])
    assert resume_mcp.argv == ("opencode", "--standalone", "--auto", "-s", "abc")


def _capture_backend(tmp_path, namespace):
    backend = TmuxBackend(namespace, root=str(tmp_path / "home"))
    captured: list[list[str]] = []
    orig = backend._run

    def spy(*args, **kwargs):
        captured.append(list(args))
        return orig(*args, **kwargs)

    backend._run = spy  # type: ignore[method-assign]
    return backend, captured


@needs_tmux
def test_tmux_host_private_env_no_argv_leak_and_delivers(tmp_path):
    backend, captured = _capture_backend(tmp_path, f"sec-{os.getpid()}-1")
    secret = HOSTILE + "-1"
    out = os.path.join(str(tmp_path), "out1.txt")
    code = (
        f"import os; open({out!r},'w',encoding='utf-8')"
        ".write(os.environ.get('ASC_SEC','<m>')); import time; time.sleep(20)"
    )
    pane = backend.host(
        (sys.executable, "-c", code),
        runtime="echo",
        cwd=str(tmp_path),
        env={"ASC_SEC": secret},
        ident=backend.new_ident(),
    )
    try:
        assert not any(a == "-e" for cmd in captured for a in cmd)
        assert not any(secret in a for cmd in captured for a in cmd)
        deadline = __import__("time").monotonic() + 15.0
        got = "<missing>"
        while __import__("time").monotonic() < deadline:
            if os.path.exists(out):
                with open(out, encoding="utf-8") as handle:
                    got = handle.read()
                if got:
                    break
            __import__("time").sleep(0.2)
        assert got == secret
        proc = backend._run("show-environment", "-t", pane)
        assert secret not in (proc.stdout or b"").decode("utf-8", "replace")
        # Registry record and state dir must never hold the value.
        record_path = backend.registry._path(pane)
        with open(record_path, encoding="utf-8") as handle:
            assert secret not in handle.read()
        for root, _dirs, files in os.walk(str(tmp_path)):
            for name in files:
                if name == "out1.txt":
                    continue
                with open(os.path.join(root, name), encoding="utf-8", errors="replace") as handle:
                    assert secret not in handle.read()
    finally:
        backend.close(pane)
    import glob as _glob

    assert _glob.glob("/tmp/asc-env-*") == []


@needs_tmux
def test_tmux_private_env_per_session_scope(tmp_path):
    backend, _ = _capture_backend(tmp_path, f"sec-{os.getpid()}-2")
    out_a = os.path.join(str(tmp_path), "a.txt")
    out_b = os.path.join(str(tmp_path), "b.txt")
    secret_a = "scope-A-" + HOSTILE
    secret_b = "scope-B-🎉-different"
    code_a = (
        f"import os; open({out_a!r},'w',encoding='utf-8').write(os.environ.get('ASC_SCOPE','<m>'));"
        " import time; time.sleep(20)"
    )
    code_b = (
        f"import os; open({out_b!r},'w',encoding='utf-8').write(os.environ.get('ASC_SCOPE','<m>'));"
        " import time; time.sleep(20)"
    )
    pane_a = backend.host(
        (sys.executable, "-c", code_a),
        runtime="echo",
        cwd=str(tmp_path),
        env={"ASC_SCOPE": secret_a},
        ident=backend.new_ident(),
    )
    pane_b = backend.host(
        (sys.executable, "-c", code_b),
        runtime="echo",
        cwd=str(tmp_path),
        env={"ASC_SCOPE": secret_b},
        ident=backend.new_ident(),
    )
    try:
        import time as _time

        deadline = _time.monotonic() + 15.0
        while _time.monotonic() < deadline:
            ok = True
            for path in (out_a, out_b):
                if not os.path.exists(path):
                    ok = False
                    continue
                with open(path, encoding="utf-8") as handle:
                    if not handle.read():
                        ok = False
            if ok:
                break
            _time.sleep(0.2)
        with open(out_a, encoding="utf-8") as handle:
            assert handle.read() == secret_a
        with open(out_b, encoding="utf-8") as handle:
            assert handle.read() == secret_b
    finally:
        backend.close(pane_a)
        backend.close(pane_b)


@needs_tmux
def test_tmux_rejects_invalid_env_without_launch(tmp_path):
    backend, captured = _capture_backend(tmp_path, f"sec-{os.getpid()}-3")
    with pytest.raises(TmuxError) as excinfo:
        backend.host(
            (sys.executable, "-c", "pass"),
            runtime="echo",
            cwd=str(tmp_path),
            env={"BAD=KEY": "v"},
            ident=backend.new_ident(),
        )
    assert excinfo.value.code == "usage_error"
    assert "BAD" not in str(excinfo.value)
    with pytest.raises(TmuxError):
        backend.host(
            (sys.executable, "-c", "pass"),
            runtime="echo",
            cwd=str(tmp_path),
            env={"K": "a\x00b"},
            ident=backend.new_ident(),
        )
    assert captured == []


def test_tmux_timeout_message_never_carries_secrets(tmp_path, monkeypatch):
    backend, _ = _capture_backend(tmp_path, f"sec-{os.getpid()}-4")

    def _boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["tmux", "new-session"], timeout=1.0)

    monkeypatch.setattr(backend, "_run", _boom)
    with pytest.raises(TmuxError) as excinfo:
        backend.host(
            (sys.executable, "-c", "pass"),
            runtime="echo",
            cwd=str(tmp_path),
            env={"ASC_SEC": SECRET},
            ident="fixedident",
        )
    assert SECRET not in str(excinfo.value)
    assert SECRET not in repr(excinfo.value)


# -- CLI file channel ---------------------------------------------------------

EXE = [sys.executable, "-m", "agent_session_control"]


def _run_cli(*argv, cwd=None, stdin_text=None):
    env = dict(os.environ)
    src = os.path.join(os.path.dirname(__file__), "..", "src")
    src = os.path.abspath(src)
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["XDG_CONFIG_HOME"] = "/tmp/asc-cli-sec-test"
    env["NO_COLOR"] = "1"
    return subprocess.run(
        [*EXE, *argv],
        capture_output=True,
        text=True,
        input=stdin_text,
        cwd=str(cwd) if cwd else None,
        env=env,
        timeout=60,
    )


def test_cli_mcp_file_channel(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    secret = "FILE-CHANNEL-SECRET-xyz"
    payload = json.dumps([{"name": "fs", "command": "/bin/echo", "env": {"TOKEN": secret}}])
    conf = tmp_path / "mcp.json"
    conf.write_text(payload, encoding="utf-8")
    proc = _run_cli(
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
        "--mcp-servers-file",
        str(conf),
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert secret not in proc.stdout and secret not in proc.stderr
    body = json.loads(proc.stdout)
    assert body["ok"] is True
    assert secret not in json.dumps(body["data"]["launch"])
    assert not (tmp_path / "root").exists()
    # Piped stdin variant.
    piped = _run_cli(
        "session",
        "create",
        "--runtime",
        "codex",
        "--cwd",
        str(proj),
        "--dry-run",
        "--json",
        "--root",
        str(tmp_path / "root2"),
        "--mcp-servers-file",
        "-",
        stdin_text=payload,
    )
    assert piped.returncode == 0, piped.stderr[-2000:]
    assert secret not in piped.stdout and secret not in piped.stderr
    # Mutual exclusivity refuses without state and without echo.
    both = _run_cli(
        "session",
        "create",
        "--runtime",
        "codex",
        "--cwd",
        str(proj),
        "--dry-run",
        "--json",
        "--root",
        str(tmp_path / "root3"),
        "--mcp-servers",
        payload,
        "--mcp-servers-file",
        str(conf),
    )
    assert both.returncode == 2
    assert secret not in both.stdout + both.stderr
    assert not (tmp_path / "root3").exists()
    # Malformed file refuses without echo or state.
    bad = tmp_path / "bad.json"
    bad.write_text('{"not": "an array with ' + secret + '"}', encoding="utf-8")
    refused = _run_cli(
        "session",
        "create",
        "--runtime",
        "codex",
        "--cwd",
        str(proj),
        "--dry-run",
        "--json",
        "--root",
        str(tmp_path / "root4"),
        "--mcp-servers-file",
        str(bad),
    )
    assert refused.returncode == 2
    assert secret not in refused.stdout + refused.stderr
    assert not (tmp_path / "root4").exists()


def test_cli_describe_lists_file_channel():
    proc = _run_cli("describe", "--command", "session create", "--json")
    assert proc.returncode == 0
    flags = [f for p in json.loads(proc.stdout)["data"]["commands"]["params"] for f in p["flags"]]
    assert "--mcp-servers" in flags and "--mcp-servers-file" in flags


def test_cli_mcp_file_oversized_and_stdin_oversized_refuse(tmp_path):
    from agent_session_control.cli import MAX_MCP_FILE_CHARS

    proj = tmp_path / "proj"
    proj.mkdir()
    big = "x" * (MAX_MCP_FILE_CHARS + 1)
    conf = tmp_path / "big.json"
    conf.write_text(big, encoding="utf-8")
    proc = _run_cli(
        "session",
        "create",
        "--runtime",
        "codex",
        "--cwd",
        str(proj),
        "--dry-run",
        "--json",
        "--root",
        str(tmp_path / "r1"),
        "--mcp-servers-file",
        str(conf),
    )
    assert proc.returncode == 2
    assert "Traceback" not in proc.stdout + proc.stderr
    assert not (tmp_path / "r1").exists()
    piped = _run_cli(
        "session",
        "create",
        "--runtime",
        "codex",
        "--cwd",
        str(proj),
        "--dry-run",
        "--json",
        "--root",
        str(tmp_path / "r2"),
        "--mcp-servers-file",
        "-",
        stdin_text=big,
    )
    assert piped.returncode == 2
    assert "Traceback" not in piped.stdout + piped.stderr
    assert not (tmp_path / "r2").exists()


def test_cli_mcp_file_invalid_utf8_missing_and_empty_inline_conflict(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    bad_bytes = tmp_path / "bad-bytes.json"
    bad_bytes.write_bytes(b'[{"name": "x", "command": "\xff\xfe"}]')
    refused = _run_cli(
        "session",
        "create",
        "--runtime",
        "codex",
        "--cwd",
        str(proj),
        "--dry-run",
        "--json",
        "--root",
        str(tmp_path / "r3"),
        "--mcp-servers-file",
        str(bad_bytes),
    )
    assert refused.returncode == 2
    assert "Traceback" not in refused.stdout + refused.stderr
    assert not (tmp_path / "r3").exists()
    missing = _run_cli(
        "session",
        "create",
        "--runtime",
        "codex",
        "--cwd",
        str(proj),
        "--dry-run",
        "--json",
        "--root",
        str(tmp_path / "r4"),
        "--mcp-servers-file",
        str(tmp_path / "does-not-exist.json"),
    )
    assert missing.returncode == 2
    assert "Traceback" not in missing.stdout + missing.stderr
    assert not (tmp_path / "r4").exists()
    # Empty inline string plus a file flag must still conflict (presence,
    # not truthiness). Old `if inline and file_path` missed this.
    conf = tmp_path / "ok.json"
    conf.write_text("[]", encoding="utf-8")
    both = _run_cli(
        "session",
        "create",
        "--runtime",
        "codex",
        "--cwd",
        str(proj),
        "--dry-run",
        "--json",
        "--root",
        str(tmp_path / "r5"),
        "--mcp-servers",
        "",
        "--mcp-servers-file",
        str(conf),
    )
    assert both.returncode == 2
    assert not (tmp_path / "r5").exists()


def _unique_backend(tmp_path, tag):
    import uuid as _uuid

    return TmuxBackend(f"sec-{os.getpid()}-{tag}-{_uuid.uuid4().hex[:6]}", root=str(tmp_path / "home"))


def test_preflight_oversized_and_unencodable_env_never_creates_pane(tmp_path):
    import glob as _glob

    from agent_session_control.terminal import _ENV_RDV_MAX_BYTES

    backend = _unique_backend(tmp_path, "pre")
    calls: list[list[str]] = []
    orig = backend._run

    def spy(*args, **kwargs):
        calls.append(list(args))
        return orig(*args, **kwargs)

    backend._run = spy  # type: ignore[method-assign]
    before = set(_glob.glob("/tmp/asc-env-*"))
    huge = "H" * (_ENV_RDV_MAX_BYTES + 1)
    with pytest.raises(TmuxError) as exc:
        backend.host(
            (sys.executable, "-c", "pass"),
            runtime="echo",
            cwd=str(tmp_path),
            env={"ASC_HUGE": huge},
            ident=backend.new_ident(),
        )
    assert exc.value.code == "usage_error"
    assert "H" * 16 not in str(exc.value)
    lone_surrogate = "\ud800"
    with pytest.raises(TmuxError):
        backend.host(
            (sys.executable, "-c", "pass"),
            runtime="echo",
            cwd=str(tmp_path),
            env={"ASC_BAD": lone_surrogate},
            ident=backend.new_ident(),
        )
    assert calls == [], f"preflight must run before new-session, got {calls!r}"
    assert set(_glob.glob("/tmp/asc-env-*")) == before


def test_handshake_replacement_never_killed_and_failed_cleanup_safe(tmp_path, monkeypatch):
    import socket as _socket_mod

    backend = _unique_backend(tmp_path, "repl")
    witness = ("%99", "12345")
    replacement = ("%99", "99999")
    kill_calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        if args and args[0] == "new-session":
            return subprocess.CompletedProcess(args, 0, stdout=b"%99:12345\n", stderr=b"")
        if args and args[0] in ("kill-pane", "kill-session"):
            kill_calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        if args and args[0] == "has-session":
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(backend, "_run", fake_run)
    # Replacement BEFORE the first by-name query: the atomic stdout witness
    # (%99:12345) is authoritative, so the pre-accept comparison against the
    # first observation (%99:99999) refuses BEFORE accept -> already_hosted,
    # no register, no secret handoff, no kill.
    seq = iter([replacement, replacement])

    def fake_instance(pane):
        try:
            return next(seq)
        except StopIteration:
            return replacement

    monkeypatch.setattr(backend, "current_instance", fake_instance)

    accept_calls: list[int] = []

    class _FakeListener:
        def bind(self, *a, **k):
            return None

        def listen(self, *a, **k):
            return None

        def settimeout(self, *a, **k):
            return None

        def accept(self):
            accept_calls.append(1)
            raise TimeoutError("no client")

        def close(self):
            return None

    monkeypatch.setattr(_socket_mod, "socket", lambda *a, **k: _FakeListener())
    with pytest.raises(TmuxError) as exc:
        backend._host_with_private_env(
            "asc-repl-pane",
            [sys.executable, "-c", "pass"],
            {"ASC_SEC": "v"},
            runtime="echo",
            cwd=str(tmp_path),
            ident="replident",
        )
    assert exc.value.code == "already_hosted"
    assert accept_calls == [], "refusal must precede the rendezvous accept"
    assert kill_calls == [], f"replacement must not be killed, got {kill_calls!r}"
    assert backend.registry.lookup("asc-repl-pane") is None
    # Failed cleanup path: witness still current but kill raises; host must
    # still report launch_failed, never success, never adopt.
    kill_calls.clear()
    seq2 = iter([witness, witness])

    def fake_instance2(pane):
        try:
            return next(seq2)
        except StopIteration:
            return witness

    monkeypatch.setattr(backend, "current_instance", fake_instance2)

    def fake_run_fail_kill(*args, **kwargs):
        if args and args[0] == "new-session":
            return subprocess.CompletedProcess(args, 0, stdout=b"%99:12345\n", stderr=b"")
        if args and args[0] in ("kill-pane", "kill-session"):
            kill_calls.append(list(args))
            raise OSError("kill failed")
        if args and args[0] == "has-session":
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(backend, "_run", fake_run_fail_kill)
    with pytest.raises(TmuxError) as exc2:
        backend._host_with_private_env(
            "asc-repl-pane2",
            [sys.executable, "-c", "pass"],
            {"ASC_SEC": "v"},
            runtime="echo",
            cwd=str(tmp_path),
            ident="replident2",
        )
    assert exc2.value.code == "launch_failed"
    assert kill_calls and kill_calls[0][0] == "kill-pane" and kill_calls[0][2] == "%99"


def test_replacement_before_and_after_accept_sends_zero_secret_bytes(tmp_path, monkeypatch):
    """Compare-before-accept AND compare-before-send: zero secret bytes.

    A fake listener offers a connected peer that captures every sent byte.
    Whether the replacement is already live at the pre-accept comparison or
    appears between accept and send, the result must be ``already_hosted``
    with an empty capture buffer, no registry entry and no kill. Narrow
    same-user race invariant only; no hostile-user isolation is claimed.
    """
    import socket as _socket_mod

    witness = ("%99", "12345")
    replacement = ("%99", "99999")

    class _FakeConn:
        def __init__(self):
            self.sent = bytearray()
            self.closed = False

        def settimeout(self, *a, **k):
            return None

        def sendall(self, data):
            self.sent += data

        def shutdown(self, *a, **k):
            return None

        def close(self):
            self.closed = True
            return None

    class _AcceptingListener:
        def __init__(self):
            self.conn = _FakeConn()
            self.accept_calls = 0

        def bind(self, *a, **k):
            return None

        def listen(self, *a, **k):
            return None

        def settimeout(self, *a, **k):
            return None

        def accept(self):
            self.accept_calls += 1
            return (self.conn, ("peer", 0))

        def close(self):
            return None

    listeners: list[_AcceptingListener] = []
    kill_calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        if args and args[0] == "new-session":
            return subprocess.CompletedProcess(args, 0, stdout=b"%99:12345\n", stderr=b"")
        if args and args[0] in ("kill-pane", "kill-session"):
            kill_calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    def _factory(*a, **k):
        listener = _AcceptingListener()
        listeners.append(listener)
        return listener

    monkeypatch.setattr(_socket_mod, "socket", _factory)

    # Case 1: replacement already live before accept.
    backend = _unique_backend(tmp_path, "zero1")
    monkeypatch.setattr(backend, "_run", fake_run)
    monkeypatch.setattr(backend, "current_instance", lambda pane: replacement)
    with pytest.raises(TmuxError) as exc1:
        backend._host_with_private_env(
            "asc-zero-pane1",
            [sys.executable, "-c", "pass"],
            {"ASC_SEC": "before-accept-secret"},
            runtime="echo",
            cwd=str(tmp_path),
            ident="zeroident1",
        )
    assert exc1.value.code == "already_hosted"
    assert listeners[0].accept_calls == 0, "refusal must precede the rendezvous accept"
    assert bytes(listeners[0].conn.sent) == b""
    assert kill_calls == []
    assert backend.registry.lookup("asc-zero-pane1") is None

    # Case 2: replacement appears between accept and send.
    backend2 = _unique_backend(tmp_path, "zero2")
    monkeypatch.setattr(backend2, "_run", fake_run)
    seq = iter([witness, replacement])

    def fake_instance(pane):
        try:
            return next(seq)
        except StopIteration:
            return replacement

    monkeypatch.setattr(backend2, "current_instance", fake_instance)
    with pytest.raises(TmuxError) as exc2:
        backend2._host_with_private_env(
            "asc-zero-pane2",
            [sys.executable, "-c", "pass"],
            {"ASC_SEC": "after-accept-secret"},
            runtime="echo",
            cwd=str(tmp_path),
            ident="zeroident2",
        )
    assert exc2.value.code == "already_hosted"
    assert listeners[1].accept_calls == 1
    assert bytes(listeners[1].conn.sent) == b"", "no secret byte may reach a post-accept replacement"
    assert listeners[1].conn.closed, "connection must be closed without sending"
    assert kill_calls == []
    assert backend2.registry.lookup("asc-zero-pane2") is None


@pytest.mark.parametrize("bad_out", [b"", b"\n", b"%99\n", b"%99:notapid\n", b"garbage\n", b"%99:1:2\n", b"pane:123\n"])
def test_atomic_witness_unreadable_forged_never_adopts_or_kills(tmp_path, monkeypatch, bad_out):
    """Unreadable/forged ``-P -F`` output: no register, no handoff, no kill."""
    import socket as _socket_mod

    from agent_session_control.terminal import _parse_creation_witness

    assert _parse_creation_witness(subprocess.CompletedProcess([], 0, stdout=bad_out, stderr=b"")) is None
    backend = _unique_backend(tmp_path, "badwit")
    kill_calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        if args and args[0] == "new-session":
            return subprocess.CompletedProcess(args, 0, stdout=bad_out, stderr=b"")
        if args and args[0] in ("kill-pane", "kill-session"):
            kill_calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(backend, "_run", fake_run)
    observed: list[str] = []
    monkeypatch.setattr(backend, "current_instance", lambda pane: observed.append(pane) or ("%0", "1"))

    class _FakeListener:
        def bind(self, *a, **k):
            return None

        def listen(self, *a, **k):
            return None

        def settimeout(self, *a, **k):
            return None

        def accept(self):  # must never be reached: witness fails first
            raise AssertionError("secret handoff attempted with unreadable witness")

        def close(self):
            return None

    monkeypatch.setattr(_socket_mod, "socket", lambda *a, **k: _FakeListener())
    with pytest.raises(TmuxError) as exc:
        backend._host_with_private_env(
            "asc-bad-pane",
            [sys.executable, "-c", "pass"],
            {"ASC_SEC": "v"},
            runtime="echo",
            cwd=str(tmp_path),
            ident="badident",
        )
    assert exc.value.code == "launch_failed"
    assert kill_calls == []
    assert observed == [], "no by-name query may substitute for atomic creation proof"
    assert backend.registry.lookup("asc-bad-pane") is None


def test_direct_atomic_witness_mismatch_never_registers(tmp_path, monkeypatch):
    """Direct path: forged stdout witness vs replacement observation."""
    backend = _unique_backend(tmp_path, "direct")
    kill_calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        if args and args[0] == "new-session":
            return subprocess.CompletedProcess(args, 0, stdout=b"%7:777\n", stderr=b"")
        if args and args[0] in ("kill-pane", "kill-session"):
            kill_calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(backend, "_run", fake_run)
    monkeypatch.setattr(backend, "current_instance", lambda pane: ("%7", "888"))
    with pytest.raises(TmuxError) as exc:
        backend._host_direct(
            "asc-direct-pane", [sys.executable, "-c", "pass"], runtime="echo", cwd=str(tmp_path), ident="d1"
        )
    assert exc.value.code == "already_hosted"
    assert kill_calls == []
    assert backend.registry.lookup("asc-direct-pane") is None

"""Engine terminal tests: owned tmux hosting on unique namespace sockets.

Real-path tests host a disposable echo agent (no model, no login) in panes on
a per-test tmux socket. They never enumerate or touch user sessions: every
pane is created by the backend under test and killed in teardown.
"""

import os
import shutil
import sys
import threading
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))

from agent_session_control.terminal import (  # noqa: E402
    TmuxBackend,
    TmuxError,
    blocked_input_cause,
    pane_name_for,
    socket_name_for,
)

FIXTURE_DIR = os.path.join(_HERE, "fixtures", "engine")
ECHO_AGENT = os.path.join(FIXTURE_DIR, "echo_agent.py")

needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")


def make_backend(tmp_path, namespace=None):
    namespace = namespace or f"t{os.getpid()}-{threading.get_ident() % 100000}-{len(os.listdir(tmp_path))}"
    backend = TmuxBackend(namespace, root=str(tmp_path / "tmux-home"))
    return backend


def host_echo(backend, tmp_path):
    pane = backend.host((sys.executable, ECHO_AGENT), runtime="echo", cwd=str(tmp_path), ident=backend.new_ident())
    assert backend.wait_ready(pane, ("ECHO-READY",), timeout=20.0)
    return pane


def test_socket_identity_covers_root_and_namespace():
    same_ns_a = socket_name_for("/tmp/root-a", "My NS/1")
    same_ns_b = socket_name_for("/tmp/root-b", "My NS/1")
    assert same_ns_a != same_ns_b, "different roots must never share a server"
    assert socket_name_for("/tmp/root-a", "My NS/1") == same_ns_a, "identity is stable"
    assert socket_name_for("/tmp/root-a", "my-ns-1") != socket_name_for("/tmp/root-a", "my-ns-2")
    # Truncation cannot alias: long names differing past the display prefix
    # still hash differently.
    long_a = socket_name_for("/tmp/r", "a" * 100 + "-1")
    long_b = socket_name_for("/tmp/r", "a" * 100 + "-2")
    assert long_a != long_b
    pane = pane_name_for("My NS", "Claude", "abcdef123456")
    assert pane.startswith("asc-")
    assert " " not in pane and "/" not in pane


def test_blocked_dialog_guard_refuses_keyboard_owned_panes():
    trust = "\n".join(
        [
            "Quick safety check: Is this a project you trust?",
            "  Yes, I trust this folder",
            "",
            "  Enter to confirm \u00b7 Esc to cancel",
        ]
    )
    assert blocked_input_cause(trust) is not None
    assert blocked_input_cause("❯ hello\nsome output") is None
    assert blocked_input_cause(None) is None
    assert blocked_input_cause("") is None


def test_backend_constructor_has_no_side_effects(tmp_path):
    backend = make_backend(tmp_path, namespace="no-side-effects-xyz")
    # No server socket may exist for this namespace before any call.
    import subprocess

    proc = subprocess.run(
        ["tmux", "-L", backend.socket, "list-sessions"], capture_output=True, env={**os.environ, "TMUX_TMPDIR": "/tmp"}
    )
    assert proc.returncode != 0


@needs_tmux
def test_host_capture_liveness_and_close(tmp_path):
    backend = make_backend(tmp_path)
    pane = backend.host((sys.executable, ECHO_AGENT), runtime="echo", cwd=str(tmp_path), ident=backend.new_ident())
    try:
        assert backend.liveness(pane) == "alive"
        assert backend.wait_ready(pane, ("ECHO-READY",), timeout=20.0)
        screen = backend.capture(pane)
        assert screen is not None and "ECHO-READY" in screen.replace("\x1b", "")
    finally:
        assert backend.close(pane) is True
    assert backend.liveness(pane) == "dead"


@needs_tmux
def test_same_ident_reuses_pane(tmp_path):
    backend = make_backend(tmp_path)
    ident = backend.new_ident()
    first = backend.host((sys.executable, ECHO_AGENT), runtime="echo", cwd=str(tmp_path), ident=ident)
    try:
        second = backend.host((sys.executable, ECHO_AGENT), runtime="echo", cwd=str(tmp_path), ident=ident)
        assert first == second
    finally:
        backend.close(first)


@needs_tmux
def test_close_refuses_foreign_panes(tmp_path):
    backend = make_backend(tmp_path)
    with pytest.raises(TmuxError) as excinfo:
        backend.close("some-users-own-session")
    assert excinfo.value.code == "not_owned"
    # Prefix mimicry is not ownership proof either.
    with pytest.raises(TmuxError) as excinfo:
        backend.close("asc-t99999-echo-deadbeef")
    assert excinfo.value.code == "not_owned"


@needs_tmux
def test_live_foreign_pane_is_never_adopted_or_killed(tmp_path):
    first = make_backend(tmp_path, namespace=f"owner-{os.getpid()}")
    pane = first.host((sys.executable, ECHO_AGENT), runtime="echo", cwd=str(tmp_path), ident=first.new_ident())
    try:
        assert first.wait_ready(pane, ("ECHO-READY",), timeout=20.0)
        second = TmuxBackend(f"owner-{os.getpid()}", root=str(tmp_path / "other-home"))
        assert second.liveness(pane) == "dead", "separate sockets never see each other's panes"
        with pytest.raises(TmuxError) as excinfo:
            second.close(pane)
        assert excinfo.value.code == "not_owned"
        assert first.liveness(pane) == "alive", "the refused close disturbed nothing"
    finally:
        first.close(pane)


@needs_tmux
def test_multiline_unicode_paste_and_submit(tmp_path):
    backend = make_backend(tmp_path)
    pane = host_echo(backend, tmp_path)
    try:
        text = "你好世界 hello\n第二行 line2\n\tindented@Nope#done"
        backend.submit_text(pane, text)
        backend.verify_alive_after_inject(pane)
        # Poll until the echo agent round-trips every line.
        import time

        deadline = time.monotonic() + 15.0
        seen = ""
        while time.monotonic() < deadline:
            raw = backend.capture(pane) or ""
            seen = raw.replace("\x1b", "")
            if "ECHO:你好世界 hello" in seen and "ECHO:第二行 line2" in seen and "ECHO:\tindented@Nope#done" in seen:
                break
            time.sleep(0.2)
        assert "ECHO:你好世界 hello" in seen
        assert "ECHO:第二行 line2" in seen
    finally:
        backend.close(pane)


@needs_tmux
def test_interrupt_kills_fixture_agent(tmp_path):
    import time

    backend = make_backend(tmp_path)
    pane = host_echo(backend, tmp_path)
    try:
        # The fixture exits on SIGINT; tmux translates C-c for the foreground process.
        backend.interrupt(pane, ("C-c",))
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if backend.liveness(pane) == "dead":
                break
            time.sleep(0.2)
        assert backend.liveness(pane) == "dead"
    finally:
        backend.close(pane)


@needs_tmux
def test_two_concurrent_writers_do_not_intermix(tmp_path):
    import time

    backend = make_backend(tmp_path)
    pane = host_echo(backend, tmp_path)
    try:
        payloads = [f"writer-{i}-" + "x" * 200 + "-你好" for i in range(2)]
        errors = []

        def writer(text):
            try:
                backend.submit_text(pane, text)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(p,)) for p in payloads]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert not errors
        deadline = time.monotonic() + 15.0
        seen = ""
        while time.monotonic() < deadline:
            raw = (backend.capture(pane) or "").replace("\x1b", "")
            # Long lines wrap at the pane width, splitting one logical line
            # across capture rows; compare on the de-wrapped text.
            seen = raw.replace("\n", "")
            if all(f"ECHO:{p}" in seen for p in payloads):
                break
            time.sleep(0.2)
        for payload in payloads:
            assert f"ECHO:{payload}" in seen
    finally:
        backend.close(pane)


@needs_tmux
def test_namespace_sockets_are_isolated(tmp_path):
    backend_a = make_backend(tmp_path, namespace=f"iso-a-{os.getpid()}")
    backend_b = make_backend(tmp_path, namespace=f"iso-b-{os.getpid()}")
    assert backend_a.socket != backend_b.socket
    pane_a = backend_a.host(
        (sys.executable, ECHO_AGENT), runtime="echo", cwd=str(tmp_path), ident=backend_a.new_ident()
    )
    try:
        # The sibling namespace cannot see or disturb this pane: its socket
        # has no such session, which reads as unknown/dead, never alive.
        assert backend_b.liveness(pane_a) in ("dead", "unknown")
        assert pane_a not in backend_b.owned_panes()
        assert pane_a in backend_a.owned_panes()
    finally:
        backend_a.close(pane_a)


@needs_tmux
def test_pane_input_lock_serializes_threads(tmp_path):
    backend = make_backend(tmp_path)
    pane = host_echo(backend, tmp_path)
    try:
        order = []
        started = threading.Event()

        def holder(tag, hold):
            started.wait(timeout=10)
            with backend.pane_lock(pane):
                order.append(f"{tag}-in")
                import time

                time.sleep(hold)
                order.append(f"{tag}-out")

        first = threading.Thread(target=holder, args=("a", 0.5))
        second = threading.Thread(target=holder, args=("b", 0.0))
        first.start()
        time.sleep(0.2)  # let the first thread take the lock before contending
        second.start()
        started.set()
        first.join(timeout=20)
        second.join(timeout=20)
        # One lock: the two critical sections never overlap.
        assert order in (["a-in", "a-out", "b-in", "b-out"], ["b-in", "b-out", "a-in", "a-out"])
    finally:
        backend.close(pane)


def test_socket_names_distinguish_case(tmp_path):
    """R6: case-distinct namespaces must never alias one server."""
    from agent_session_control.terminal import socket_name_for

    assert socket_name_for(str(tmp_path), "Review") != socket_name_for(str(tmp_path), "review")
    assert socket_name_for(str(tmp_path), "Review") == socket_name_for(str(tmp_path), "Review")
    assert socket_name_for(str(tmp_path / "a"), "review") != socket_name_for(str(tmp_path / "b"), "review")


def test_registry_filenames_carry_socket_identity(tmp_path):
    """R6: same-root namespaces share a record dir without sharing files."""
    first = make_backend(tmp_path, namespace="Review")
    second = make_backend(tmp_path, namespace="review")
    assert first.socket != second.socket
    assert first.registry._path("asc-review-x-12345678") != second.registry._path("asc-review-x-12345678")


@needs_tmux
def test_replacement_process_is_not_owned_or_killed(tmp_path):
    """R6: a stale name record never authorizes controlling a replacement."""
    backend = TmuxBackend("replacement", root=str(tmp_path / "home"))
    pane = backend.host(("/bin/sleep", "60"), runtime="fixture", cwd=str(tmp_path), ident="f12a983b")
    try:
        assert backend.live_owned(pane), "the just-launched instance must verify"
        old = backend.current_instance(pane)
        assert old is not None
        # Kill outside the backend and recreate the same name independently:
        # the stale record must not cover the replacement.
        assert backend._run("kill-session", "-t", pane).returncode == 0
        assert backend._run("new-session", "-d", "-s", pane, "--", "/bin/sleep", "60").returncode == 0
        new = backend.current_instance(pane)
        assert new is not None and new != old
        assert backend.liveness(pane) == "alive"
        assert not backend.live_owned(pane), "a replacement process is never live-owned"
        with pytest.raises(TmuxError) as excinfo:
            backend.close(pane)
        assert excinfo.value.code == "not_owned"
        assert backend.liveness(pane) == "alive", "the refused close must not disturb the replacement"
    finally:
        backend._run("kill-session", "-t", pane)
        backend._run("kill-server")
        backend.registry.forget(pane)
    assert backend.liveness(pane) == "dead"
    assert backend._run("list-sessions").returncode != 0, "no server may remain"


@needs_tmux
def test_close_dead_owned_pane_forgets_record(tmp_path):
    backend = make_backend(tmp_path)
    pane = backend.host((sys.executable, ECHO_AGENT), runtime="echo", cwd=str(tmp_path), ident=backend.new_ident())
    assert backend.live_owned(pane)
    assert backend._run("kill-session", "-t", pane).returncode == 0
    assert backend.liveness(pane) == "dead"
    assert backend.close(pane) is True
    assert backend.registry.lookup(pane) is None
    backend._run("kill-server")

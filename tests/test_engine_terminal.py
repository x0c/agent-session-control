"""Engine terminal tests: owned tmux hosting on unique namespace sockets.

Real-path tests host a disposable echo agent (no model, no login) in panes on
a per-test tmux socket. They never enumerate or touch user sessions: every
pane is created by the backend under test and killed in teardown.
"""

import os
import shutil
import signal
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
    agy_trust = "\n".join(
        [
            "Do you trust the files in this folder?",
            "  1. Yes, trust this workspace",
            "  ↑/↓ Navigate · enter Confirm",
        ]
    )
    assert blocked_input_cause(trust) is not None
    assert blocked_input_cause(agy_trust) is not None
    assert blocked_input_cause("❯ hello\nsome output") is None
    assert blocked_input_cause(None) is None
    assert blocked_input_cause("") is None


def test_claude_workspace_trust_dialog_is_refused():
    """The real first-launch trust dialog of claude 2.1.303 must be refused.

    Text captured verbatim (tmux capture-pane, fresh CLAUDE_CONFIG_DIR,
    directory never opened before, 120x40 pane) from the pane bottom::

        \u276f  No, exit
           Yes, I trust this folder
        Enter to confirm \u00b7 Esc to cancel

    "No, exit" is the highlighted default: pasting a prompt plus Enter would
    confirm it and quit claude, so the injection layer must refuse first.
    """
    dialog = "\n".join(
        [
            " Accessing workspace:",
            "",
            " /private/tmp/some/proj",
            "",
            " Quick safety check: Is this a project you created or one you trust?",
            "",
            " Claude Code'll be able to read, edit, and execute files here.",
            "",
            " Security guide",
            "",
            " \u276f  No, exit",
            "    Yes, I trust this folder",
            "",
            " Enter to confirm \u00b7 Esc to cancel",
        ]
    )
    cause = blocked_input_cause(dialog)
    assert cause is not None
    assert "workspace-trust" in cause
    # The same dialog with live SGR styling (backend.capture keeps escapes).
    styled = "\x1b[36m\u276f\x1b[0m  No, exit\n Yes, I trust this folder\n Enter to confirm \u00b7 Esc to cancel"
    assert blocked_input_cause(styled) is not None
    # The numbered settings-driven variant ("1. Yes, proceed" / "2. No, exit"),
    # reported for claude's settings.local.json trust prompt.
    numbered = "\n".join(
        [
            " Do you trust the files in this folder?",
            " Claude Code may read, write, or execute files contained in this directory.",
            " \u276f 1. Yes, proceed",
            "   2. No, exit",
        ]
    )
    assert blocked_input_cause(numbered) is not None


def test_dialog_guard_does_not_fire_on_normal_tui():
    """A normal claude prompt footer must never read as a blocking dialog."""
    normal = "\n".join(
        [
            "❯ Reply with ACK and nothing else.",
            "✻ Brewed for 3s · done 5:00 PM",
            "─" * 100,
            '❯ Try "edit <filepath> to..."',
            "─" * 100,
            "  Claude-Opus | high",
            "  ⏵⏵ auto mode on (shift+tab to cycle)",
        ]
    )
    assert blocked_input_cause(normal) is None
    # "No, exit" alone (no matching yes-option line in the footer window) is
    # not dialog proof: the pair is the anchor.
    orphan = "the agent answered No, exit\n❯ next prompt"
    assert blocked_input_cause(orphan) is None


def test_dialog_guard_keeps_footer_window_semantics():
    """Dialog text above the bottom-6 non-empty lines stays invisible to the guard."""
    lines_above = [f"filler line {i}" for i in range(6)]
    dialog = "\n".join(
        [
            "❯  No, exit",
            "   Yes, I trust this folder",
            "Enter to confirm · Esc to cancel",
            *lines_above,
        ]
    )
    assert blocked_input_cause(dialog) is None


def test_claude_first_run_onboarding_theme_picker_is_refused():
    """The real first-run onboarding of claude 2.1.303 must be refused.

    Text captured verbatim (tmux capture-pane, completely fresh
    CLAUDE_CONFIG_DIR, 120x40 pane): the theme picker owns the keyboard —
    a pasted brief plus Enter selects a theme instead of reaching the
    conversation, and the turn then dies unattributed. The welcome banner
    puts the "run /theme" hint 14 non-empty lines above the syntax-theme
    footer, which is exactly why the onboarding window is wider than the
    dialog footer window.
    """
    banner = [
        "Welcome to Claude Code v2.1.303",
        "." * 58,
        *["  ░░░░  ███▓▓░   *" for _ in range(13)],
        ".......█ █   █ █..........................................",
        " Let's get started.",
        " Choose the text style that looks best with your terminal",
    ]
    picker = [
        " To change this later, run /theme",
        "     Auto (match terminal)",
        " ❯ ✔ Dark mode",
        "     Light mode",
        "     Dark mode (colorblind-friendly)",
        "     Light mode (colorblind-friendly)",
        "     Dark mode (ANSI colors only)",
        "     Light mode (ANSI colors only)",
        " ╌" * 12,
        "  1  function greet() {",
        '  2 -  console.log("Hello, World!");',
        '  2 +  console.log("Hello, Claude!");',
        "  3  }",
        " ╌" * 12,
        "  Syntax theme: Monokai Extended (ctrl+t to disable)",
    ]
    onboarding = "\n".join([*banner, *picker])
    cause = blocked_input_cause(onboarding)
    assert cause is not None
    assert "onboarding" in cause
    styled_footer = (
        "\x1b[38;5;246m  Syntax\x1b[39m \x1b[38;5;246mtheme:\x1b[39m Monokai Extended "
        "\x1b[38;5;246m(ctrl+t\x1b[39m \x1b[38;5;246mto\x1b[39m \x1b[38;5;246mdisable)\x1b[39m"
    )
    styled = onboarding.replace("  Syntax theme: Monokai Extended (ctrl+t to disable)", styled_footer)
    assert blocked_input_cause(styled) is not None


def test_claude_first_run_login_selection_is_refused():
    """The onboarding login-method step of claude 2.1.303 must be refused.

    Text captured verbatim (fresh CLAUDE_CONFIG_DIR after the theme step):
    the highlighted login option would consume a pasted brief plus Enter.
    """
    login = "\n".join(
        [
            *["  ░░░░  ███▓▓░" for _ in range(10)],
            ".......█ █   █ █..........................................",
            " Claude Code can be used with your Claude subscription or billed based on API usage "
            "through your Console account.",
            " Select login method:",
            " ❯ 1. Claude account with subscription · Pro, Max, Team, or Enterprise",
            "   2. Anthropic Console account · API usage billing",
            "   3. 3rd-party platform · Amazon Bedrock, Microsoft Foundry, Google Vertex AI",
        ]
    )
    cause = blocked_input_cause(login)
    assert cause is not None
    assert "login" in cause


def test_onboarding_guard_needs_both_anchors():
    """Neither onboarding anchor alone is refusal proof."""
    footer_only = "❯ a normal prompt\n  Syntax theme: Monokai Extended (ctrl+t to disable)"
    assert blocked_input_cause(footer_only) is None
    hint_only = "\n".join(
        [
            " To change this later, run /theme",
            *[f"filler {i}" for i in range(18)],
            "❯ a normal prompt",
        ]
    )
    assert blocked_input_cause(hint_only) is None
    # A conversation that merely mentions console accounts never refuses:
    # the "Select login method:" anchor line is required too.
    chatter = "\n".join(
        [
            "user: how do I use an Anthropic Console account?",
            "assistant: open the console and create an API key.",
            "❯ next prompt",
        ]
    )
    assert blocked_input_cause(chatter) is None


def test_submit_text_refuses_claude_trust_dialog_before_any_byte(tmp_path):
    """A submit into a visible trust dialog must fail certainly, not inject.

    The controller's pre-submit check races the runtime's first paint; the
    backend re-checks under the pane lock right before the paste.
    """
    backend = make_backend(tmp_path, namespace="dialog-refuse-a")
    pane = "asc-dialog-claude-00000001"
    backend.capture = lambda target: (
        " \u276f  No, exit\n   Yes, I trust this folder\n Enter to confirm \u00b7 Esc to cancel"
    )
    pasted = []

    def fail_paste(target, text):
        pasted.append(text)
        raise AssertionError("paste must never run while a dialog owns the pane")

    backend._paste_locked = fail_paste
    with pytest.raises(TmuxError) as excinfo:
        backend.submit_text(pane, "Reply with ACK and nothing else.")
    assert excinfo.value.code == "startup_dialog"
    assert "workspace-trust" in excinfo.value.message
    assert not pasted


def test_submit_text_refuses_enter_when_dialog_appears_after_paste(tmp_path):
    """A dialog appearing between paste and Enter must not receive the Enter."""
    from agent_session_control.terminal import InjectionResult

    backend = make_backend(tmp_path, namespace="dialog-refuse-b")
    pane = "asc-dialog-claude-00000002"
    captures = {"count": 0}

    def fake_capture(target):
        captures["count"] += 1
        if captures["count"] < 2:
            return "\u2500" * 100 + '\n❯ Try "edit <filepath> to..."\n' + "\u2500" * 100
        return " \u276f  No, exit\n   Yes, I trust this folder\n Enter to confirm \u00b7 Esc to cancel"

    backend.capture = fake_capture
    backend._paste_locked = lambda target, text: InjectionResult(True)
    enter_sent = []

    def fake_send(target, *args):
        enter_sent.append(args)
        return InjectionResult(True)

    backend._send_detailed = fake_send
    with pytest.raises(TmuxError) as excinfo:
        backend.submit_text(pane, "hello")
    assert excinfo.value.code == "startup_dialog"
    assert not enter_sent


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
def test_close_signals_the_exact_recorded_process_not_the_name(tmp_path):
    """close terminates the recorded pane process, never a name/id-targeted tmux kill."""
    backend = make_backend(tmp_path)
    pane = host_echo(backend, tmp_path)
    record = backend.registry.lookup(pane)
    assert record and int(record["pane_pid"]) > 0
    tmux_kills: list[tuple[str, ...]] = []
    signals: list[tuple[int, int]] = []
    real_run, real_kill = backend._run, os.kill

    def spy_run(*args, **kwargs):
        if args and args[0] in ("kill-pane", "kill-session"):
            tmux_kills.append(args)
        return real_run(*args, **kwargs)

    def spy_kill(pid, sig):
        signals.append((pid, sig))
        return real_kill(pid, sig)

    backend._run = spy_run
    os.kill = spy_kill
    try:
        assert backend.close(pane) is True
    finally:
        backend._run = real_run
        os.kill = real_kill
        if backend.liveness(pane) == "alive":
            backend._run("kill-session", "-t", pane)
            backend.registry.forget(pane)
        backend._run("kill-server")
    assert backend.liveness(pane) == "dead"
    assert not tmux_kills, "close must never kill through the reusable session name or pane id"
    assert signals, "close must signal the recorded pane process"
    assert all(pid == int(record["pane_pid"]) for pid, _ in signals), "signals target the recorded process only"
    assert signals[0][1] == signal.SIGTERM, "graceful termination is attempted before force"


@needs_tmux
def test_close_never_kills_a_replacement_after_the_ownership_check(tmp_path):
    """A name-reuse race inside the check-to-kill window must spare the replacement."""
    backend = make_backend(tmp_path)
    pane = backend.host(("/bin/sleep", "60"), runtime="fixture", cwd=str(tmp_path), ident=backend.new_ident())
    old = backend.current_instance(pane)
    assert old is not None
    real_live_owned = backend.live_owned

    def racing_live_owned(target):
        assert real_live_owned(target), "the original instance must verify first"
        # The verified pane dies and its name is instantly reused by an
        # unrelated process, inside the window before close() issues its kill.
        backend._run("kill-session", "-t", pane)
        backend._run("new-session", "-d", "-s", pane, "--", "/bin/sleep", "60")
        return True

    try:
        backend.live_owned = racing_live_owned
        assert backend.close(pane) is False, "an unprovable kill keeps the record and reports failure"
        assert backend.liveness(pane) == "alive", "the replacement must survive the raced close"
        assert backend.current_instance(pane) not in (None, old)
        assert backend.registry.lookup(pane) is not None, "the failed close keeps ownership recoverable"
    finally:
        backend._run("kill-session", "-t", pane)
        backend._run("kill-server")
        backend.registry.forget(pane)
    assert backend._run("list-sessions").returncode != 0, "no server may remain"


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


@needs_tmux
@pytest.mark.skipif(shutil.which("agy") is None, reason="agy is not installed")
def test_antigravity_disposable_tmux_composer_probe(tmp_path):
    """Host real ``agy`` in an isolated tmux socket without mutating user settings or sending prompts."""
    import hashlib
    import json
    from pathlib import Path

    from agent_session_control.runtime import build_create_plan, composer_region
    from agent_session_control.terminal import plain_text

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".sesskit-ignore").write_text("", encoding="utf-8")

    settings_path = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
    before_hash = hashlib.sha256(settings_path.read_bytes()).hexdigest() if settings_path.is_file() else None

    plan = build_create_plan(
        "antigravity",
        str(proj),
        auto_approve=False,
        extra_args=("--settings", json.dumps({"trustedWorkspaces": [str(proj.resolve()), str(proj)]})),
    )
    backend = make_backend(tmp_path, namespace="agy-probe")
    pane = backend.host(plan.argv, runtime="antigravity", cwd=str(proj), ident=backend.new_ident())
    try:
        deadline = time.monotonic() + 20.0
        region = None
        while time.monotonic() < deadline:
            screen = plain_text(backend.capture(pane))
            region = composer_region("antigravity", screen)
            if region is not None and region.lstrip().startswith(">"):
                break
            time.sleep(0.3)
        assert region is not None and region.lstrip().startswith(">"), (
            f"composer not ready: {plain_text(backend.capture(pane))!r}"
        )
        assert blocked_input_cause(backend.capture(pane)) is None
        pasted = backend.paste_detailed(pane, "draft probe")
        assert pasted.ok, pasted
        hold_deadline = time.monotonic() + 5.0
        holds = False
        while time.monotonic() < hold_deadline:
            holds = bool(
                backend.composer_holds(
                    lambda s: composer_region("antigravity", s),
                    backend.capture(pane),
                    "draft probe",
                )
            )
            if holds:
                break
            time.sleep(0.2)
        assert holds, f"pasted text not reflected in composer: {plain_text(backend.capture(pane))!r}"
        updated = composer_region("antigravity", plain_text(backend.capture(pane)))
        assert updated is not None and "draft probe" in updated
    finally:
        assert backend.close(pane) is True
        backend._run("kill-server")
    assert backend.liveness(pane) == "dead"
    after_hash = hashlib.sha256(settings_path.read_bytes()).hexdigest() if settings_path.is_file() else None
    assert after_hash == before_hash

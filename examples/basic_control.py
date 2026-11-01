"""Independent external consumer: plan, create, send, observe, cancel, resume.

Uses only the installed ``agent_session_control`` public API -- never
downstream-host modules, SessKit internals, UI frameworks or vendor SDKs.
Control state lives in a disposable root with a unique namespace per run.

Cost and login gates:

- Read-only phases (launch plans, ``version``/``describe``/``doctor``/dry
  runs) are free and run by default.
- Live phases host a real terminal pane and submit one brief turn. They run
  only with ``--live`` and may spend model quota, require the selected
  runtime's executable plus login/quota, and use the default tmux backend on
  macOS/Linux. Missing executables, login or quota are reported as limits,
  never as passes.
- Approval policy is never relaxed here: every plan and session create uses
  the ``auto_approve=False`` default.

Native-history note: the disposable project directory and control root
isolate *control* state only. Hosting a native CLI may still record native
history under that runtime's standard locations. ``close_session`` releases
the owned pane but never deletes native history.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import NoReturn

BLOCKED = 3
RUNTIMES = ("claude", "codex", "opencode", "cursor", "pi")


def fail_blocked(message: str) -> NoReturn:
    print(f"BLOCKED: {message}", file=sys.stderr)
    raise SystemExit(BLOCKED)


def phase_plans(cwd: str) -> None:
    try:
        from agent_session_control import runtime as rt
    except ImportError:
        fail_blocked("foundation module agent_session_control.runtime is not implemented yet")
    for runtime_id in RUNTIMES:
        profile = rt.runtime_profile(runtime_id)
        print(f"profile[{runtime_id}]: executable={profile.get('executable')} available={profile.get('available')}")
        plan = rt.build_create_plan(runtime_id, cwd)
        assert isinstance(plan.argv, tuple) and plan.argv, "LaunchPlan.argv must be a nonempty tuple"
        assert plan.cwd == cwd and dict(plan.env) == {}, "fresh plan keeps cwd, carries no stray env"
        assert isinstance(plan.to_dict(), dict), "plan must offer a redacted dict projection"
        print(f"create-plan[{runtime_id}]: {' '.join(plan.argv[:3])}...")


def phase_cli_envelope(cwd: str) -> None:
    """Capture one real CLI envelope per read-only command; assert the contract."""
    commands = [
        [sys.executable, "-m", "agent_session_control", "version", "--json"],
        [sys.executable, "-m", "agent_session_control", "describe", "--json"],
        [sys.executable, "-m", "agent_session_control", "doctor", "--json"],
        [
            sys.executable,
            "-m",
            "agent_session_control",
            "plan",
            "create",
            "--runtime",
            "codex",
            "--cwd",
            cwd,
            "--json",
        ],
    ]
    for argv in commands:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, f"{' '.join(argv[3:5])} failed: {proc.stderr[-1000:]}"
        try:
            body = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"CLI stdout is not JSON for {' '.join(argv)}: {proc.stdout[-500:]}") from exc
        assert body.get("ok") is True, f"expected ok envelope: {body}"
        assert "error" in body and "meta" in body, f"envelope must carry ok/data/error/meta: {body}"
        print(f"cli[{argv[4] if len(argv) > 4 else argv[3]}]: ok=true version={body['meta'].get('version')}")


async def phase_control(runtime_id: str, project: str, root: Path, namespace: str, prompt: str) -> None:
    try:
        from agent_session_control.controller import LocalController
        from agent_session_control.errors import ControlError
    except ImportError:
        fail_blocked("engine module agent_session_control.controller is not implemented yet")
    controller = LocalController(root=root, namespace=namespace)
    handle = None
    try:
        handle = await controller.create(runtime_id, project)
        assert handle.id and handle.id.startswith("ses_"), "create must return an opaque ses_ id"
        assert handle.id != (handle.native_id or object()), "control id stays opaque"
        assert handle.runtime == runtime_id, "returned handle must carry the requested runtime"
        print(f"session: id={handle.id} runtime={handle.runtime} native_id={handle.native_id}")
        request_id = f"example-{uuid.uuid4().hex[:12]}"
        blocks = [{"type": "text", "text": prompt}]
        run_id = await controller.start_turn(handle.id, blocks, request_id=request_id)
        assert isinstance(run_id, str) and run_id.startswith("run_"), "start_turn must return a run_ id"
        print(f"run: {run_id}")
        again = await controller.start_turn(handle.id, blocks, request_id=request_id)
        assert again == run_id, "immutable request id must converge, not fork"
        try:
            result = await controller.wait(run_id, timeout=120.0)
        except ControlError as exc:
            if exc.code != "wait_timeout":
                raise
            print("wait timed out; cancelling the exact run (a wait timeout never cancels by itself)")
            await controller.interrupt(run_id)
            result = await controller.wait(run_id, timeout=120.0)
        assert result.run_id == run_id, "wait must settle the submitted run"
        print(
            f"result: state={result.state} evidence={result.evidence} "
            f"final={bool(result.final_response)} stop={result.stop_reason}"
        )
        if result.final_response:
            print(f"final_response: {result.final_response[:200]}")
        resumed = await controller.resume(handle.id)
        assert resumed.id == handle.id, "resume must return the same control id"
        print(f"resumed handle: id={resumed.id} native_id={resumed.native_id}")
    finally:
        # Public-API cleanup only: close the owned pane; history is kept.
        if handle is not None:
            try:
                await controller.close_session(handle.id)
            except Exception as exc:  # noqa: BLE001 - best-effort close in examples
                print(f"close_session: {type(exc).__name__}: {exc}")
        print("closed (owned pane released; native history, if any, is kept)")


def phase_restart(root: Path, namespace: str) -> None:
    try:
        from agent_session_control.controller import LocalController
    except ImportError:
        fail_blocked("engine module agent_session_control.controller is not implemented yet")

    async def _reopen() -> None:
        first = LocalController(root=root, namespace=namespace)
        listing = await first.list_sessions()
        assert listing.get("sessions") is not None, "list_sessions must return a sessions page"
        print(f"reopened controller sees sessions: {len(listing['sessions'])}")
        first.reap_children()
        second = LocalController(root=root, namespace=namespace)
        listing = await second.list_sessions()
        assert listing.get("sessions") is not None
        print(f"second reopen still consistent: {len(listing['sessions'])}")
        second.reap_children()

    asyncio.run(_reopen())
    print("restart recovery observed without replaying uncertain input")


def phase_acp_entry() -> None:
    try:
        from agent_session_control.acp import ControlAgent  # noqa: F401
    except ImportError:
        fail_blocked("ACP module agent_session_control.acp is not implemented yet")
    print("ACP adapter present: agent_session_control.acp.ControlAgent")
    print("Verified ACP serving (stdio initialize handshake) is covered by examples/native_smoke.py --skip-native")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independent agent_session_control consumer example")
    parser.add_argument("--runtime", default="claude", help="runtime id for plan/controller demo")
    parser.add_argument("--cwd", default=None, help="project dir (default: disposable temp dir)")
    parser.add_argument(
        "--prompt",
        default="Summarize the repository layout in five bullets.",
        help="initial brief for the controlled turn (live only)",
    )
    parser.add_argument("--root", default=None, help="state root (default: disposable temp dir)")
    parser.add_argument("--namespace", default=None, help="store namespace (default: unique per run)")
    parser.add_argument(
        "--live",
        action="store_true",
        help="run live control/restart phases (spends model quota; needs runtime login)",
    )
    parser.add_argument(
        "--skip-live",
        action="store_true",
        help="deprecated alias: live phases are skipped unless --live is given",
    )
    args = parser.parse_args(argv)

    if args.runtime not in RUNTIMES:
        print(f"unknown runtime: {args.runtime} (expected one of {', '.join(RUNTIMES)})", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="asc-example-") as scratch:
        scratch_path = Path(scratch)
        if args.cwd is not None:
            project_path = Path(args.cwd).expanduser()
            if not project_path.is_absolute():
                print(f"--cwd must be absolute: {args.cwd}", file=sys.stderr)
                return 2
            if not project_path.is_dir():
                print(f"--cwd does not exist: {project_path}", file=sys.stderr)
                return 2
            project = str(project_path.resolve())
        else:
            project = str(scratch_path / "project")
            Path(project).mkdir(parents=True, exist_ok=True)
        root = Path(args.root).expanduser() if args.root else scratch_path / "control"
        namespace = args.namespace or f"example-{uuid.uuid4().hex[:8]}"

        phase_plans(project)
        phase_cli_envelope(project)
        if not args.live:
            print("live phases skipped (pass --live to host a real pane and submit one turn)")
            return 0
        asyncio.run(phase_control(args.runtime, project, root, namespace, args.prompt))
        phase_restart(root, namespace)
        phase_acp_entry()
        print("example complete: control state was disposable; native history, if any, is kept")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

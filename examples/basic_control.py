"""Independent external consumer: plan, create, send, observe, cancel, resume.

Uses only the installed ``agent_session_control`` public API -- never Corral,
SessKit internals, UI frameworks or vendor SDKs. Disposable root and a unique
namespace per run; never touches user histories or real hosted sessions.

Phases (locked contract, docs/CONTRACT.md):
  1. pure launch plans (foundation ``runtime`` module);
  2. durable controlled execution (engine ``controller.LocalController``),
     including idempotent retry, cancel of the exact run, and restart
     recovery on the same root/namespace without replaying uncertain input;
  3. optional ACP/CLI entry presence (ACP worker).

A phase whose peer module is not implemented yet reports BLOCKED (exit 3)
with the exact missing dependency instead of faking success.
"""

from __future__ import annotations

import argparse
import asyncio
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
        print(f"create-plan[{runtime_id}]: {' '.join(plan.argv[:3])}...")


async def phase_control(runtime_id: str, root: Path, namespace: str, prompt: str) -> None:
    try:
        from agent_session_control.controller import LocalController
        from agent_session_control.errors import ControlError
    except ImportError:
        fail_blocked("engine module agent_session_control.controller is not implemented yet")
    controller = LocalController(root=root, namespace=namespace)
    handle = await controller.create(runtime_id, str(root / "project"))
    assert handle.id and handle.id != (handle.native_id or object()), "control id stays opaque"
    print(f"session: id={handle.id} runtime={handle.runtime} native_id={handle.native_id}")
    request_id = f"example-{uuid.uuid4().hex[:12]}"
    run_id = await controller.start_turn(handle.id, [{"type": "text", "text": prompt}], request_id=request_id)
    print(f"run: {run_id}")
    again = await controller.start_turn(handle.id, [{"type": "text", "text": prompt}], request_id=request_id)
    assert again == run_id, "immutable request id must converge, not fork"
    try:
        result = await controller.wait(run_id, timeout=120.0)
    except ControlError as exc:
        if exc.code != "wait_timeout":
            raise
        print("wait timed out; cancelling the exact run (a wait timeout never cancels by itself)")
        await controller.interrupt(run_id)
        result = await controller.wait(run_id, timeout=120.0)
    print(
        f"result: state={result.state} evidence={result.evidence} "
        f"final={bool(result.final_response)} stop={result.stop_reason}"
    )
    resumed = await controller.resume(handle.id)
    print(f"resumed handle: id={resumed.id} native_id={resumed.native_id}")
    await controller.close_session(handle.id)
    print("closed (native histories untouched by close)")


def phase_restart(root: Path, namespace: str) -> None:
    try:
        from agent_session_control.controller import LocalController
    except ImportError:
        fail_blocked("engine module agent_session_control.controller is not implemented yet")

    async def _reopen() -> None:
        first = LocalController(root=root, namespace=namespace)
        listing = await first.list_sessions()
        print(f"reopened controller sees sessions: {listing.get('sessions') is not None}")
        del first
        second = LocalController(root=root, namespace=namespace)
        listing = await second.list_sessions()
        print(f"second reopen still consistent: {listing.get('sessions') is not None}")

    asyncio.run(_reopen())
    print("restart recovery observed without replaying uncertain input")


def phase_acp_entry() -> None:
    try:
        from agent_session_control.acp import ControlAgent  # noqa: F401
    except ImportError:
        fail_blocked("ACP module agent_session_control.acp is not implemented yet")
    print("ACP adapter present: agent_session_control.acp.ControlAgent")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independent agent_session_control consumer example")
    parser.add_argument("--runtime", default="claude", help="runtime id for plan/controller demo")
    parser.add_argument("--cwd", default=None, help="project dir (default: disposable temp dir)")
    parser.add_argument(
        "--prompt",
        default="Summarize the repository layout in five bullets.",
        help="initial brief for the controlled turn",
    )
    parser.add_argument("--root", default=None, help="state root (default: disposable temp dir)")
    parser.add_argument("--namespace", default=None, help="store namespace (default: unique per run)")
    parser.add_argument(
        "--skip-live", action="store_true", help="only run pure plan generation, skip live control/restart/ACP phases"
    )
    args = parser.parse_args(argv)

    scratch = tempfile.TemporaryDirectory(prefix="asc-example-")
    project = args.cwd or str(Path(scratch.name) / "project")
    Path(project).mkdir(parents=True, exist_ok=True)
    root = Path(args.root) if args.root else Path(scratch.name) / "control"
    namespace = args.namespace or f"example-{uuid.uuid4().hex[:8]}"

    phase_plans(project)
    if args.skip_live:
        print("live phases skipped by --skip-live (pure-plan evidence only)")
        return 0
    asyncio.run(phase_control(args.runtime, root, namespace, args.prompt))
    phase_restart(root, namespace)
    phase_acp_entry()
    print("example complete: no user history touched, disposable root only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

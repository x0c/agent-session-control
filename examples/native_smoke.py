"""Bounded native acceptance: clean installed wheel plus production defaults.

Runs OUTSIDE both repos against the installed (non-editable) wheel:

1. pure launch plans for all five active runtimes;
2. production ``LocalController`` create, liveness read and close per
   available runtime (no prompt is ever sent: hosting evidence needs no
   model, login or quota; unavailable executables and login/quota gaps are
   recorded per runtime, never counted as passes);
3. a real ACP stdio ``initialize`` handshake with the official SDK client
   (capability read only; no session creation, no model).

Bounds: 90 seconds per runtime leg, a unique namespace/socket per leg,
disposable control root/cwd, scrubbed ``TMUX`` env. Prints per-leg JSON
outcomes; exits nonzero when a leg fails. Native turns, model selection,
real cancellation and per-session MCP remain login/quota-gated follow-ups.

Isolation note: the disposable project directory and control root isolate
*control* state only. Hosting a native CLI may still record native history
under that runtime's standard locations even though no prompt is sent.
``close_session`` releases the owned pane but never deletes native
history. A scratch directory is removed only on verified release
(``closed == \"closed\"`` with no recorded error); otherwise it is retained
and reported in ``retained_root`` for recovery.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

RUNTIMES = ("claude", "codex", "opencode", "cursor", "pi")
LEG_TIMEOUT = 90.0


def scrubbed_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("TMUX", None)
    return env


def leg_plans() -> dict:
    from agent_session_control import runtime as rt

    legs = {}
    for rid in RUNTIMES:
        profile = rt.runtime_profile(rid)
        plan = rt.build_create_plan(rid, tempfile.gettempdir())
        legs[rid] = {
            "executable": profile.get("executable"),
            "available": bool(profile.get("available")),
            "argv_head": list(plan.argv)[:3],
            "argv_is_tuple": isinstance(plan.argv, tuple),
        }
    return legs


async def leg_native(rid: str) -> dict:
    """Production-default create, liveness and close for one runtime.

    Failure contract (never a pass on doubt): any recorded ``error``,
    a missing listing, a non-``alive`` liveness while hosted, or a
    failed/unreadable close readback counts as failure. ``closed`` is
    never invented: it is set only from a readable post-close listing
    whose status is ``closed``. The scratch directory (which contains
    the control root) is removed only on verified release; otherwise it
    is retained and reported in ``retained_root``.
    """
    import shutil as _shutil

    from agent_session_control.controller import LocalController

    outcome: dict = {"runtime": rid}
    scratch = tempfile.mkdtemp(prefix=f"asc-smoke-{rid}-")
    project = str(Path(scratch, "project").resolve())
    os.makedirs(project, exist_ok=True)
    root = str(Path(scratch, "control").resolve())
    outcome["control_root"] = root
    outcome["retained_root"] = None
    namespace = f"smoke-{rid}-{uuid.uuid4().hex[:8]}"
    started = time.monotonic()
    controller = LocalController(root=root, namespace=namespace)
    handle = None
    try:
        handle = await asyncio.wait_for(controller.create(rid, project), LEG_TIMEOUT)
        assert handle.id.startswith("ses_"), "create must return an opaque ses_ id"
        outcome["created"] = handle.id
        listing = await asyncio.wait_for(controller.list_sessions(), LEG_TIMEOUT)
        sessions = listing.get("sessions")
        assert isinstance(sessions, list), "list_sessions must return a sessions list"
        mine = [s for s in sessions if s.get("session_id") == handle.id]
        outcome["liveness"] = mine[0].get("pane_liveness") if mine else None
        outcome["listed"] = bool(mine)
        if not mine:
            raise AssertionError("create succeeded but session is missing from listing")
        if mine[0].get("pane_liveness") != "alive":
            raise AssertionError(f"expected pane_liveness 'alive' while hosted, got {mine[0].get('pane_liveness')!r}")
        await asyncio.sleep(3.0)
        listing2 = await asyncio.wait_for(controller.list_sessions(), LEG_TIMEOUT)
        sessions2 = listing2.get("sessions")
        assert isinstance(sessions2, list), "list_sessions must return a sessions list"
        mine2 = [s for s in sessions2 if s.get("session_id") == handle.id]
        outcome["still_listed"] = bool(mine2)
        outcome["pane_liveness_after_wait"] = mine2[0].get("pane_liveness") if mine2 else None
        if not mine2:
            raise AssertionError("session missing from listing after wait")
        if mine2[0].get("pane_liveness") != "alive":
            raise AssertionError(f"expected pane_liveness 'alive' after wait, got {mine2[0].get('pane_liveness')!r}")
    except Exception as exc:  # noqa: BLE001 - per-leg outcome, never a pass
        outcome["error"] = f"{getattr(exc, 'code', type(exc).__name__)}: {exc}"
    finally:
        if handle is not None:
            try:
                await asyncio.wait_for(controller.close_session(handle.id), LEG_TIMEOUT)
            except Exception as exc:  # noqa: BLE001 - per-leg outcome
                outcome.setdefault("error", f"close: {type(exc).__name__}: {exc}")
            else:
                try:
                    listing3 = await asyncio.wait_for(controller.list_sessions(), LEG_TIMEOUT)
                except Exception as exc:  # noqa: BLE001 - unreadable readback is failure, never invented
                    outcome.setdefault("error", f"close_readback_failed: {type(exc).__name__}: {exc}")
                else:
                    sessions3 = listing3.get("sessions")
                    if not isinstance(sessions3, list):
                        outcome.setdefault("error", "close_readback_failed: sessions list unreadable")
                    else:
                        mine3 = [s for s in sessions3 if s.get("session_id") == handle.id]
                        if not mine3:
                            outcome.setdefault("error", "close_readback_failed: session missing after close")
                        else:
                            outcome["closed"] = mine3[0].get("status")
                            if outcome["closed"] != "closed":
                                outcome.setdefault("error", f"close_failed: {outcome['closed']!r}")
        outcome["elapsed_s"] = round(time.monotonic() - started, 1)
        verified = ("error" not in outcome) and (
            (handle is not None and outcome.get("closed") == "closed")
            or (handle is None and outcome.get("created") is None)
        )
        if verified:
            _shutil.rmtree(scratch, ignore_errors=True)
            outcome["retained_root"] = None
        else:
            # Unconfirmed cleanup: retain scratch/control root for recovery.
            outcome["retained_root"] = scratch
    if outcome.get("closed") != "closed" and "error" not in outcome:
        outcome["error"] = f"close_failed: {outcome.get('closed')}"
    return outcome


def _native_leg_failed(leg: dict) -> bool:
    """Failure predicate shared by ``main``: doubt never passes."""
    if leg.get("skipped"):
        return False
    if "error" in leg:
        return True
    if leg.get("closed") != "closed":
        return True
    if not leg.get("listed") or not leg.get("still_listed"):
        return True
    if leg.get("liveness") != "alive" or leg.get("pane_liveness_after_wait") != "alive":
        return True
    if leg.get("retained_root"):
        return True
    return False


async def leg_stdio(rid: str) -> dict:
    """Real ACP stdio initialize handshake against the installed entry."""
    from acp import PROTOCOL_VERSION, spawn_agent_process

    outcome: dict[str, object] = {"runtime": rid}

    class Client:
        def __init__(self) -> None:
            self.conn: object | None = None

        def on_connect(self, conn: object) -> None:
            self.conn = conn

        async def session_update(self, session_id: str, update: object, **kwargs: object) -> None:
            return None

    env = scrubbed_env()
    env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    started = time.monotonic()
    try:

        async def _go() -> None:
            async with spawn_agent_process(
                Client(),
                sys.executable,
                "-m",
                "agent_session_control",
                "acp",
                "--runtime",
                rid,
                env=env,
            ) as (conn, _process):
                resp = await asyncio.wait_for(conn.initialize(PROTOCOL_VERSION), 60)
                outcome["protocol_version"] = getattr(resp, "protocol_version", None)
                caps = getattr(resp, "agent_capabilities", None)
                outcome["has_capabilities"] = caps is not None

        await asyncio.wait_for(_go(), 120)
    except Exception as exc:  # noqa: BLE001 - per-leg outcome, never a pass
        outcome["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        outcome["elapsed_s"] = round(time.monotonic() - started, 1)
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded native smoke over the installed wheel")
    parser.add_argument("--runtime", default=None, help="single runtime leg (default: all available)")
    parser.add_argument("--skip-native", action="store_true", help="skip real-process legs")
    parser.add_argument("--skip-stdio", action="store_true", help="skip the ACP stdio handshake")
    args = parser.parse_args(argv)

    report: dict = {"plans": leg_plans()}
    print(json.dumps({"plans": report["plans"]}, indent=1))
    failures = 0
    if not args.skip_native:
        wanted = [args.runtime] if args.runtime else list(RUNTIMES)
        native = {}
        for rid in wanted:
            if not shutil.which(
                {"claude": "claude", "codex": "codex", "opencode": "opencode", "cursor": "agent", "pi": "pi"}[rid]
            ):
                native[rid] = {"runtime": rid, "skipped": "executable not installed"}
                continue
            try:
                leg = asyncio.run(asyncio.wait_for(leg_native(rid), LEG_TIMEOUT + 60))
            except Exception as exc:  # noqa: BLE001 - per-leg outcome
                leg = {"runtime": rid, "error": f"{type(exc).__name__}: {exc}"}
            native[rid] = leg
            print(json.dumps(leg))
            if _native_leg_failed(leg):
                failures += 1
        report["native"] = native
    if not args.skip_stdio:
        try:
            leg = asyncio.run(leg_stdio(args.runtime or "claude"))
        except Exception as exc:  # noqa: BLE001 - per-leg outcome
            leg = {"runtime": args.runtime or "claude", "error": f"{type(exc).__name__}: {exc}"}
        report["stdio"] = leg
        print(json.dumps(leg))
        if "error" in leg or leg.get("protocol_version") != 1 or not leg.get("has_capabilities"):
            failures += 1
    print(f"smoke done: failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

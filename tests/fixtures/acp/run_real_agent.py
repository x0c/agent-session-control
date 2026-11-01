"""Stdio driver for ACP baseline tests: production controller, doubled terminal.

Usage: run_real_agent.py --runtime <id> --root <dir> --namespace <ns> [--fixture park|history]

Runs the production ``serve_stdio`` path (official SDK framing) over the real
engine: real session registry, operation store and runtime plans on the given
disposable root. Only terminal hosting is doubled (``ScriptedTerminal``:
records hosting, never touches tmux, user sessions or model APIs, never
spends quota or needs logins). The ``history`` fixture additionally serves a
canned deterministic snapshot and background-binds it onto created sessions
so ``session/load`` replays without native history. Never touches real panes.
"""

import argparse
import asyncio
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from real_helpers import (  # noqa: E402  # pyrefly: ignore[missing-import]
    CannedHistoryObserver,
    ScriptedTerminal,
    mcp_profile,
    start_canned_binder,
)

from agent_session_control.acp import serve_stdio  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", default="t")
    parser.add_argument("--root", default=None)
    parser.add_argument("--namespace", default="baseline")
    parser.add_argument("--fixture", default="park", choices=("park", "history"))
    args = parser.parse_args()
    root = Path(args.root) if args.root else Path(tempfile.mkdtemp(prefix="asc-real-acp-"))
    profiles = {"t": mcp_profile()} if args.runtime == "t" else None
    observer = CannedHistoryObserver() if args.fixture == "history" else None
    from agent_session_control.controller import LocalController  # noqa: E402

    controller = LocalController(
        root=root,
        namespace=args.namespace,
        runtime_profiles=profiles,
        terminal=ScriptedTerminal(),
        observer=observer,
        spawn=False,
    )
    stop = threading.Event()
    if args.fixture == "history":
        start_canned_binder(controller, stop)
    try:
        asyncio.run(serve_stdio(controller, args.runtime, actor="acp-test"))
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

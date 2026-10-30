"""Stdio driver for ACP tests: ControlAgent over a FakeController.

Usage: run_fake_agent.py --runtime <id>
Runs the production ``serve_stdio`` path (official SDK framing) with a
deterministic controller. Never touches terminals, storage or model APIs.
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_controller import FakeController  # noqa: E402  # pyrefly: ignore[missing-import]

from agent_session_control.acp import serve_stdio  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", default="fake-test")
    args = parser.parse_args()
    delay = float(os.environ.get("ASC_FAKE_DELAY", "0.01"))
    controller = FakeController(runtime=args.runtime, delay=delay)
    asyncio.run(serve_stdio(controller, args.runtime, actor="acp-test"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

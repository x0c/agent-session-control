#!/usr/bin/env python3
"""Separate exiting caller for production-default detached acceptance.

Uses only production defaults (real RunStore, owned TmuxBackend, foundation
registry, SessKit observer, default supervisor factories): no fixture hooks,
no fixture factories, no ASC_TEST_FIXTURES. Submits one turn to an existing
session, prints the run id as JSON, and exits; the detached supervisor owns
the run afterwards. Session creation and workspace-trust preparation stay
with the calling operator (the test), which owns that directory choice.

Usage: detached_caller.py <root> <namespace> <session_id> <prompt> <request-id>
"""

import asyncio
import json
import sys


async def main() -> None:
    root, namespace, session_id, prompt, request_id = sys.argv[1:6]
    from agent_session_control.controller import LocalController

    controller = LocalController(root=root, namespace=namespace)
    try:
        run_id = await controller.start_turn(session_id, [{"type": "text", "text": prompt}], request_id=request_id)
    finally:
        controller.reap_children()
    print(json.dumps({"session_id": session_id, "run_id": run_id}))
    sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(main())

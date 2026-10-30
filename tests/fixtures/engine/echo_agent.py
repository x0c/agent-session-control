#!/usr/bin/env python3
"""Disposable fixture agent for owned-terminal tests.

Reads stdin lines and echoes each as ``ECHO:<line>``. Prints ``ECHO-READY``
once interactive. Exits on EOF or a delivered SIGINT (so an interrupt key
that the terminal translates to SIGINT ends the process, like a real agent).
No network, no model, no global state; safe to host in throwaway tmux panes.
"""

import sys


def main() -> int:
    sys.stdout.write("ECHO-READY\n")
    sys.stdout.flush()
    for line in sys.stdin:
        text = line.rstrip("\n")
        if text == "":
            continue
        sys.stdout.write(f"ECHO:{text}\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

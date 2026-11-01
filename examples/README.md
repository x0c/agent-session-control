# Consumer examples

Independent non-host consumers using only the installed public
`agent_session_control` API.

- [`basic_control.py`](basic_control.py): pure launch plans for all
  five runtimes plus captured CLI envelopes by default (free, offline).
  With `--live`: durable create/send/observe/cancel/resume, restart
  recovery on the same root/namespace, and the optional ACP entry check.
  The live turn spends model quota and needs the selected runtime's
  executable plus login; approval policy stays at the default
  (`auto_approve=False`).
- [`native_smoke.py`](native_smoke.py): bounded native acceptance over
  the installed wheel with production defaults — per-runtime create/
  liveness/close over real owned tmux panes (no prompt is ever sent) plus
  a real ACP stdio `initialize` handshake with the official SDK client.
  Per-leg bounds (90s), unique namespaces/sockets, disposable roots on
  verified release (retained `retained_root` on failure), scrubbed `TMUX` env.

Run with the packaged wheel installed (never an editable checkout or
`PYTHONPATH` for final acceptance):

```bash
python3 examples/basic_control.py                  # plans + CLI envelopes only
python3 examples/basic_control.py --live --runtime claude   # one live turn (costs quota)
python3 examples/native_smoke.py                  # bounded native + stdio
```

Each run uses a disposable state root and a unique namespace for
*control* state (removed only on verified release; a failed native leg
retains its scratch/control root and reports `retained_root` for recovery).
Hosting a native CLI may still record native history
under that runtime's standard locations even when no prompt is sent;
`close_session` releases the owned pane but never deletes native
history. A phase whose peer module is not implemented yet exits 3
(`BLOCKED`) naming the exact missing dependency instead of faking
success. Unavailable executables, login or quota are reported as limits,
never as passes.

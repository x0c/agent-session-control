# Consumer examples

Independent non-Corral consumers using only the installed public
`agent_session_control` API.

- [`basic_control.py`](basic_control.py): pure launch-plan generation for all
  five runtimes, then (unless `--skip-live`) durable create/send/observe/
  cancel/resume, restart recovery on the same root/namespace, and the
  optional ACP entry check.
- [`native_smoke.py`](native_smoke.py): bounded native acceptance over the
  installed wheel with production defaults — per-runtime create/liveness/
  close over real owned tmux panes (no prompt is ever sent) plus a real ACP
  stdio `initialize` handshake with the official SDK client. Per-leg bounds
  (90s), unique namespaces/sockets, disposable roots, scrubbed `TMUX` env.

Run with the packaged wheel installed (never an editable checkout or
`PYTHONPATH` for final acceptance):

```bash
python3 examples/basic_control.py --skip-live   # pure plans only
python3 examples/native_smoke.py                # bounded native + stdio
```

Run with the packaged wheel installed (never an editable checkout or
`PYTHONPATH` for final acceptance):

```bash
python3 examples/basic_control.py --runtime claude --skip-live   # pure plans only
python3 examples/basic_control.py --runtime claude                # full flow
```

Each run uses a disposable state root and a unique namespace; no user
history or hosted session is touched. A phase whose peer module is not
implemented yet exits 3 (`BLOCKED`) naming the exact missing dependency
instead of faking success. Live control additionally needs a real terminal
backend and the selected runtime's login/quota; unavailable login or quota
is reported as a limit, not a pass.

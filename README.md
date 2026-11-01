# Agent Session Control

**Languages:** English | [简体中文](README.zh-CN.md)

Launch and track local **Claude Code**, **Codex CLI**, **OpenCode**, **Cursor**, and **Pi** coding-agent sessions from Python: pure launch plans, audited runs, and per-session stdio MCP wiring. Also ships an [agent-client-protocol](https://github.com/agentclientprotocol/agent-client-protocol) (ACP) adapter.

This is a **Python library + JSON CLI + ACP adapter** — not a TUI, not a session manager, not a history parser (history observation is delegated to [SessKit](https://github.com/x0c/sesskit)).

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/x0c/agent-session-control/actions/workflows/ci.yml/badge.svg)](https://github.com/x0c/agent-session-control/actions/workflows/ci.yml)

Status: 0.1.1 (see [docs/INTEGRATION.md](docs/INTEGRATION.md) and [docs/CONTRACT.md](docs/CONTRACT.md)). Per-runtime limits are listed below.

## Install

Use a fixed released wheel in your project's virtual environment.

Requires **Python 3.10–3.14 (3.12 recommended)**. Default tmux backend runs on **macOS and Linux**; Windows/WSL are not supported. A Homebrew `python3` may be 3.15, which the optional `agent-client-protocol` SDK excludes — use a 3.10–3.14 interpreter for the `acp` extra.

```bash
VERSION=0.1.1
BASE=https://github.com/x0c/agent-session-control/releases/download/v${VERSION}
WHEEL=${BASE}/agent_session_control-${VERSION}-py3-none-any.whl
SESSKIT=https://github.com/x0c/sesskit/releases/download/v0.2.21/sesskit-0.2.21-py3-none-any.whl

# Core only: plans, store, correlation, JSON CLI (stdlib only).
python -m pip install "agent-session-control @ ${WHEEL}"

# Observing controller (+ SessKit native-history observation).
# SessKit is not on PyPI: pass its fixed wheel explicitly with the extra.
# (tool.uv.sources is uv-only and is not wheel metadata.)
python -m pip install "sesskit @ ${SESSKIT}" "agent-session-control[history] @ ${WHEEL}"

# ACP adapter over the observing controller (default full setup).
python -m pip install "sesskit @ ${SESSKIT}" "agent-session-control[history,acp] @ ${WHEEL}"

python -c "import agent_session_control; print(agent_session_control.__version__)"
```

Need a virtual environment first? Run `python3 -m venv .venv && . .venv/bin/activate`.

## What works today (tested)

- **Pure launch plans** (`agent_session_control.runtime`): `build_create_plan`
  and `build_resume_plan` for the five built-in runtimes (`claude`, `codex`,
  `opencode`, `cursor`, `pi`) plus explicit third-party `RuntimeProfile`
  registration. Plans are immutable argv arrays — building one never launches
  a process, mutates storage, edits global config, or sends a model request.
  Caller defaults are preserved (no model flag, no approval relaxation unless
  requested). Unsupported options fail before launch instead of silently
  degrading.
- **Per-session stdio MCP** (`mcp_servers=`): verified launch-only channels
  for `claude` / `codex` / `opencode`, each proven end-to-end with a
  disposable stdio fixture against the real installed CLI (no global config
  touched). Secrets never travel on argv; live values travel only in
  `plan.env`, redacted from every display projection. `cursor` and `pi`
  read MCP servers only from persistent config files, so per-launch
  servers are refused as `unsupported` (no silent downgrade).
- **Durable audited runs** (`agent_session_control.store`): `RunStore` with
  same-transaction actor audit, immutable idempotency keys, cross-process
  admission through `UNIQUE` constraints, per-namespace database files,
  configurable root and supervisor identity. Reads never create storage.
- **Evidence correlation** (`agent_session_control.correlation`): `marker`,
  `wire_prompt`, `own_message`, `correlate`, `build_result`. Only native
  user-message evidence establishes input recording. Missing usage stays
  `null`; repeated usage observations are deduplicated, never summed into
  a fake billing total. `completed` means the correlated turn ended, never
  that a task passed acceptance.
- **Shared models and port** (`agent_session_control.models`):
  `SessionHandle`, `ControlEvent`, `RunResult`, prompt-block helpers
  (canonical `text` + `resource_link`), and the async `SessionController`
  port consumed by the engine `LocalController`, the JSON CLI and the ACP
  adapter.
- **Stable errors** (`agent_session_control.errors`): `ControlError` with
  stable codes, observable fields and exit codes, plus the `{ok, data,
  error, meta}` envelope helpers used by the CLI.

Core imports need only the standard library. Optional extras:

- `history`: SessKit `>=0.2.21` for native-history observation.
- `acp`: official `agent-client-protocol >=0.12.1,<0.13` for the ACP adapter.

## Quick use

```python
from agent_session_control import build_create_plan, build_resume_plan, runtime_profile

# Approval is never relaxed by default: auto_approve=False. Pass
# auto_approve=True only when the host's explicit policy allows it.
plan = build_create_plan("codex", "/path/to/project")
print(plan.argv)  # ('codex', ...)

resume = build_resume_plan("claude", {"id": "<exact-native-id>", "cwd": "/path/to/project"})
print(resume.argv)

with_mcp = build_create_plan(
    "claude", "/path/to/project",
    mcp_servers=[{"name": "docs", "command": "/usr/local/bin/docs-mcp", "env": {"API_KEY": "..."}}],
)
print(with_mcp.to_dict())  # redacted projection: no secret values, secret-free argv
# Live values travel only in plan.env for the child process; argv carries
# ${NAME} references (claude) or env_vars forwarding names (codex).

print(runtime_profile("cursor"))  # capabilities, interrupt keys, limitations
```

```python
from agent_session_control import RunStore

store = RunStore()  # ~/.config/agent-session-control/control/runs.sqlite3
record, created = store.create("local-user", "request-key-1",
                               {"target": "claude:<id>", "text": "..."},
                               {"execution": "queued", "delivery": "accepted"})
```

SDK lifecycle (live; spends quota, needs runtime login):

```python
import asyncio
import tempfile
from pathlib import Path
from agent_session_control.controller import LocalController

async def main() -> None:
    # Selected temp project + isolated control root: never a fixed /tmp path.
    with tempfile.TemporaryDirectory(prefix="asc-sdk-") as scratch:
        project = str(Path(scratch, "project"))
        Path(project).mkdir(parents=True, exist_ok=True)
        controller = LocalController(root=str(Path(scratch, "control")), namespace="demo")
        handle = None
        try:
            handle = await controller.create("claude", project)
            run_id = await controller.start_turn(
                handle.id, [{"type": "text", "text": "Reply with ACK."}], request_id="demo-1")
            assert run_id.startswith("run_"), run_id  # exact run identity
            result = await controller.wait(run_id, timeout=120.0)
            print(result.state, result.evidence)
            assert result.run_id == run_id
            if result.final_response:
                print(result.final_response[:200])  # returned final text
        finally:
            # Public-API cleanup only: releases the pane; keeps history.
            if handle is not None:
                await controller.close_session(handle.id)

asyncio.run(main())
```

JSON CLI (machine envelope on stdout; ACP owns its own stdout):

```sh
python -m agent_session_control version --json
# {"ok": true, "data": {"package": "agent-session-control", "version": "0.1.1", ...}, "error": null, "meta": {...}}

python -m agent_session_control plan create --runtime codex --cwd /tmp/demo-proj --json
# {"ok": true, "data": {"dry_run": true, "status": "planned",
#   "launch": {"argv": ["codex", "--no-daemon"], "cwd": "/tmp/demo-proj", "env_keys": []}}, ...}

python -m agent_session_control session create --runtime codex --cwd /tmp/demo-proj \
  --dry-run --json --root /tmp/demo-control
# {"ok": true, "data": {"dry_run": true, "status": "planned", "session": {...}}, ...}
```

ACP adapter (one runtime per invocation; protocol messages only on stdout):

```sh
python -m agent_session_control acp --runtime codex --root /tmp/demo-control --namespace demo
```

See [docs/INTEGRATION.md](docs/INTEGRATION.md) for adoption recipes, isolated
roots/namespaces, and platform notes. Protocol details live in
[docs/ACP.md](docs/ACP.md); the full contract is [docs/CONTRACT.md](docs/CONTRACT.md).
`examples/basic_control.py` (plans + CLI envelopes; `--live` for a real
turn) and `examples/native_smoke.py` (bounded native + stdio handshake)
run these paths end to end.

## Capability matrix

| Capability | claude | codex | opencode | cursor | pi |
|---|---|---|---|---|---|
| Pure plans | yes | yes | yes | yes | yes |
| Owned hosting + liveness + close (no prompt) | yes | yes | yes | yes | yes |
| Native turn (login/quota-gated, spends quota) | gated | gated | gated | gated | gated |
| Per-session MCP launch channel | headless-verified | headless-verified | headless-verified | refused | refused |
| Hosted TUI MCP delivery | pending host run | pending host run | launcher-owned only | refused (`unsupported`) | refused (`unsupported`) |
| ACP serving over this runtime | yes | yes | yes | SDK/CLI only | SDK/CLI only |

Headless proof: disposable stdio fixtures against the real installed CLIs
(`claude -p`, `codex exec`, `opencode --standalone run`) with no global
config touched. Hosted TUI delivery shares the same argv/env channel but is
pending a host run (final hosted-MCP truth lives with the adapter/backend
owners, see `docs/ACP.md`, `docs/BACKENDS.md`). Availability probes report
executable presence only — never login, quota, or model entitlement.
Unavailable login/quota surface as explicit limits, not passes.

Companion Skill: agents driving this library should install
[`agent-session-control`](https://github.com/x0c/agent-session-control/blob/v0.1.1/skills/agent-session-control/SKILL.md)
([`references/mcp.md`](https://github.com/x0c/agent-session-control/blob/v0.1.1/skills/agent-session-control/references/mcp.md)),
shipped in the wheel at `agent_session_control/skills/agent-session-control`:

```sh
npx skills add x0c/agent-session-control@v0.1.1 --skill agent-session-control
```

The Skill teaches the existing JSON CLI lifecycle (plan/host/observe/release);
it adds no new lifecycle.

## Related Tools

| Tool | Role | Relation |
|---|---|---|
| [SessKit](https://github.com/x0c/sesskit) | Native-history observation (`history` extra, fixed wheel `v0.2.21`) | Consumed as an explicit pinned install; never vendored |
| [agent-client-protocol](https://github.com/agentclientprotocol/agent-client-protocol) | ACP wire protocol + Python SDK (`acp` extra, `>=0.12.1,<0.13`) | Consumed from PyPI; adapter serves one runtime per invocation |
| `tmux` | Default terminal backend (macOS/Linux) | External dependency; installed per-OS in CI |

## Limitations

- Cursor and Pi read MCP servers only from persistent config files, so per-session `mcp_servers=` is refused as `unsupported` (no silent downgrade).
- OpenCode per-session MCP takes effect only when the launcher owns the server; an ambient background service ignores it.
- `completed` means the correlated turn ended, never that a task passed acceptance. Missing usage stays `null`.
- Live control spends model quota and needs the runtime executable plus login. Disposable project/state roots isolate control state only: hosting may still record native history under the runtime's standard locations, and `close_session` never deletes it.

## Development checks

Contributors only — not part of installation:

```sh
uv sync --group dev --all-extras
uv run --python 3.12 --group dev scripts/check.py       # FULL: includes live-marked tests
uv run --python 3.12 --group dev scripts/check.py --ci  # CI: -m "not live", no quota/login
```

Runs Ruff, Pyrefly, the pytest suite (warnings are failures),
wheel/sdist packaging, metadata checks, and clean installs (core,
`history`, `acp`, combined; file:// URIs verified under both uv and
plain pip) plus a unique-temp external consumer probe. Default CI never
spends quota; `live` tests run only via the manual canonical-repo job.

## License

MIT — see [LICENSE](LICENSE).

# agent-session-control

Reusable coding-agent session control: pure launch plans, durable audited runs,
and native-evidence correlation. MIT-licensed (see [LICENSE](LICENSE)).

Status: 0.1.0 released and tested (see [docs/DESIGN.md](docs/DESIGN.md) and
[docs/CONTRACT.md](docs/CONTRACT.md)). This README claims only what the
release delivers and its tests prove; per-runtime limits are listed below.

## What works today (tested)

- **Pure launch plans** (`agent_session_control.runtime`): `build_create_plan`
  and `build_resume_plan` for the five built-in runtimes (`claude`, `codex`,
  `opencode`, `cursor`, `pi`) plus explicit third-party `RuntimeProfile`
  registration. Plans are immutable argv arrays — building one never launches
  a process, mutates storage, edits global config, or sends a model request.
  Caller defaults are preserved (no model flag, no approval relaxation unless
  requested). Unsupported options — unknown runtimes, `--model` on OpenCode,
  preassigned native ids where no flag exists — fail before launch instead of
  silently degrading.
- **Per-session stdio MCP** (`mcp_servers=`): verified launch-only channels, each
  proven end-to-end with a disposable stdio fixture against the real installed
  CLI (no global config touched). Secrets never travel on argv — Claude argv
  carries `${NAME}` references, Codex argv carries `env_vars` forwarding
  names, OpenCode carries the document in private process env; live values
  travel only in `plan.env`, redacted from every display projection:
  - Claude 2.1.314: `--mcp-config '<.mcp.json document>'` argv plus
    `--strict-mcp-config` (proven headless via `claude -p`; interactive TUI
    delivery pending host run).
  - Codex 0.166.0: repeated `-c mcp_servers.<name>.command/args/env_vars`
    argv overrides (proven headless via `codex exec`, secret received through
    forwarding; `~/.codex/config.toml` carries no fixture).
  - OpenCode v2.2.2: private process env `OPENCODE_CONFIG_CONTENT` with
    `mcp.servers.<name>` documents (proven headless via `--standalone run`;
    global `opencode.json`/`config.json` checksummed unchanged). Takes effect
    only when the launcher owns the server — an ambient background service
    ignores it.
  - Cursor and Pi read MCP servers only from persistent config files, so they
    honestly refuse per-launch servers (`unsupported`).
  Server specs are validated (absolute command, string args/env, identifier
  env keys, unique `[A-Za-z0-9_-]` names, stdio only, no remote transports,
  no unknown keys); secret-bearing configs on channels without a verified
  private route are refused before launch.
- **Durable audited runs** (`agent_session_control.store`): `RunStore` with
  same-transaction actor audit, immutable idempotency keys (identical retry
  converges, changed payload conflicts), cross-process admission through
  `UNIQUE` constraints, per-namespace database files, configurable root and
  supervisor identity. Reads never create storage. Adapted from Corral
  `src/corral/control_store.py`.
- **Evidence correlation** (`agent_session_control.correlation`): `marker`,
  `wire_prompt`, `own_message`, `correlate`, `build_result` with an explicit
  marker namespace (neutral default `session-control`; legacy `Corral`
  namespace available for compatibility hosts). Only native user-message
  evidence establishes input recording; quoted markers in assistant/tool
  output never count. Missing usage stays `null`; repeated usage observations
  are deduplicated, never summed into a fake billing total. `completed` means
  the correlated turn ended, never that a task passed acceptance.
- **Shared models and port** (`agent_session_control.models`): `SessionHandle`,
  `ControlEvent`, `RunResult`, prompt-block helpers (canonical `text` +
  `resource_link`; legacy neutral `resource` accepted explicitly as URI-link
  compatibility input; rich content and embedded resource data explicitly
  rejected), and the async `SessionController` port consumed by the engine
  `LocalController`, the JSON CLI and the ACP adapter.
- **Stable errors** (`agent_session_control.errors`): `ControlError` with the
  same constructor, observable fields and exit codes as the extraction source,
  plus the `{ok, data, error, meta}` envelope helpers used by the CLI.

Core imports need only the standard library. Optional extras:

- `history`: [SessKit](https://github.com/x0c/sesskit) `>=0.2.21` for
  native-history observation (consumed by the engine layer, not parsed here).
- `acp`: official `agent-client-protocol >=0.12.1,<0.13` for the ACP adapter.

## Install

Requires Python `>=3.10,<3.15` (use 3.12; the default macOS `python3` may be
3.15, which the ACP SDK does not support).

```sh
WHEEL=https://github.com/x0c/agent-session-control/releases/download/v0.1.0/agent_session_control-0.1.0-py3-none-any.whl
pip install "agent-session-control @ $WHEEL"              # core, standard library only
pip install "agent-session-control[history] @ $WHEEL"    # + SessKit native-history observation
pip install "agent-session-control[acp] @ $WHEEL"         # + official agent-client-protocol SDK
```

With `uv`, SessKit resolves from its immutable published wheel:

```toml
[tool.uv.sources]
sesskit = { url = "https://github.com/x0c/sesskit/releases/download/v0.2.21/sesskit-0.2.21-py3-none-any.whl" }
```

## Quick use

```python
from agent_session_control import build_create_plan, build_resume_plan, runtime_profile

plan = build_create_plan("codex", "/path/to/project", auto_approve=True)
print(plan.argv)  # ('codex', '--no-daemon', '--dangerously-bypass-approvals-and-sandbox')

resume = build_resume_plan("claude", {"id": "<exact-native-id>", "cwd": "/path/to/project"},
                           auto_approve=True)
print(resume.argv)  # ('claude', '--dangerously-skip-permissions', '--resume', '<exact-native-id>')

with_mcp = build_create_plan(
    "claude", "/path/to/project",
    mcp_servers=[{"name": "docs", "command": "/usr/local/bin/docs-mcp", "env": {"API_KEY": "..."}}],
)
print(with_mcp.to_dict())  # secret values redacted; live argv keeps them for the child

print(runtime_profile("cursor"))  # capabilities, interrupt keys, limitations
```

```python
from agent_session_control import RunStore

store = RunStore()  # ~/.config/agent-session-control/control/runs.sqlite3
record, created = store.create("local-user", "request-key-1",
                               {"target": "claude:<id>", "text": "..."},
                               {"execution": "queued", "delivery": "accepted"})
```

See [docs/INTEGRATION.md](docs/INTEGRATION.md) for adoption recipes, isolated
roots/namespaces, and platform notes. The full contract is
[docs/CONTRACT.md](docs/CONTRACT.md).

## Checks

```sh
uv run --python 3.12 --group dev scripts/check.py
```

Runs Ruff, Pyrefly, the complete pytest suite (warnings are failures),
wheel/sdist packaging, and a clean offline install importing only the core.

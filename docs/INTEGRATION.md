# Integration

Status: foundation slice implemented and tested (0.1.0), integrated with the
sibling engine controller, JSON CLI and ACP adapter. Sibling layers report
their own acceptance; this guide covers what consumers can adopt today and
the exact tested limits.

## Fixed-version installation

Requires Python `>=3.10,<3.15`. Use 3.12: the default macOS `python3` may be
3.15, which the optional ACP SDK does not support. Core needs only the
standard library.

```sh
pip install agent-session-control==0.1.0
pip install "agent-session-control[history]==0.1.0"  # SessKit native-history observation
pip install "agent-session-control[acp]==0.1.0"      # official agent-client-protocol SDK
```

## Fixed-version installation

Requires Python `>=3.10,<3.15`. Use 3.12: the default macOS `python3` may be
3.15, which the optional ACP SDK does not support. Core needs only the
standard library.

```sh
pip install agent-session-control==0.1.0
pip install "agent-session-control[history]==0.1.0"  # SessKit native-history observation
pip install "agent-session-control[acp]==0.1.0"      # official agent-client-protocol SDK
```

`uv` users pin SessKit to its immutable published wheel (the only authority
for that URL is this repository's `pyproject.toml`):

```toml
[tool.uv.sources]
sesskit = { url = "https://github.com/x0c/sesskit/releases/download/v0.2.21/sesskit-0.2.21-py3-none-any.whl" }
```

The final dependency handoff is this versioned artifact, never an
editable/path dependency into this checkout.

## Lightweight launch-plan use (no backend required)

```python
from agent_session_control import build_create_plan, build_resume_plan, runtime_profile

plan = build_create_plan("claude", "/path/to/project", auto_approve=True)
# plan.argv is an immutable tuple; execute it with your own supervision, e.g.
# subprocess.Popen(plan.argv, cwd=plan.cwd, env={**os.environ, **plan.env}).

resume = build_resume_plan(
    "codex", {"id": "<exact-native-id>", "cwd": "/path/to/project"}, auto_approve=True
)
print(runtime_profile("cursor"))  # executable, aliases, interrupt keys, limitations
```

Rules consumers must keep:

- Plans are pure data. Building one launches nothing and mutates nothing.
- An omitted `model` inherits the runtime's own configuration; a supplied
  model applies to that launch only and is never written to global defaults.
- `auto_approve=False` (the default) adds no approval relaxation. Pass
  `auto_approve=True` only when the host's explicit policy allows it.
- Resume needs the exact native identity (`id` for claude/codex/opencode/
  cursor, `path` for pi). Missing identity raises instead of starting a blank
  replacement conversation.
- Unsupported options raise `ControlError` before launch: unknown runtimes,
  `--model` on OpenCode, preassigned native ids where no flag exists, and any
  `mcp_servers` on the five built-ins (no verified per-launch MCP path).
  MCP configuration is per-session/process and private: `LaunchPlan.env`
  values are redacted from `repr` and excluded from the default JSON
  projection.
- Third-party runtimes register an explicit `RuntimeProfile`; behavior is
  never guessed from the built-ins. Dormant runtimes (e.g. Kimi) are not
  registered and must not become visible product entries.
- `plan.cwd` must be absolute when set; executors verify existence and ownership.

## Per-session MCP servers

```python
plan = build_create_plan(
    "codex", "/path/to/project", auto_approve=True,
    mcp_servers=[{"name": "docs", "command": "/usr/local/bin/docs-mcp",
                  "args": ["--root", "/docs"], "env": {"API_KEY": "..."}}],
)
```

Each entry is validated (`name` in `[A-Za-z0-9_-]`, absolute `command`,
string `args`/`env`, optional `type: "stdio"`; anything else fails loudly)
and rendered through the runtime's verified launch-only channel — never
written to global or project config:

| Runtime | Channel | Plan form | Execution proof | Hosted interactive |
|---|---|---|---|---|
| claude | `--mcp-config '<.mcp.json>'` + `--strict-mcp-config` on argv | interactive `build_create_plan` argv | headless `claude -p` called the fixture | flags shared with hosted argv; TUI delivery pending host run |
| codex | `-c mcp_servers.<name>.command/args/env_vars` on argv, secrets in process env | interactive `build_create_plan` argv | headless `codex exec` received the secret via forwarding | overrides shared with hosted argv; TUI delivery pending host run |
| opencode | `OPENCODE_CONFIG_CONTENT` in private process env | `plan.env` | headless `--standalone run` called the fixture | needs launcher-owned server; ambient daemon ignores env |
| cursor, pi | none (persistent files only) | — | — | refused as `unsupported` |

Reference behavior established 2026-10-30 (installed CLIs plus binary
evidence; no invented flags): Claude's `--mcp-config` takes files or JSON
strings but is variadic (`<configs...>`), so plans keep it last — it would
otherwise swallow following positionals, and `mcp list` ignores session
flags entirely. Codex `-c` values are TOML-parsed dotted overrides
(`codex mcp get <name> -c ...` shows the merge; global `config.toml`
carries no fixture). OpenCode consumes `OPENCODE_CONFIG_CONTENT` as a
virtual config document (confirmed in the v2.2.2 binary), with servers under
`mcp.servers.<name>` (shape confirmed via `mcp add`); it takes effect only
with a launcher-owned server — the ambient background service ignores it,
so hosted TUI use needs host cooperation (private server or project file).

Secrets: server `env` values never travel on argv. Claude argv carries
`${NAME}` references expanded from the child process environment (official
Claude Code behavior; unset names warn and stay unexpanded); Codex argv
carries `env_vars` forwarding names; OpenCode carries the whole document in
private process env. Live values travel only in `plan.env`, redacted from
`repr` and every `to_dict()` projection — including the CLI dry-run path,
which must use the redacted projection and never `list(plan.argv)` /
`dict(plan.env)`. Secret-bearing configs on channels without a verified
private route are refused before launch; basic env-less stdio MCP stays
usable everywhere it is verified. Test fixtures obey the same rule: an MCP
test fixture must never dump the ambient environment — forwarding proof
returns only an explicitly allowlisted synthetic value, and the regression
(`test_fixture_reveals_only_allowlisted_key`) asserts an unrelated synthetic
ambient secret appears in no tool result or log. Runtimes needing an ephemeral *file*
channel (none of the three verified ones do) cannot be expressed with the
current `LaunchPlan` (argv/cwd/env only, and a pure plan never creates
files) — that needs a coordinator-approved plan extension, not a silent
side channel.

## Durable runs (foundation store)

```python
from agent_session_control import RunStore

store = RunStore()  # ~/.config/agent-session-control/control/runs.sqlite3
record, created = store.create(
    "local-user",                       # actor
    "request-key-1",                    # immutable idempotency key
    {"target": "claude:<id>", "text": "..."},  # immutable spec
    {"execution": "queued", "delivery": "accepted"},
)
```

- Repeating the identical `(key, spec)` returns the original run
  (`created=False`); the same key with a changed payload raises
  `idempotency_conflict`. Cross-process races converge through `UNIQUE`
  constraints; the loser re-reads the winner, never a second run.
- Every write commits its actor/action/target/timestamp audit row in the same
  transaction (`store.audit_trail()` inspects it). Reads
  (`get`/`recent`/`find`/`unsettled`/`events`/`audit_trail`) never create storage.
- Terminal runs (`completed`/`failed`/`interrupted`) are immutable; follow-ups
  start new runs. `store.update(..., expected=record["version"])` enforces
  read-before-write with `conflict` on stale versions.
- Isolated use passes an explicit root and namespace:
  `RunStore(root=Path(...), namespace="team-x", supervisor="host-name")`.
  Namespaces map to separate database files in one root (`runs.sqlite3` for
  the default namespace — the exact historical host path — and
  `runs.<namespace>.sqlite3` otherwise); they prevent
  same-user mistakes, not hostile-tenant access. File permissions are
  `0600`/`0700`; a symlinked database file is refused.

## Direct controller use (engine worker)

The controller port is `agent_session_control.models.SessionController`
(`create`, `resume`, `start_turn`, `watch`, `wait`, `interrupt`,
`close_session`, `list_sessions`, `load_history`). The production
`LocalController` accepts an explicit `root`, `namespace`, runtime/terminal/
observer hooks and approval policy, is side-effect free to construct, and
shares process-owned target/input locks with one authoritative operation
store per root/namespace. The engine worker owns its behavior and
acceptance; foundation guarantees the port shape and the plan/store/
correlation semantics it stands on.

## JSON CLI (ACP worker)

The entry `agent-session-control = agent_session_control.cli:main` serves
`python -m agent_session_control
describe|version|doctor|plan|session|run ...` with `{ok, data, error, meta}`
envelopes (`agent_session_control.errors.ControlError.to_envelope`) and
`--root`/`--namespace` for isolated use. ACP stdout stays protocol messages
only; CLI envelopes never enter it. The ACP worker owns CLI behavior and
acceptance.

## ACP adapter (ACP worker)

`acp.ControlAgent` wraps any `SessionController` with one explicitly selected
runtime per invocation so capability claims stay truthful. Protocol baseline:
schema-v1.27.0, Python SDK 0.12.1, wire major v1. See [ACP](ACP.md).

## Evidence correlation

```python
from agent_session_control.correlation import build_result, correlate, wire_prompt

wired = wire_prompt(run_id, "msg-1", brief)  # visible envelope; original brief verbatim inside
observation = correlate(events, turns, run_id, baseline_seq)  # SessKit objects via public attributes
result = build_result(observation)  # acceptance stays "unverified"; usage None when absent
```

Only native user-message evidence after the baseline establishes input
recording; quoted markers in assistant/tool output never count. Foreign input
inside the bound range yields `mixed` attribution. The shared record carries
`stop_reason` (native only, `None` otherwise), `current_turn`,
`pending_interaction`, outcome evidence and error. Usage is keyed by native
event identity plus usage evidence origin — never by token values — so two
distinct events with equal counts both count, while one observation repeated
by overlapping evidence ranges counts once (`usage_duplicates_skipped`).
`total_tokens`/`cost` are never aggregated (they would double-count or
invent billing).

## Supported platforms and limits

- macOS and Linux on Python 3.10–3.14 (3.12 recommended). Windows/WSL are not
  supported by the extraction source and inherit that limit.
- Availability probes (`is_available`, `runtime_profile()["available"]`)
  reflect executable presence only — never login, quota, or model entitlement.
  Missing login/quota must surface as explicit limits from the backend, not as
  passes.
- Same-user namespaces are not security isolation. The library never infers
  host identity from host-prefixed environment variables; hosts pass
  actor/namespace explicitly.
- The source host keeps its historical root, actor/marker namespace
  (`correlation.CORRAL_NAMESPACE`), auto-approval policy and managed identity
  preparation as thin compatibility facades over these generic mechanisms;
  native-history parsing stays SessKit-owned.

## Clean external consumer (acceptance shape)

```sh
python -m venv /tmp/consumer-venv && /tmp/consumer-venv/bin/pip install agent-session-control==0.1.0
/tmp/consumer-venv/bin/python - <<'EOF'
from agent_session_control import RunStore, build_create_plan
print(build_create_plan("cursor", "/tmp/demo", auto_approve=True).argv)
print(RunStore(root="/tmp/demo-control").create("op", "k", {"t": 1}, {"execution": "queued"})[1])
EOF
```

No checkout on `sys.path`, no extras installed, no network beyond the pinned
install. The repository's `scripts/check.py` automates this shape against the
built wheel.

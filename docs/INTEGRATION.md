# Integration

Status: 0.1.1. This guide covers what consumers can adopt today and the
exact tested limits.

## Fixed-version installation

Requires Python `>=3.10,<3.15` (3.12 recommended). The default tmux
backend runs on macOS and Linux; Windows/WSL are not supported (see
platform limits below). Core needs only the standard library.

```sh
VERSION=0.1.1
BASE=https://github.com/x0c/agent-session-control/releases/download/v${VERSION}
WHEEL=${BASE}/agent_session_control-${VERSION}-py3-none-any.whl
SESSKIT=https://github.com/x0c/sesskit/releases/download/v0.2.21/sesskit-0.2.21-py3-none-any.whl

# Core only (launch plans, store, correlation, JSON CLI): no extras.
pip install "agent-session-control @ ${WHEEL}"

# Observing controller: SessKit is not on PyPI, so pass its fixed wheel
# explicitly alongside the extra. (tool.uv.sources is a uv-only setting
# and is not wheel metadata, so pip needs the URL on the command line.)
pip install "sesskit @ ${SESSKIT}" "agent-session-control[history] @ ${WHEEL}"

# ACP adapter over the observing controller (default full setup):
pip install "sesskit @ ${SESSKIT}" "agent-session-control[history,acp] @ ${WHEEL}"
```

`uv` users keep the same fixed SessKit wheel (the only authority for
that URL is this repository's `pyproject.toml`):

```toml
[tool.uv.sources]
sesskit = { url = "https://github.com/x0c/sesskit/releases/download/v0.2.21/sesskit-0.2.21-py3-none-any.whl" }
```

The final dependency handoff is these versioned artifacts, never an
editable/path dependency into this checkout.

Homebrew Python note: a Homebrew `python3` may be 3.15, which the
optional `agent-client-protocol` SDK excludes (`<3.15`). Use a 3.10–3.14
interpreter (3.12 recommended) when installing the `acp` extra.

## 0.1.1 verification

The coordinator's final macOS/Python 3.12.12 gate passed 366 tests and 30
subtests with warnings treated as errors, including all four live-marked tests.
Ruff, format checking, Pyrefly, wheel/sdist checks, clean core/history/ACP/
combined installs, plain-pip installation and a repository-external audited
consumer also passed. The native Codex turn and headless Claude/Codex/OpenCode
MCP fixtures used native approval defaults; these are not hosted-TUI MCP proofs.
Linux and other supported Python versions use the CI matrix; a workflow definition
alone is not evidence that those runs passed.

The binding/idempotency regression is deterministic: the same immutable request
converges after native-history discovery, on reopening and under concurrent
admission, while changed prompt or busy policy still conflicts. Existing stored
request digests are preserved; no native input is replayed to repair a retry.

Fresh OpenCode sessions on Muse Spark 1.3 Contributor independently exercised
the installed companion Skill. Explicit loading, selected-venv bootstrap, safe
provided-file MCP previews, unsupported-runtime refusal and the offline-history
component boundary passed. The revised implicit-selection case completed through
CLI help without loading the Skill, so reliable implicit selection or an improved
trigger rate is not claimed. A real owned Codex lifecycle returned the exact
requested answer, converged on one run for an identical retry, and closed; its
very short wait found an already-settled run, leaving that native timeout branch
unexercised. Deterministic controller tests separately cover wait-timeout honesty.
Assessment prompts/expected answers remain test fixtures, not installed Skill
references. The catalog, model and case limits preclude an all-runtime guarantee.

GitHub release artifacts remain the installation route. PyPI publishing is
prepared but is not an advertised channel until registration, upload and clean
index readback have actually succeeded.

## Capability matrix

Plan generation, hosting, native turns, hosted MCP delivery, and
protocol evidence are separate claims. One passing does not imply the
others.

| Capability | claude | codex | opencode | cursor | pi |
|---|---|---|---|---|---|
| Pure launch/resume plans | yes | yes | yes | yes | yes |
| Owned tmux hosting + liveness + close (no prompt) | yes | yes | yes | yes | yes |
| Native turn settlement (login/quota-gated, spends quota) | gated | gated | gated | gated | gated |
| Per-session MCP launch channel | headless-verified | headless-verified | headless-verified | refused (`unsupported`) | refused (`unsupported`) |
| Hosted TUI MCP delivery | pending host run | pending host run | launcher-owned only | refused (`unsupported`) | refused (`unsupported`) |
| Native history observation (needs `history` extra) | via SessKit | via SessKit | via SessKit | via SessKit | via SessKit |
| ACP serving over this runtime | yes | yes | yes | SDK/CLI only | SDK/CLI only |

Native turns always need the runtime executable plus login/quota and
spend model quota. Final capability truth for the ACP adapter and the
terminal backend lives with their owners (see `docs/ACP.md` and
`docs/BACKENDS.md`); this guide does not re-adjudicate it.

## Lightweight launch-plan use (no backend required)

```python
from agent_session_control import build_create_plan, build_resume_plan, runtime_profile

# Approval is never relaxed by default: auto_approve=False. Pass
# auto_approve=True only when the host's explicit policy allows it.
plan = build_create_plan("claude", "/path/to/project")
# plan.argv is an immutable tuple; execute it with your own supervision, e.g.
# subprocess.Popen(plan.argv, cwd=plan.cwd, env={**os.environ, **plan.env}).

resume = build_resume_plan(
    "codex", {"id": "<exact-native-id>", "cwd": "/path/to/project"}
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
  cursor, `path` for pi). Missing identity raises instead of starting a
  blank replacement conversation.
- Unsupported options raise `ControlError` before launch: unknown runtimes,
  `--model` on OpenCode, preassigned native ids where no flag exists, and
  `mcp_servers` on cursor/pi (per-launch MCP is refused as `unsupported`
  there; claude/codex/opencode use the verified launch-only channels
  below). MCP configuration is per-session/process and private:
  `LaunchPlan.env` values are redacted from `repr` and excluded from the
  default JSON projection.
- Third-party runtimes register an explicit `RuntimeProfile`; behavior is
  never guessed from the built-ins. Dormant runtimes (e.g. Kimi) are not
  registered and must not become visible product entries.
- `plan.cwd` must be absolute when set; executors verify existence and ownership.

## Per-session MCP servers

```python
# Pass auto_approve=True only when the host's explicit policy allows it
# (default False adds no approval relaxation).
plan = build_create_plan(
    "codex", "/path/to/project",
    mcp_servers=[{"name": "docs", "command": "/usr/local/bin/docs-mcp",
                  "args": ["--root", "/docs"], "env": {"API_KEY": "..."}}],
)
# Redacted projection carries no secret values and secret-free argv:
print(plan.to_dict())
# Live values travel only in plan.env for the child; argv carries ${NAME}
# references (claude) or env_vars forwarding names (codex). Caller-supplied
# secret args (e.g. `--header "Authorization: Bearer ..."`) are the caller's
# own argv and are never redacted: keep live secrets in mcp_servers env.
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
virtual config document (confirmed in the v2.2.2 binary), with servers
under `mcp.servers.<name>` (shape confirmed via `mcp add`); it takes
effect only with a launcher-owned server — the ambient background service
ignores it, so hosted TUI use needs host cooperation (private server or
project file).

Secrets: server `env` values never travel on argv. Claude argv carries
`${NAME}` references expanded from the child process environment (official
Claude Code behavior; unset names warn and stay unexpanded); Codex argv
carries `env_vars` forwarding names; OpenCode carries the whole document
in private process env. Live values travel only in `plan.env`, redacted
from `repr` and every `to_dict()` projection — including the CLI dry-run
path, which must use the redacted projection and never `list(plan.argv)` /
`dict(plan.env)`. Secret-bearing configs on channels without a verified
private route are refused before launching; basic env-less stdio MCP stays
usable everywhere it is verified. Test fixtures obey the same rule: an MCP
test fixture must never dump the ambient environment — forwarding proof
returns only an explicitly allowlisted synthetic value. Runtimes needing an
ephemeral *file* channel (none of the three verified ones do) cannot be
expressed with the current `LaunchPlan` (argv/cwd/env only, and a pure
plan never creates files) — that needs a plan extension, not a silent
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
- Every write commits its actor/action/target/timestamp audit row in the
  same transaction (`store.audit_trail()` inspects it). Reads
  (`get`/`recent`/`find`/`unsettled`/`events`/`audit_trail`) never create
  storage.
- Terminal runs (`completed`/`failed`/`interrupted`) are immutable;
  follow-ups start new runs. `store.update(...,
  expected=record["version"])` enforces read-before-write with `conflict`
  on stale versions.
- Isolated use passes an explicit root and namespace:
  `RunStore(root=Path(...), namespace="team-x", supervisor="host-name")`.
  Namespaces map to separate database files in one root (`runs.sqlite3`
  for the default namespace and `runs.<namespace>.sqlite3` otherwise);
  they prevent same-user mistakes, not hostile-tenant access. File
  permissions are `0600`/`0700`; a symlinked database file is refused.

## Direct controller use (SDK lifecycle)

The controller port is `agent_session_control.models.SessionController`
(`create`, `resume`, `start_turn`, `watch`, `wait`, `interrupt`,
`close_session`, `list_sessions`, `load_history`). The production
`LocalController` accepts an explicit `root`, `namespace`,
runtime/terminal/ observer hooks and approval policy, is side-effect free
to construct, and shares process-owned target/input locks with one
authoritative operation store per root/namespace.

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
                handle.id, [{"type": "text", "text": "Reply with ACK."}], request_id="demo-1"
            )
            assert run_id.startswith("run_"), run_id  # exact run identity
            result = await controller.wait(run_id, timeout=120.0)
            print(result.state, result.evidence, bool(result.final_response))
            assert result.run_id == run_id
            if result.final_response:
                print(result.final_response[:200])  # returned final text
        finally:
            # Public-API cleanup only: releases the pane; keeps history.
            if handle is not None:
                await controller.close_session(handle.id)

asyncio.run(main())
```

Live use spends model quota and needs the runtime executable plus login.
`wait` never cancels work (a timeout raises `wait_timeout` while the run
continues); `close_session` cancels owned work and releases resources but
never deletes history. `examples/basic_control.py --live` runs this shape
end to end.

## JSON CLI

The entry `agent-session-control = agent_session_control.cli:main` serves
`python -m agent_session_control
describe|version|doctor|plan|session|run ...` with `{ok, data, error,
meta}` envelopes and `--root`/`--namespace` for isolated use. ACP stdout
stays protocol messages only; CLI envelopes never enter it.

```sh
python -m agent_session_control version --json
# {"ok": true, "data": {"package": "agent-session-control", "version": "0.1.1", ...}, "error": null, "meta": {...}}

python -m agent_session_control plan create --runtime codex --cwd /tmp/demo-proj --json
# {"ok": true, "data": {"dry_run": true, "status": "planned",
#   "launch": {"argv": ["codex", "--no-daemon"], "cwd": "/tmp/demo-proj", "env_keys": []}}, ...}

python -m agent_session_control session create --runtime codex --cwd /tmp/demo-proj \
  --dry-run --json --root /tmp/demo-control   # planned only; creates no state
# {"ok": true, "data": {"dry_run": true, "status": "planned", "session": {...}}, ...}
```

Read-only commands (`version`, `describe`, `doctor`, `plan`) work offline.
`session`/`run` commands share the engine controller; `--dry-run`
performs no side effect. Plan/dry-run output uses the redacted
`LaunchPlan.to_dict()` projection (no environment values, secret-free
argv).

## ACP adapter

`acp.ControlAgent` wraps any `SessionController` with one explicitly
selected runtime per invocation so capability claims stay truthful.
Protocol baseline: schema-v1.27.0, Python SDK 0.12.1, wire major v1. See
[ACP](ACP.md). Cursor and pi have no per-session MCP support: SDK/CLI
control remains available, but serving a production ACP connection for
those runtimes is refused before anything launches.

```sh
python -m agent_session_control acp --runtime codex \
  --root /tmp/demo-control --namespace demo   # stdio: protocol messages only
```

Drive it with the official client (`acp.spawn_agent_process`), run
`initialize`, then `session/new` with stdio MCP entries only. A complete
bounded handshake ships in `examples/native_smoke.py`.

## Evidence correlation

```python
from agent_session_control.correlation import build_result, correlate, wire_prompt

wired = wire_prompt(run_id, "msg-1", brief)  # visible envelope; original brief verbatim inside
observation = correlate(events, turns, run_id, baseline_seq)  # SessKit objects via public attributes
result = build_result(observation)  # acceptance stays "unverified"; usage None when absent
```

Only native user-message evidence after the baseline establishes input
recording; quoted markers in assistant/tool output never count. Foreign
input inside the bound range yields `mixed` attribution. The shared record
carries `stop_reason` (native only, `None` otherwise), `current_turn`,
`pending_interaction`, outcome evidence and error. Usage is keyed by
native event identity plus usage evidence origin — never by token values
— so two distinct events with equal counts both count, while one
observation repeated by overlapping evidence ranges counts once
(`usage_duplicates_skipped`). `total_tokens`/`cost` are never aggregated
(they would double-count or invent billing).

## Supported platforms and limits

- macOS and Linux on Python 3.10–3.14 (3.12 recommended); default tmux
  backend. Windows/WSL are not supported by the terminal backend.
- Availability probes (`is_available`, `runtime_profile()["available"]`)
  reflect executable presence only — never login, quota, or model
  entitlement. Missing login/quota must surface as explicit limits from
  the backend, not as passes.
- Live control spends model quota; native turns, model selection, real
  cancellation and hosted MCP delivery are login/quota-gated and reported
  per runtime, never counted as passes when unavailable.
- Same-user namespaces are not security isolation. The library never
  infers host identity from host-prefixed environment variables; hosts
  pass actor/namespace explicitly.

## Clean external consumer (acceptance shape)

```sh
VERSION=0.1.1
BASE=https://github.com/x0c/agent-session-control/releases/download/v${VERSION}
CASE="$(mktemp -d "${TMPDIR:-/tmp}/asc-consumer-XXXXXX")"
python -m venv "${CASE}/consumer-venv" && "${CASE}/consumer-venv/bin/pip" install \
  "sesskit @ https://github.com/x0c/sesskit/releases/download/v0.2.21/sesskit-0.2.21-py3-none-any.whl" \
  "agent-session-control[history,acp] @ ${BASE}/agent_session_control-${VERSION}-py3-none-any.whl"
"${CASE}/consumer-venv/bin/python" - <<'EOF'
import tempfile
from pathlib import Path
from agent_session_control import RunStore, build_create_plan
with tempfile.TemporaryDirectory(prefix="asc-consumer-doc-") as tmp:
    print(build_create_plan("cursor", tmp).argv)
    print(RunStore(root=str(Path(tmp) / "control")).create("op", "k", {"t": 1}, {"execution": "queued"})[1])
EOF
rm -rf "${CASE}"
```

No checkout on `sys.path`, no extras installed beyond the pinned
command, no network beyond the pinned install. The repository's
`scripts/check.py` automates this shape against the built wheel.

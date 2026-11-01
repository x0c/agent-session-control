---
name: agent-session-control
description: "Run locally owned coding-agent sessions with the agent-session-control CLI: plan or host a session, submit one prompt turn, and observe the run. Use for local session control; not a task scheduler, planner, or permission bypass."
---

# Agent Session Control

Control coding-agent sessions you own on this machine through the
`agent-session-control` JSON CLI. Plans are pure data; hosting, turns,
and observation share one controller. This skill is local session
control only: it does not schedule work, plan tasks, bypass approvals,
or manage other products.

## 1. Probe the CLI first

Pin one absolute CLI path and reuse it for every command in the task.
Prefer the installed entry; the module form is equivalent:

```sh
command -v agent-session-control
ABS=/path/from/above/agent-session-control
"$ABS" version --json
"$ABS" describe --json
```

Or `python -m agent_session_control` with the project interpreter.
`version` and `describe` work offline. `describe` is the live contract:
when this skill and `--help` disagree, `--help` and `describe` win.

Judge success by exit code or the `ok` field, never by non-empty
stdout. Every non-streaming command prints exactly one
`{ok, data, error, meta}` envelope on stdout; logs go to stderr.
`run events --follow` is the only streaming command and speaks JSONL.

`next_commands` in error envelopes is rendered from the serving
interpreter (`python -m agent_session_control ...`). When several
installs exist, re-issue follow-ups through your pinned `$ABS` path
instead of assuming the hint resolves to the same copy.

### If the CLI is missing

Install only from the fixed product release until real PyPI exists.
Do not invent URLs, do not overwrite another installed version without
explicit approval, and do not write outside the user directory without
approval. Requires Python `>=3.10,<3.15`: pick any compatible
interpreter the caller selects (`3.10`–`3.14`) and create the venv under a concrete user-directory
default or a caller-selected path — then address the CLI through that
venv only, never through the global `PATH`. Always call pip as
`python -m pip` from the venv `python` — never a bare `pip`. Pin
0.1.1: 0.1.0 is superseded (MCP secrets could reach process argv there).

```sh
PY=/path/to/python3.12          # caller-selected 3.10–3.14 interpreter
VENV=~/.local/asc-venv          # concrete default; caller may select another user-directory path
"$PY" -m venv "$VENV"
VERSION=0.1.1
BASE=https://github.com/x0c/agent-session-control/releases/download/v${VERSION}
WHEEL=${BASE}/agent_session_control-${VERSION}-py3-none-any.whl
SESSKIT=https://github.com/x0c/sesskit/releases/download/v0.2.21/sesskit-0.2.21-py3-none-any.whl
"$VENV/bin/python" -m pip install "agent-session-control @ ${WHEEL}"
"$VENV/bin/python" -m pip install "sesskit @ ${SESSKIT}" "agent-session-control[history] @ ${WHEEL}"       # native-history observation
"$VENV/bin/python" -m pip install "sesskit @ ${SESSKIT}" "agent-session-control[history,acp] @ ${WHEEL}"  # full setup with ACP adapter
```

SessKit is not on PyPI, so pip needs its fixed wheel on the command
line (`tool.uv.sources` is uv-only and is not wheel metadata). `uv`
users pin the same immutable wheel; the only authority for that URL is
the product `pyproject.toml`:

```toml
[tool.uv.sources]
sesskit = { url = "https://github.com/x0c/sesskit/releases/download/v0.2.21/sesskit-0.2.21-py3-none-any.whl" }
```

Then bind `$ABS` straight to that venv's entry and verify the
executable and its version directly — no `command -v` re-probe, no
global-`PATH` dependence:

```sh
ABS="$VENV/bin/agent-session-control"
test -x "$ABS"
"$ABS" version --json   # data: {package, version, python}; expect 0.1.1 on the venv interpreter
"$ABS" doctor --json    # read-only: interpreter, extras, runtimes, state root
```

Reinstalls converge; an existing install is not an error.

## 2. Select runtime, model, and approvals explicitly

SDK control covers five runtimes: `claude`, `codex`, `opencode`,
`cursor`, `pi`. There are no other built-ins; never guess a sixth
name. ACP serving is narrower: it requires the stdio MCP baseline, so
only `claude`, `codex`, and `opencode` can be served (`acp --runtime`
with `cursor` or `pi` is refused on stderr with exit 2 before any
protocol bytes; their SDK/CLI control is unaffected). `doctor`
reports executable presence only (`available`), never native login,
quota, or model entitlement. A missing login surfaces as an explicit
backend limit, not a pass.

- Omit `--model` to inherit the runtime default. A supplied model
  applies to that launch only and is never written anywhere.
  `opencode` has no verified per-launch model flag: `--model` there is
  refused (`model_selection_unsupported`), never silently remapped.
- Default approvals add no relaxation. Pass approval relaxation only
  when the user's explicit policy allows it for this launch; never
  auto-grant it and never write it to global or project config.
- Preview before hosting: `plan create` and `session create --dry-run`
  are pure and create no state. `--cwd` must be an absolute existing
  project directory, never `/`, the home root, Desktop, or Documents.

## 3. One scoped session lifecycle

Pick one caller-selected unique scope for the whole task and carry it
on every command — never a fixed shared path reused across tasks.
Save the exact identifiers each command returns; later steps need the
returned values, not guesses.

```sh
ROOT=$(umask 077; mktemp -d "${TMPDIR:-/tmp}/asc-task.XXXXXX")   # unique per task, owner-only
NS=demo   # stable namespace inside the scope
```

```sh
# 0. Preview (pure, no state).
"$ABS" plan create --runtime codex --cwd /tmp/proj --json

# 1. Host. Returns data.session.id, e.g. "ses_9f3a..."; save it as SES.
"$ABS" session create --runtime codex --cwd /tmp/proj --root "$ROOT" --namespace "$NS" --json

# 2. Submit one turn. Acceptance, not completion. Returns data.run.run_id; save it as RUN.
"$ABS" run submit --session-id "$SES" --prompt-file /tmp/brief.txt --request-id req-001 \
  --root "$ROOT" --namespace "$NS" --json
```

Pass exactly one of `--prompt` or `--prompt-file`; prefer
`--prompt-file PATH` with `-` for piped stdin so long briefs never
travel as argv fragments:

```sh
printf '%s\n' "continued brief" | "$ABS" run submit --session-id "$SES" --prompt-file - \
  --request-id req-002 --root "$ROOT" --namespace "$NS" --json
```

- `--request-id` is the immutable idempotency key (1-200 printable
  characters, no whitespace). Reusing the same key with the same prompt
  returns the existing run; the same key with a changed prompt is a
  `idempotency_conflict` (exit 5). Generate one stable key per prompt
  and record it; on uncertain delivery observe with `run events` /
  `run wait` instead of retrying under a new key.
- `--on-busy` is `queue` (default), `steer`, or `reject`. runtimes
  without true steering refuse `steer`; a refused steer never becomes
  a queued follow-up disguised as steering.
- Supported commands are exactly those in `describe`: `version`,
  `describe`, `doctor`, `plan create`, `plan resume`, `session create`,
  `session resume`, `session close`, `session list`, `run submit`,
  `run wait`, `run events`, `run interrupt`, `acp`. There is no
  `run status`, `run result`, or `run recover`; poll with `run events`
  and settle with `run wait`.

### MCP servers: never inline credentials

`session create` / `session resume` accept per-session stdio MCP
servers launch-only; they are never written to global or project
config and never echoed back with secret values. `--mcp-servers`
inline JSON is for entries without secrets only. Secret-bearing
configs must go through `--mcp-servers-file PATH` (`-` for piped
stdin); the two inputs are mutually exclusive. Verify the exact
spelling against `describe --command "session create"` before
running:

```sh
umask 077; set +x   # owner-only temp files from here on; never trace secret-bearing commands
CFG="$ROOT/mcp.json"   # owned temp secret config only — existing protected configs stay untouched
printf '%s\n' "$MCP_JSON" > "$CFG.tmp" && mv "$CFG.tmp" "$CFG"   # by reference, never typed
trap 'rm -f "$CFG"' EXIT   # remove only this owned temp file after the CLI returns, success or failure
"$ABS" session create --runtime codex --cwd /tmp/proj --root "$ROOT" --namespace "$NS" \
  --mcp-servers-file "$ROOT/mcp.json" --json
printf '%s\n' "$MCP_JSON" | "$ABS" session create --runtime codex --cwd /tmp/proj \
  --root "$ROOT" --namespace "$NS" --mcp-servers-file - --json
```

Prepare the file with no secret literals anywhere before the CLI:
prefer an existing protected config (it stays in place — never move
or delete it), else resolve values from the secret store or
environment by reference (`"$MCP_JSON"`, never a typed value) — no
literals in shell commands, heredocs, or `python -c`. Temp configs
and captures you create must be `0600` in the `0700` scope
(`umask 077`, xtrace off: `set +x`); remove only your owned temp
secret config in a trap after the CLI returns, success or failure —
never user configs, native histories, or control state. Never `cat`
or display a secret config; report only names, keys, and the
redacted plan. Keep secret configs and captures under `$ROOT`,
never generic `/tmp/*.json` names. Synthetic fixture values get
identical handling.

`cursor` and `pi` have no verified per-launch MCP path and refuse
servers (`unsupported`). Full entry shape, limits, and returned
fields: [per-session MCP servers](references/mcp.md).

## 4. Observe, settle, release (same scope)

```sh
"$ABS" run events --run-id "$RUN" --after 0 --root "$ROOT" --namespace "$NS" --json
"$ABS" run wait --run-id "$RUN" --timeout 120 --root "$ROOT" --namespace "$NS" --json
"$ABS" run interrupt --run-id "$RUN" --root "$ROOT" --namespace "$NS" --json
"$ABS" session close --session-id "$SES" --root "$ROOT" --namespace "$NS" --json
```

- A `wait` timeout (exit 6, `wait_timeout`) never cancels the work;
  read `run events` again to continue. `--follow` budget/timeout ends
  likewise never cancel.
- `run interrupt` only requests cancellation of exactly that run
  (`interrupt_requested`); it is confirmed when `run wait` / `run
  events` settles the run to `interrupted`. Never interrupt unrelated
  work.
- `session close` cancels owned work and releases resources but never
  deletes native history.
- `state: completed` means the turn ended; review the work before
  treating any task as done. Completion is not task acceptance.

## 5. Read outcomes from the envelope

Success: `ok: true`, `error: null`, exit 0. `data` carries
`dry_run` / `status` (`planned`, `created`, `resumed`, `submitted`,
`closed`, `interrupt_requested`), plus `session`, `run`
(`run_id`, `session_id`, `request_id`, `on_busy`), `launch`
(redacted `argv`/`env_keys`), `events` / `next_cursor` / `has_more`,
or `run` (`run_id`, `state`, `final_response`, `stop_reason`,
`error`, `evidence`). `meta` carries package/version.

Failure: `ok: false`, `data: null`, exit non-zero, with
`error: {code, message, hint, next_commands, details}`.
The ACP entry is the exception: it must keep stdout protocol-clean,
so serving refusals go to stderr as `acp: <message>` plus
`acp: hint: <hint>` with exit 2 and empty stdout.

| `error.code` | Exit | Meaning and recovery |
|---|---|---|
| `usage_error` | 2 | Fix arguments; one-step recovery via `hint` / `next_commands`. |
| `model_selection_unsupported`, `unsupported` | 2 | Refusal: unsupported runtime, model, MCP path, or ACP baseline. Do not retry with new spellings. |
| `not_found` | 3 | Exact id/path/cwd required; `session list` to locate. |
| `not_authorized` | 4 | Auth/lacks-permission; do not bypass. |
| `conflict`, `idempotency_conflict` | 5 | Same key, changed payload. Re-read the existing run; do not mint a new key to force it. |
| `wait_timeout` | 6 | Deadline passed, work continues. Observe again. |
| `uncertain_delivery` | 1 | Injection unverified. Observe via `run events` / `run wait`; never replay the write. |
| `failed` / `interrupted` / `unknown` states | 0 on `run wait` | Turn settled without success. `unknown` never masquerades as success or cancellation. |

Refusals and timeouts are informative, not retryable by rewording.
Unknown delivery never authorizes replay under any key.

## Boundaries

Never auto-grant approvals, write global or project config, grant new
MCP/database authority, or delete histories. This library controls
sessions across the five runtimes, but native conversation identity
never migrates: resume needs the exact native id/path of the same
runtime. One runtime per ACP invocation (`acp --runtime <id>`,
protocol-only stdout). Keep user content out of diagnostics; report
only ids, codes, and exits. Server setup lives in
[per-session MCP servers](references/mcp.md).

Retrospective history parsing belongs to SessKit, not to a control
turn: parsing-only questions never force-load this skill and never
submit a turn.

# Per-session MCP servers

`session create` and `session resume` accept per-session stdio MCP
servers for that launch only. Servers are never written to global or
project config, and secret values are never echoed back in output,
errors, or diagnostics — dry-run projections carry secret **names**
only (see `env_keys` below).

## Canonical stdio JSON entry

A JSON array of mappings with exactly these keys (`type`, when
present, must be `"stdio"`; remote `url` entries and any other key are
refused instead of being silently dropped):

```json
[
  {
    "name": "db",
    "command": "/usr/bin/mcp-db",
    "args": ["--ro"],
    "env": {"PASS": "<secret value lives only in the file>"}
  }
]
```

- `name`: required, `[A-Za-z0-9_-]`, unique within the session.
- `command`: required, nonempty absolute path, no control characters.
  `PATH` lookup is not per-session: resolve the executable first.
- `args`: optional list of strings (default `[]`).
- `env`: optional string-to-string map (default `{}`); keys must be
  plain process-env identifiers (`[A-Za-z_][A-Za-z0-9_]*`).

## Safe file/stdin use

`--mcp-servers` inline JSON is for entries **without** secrets only.
Secret-bearing configs must go through `--mcp-servers-file PATH`
(`-` reads piped stdin). The two inputs are mutually exclusive;
passing both is a `usage_error` (exit 2). The file must be non-empty
UTF-8 JSON (max 1,000,000 chars, no NUL byte); `-` with a TTY and no
pipe is refused.

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

## Input preparation (no literals anywhere before the CLI)

The safe channel starts before argv: secret values must never appear
as literals in a shell command, heredoc, or `python -c` fragment —
even synthetic fixture values get identical handling. Prefer an
existing protected config and point `--mcp-servers-file` at it
directly; it stays in place and is never moved or deleted. Otherwise
resolve values from the secret store or environment by reference and
write an owned temp file atomically without echoing, as shown above.
Temp configs and captures must be `0600` in the `0700` scope
(`umask 077`, xtrace off); remove only the owned temp secret config
in a trap after the CLI returns, success or failure — never user
configs, native histories, or control state.

Never `cat`, debug-print, or otherwise display a secret config or
secret tool arguments; report only server names, keys, and the
redacted plan. Keep secret configs and captures under the task-owned
root (`$ROOT`), never generic shared names like `/tmp/*.json`.

## Refusals

- `cursor` and `pi` have no verified per-launch MCP path: any servers
  are refused as `unsupported` (exit 2). Their SDK/CLI control is
  unaffected.
- Malformed JSON, non-array payload, bad entry shape, duplicate
  names, or unreadable/empty file: `usage_error` (exit 2) with no
  secret values in the message.

## Scope and returned fields

Scope is launch-only for exactly the created/resumed session of a
baseline runtime (`claude`, `codex`, `opencode`). A `--dry-run`
projection returns the redacted launch plan: `launch.argv` carries the
runtime's server overrides (e.g. `-c mcp_servers.db.command=...`)
**without** secret values, and `launch.env_keys` lists only the
secret names (`["PASS"]`). Values travel to the child by name at
spawn and never appear in envelopes.

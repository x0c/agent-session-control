# ACP compatibility

Status: implemented against the released stable baseline and covered by
protocol tests over a real stdio child, the production engine/store, a clean
installed wheel and Pi-review regression probes. Live-terminal turn
settlement stays engine-owned acceptance; backend/profile gaps are explicit
per profile, never counted as passes.

## Released baseline

- Protocol schema [`schema-v1.27.0`](https://github.com/agentclientprotocol/agent-client-protocol/releases/tag/schema-v1.27.0),
  source commit `97fcb0f5b88567dd91d585ea6885761365a6a7b`, released 2026-10-27.
  Wire major is v1. v2 (`schema/v2`) is a draft and is not served.
- Official Python SDK [`agent-client-protocol 0.12.1`](https://github.com/agentclientprotocol/python-sdk/releases/0.12.1),
  PyPI metadata requires Python >=3.10,<3.15. The library's `acp` extra pins
  `agent-client-protocol>=0.12.1,<0.13`.
- A schema release number is not a wire major: SDK coverage was checked
  against the released stable schema, not inferred from versions. Wire
  validation in tests uses the vendored `tests/fixtures/acp/schema-v1.27.0.json`,
  never the SDK's draft surface.

Inspected outside the workspace before adapting:

- `python-sdk@0.12.1`: `examples/echo_agent.py`, `src/acp/interfaces.py`,
  `src/acp/agent/{connection,router}.py`, `src/acp/client/{connection,router}.py`,
  `src/acp/{core,stdio,router,exceptions,helpers,meta,schema}.py`.
- `agent-client-protocol@schema-v1.27.0`: `schema/v1/schema.json`,
  `schema/v1/meta.json` and the `session-setup`, `prompt-turn`
  (cancellation, stop reasons, tool calls), `initialization`,
  `session-delete` and `error` protocol pages at <https://agentclientprotocol.com/protocol/v1/>.

## SDK versus released-schema gaps (verified, not assumed)

| # | Gap | Handling |
|---|---|---|
| G1 | SDK `meta.py` is generated from schema-v1.19.0 while the release is v1.27.0. | All wire claims were checked against `schema/v1/schema.json` + `meta.json` directly. |
| G2 | SDK gates `session/resume` and `session/close` behind `use_unstable_protocol`, but v1.27.0 declares both stable. | `serve_stdio` passes `use_unstable_protocol=True` solely to unblock those two stable routes. No draft method is implemented, so the flag changes nothing else observable. |
| G3 | SDK exposes methods absent from stable v1.27.0 (`session/fork`, `providers/*`, `mcp/message`, `document/*`, `nes/*`). | None are implemented or advertised; the router answers them with `-32601`. `session/fork` stays unimplemented even though the unstable flag would route it. |
| G4 | SDK `ToolCallProgress.session_update` serializes as `tool_call_update`. | Matched: the adapter emits `tool_call` for starts and `tool_call_update` for results, as the released schema requires. |
| G5 | SDK `NdjsonTransport` logs unparsable lines and continues with no `-32700`. | Covered by test: malformed input is survived, the next valid request still answers, EOF exits 0. |
| G6 | SDK `ListSessionsResponse`/`SessionInfo` serialize `None` optionals away on the wire (`exclude_none`). | Schema validation in tests uses the same wire form. |
| G7 | SDK `x-deserialize-skip-invalid-items` silently drops MCP entries that miss required SDK fields (e.g. no `args`/`env`) before the agent ever sees them. | Documented here; tests use complete entries. Clients must send complete `name`/`command`/`args`/`env` objects — a dropped entry is never reported back. |

Upstream references: SDK router flagging in
`python-sdk@0.12.1/src/acp/agent/router.py` (`unstable=True` on close/resume/fork);
stable method set in `agent-client-protocol@schema-v1.27.0/schema/v1/meta.json`;
skip-invalid-items behavior in the SDK's generated `schema.py` field metadata.

## Capability policy (explicit verified values only)

`ControlAgent(controller, runtime)` serves one explicitly selected runtime per
invocation. Each optional feature needs an explicit opt-in — `True` or an
agreed `{"supported": true}` — plus an implemented controller method. A
failed probe, a missing key or an explicit opt-out advertises nothing. MCP
`http`/`sse` stay unadvertised without an explicit opt-in. Link block forms
follow the backend's `prompt_blocks`, defaulting to the canonical form.

`session/prompt` requires a session created, loaded or resumed on the same
connection first: unknown ids fail `-32002` (no auto-resume, no blank
replacement). One v1 turn is reserved per session before the first await; a
second prompt during starting or active states fails `-32602` `turn_active`.
Cancel scopes to the reservation: with no reservation it is a no-op and never
poisons a future prompt. Close serializes on the same lifecycle state and
commits attachment removal only after the controller reports success, so a
failed close retains binding and live work for cancel/recovery.

## Method and capability matrix

| Method | Support | Notes |
|---|---|---|
| `initialize` | yes | Returns wire major 1 with truthful per-runtime caps; `image`/`audio`/`embeddedContext` always false, `mcp http/sse` false. |
| `session/new` | yes | `cwd` must be absolute; non-empty `additionalDirectories` refused (`-32602`); non-stdio MCP refused (`-32602`); stdio entries translated to the neutral shape and validated at the foundation boundary. |
| `session/load` | optional (`loadSession`) | Replays full stored history as `session/update` (user/agent/thought/tool chunks), then responds. Unmappable content fails the load instead of silent loss. |
| `session/resume` | optional (`sessionCapabilities.resume`) | Reconnects with **no** replay. Unknown ids fail `-32002`. |
| `session/prompt` | yes | Stays pending while the run settles; streams `agent_message_chunk`/`agent_thought_chunk` deltas in order, `tool_call`/`tool_call_update` for observed calls and results. One `queue` disposition; no steer flag exists on the wire. |
| `session/cancel` | yes | Notification; interrupts only the reservation's run (never unrelated runs). The pending prompt then returns `cancelled`. |
| `session/list` | optional (`sessionCapabilities.list`) | Maps controller rows to `SessionInfo`; engine cursor input is passed through as returned. |
| `session/close` | optional (`sessionCapabilities.close`) | Cancels owned work, waits briefly for settlement confirmation, releases resources, never deletes history. Unknown ids still succeed (idempotent close); `cancelConfirmed` rides in `_meta`. Failed close keeps the attachment. |
| `authenticate`, `session/set_mode`, `session/set_config_option`, `session/fork`, client terminal/filesystem/permission bridges | no | Answered `-32601` / never initiated. No pre-execution permissions, client buffers, token streams or mode state are fabricated. |

## Evidence and streaming semantics

- **Ordering:** updates stream before the settled `session/prompt` response.
  Stable identity is the native message (generation + native seq or call
  id, falling back to the store seq), so repeated polls of one snapshot
  never duplicate and cumulative text arrives as deltas under one message id
  per native message. Assistant and tool delivery are tracked separately.
- **Lossless reconciliation:** compact summaries append an omission marker;
  the adapter strips that marker (it is transport, not native content) and
  reconciles the stripped prefix against the terminal result, delivering
  exactly the missing tail — or the whole result when nothing streamed.
  Never streams the marker as native text, never resends, never invents
  correction chatter. The shared lossless `project_event` path is the
  forward direction once the engine serves it on the live path.
- **Allowlist:** only assistant text, thinking and tool call/result updates
  stream on the live turn. Thinking replays as thought chunks (never agent
  text); user input, diagnostics, state and unknown kinds are counted, never
  converted. Tool results resolve to their stable call id with true
  ok/error/unknown status and available content; a missed start is announced
  so the result keeps its identity.
- **Stop reasons:** `end_turn`/`max_tokens`/`max_turn_requests`/`refusal`
  pass through; anything else on a completed turn is `-32603`; completed
  without native or inferred evidence is `-32603`; `interrupted` maps to
  `cancelled` with cancellation provenance in `_meta`; `failed`/`unknown`
  are `-32603` with structured data. Failed/unknown settlements always get
  an adapter-marked note with the controller's error.
- **Errors:** unknown sessions `-32002` (with `sessionId`), unsupported values
  `-32602`, authentication `-32000`, unexpected failures `-32603`.
- **MCP baseline:** stdio entries are structurally accepted; `http`/`sse` are
  refused because no capability is advertised. Per-session MCP is currently
  supported by the claude, codex and opencode profiles and refused by the
  others before anything launches; the refusal names only the server, never
  secret values, and leaves no hosted pane behind. Secret values travel only
  in the private per-session env/argv the launcher needs and never appear in
  errors, events, diagnostics, stored run specs, display plans or snapshots.
- **No replay:** `session/load` replays, `session/resume` never does.
  Recovery never resubmits uncertain input; request ids are fresh per prompt
  so a client retry after transport loss cannot silently merge two turns.
- **Stdout discipline:** `acp --runtime <id>` owns stdout for protocol
  bytes only; envelopes, logs and diagnostics go to stderr or are absent.

## Protocol acceptance

- `tests/test_acp_agent.py`: handler tests over the deterministic fake plus
  stdio tests over a real child with the official client (handshake/caps,
  content and resource links incl. legacy-alias negotiation, update ordering,
  Unicode and ~120 KiB messages, failures, unknown outcomes, concurrent
  sessions, gated cancel-during-start and parallel-prompt races,
  EOF with an active run, optional load/resume/list/close, unsupported and
  malformed input, clean EOF, production `compact_event`/`build_result`
  lossless projections, full `project_event` path, tool-only turns).
- `tests/test_acp_real.py`: adapter against the real production controller
  and store with only terminal hosting doubled (MCP pass-through/refusal,
  durable acceptance, interrupt delivery, wait-timeout honesty, unknown
  sessions, missing history, list/close lifecycle, failed-close retention,
  no secret leakage into stored specs).
- `tests/test_acp_cli.py`: JSON CLI contract (incl. dry-run redaction probes
  for MCP-capable profiles on stdout+stderr) plus a packaged `acp` handshake
  against the real engine (initialize, list, idempotent close, unknown
  prompt refusal, MCP refusal with no hosted pane).
- `tests/test_acp_install.py`: clean installed wheel with no PYTHONPATH
  (version/describe/doctor, dry runs, real store error paths, packaged stdio
  handshake). Preparation failures fail loudly, never skip.
- Representative results, notifications and errors are validated against the
  vendored released schema with the SDK's own wire form.

Run: `.venv/bin/python -m pytest -W error -q tests/test_acp_agent.py tests/test_acp_real.py tests/test_acp_cli.py tests/test_acp_install.py`.

Still engine-owned: live-terminal turn settlement that spends quota,
per-runtime login/quota paths, future `session/list` cursor semantics, and
the engine live/load/final switch from bounded `compact_event` to lossless
`project_event`. Backend/profile gaps stay explicit per profile rather than
counted as passes.

## JSON CLI surface (agent-friendly, no UI)

`python -m agent_session_control <command> --json` prints one
`{ok, data, error, meta}` envelope on stdout; logs stay on stderr. Non-TTY
output is machine JSON without needing `--json`. Stable exits: 0 ok,
2 usage, 3 not found, 4 auth, 5 conflict, 6 wait deadline, 1 other failures.

`version`, `describe`, `doctor`, `plan create|resume` are read-only and work
offline. `session create|resume|close|list` and `run submit|wait|events|interrupt`
share the engine controller with `--root`/`--namespace` isolation;
`--dry-run` performs no side effect and creates no state, and plan/dry-run
output uses the redacted `LaunchPlan.to_dict()` projection (no environment
values, secret-free argv). `run events --follow` streams JSONL
(`start`/`record`/`end`) with a time/row budget instead of the envelope.
Every subcommand registers only flags it reads; `describe` is generated from
the same table as the parser. There is no `run status`, `run result` or
`run recover`: the controller port offers no such operation, so the CLI does
not invent one (result and liveness arrive via `run wait`/`run events`).

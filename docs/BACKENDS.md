# Control backends

Status: engine implementation revised per Pi reliability review (R3–R8) and
acceptance-2 corrections, 2026-10-30. This records the extracted
runtime/terminal mechanisms, host injection hooks, SessKit observation,
process/input ownership, persistent supervision, cancellation evidence and
tested per-runtime limits. No runtime or platform coverage is claimed beyond
the evidence in the last section. Tests never attach to, stop or mutate
existing user sessions. Native history interpretation remains owned by
SessKit.

## Modules

| Module | Owns |
|---|---|
| `terminal.py` | Owned tmux hosting on root+namespace sockets, launch-instance identity, verified paste/submit, readiness, runtime interrupt sequences, pane liveness, input locks. Stdlib only. |
| `history.py` | Thin typed wrappers over the sole shared correlation implementation plus SessKit adaptation (public readers/outcomes/scans, lazy import). No native parser, no duplicated algorithm. |
| `supervisor.py` | Generic per-run supervision (admit, deliver once, observe, settle) over injected `SupervisorHooks`; run/target file locks; crash recovery without replay; detached entry. Usable by `LocalController` and by the existing host. |
| `controller.py` | Production `LocalController` implementing the `SessionController` port; durable session registry; exact native binding; plan/host/submit/wait/interrupt/close/detach; shared hook builder used in-process and detached. |

## Extracted mechanics and source attribution

Adapted from Corral (MIT, corral contributors); product coupling (layout,
titles, attention boards, transport, notifications) was deliberately left
behind:

- `terminal.py`: `embed.py` (`host_session` detached launch, `capture`,
  per-call paste buffers, send-result classification, `pane_liveness`
  three-state probe) and `keepalive.py` (dedicated server socket, clean
  child env, same-name pane reuse). The control-channel fast path, theme/OSC
  handling, viewer registries and navigation state were not carried over.
- `supervisor.py`: `control_worker.py` `Supervisor` (admission with
  queue/steer/reject, dispatch-once with certain-rejected vs
  uncertain-unknown, SessKit correlation loop, native-settles-at-once vs
  inferred-settles-after-quiet, mixed-attribution honesty, crash recovery
  that never retypes) and `control_store.py`/`control.py` (run/target locks,
  idempotency, audit).
- `history.py`: thin wrappers over foundation `correlation` (sole
  implementation of marker/wire/own/compact/correlate/result); lazy public
  SessKit use per `runtime/sesskit_bridge.py`.
- `controller.py`: `remote/sessions.py` (readiness gates, verified submit,
  post-inject liveness, interrupt sequences, resume/host flows,
  composer/draft guards) reduced to hook calls; composer-navigation restore,
  managed identity prep and native-claim preparation stay host-injected.

Two verified environment differences from the source host: this machine's
tmux 3.8 does **not** read stdin on `set-buffer -b name` (`"no data
specified"`, exit 1), so pastes go through `load-buffer -b name -`, which
does; and a missing server reports `error connecting to ...`, which counts
as authoritative target loss (dead), same as `can't find session`.

## Runtime profiles (from foundation `runtime.py`, verified 2026-10-30)

The engine consumes profiles; it does not define runtimes. No native
identity is reserved or fabricated at create time for any runtime: binding
is earned afterwards through exact evidence (see below).

| Runtime | Interrupt keys | Composer rule | Model flag | Resume plan | Binding path |
|---|---|---|---|---|---|
| claude | Escape | between_rules | `--model` | `--resume <exact-id>` | exact-id scan, marker evidence, or host claim |
| codex | Escape | codex_chevron | `--model` | `resume --no-daemon <exact-id>` | marker evidence or host claim (no preassign flag) |
| opencode | Escape, Escape | opencode_bars | none (`model_selection_unsupported`) | `-s <exact-id>` | marker evidence or host claim |
| cursor | C-c | cursor_arrow | `--model` | `--resume <exact-id>` | marker evidence or host claim |
| pi | Escape | between_rules | `--model` | `--session <exact-path>` | path-addressed claim or marker evidence |

`capabilities()` reports these honestly per profile: `steer` is false for
every built-in (no true steering in the generic backend), `interrupt`
reflects the key table, `model_selection` reflects the flag table, MCP is
`unsupported` unless a profile declares `supports_mcp`, `resume` states its
exact-locator requirement, `load_history` requires a bound native history.
Availability means executable presence only, never login or quota.

## Host injection hooks (for the integration worker)

`SupervisorHooks` (`supervisor.py`) is the full interface. Every side
effect is a named callable; missing callables raise `TargetError` with a
stable code instead of guessing:

- `resolve_target(session_ref) -> dict`: exact session mapping with
  `id/source/runtime/cwd/path/pane/live` plus `native_id/title/
  identity_evidence`. No newest-file/cwd/PID guessing.
- `target_busy(session) -> bool`, `target_waiting(session) -> bool`:
  admission gates for queue/steer/reject.
- `host_fresh(spec, ident) -> {session_ref, pane, native_id?}`: launch and
  host a create run. Repeated idents must reuse the pane, never launch twice.
- `prepare(session_ref, deadline) -> baseline`: readiness gate (startup
  markers, blocked-dialog refusal, composer checks live here).
- `submit(session_ref, wire_text)`: verified paste plus submit. Raises
  `CertainFailure` (nothing injected, retryable) or `UncertainDelivery`
  (unknown, never replayed).
- `verify(session_ref, baseline)`: post-inject liveness gate; only a
  proven-dead pane becomes uncertain.
- `interrupt(session_ref)`: the runtime's own key sequence, separate writes
  with a gap. Never resumes or recovers.
- `answer(session_ref, request_id, answers) -> {status, detail}`:
  structured question path; only `delivered` counts as submitted.
- `steer(session_ref, wire_text)`: true mid-turn steering. Backends without
  it raise `CertainFailure("steer_unsupported", ...)`; the controller rejects
  `on_busy="steer"` up front for the same profiles.
- `open_observation(binding) -> handle` / `poll_observation(handle, run_id,
  baseline_seq) -> (observation, reset, state)` /
  `close_observation(handle)`: SessKit reader lifecycle. Exactly one reader
  and cursor live per handle; polls are incremental, teardown closes it.
  `reset=True` with established attribution means the native history was
  replaced and the run becomes unknown. Hosts own this lifecycle, so
  continuation-following (e.g. superseded histories) belongs inside the
  host's `poll_observation`, not in a second engine executor.
- `baseline_of(binding) -> int`: last native seq before dispatch (-1 unknown).
- `bind_native(binding) -> binding|None`: resolve the exact native session
  (exact id, marker evidence, or host claim). Corral's `_bind_native`
  adapts here 1:1 (its True/False/"locked" maps to binding/None/retry);
  mid-observation continuation stays inside the host's poll implementation.
- `is_alive(session_ref) -> alive|dead|unknown`: only authoritative
  target loss is `dead`. Timeouts and spawn failures stay `unknown` and
  never trigger cleanup or a competing resume.
- `screen_hash(session_ref) -> str|None`, `phase(session_ref)`:
  `working|waiting|idle|unknown` for the inferred-settlement quiet window.
- `list_questions(session_ref) -> [...]`: pending interaction records.
- `close_owned(session_ref)`: release the owned pane/process for `close`.

`HostHooks` (`controller.py`) covers host-owned preparation:
`prepare_launch(plan, runtime, ident) -> (plan, env)` for managed identity,
`restore_composer(pane, runtime)` for composer-navigation restore,
`session_title(spec)`, and `claim_native(session_ref, runtime, pane,
native_hint) -> {native_id, history_path, evidence}` for proven managed
process/history claims on runtimes without preassignable ids.
`controller.build_hooks(...)` is the single shared builder used both
in-process and by the detached supervisor factory; there is no second
executor. Custom in-process ports with the default detached factory are
rejected before acceptance unless a serializable factory is supplied.

To delegate the existing host executor instead of keeping a second one:
implement `SupervisorHooks` with the host's terminal/observer and call
`run_supervisor(store, run_id, hooks)`. The runner never takes the run lock
itself (sole ownership stays at the entry); host entry points must not
re-enter it either.

## SessKit observation

Used public surface only (verified against SessKit 0.2.21): `load_activity`,
`open_activity_reader` (+ `poll()` → `events/reset/generation/cursor/
outcome/state`), `derive_turns(snapshot, runtime=...)`, exact-id scans with
`keep_ids`, and `load_session_conversation` for marker-evidence binding.
Load states `available`/`empty` are usable; `unavailable`/`unsupported`
raise `HistoryError` and the run stays unknown. A retained reader's
`reset=True` with established attribution means replacement: the run
becomes unknown (`history_reset`), never silently rebound. Turn `status`
values settle `completed`/`failed`/`interrupted` (`rejected` maps to
`failed`); `awaiting_input`, pending interactions and a `waiting` phase mean
`waiting_input`, never terminal. Native evidence settles immediately;
inferred evidence settles only after history and screen quiet plus no
`working`/`waiting` phase (default 8 s, configurable). `mixed` attribution
with a terminal outcome settles `unknown` (`mixed_attribution`), never an
exclusive result. Wire markers default to the foundation correlation
namespace `session-control`; compatibility hosts inject their legacy
namespace explicitly (generic code hardcodes none). Prompt blocks are
canonical `text`/`resource_link` (`resource` is the legacy URI-only alias);
the wire rendering keeps each link's metadata on an explicit line.

Losslessness note: `history.project_event` / `build_result(..., text_limit)`
delegate to `correlation.project_event` / extended `build_result`, which the
foundation has landed — replay and final responses are complete by default
(`text_limit=None`) and capped only on explicit request. The delegation was
verified live (5000-char event round-trips uncapped; capped projection
applies the bound). No second projection algorithm lives in the engine.

## Ownership, durability and cancellation

- One tmux server socket per control root and exact namespace
  (`asc-<short>-<hash12>` over root + case-sensitive namespace); pane names
  are handles only. Pane records (`panes/<socket>-<pane>.json`) persist the
  socket, namespace, root, runtime, ident and the live `(pane_id, pane_pid)`
  instance. Reuse and close verify the current instance against the record:
  a replacement process under a reused name is never adopted or killed, and
  an unreadable instance is never proof. Records are removed only after
  proven death.
- Run locks (`run:<id>`) and target locks (`target:<session>`) are
  cross-process `fcntl` locks under `<root>/locks`, shared by every process
  on the same root. Runs on one session dispatch while an older unsettled
  run holds the target lock, bounded by the spec's queue timeout. A separate
  `lifecycle:<session>` lock serializes resume/close/detach binding changes
  (short operations only; supervisors never hold it, so close works during
  active runs).
- Pane input locks are reentrant thread locks with one advisory file lock at
  the outer edge (`terminal.PaneInputLock`); `submit_text` holds the pane
  lock across paste-to-Enter so concurrent writers cannot concatenate lines.
  Paste buffers are unique per call.
- Durable intent (spec + audit) precedes side effects. `start_turn` returns
  after acceptance; an identical retry (or explicit `recover_run`) reconciles
  run-lock ownership and relaunches orphaned supervision without replaying
  uncertain writes. `accepted` runs that never dispatched resume safely;
  `dispatching` (or any message stuck in `dispatching`) becomes `unknown` on
  recovery and is never retyped.
- `interrupt(run_id)` enqueues a cancellation for that exact run: before
  dispatch it cancels locally (rejected/interrupted, nothing sent); after
  dispatch it requires positively correlated current-turn evidence, else the
  message is rejected. A delivered interrupt plus quiet evidence confirms
  `interrupted` even when the runtime records no native end.
- `resume` reuses a proven-live owned pane without forking, refuses unknown
  liveness, refuses live-but-unowned panes as lost ownership (never starts a
  competing process on the same history), recovers only provably dead panes
  through the exact native plan, preserves id/path/cwd/title, validates cwd
  overrides, serializes per target, and never resumes a closed session.
- `close_session` holds lifecycle ownership, re-reads the session, releases
  the pane only when actually gone (uncertain kills keep the binding and
  raise), cancels pre-dispatch runs, interrupts active ones, preserves
  natural completions racing the cancel, and marks closed without deleting
  history. `detach_session` only marks the session detached while accepted
  work continues.
- Crash settlement: failure before input is `rejected`/`failed`; anything
  later is `unknown` with the original error preserved.

## Tested limits (evidence, not coverage claims)

- Real owned-terminal path: disposable echo agent on unique sockets —
  host/capture/liveness/close, multiline/Unicode paste/submit round-trip,
  `C-c` interrupt killing the pane process, two concurrent writers landing
  intact, socket isolation incl. case-distinct namespaces, same-ident reuse,
  foreign-pane close refusal, replacement-process refusal with the
  replacement left alive, dead-pane record cleanup (`tests/
  test_engine_terminal.py`, 16 tests).
- Supervisor semantics over fake observer/backend: native settle, certain
  vs uncertain dispatch, no-replay recovery, binding retries and bounded
  give-up, missing-history bound, generation reset, mixed attribution,
  queued vs active cancellation, cross-process file locks, target-lock
  serialization, queue-expiry as failed work (`tests/
  test_engine_supervisor.py`, 19 tests).
- History: marker positions incl. long briefs, baseline exclusion,
  quoted-marker exclusion, mixed detection, result counts, native
  stop-reason carriage without invention, projection-delegation shims, plus
  a real `derive_turns` correlation on synthetic SessKit models (`tests/
  test_engine_history.py`, 13 tests).
- Controller port: frozen-handle persistence, verbatim caller paths,
  unknown/blank resume refusal, live reuse without forking, dead-pane
  recovery preserving identity, unknown-liveness and live-unowned refusals,
  concurrent-resume single launch, path-addressed resume argv, idempotent
  request keys with conflict, shared-store convergence, steer rejection,
  resource-link preservation incl. legacy alias, rich-block rejection,
  MCP-to-plan-env with storage-secret scan, wait-timeout-never-cancels,
  honest close (failure/race paths), detach, cursor paging, detachability
  gate, claim/scan native binding, `SessionController` shape, real
  foundation `RunStore` end-to-end with audit, real-tmux round trip, and
  detached-supervisor survival with child reaping (`tests/
  test_engine_controller.py`, 36 tests).
- Real native proof (`tests/test_engine_native.py`): production-default
  detached codex turn from a separate exiting caller — submitted, bound by
  marker evidence to the exact native thread, observed through SessKit, and
  settled `completed` with native finality and the returned `PONG`.
- Gates on engine files: `ruff check`, `ruff format --check` and `pyrefly
  check` are clean; owned pytest files pass warning-clean (`-W error`).
- Explicitly **not** established here: Claude turns (account weekly quota
  exhausted until Nov 1 — verified on-pane; no inference possible),
  OpenCode/Cursor/Pi live turns (not probed with models; executables
  present, logins unverified), Windows or non-tmux platforms,
  hostile-tenant isolation, and billing/cost semantics. Replay and final
  responses are lossless by default through foundation `project_event` /
  `build_result(..., text_limit=None)` (verified live). Unsupported paths
  fail with explicit codes instead of passing silently.

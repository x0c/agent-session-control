# Shared implementation contract

Status: implementation boundary for 0.1.0, 2026-10-30; verified by the
release gate (see README). Later changes keep this contract backward
compatible or bump the version.

## Package and dependencies

Distribution/CLI: `agent-session-control`; import: `agent_session_control`; initial version 0.1.0. Python >=3.10,<3.15 initially, matching the published optional ACP SDK. Core is standard-library only; optional `history` uses SessKit >=0.2.21, optional `acp` uses official `agent-client-protocol>=0.12.1,<0.13`. Dev dependencies include pytest, pytest-asyncio, Ruff, Pyrefly, build and schema validation as needed.

Top-level imports must not load ACP, SessKit, a UI framework, native histories, storage or terminals. Public API lives in explicit modules; lazy top-level conveniences are allowed. Library metadata cannot contain local checkout paths. uv may pin SessKit to its existing immutable published wheel; final host integration uses a versioned library artifact, not an editable/path dependency.

## Foundation modules

### errors

`ControlError(code, message, exit_code=1, *, hint=None, next_commands=None, details=None)` retains the extraction source's observable fields. Stable codes distinguish usage, not-found, conflict, unavailable/unsupported, timeout, uncertain delivery, lost ownership and upstream execution failure. The CLI maps errors to `{ok,data,error,meta}`; ACP uses protocol errors, not the CLI envelope on the wire. Preserve the native error/evidence without exposing credentials.

### runtime

- `LaunchPlan`: immutable `argv: tuple[str,...]`, `cwd: str | None`, `env: Mapping[str,str]` with an empty default; never a shell command.
- `build_create_plan(runtime, cwd, *, model=None, native_id=None, auto_approve=False, mcp_servers=()) -> LaunchPlan`.
- `build_resume_plan(runtime, session, *, auto_approve=False, mcp_servers=()) -> LaunchPlan`, where session is a normalized mapping with exact `id`/`path`/`cwd` fields. Do not rescan to guess the session. Preserve native resume identity and fail rather than create a blank replacement.
- `runtime_profile(runtime)` provides executable aliases, availability, interrupt keys, composer/readiness helpers and explicit capability limitations. Backend-specific behavior does not leak into host callers.
- A host may supply a runtime profile/registry, so support is not hard-coded to one downstream allowlist. Initial built-ins adapt `claude`, `codex`, `opencode`, `cursor`, `pi`. No dormant runtime maintenance is authorized.
- Preserve caller defaults. Source-host approval behavior is supplied explicitly with `auto_approve=True`; the generic library does not globally relax approvals. Model override is per launch and observed model stays separate.
- MCP configuration must be per-session/process and private; never persist credentials in plans returned for display, logs, snapshots, model context or global config. Unsupported configuration fails before launching.

### store and correlation

Extract the existing `RunStore`, stable state constants, `digest`, `run_version` and error/exit semantics with configurable root and supervisor/host identity. Default root is `XDG_CONFIG_HOME/agent-session-control/control`; source-host wrappers inject their historical root and names. The existing host store remains exactly `<host root>/runs.sqlite3`; use the store's default-file selection explicitly rather than silently migrating to a namespace-suffixed new database. Marker, actor and supervisor identifiers are independent of the filename. Read-only access never creates storage. Existing same-transaction audit and immutable idempotency semantics are preserved.

Extract `marker`, `wire_prompt`, `own_message`, `compact_event`, `correlate` and `build_result` into `correlation`, allowing an explicit marker namespace. This module is the sole implementation; `history` may wrap it for `ActivityObservation` types but must not duplicate correlation, marker matching or usage/result aggregation. The neutral default must not mention a downstream product. Compatibility hosts may select their legacy marker namespace. Only native user-message evidence can establish input recording; quoted markers in assistant/tool output cannot. SessKit events/turns are accepted through their public attributes, not reparsed from raw native files. Usage observations are not blindly additive when repeated native evidence represents the same usage.

`project_event(event, *, text_limit=None)` is the shared full-fidelity projection for native content delivery, retaining available message/call identity, roles, result/error records and provenance without parsing raw history. `compact_event` keeps the historical bounded query shape. `build_result(observation, *, text_limit=20000)` retains that host-compatibility default; selecting `text_limit=None` supplies the complete final response for neutral protocol delivery. Engine live/load/result paths must explicitly choose full fidelity; bounded summaries and synthetic omission suffixes cannot be presented as full native responses. The underlying interpretation remains SessKit-owned.

## Shared models (models.py)

Use typed dataclasses/Protocols; JSON serialization is explicit. Fields below are the public contract; implementations may add optional fields without requiring callers to supply them.

- `SessionHandle`: `id: str` (opaque local control ID), `runtime: str`, `cwd: str`, `native_id: str | None`, `instance_id: str | None`. Native IDs are exact and never replaced by a shortened local control ID.
- `ControlEvent`: `seq: int`, `kind: str`, `session_id: str`, optional `run_id: str | None`, `data: dict`, `evidence: str` (default `unknown`), `generation: str` (opaque). Event sequence is meaningful only within its generation; replay/reset is explicit.
- `RunResult`: `run_id: str`, `state: str` (`completed`, `failed`, `interrupted`, `unknown`), optional `final_response`, `stop_reason`, `error` (structured dict), and `evidence` (default `unknown`). Stop reasons reflect actual evidence; unknown cannot masquerade as `end_turn` or `cancelled`.

## Controller port (models.SessionController)

The ACP adapter accepts any object satisfying this async port; production CLI uses `controller.LocalController`. Methods exchange the models above. A backend used only in tests cannot become the undocumented production implementation.

```python
# Interface declaration in prose; not executable implementation.
def capabilities(self, runtime: str) -> dict: ...
async def create(self, runtime: str, cwd: str, *, model: str | None = None,
                 mcp_servers=(), actor: str = "local-user") -> SessionHandle: ...
async def resume(self, session_id: str, *, cwd: str | None = None,
                 mcp_servers=(), actor: str = "local-user") -> SessionHandle: ...
async def start_turn(self, session_id: str, prompt: list[dict], *,
                     request_id: str, on_busy: str = "queue",
                     actor: str = "local-user") -> str: ...  # durable run ID
# watch is an async iterator; calling it does not itself require await.
def watch(self, run_id: str, *, after: int = 0): ...  # AsyncIterator[ControlEvent]
async def wait(self, run_id: str, *, timeout: float | None = None) -> RunResult: ...
async def interrupt(self, run_id: str, *, actor: str = "local-user") -> None: ...
async def close_session(self, session_id: str, *, actor: str = "local-user") -> None: ...
async def list_sessions(self, *, cwd: str | None = None,
                        cursor: str | None = None) -> dict: ...
async def load_history(self, session_id: str) -> list[ControlEvent]: ...
```

`LocalController` accepts explicit `root: Path | None`, `namespace: str`, runtime/terminal/observer hooks and approval policy. Constructor is side-effect free. Injectable host hooks are documented in [BACKENDS](BACKENDS.md) and must not reverse-import a host. Persisted SDK/CLI operations on the same root/namespace share process-owned target/input locks and one authoritative operation store. Different namespaces are not tenant security isolation.

Prompt content uses ACP-shaped content blocks for an interchange structure, without importing the ACP SDK into core. Canonical block types are `text` and `resource_link`; `resource_link` has `uri` plus optional `name`/`title`/`mimeType` metadata. A legacy neutral `resource` alias, if retained, means only a URI link and must not be confused with ACP embedded `resource` content. Normalize at the foundation boundary and use the canonical form between components. Plain text and resource links are baseline for ACP; rich content is explicitly supported or rejected. Resource data is not silently flattened in ways that lose meaning. `on_busy` distinguishes queue, steer and reject; unsupported true steering must not become a queued follow-up disguised as successful steering.

`start_turn` returns after durable acceptance; `watch` and `wait` prove later states. A wait timeout never cancels work. Reusing an immutable request ID returns the existing operation; a changed payload conflicts. `interrupt` requests cancellation of that exact run, never an unrelated current turn. `wait`/terminal evidence establishes actual stop. `close_session` cancels and releases owned resources but never deletes history. An explicit detach/shutdown extension may stop observing without cancelling accepted work; that is not close.

## ACP adapter and CLI

The protocol adapter is `acp.ControlAgent`, constructed with `controller: SessionController` and an explicitly selected runtime. Prefer one runtime per ACP invocation so initialize capability claims are truthful for every session on that connection; do not combine the strongest capabilities from different runtimes.

`python -m agent_session_control acp --runtime <id>` serves ACP stdio. SDK `run_agent` owns framing and lifecycle where applicable. `python -m agent_session_control describe|version|doctor|plan|session|run ...` provides a separate agent-friendly JSON CLI sharing the same controller. `--root` and `--namespace` make isolated use/test setup explicit. ACP stdout is protocol messages only; ordinary CLI JSON envelopes are never inserted into it.

ACP v1 `session/prompt` stays pending until its corresponding run settles while updates stream. Preserve latest-schema capability negotiation, load versus resume semantics, actual cancellation confirmation, stdio MCP baseline and per-session configuration. Unsupported optional features are not advertised. Client filesystem/terminal delegation and approval bridging are not fabricated from post-execution history. Protocol errors and diagnostic metadata report unavailable/unknown outcomes without inventing standard stop reasons.

Locking, exact-instance/binding, recovery and reader-lifetime rules: one lock
owner per session; resume/close/detach share the lifecycle lock; live panes
that are not verifiably owned are never adopted or killed; readers have one
owner with explicit teardown; accepted work is reconciled, never replayed
when uncertain. See [BACKENDS](BACKENDS.md). ACP baseline/optional behavior
and SDK differences are recorded in [ACP](ACP.md). This port is a selected
implementation contract, not evidence that every backend/profile has passed
those requirements.

## Source-host integration

Replace generic mechanisms with imports/delegation to the independent package. Keep legacy names/exports as thin compatibility facades where existing callers/tests depend on them. The host injects its state root, actor/marker namespace, auto-approval policy and managed identity preparation; the library never infers these from host-prefixed environment variables. Preserve existing query/plan read-only contracts, tmux/socket ownership, working/waiting protection and live input behavior. Do not implement a new runtime parser or route ordinary host control through an ACP subprocess.

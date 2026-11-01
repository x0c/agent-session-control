# Agent session control

Independent Python library and JSON CLI for coding-agent session control, with an optional ACP agent interface. This is not a history parser, UI framework, task planner or vendor SDK replacement. No downstream-host coupling: no downstream product names, host-specific paths, or host-owned semantics belong in docs, code, or schemas.

Follow the [shared Python standards](~/Codes/_standards/python.md): standard packaging and typing conventions (typed dataclasses/Protocols, Ruff, one type checker).

## Documentation index

Read documents whose described content is relevant before decisions or changes.

- [Design](docs/DESIGN.md): Approved scope, ownership, reference reuse, dependency direction, parallel file ownership and acceptance.
- [Contract](docs/CONTRACT.md): Public models/controller port, full-fidelity versus bounded projections, errors and source-host compatibility.
- [Integration](docs/INTEGRATION.md): Distribution, SDK/CLI adoption, companion Skill acceptance and verification limits.
- [Backends](docs/BACKENDS.md): Terminal control, SessKit observation, ownership and runtime coverage.
- [ACP](docs/ACP.md): Released protocol/schema baseline, capability mapping, SDK differences and protocol acceptance.
- [Security](SECURITY.md): Supported security-fix versions, private reporting and evidence redaction.
- Local `TASKBOARD.md` (ignored, created when needed): Active work ownership only; no public task history.

## Boundaries

- No dependency on a downstream host, its paths, environment-variable prefixes, UI, configuration, titles, notifications, transport or policy. Native history parsing belongs to SessKit and must not be duplicated.
- Default core imports are lightweight and side-effect free. Generating a launch plan, reading status or describing capabilities never starts a process, creates storage, installs software or sends model requests.
- Separate native conversation identity, controlled execution instance, delegated run and request idempotency. Never guess ownership from cwd, newest file, a truncated ID or a saved PID alone.
- Launch through argv arrays, preserve model/account defaults unless explicitly overridden, inject approval policy only when requested by the host, and never edit global runtime configuration for a session.
- Unknown delivery is not retry permission. No universal exactly-once claim spans storage and terminal input. Cancellation request, confirmed interruption, process stop, detach and history deletion are distinct; this package does not delete native histories.
- Every persisted user-content change and operation transition records actor/action/target/timestamp audit in the same transaction. Same-user namespaces prevent mistakes, not hostile-tenant isolation.
- Stable ACP v1 is the production protocol. The latest released schema baseline is pinned in docs/ACP.md; v2 draft features do not silently enter the v1 contract. Optional capabilities must reflect the actual selected backend/runtime. Do not claim compatibility from schema validation alone.
- Work on the existing main checkout; no branches or worktrees for routine
  changes. UI work is excluded from this library.

## Verification

Use a repository-local uv environment and its selected interpreter. The canonical check entry is `scripts/check.py`, covering Ruff, one type checker, the complete pytest suite and packaging checks. Real-terminal tests use owned unique tmux sockets and disposable runtime histories; never control existing user sessions. ACP tests must include a real stdio connection and official SDK/schema validation, not only direct calls to handler methods. Keep complete diagnostics outside committed source.

A clean installed wheel and a repository-external consumer must demonstrate the public path. Report runtime, platform, authentication, quota and evidence limits individually. Publish versioned dependency artifacts before consumer pin changes are finalized; a local path dependency is not final delivery.

## Remote

Public source: `https://github.com/x0c/agent-session-control.git`.
The descriptive working name does not claim a unique trademark.

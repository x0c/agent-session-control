# Agent session control

Independent Python library and JSON CLI for coding-agent session control, with an optional ACP agent interface. This is not a history parser, UI framework, task planner or vendor SDK replacement.

Shared language standard: [Python](~/Codes/_standards/python.md).
Read documents whose described content is relevant before decisions or changes.

## Documents

- [Design](docs/DESIGN.md): Approved scope, ownership, reference reuse, dependency direction, parallel file ownership and acceptance.
- [Contract](docs/CONTRACT.md): Shared public models, controller port, errors and compatibility rules used by independently implemented components.
- [Integration](docs/INTEGRATION.md): Distribution and SDK/CLI adoption recipes; maintained by the foundation worker.
- [Backends](docs/BACKENDS.md): Terminal control, SessKit observation, ownership and runtime coverage; maintained by the engine worker.
- [ACP](docs/ACP.md): Released protocol/schema baseline, capability mapping, SDK differences and protocol acceptance; maintained by the ACP worker.
- [Taskboard](TASKBOARD.md): Active work ownership only.

## Boundaries

- No dependency on a downstream host, its paths, environment-variable prefixes, UI, configuration, titles, notifications, transport or policy. Native history parsing belongs to SessKit and must not be duplicated.
- Default core imports are lightweight and side-effect free. Generating a launch plan, reading status or describing capabilities never starts a process, creates storage, installs software or sends model requests.
- Separate native conversation identity, controlled execution instance, delegated run and request idempotency. Never guess ownership from cwd, newest file, a truncated ID or a saved PID alone.
- Launch through argv arrays, preserve model/account defaults unless explicitly overridden, inject approval policy only when requested by the host, and never edit global runtime configuration for a session.
- Unknown delivery is not retry permission. No universal exactly-once claim spans storage and terminal input. Cancellation request, confirmed interruption, process stop, detach and history deletion are distinct; this package does not delete native histories.
- Every persisted user-content change and operation transition records actor/action/target/timestamp audit in the same transaction. Same-user namespaces prevent mistakes, not hostile-tenant isolation.
- Stable ACP v1 is the production protocol. The latest released schema baseline is pinned in docs/ACP.md; v2 draft features do not silently enter the v1 contract. Optional capabilities must reflect the actual selected backend/runtime. Do not claim compatibility from schema validation alone.
- All workers use the existing main checkout, no branches/worktrees, no nested agents and no commit/push. The coordinator integrates and verifies. UI work is excluded from every worker.

## Verification

Use a repository-local uv environment and its selected interpreter. The foundation worker supplies the canonical check entry covering Ruff, one type checker, the complete pytest suite and packaging checks. Real-terminal tests use owned unique tmux sockets and disposable runtime histories; never control existing user sessions. ACP tests must include a real stdio connection and official SDK/schema validation, not only direct calls to handler methods. Keep complete diagnostics outside committed source.

A clean installed wheel and a repository-external consumer must demonstrate the public path. Report runtime, platform, authentication, quota and evidence limits individually. The coordinator publishes versioned dependency artifacts before consumer pin changes are finalized; a local path dependency is not final delivery.

## Remote

Private source: `ssh://git@forgejo.caozc.top:2222/Max/agent-session-control.git`.
The descriptive working name does not claim a unique public package or trademark.

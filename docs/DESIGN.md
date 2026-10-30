# Reusable session control design

Status: 0.1.0 released 2026-10-30; this document records the adopted
requirements. Provenance: extracted from a working host implementation,
built in four parallel slices (core/store, terminal execution, ACP
adapter, host integration) with independent read-only reviews; the
release gate is recorded in the README and `docs/INTEGRATION.md`.

## Outcome

External applications can independently generate native launch/resume plans, control owned coding-agent sessions, submit and observe durable work, or expose those controls as an ACP agent. They can choose only the capabilities they need. No downstream product installation/configuration, UI framework, relay or vendor agent-team API is required. The initial implementation adapts existing proven mechanisms rather than starting an unrelated executor.

## Ownership and references

- This library owns generic launch/input/lifecycle mechanisms, controlled execution identity, operation storage, idempotency, locks, supervision, typed control events and operation-to-native-evidence correlation.
- SessKit owns native-history discovery, parsing, activity projections, incremental reading and native outcome evidence. Its types/readers are consumed, not copied or reinterpreted.
- Hosts own target/task selection, handoff prompt contents, global defaults, managed claim/plugin installation, authentication policy, UI, layout, notifications and transport. Hooks/ports carry host-specific integration without reverse dependencies.
- ACP is an optional outward-facing protocol adapter over the same controller. It must not become a second execution engine. Adopting the official Python ACP SDK's stdio framing, interfaces and reference examples is preferred; do not copy another project's workflow engine.
- The existing terminal controller, runtime adapters, input transaction, durable store and correlation implementation in the extraction source are the reference. Preserve source attribution/license where code is adapted. A generic upstream module must not import the extraction source.
- The inspected acpx public runtime contract demonstrates separating session handles, live events, terminal results and targeted cancellation. Reuse these concepts, not its TypeScript workflow engine, private state shape or orchestration policy.

## Companion constraints (self-contained)

- ACP baseline: stable wire/schema/SDK distinction, required content/MCP
  baseline, optional capability evidence and protocol lifecycle/stream
  mapping. The selected package/runtime coverage is in this repository's
  `docs/ACP.md`.
- Controlled execution: exact binding/instance ownership, one lock owner,
  accepted-work recovery, serialized lifecycle, reader lifetime and
  full-fidelity event integration. See `docs/BACKENDS.md` and
  `docs/INTEGRATION.md`.
- Test safety: fixture confidentiality (allowlisted synthetic MCP values,
  never ambient environment dumps), native failure versus unavailable
  evidence, complete gates and installed-path acceptance.
- Extraction acceptance: actual production delegation and a clean installed
  consumer, not merely a new package/hook inventory. See
  `docs/INTEGRATION.md`.

This design owns the extraction scope and acceptance obligations.
Source-level fixes and partial test passes do not establish complete
runtime/ACP coverage; versioned artifacts and installed-path acceptance
are recorded with their evidence (0.1.0: `docs/INTEGRATION.md`).

## Stable protocol baseline

Official release checked 2026-10-30: `schema-v1.27.0`, source commit `97fcb0f5b88567dd91d585ea6885761365a6aa7b`, released October 27. Wire major 1 is stable; v2 remains draft. Published Python SDK is `agent-client-protocol` 0.12.1; PyPI metadata permits Python >=3.10,<3.15. The adapter records released-schema/SDK coverage with exact references, supported operations and gaps. Schema release numbers are not wire major numbers.

## Area ownership

Keep edits inside one area per change; cross-area changes update the
contract first. No UI, styling, interaction, Apple code or screenshot
work belongs in this library. Host wrappers retain product-owned identity
preparation; extraction never moves UI code here.

| Area | Files | Depends on |
|---|---|---|
| core | `pyproject.toml`, `uv.lock`, `README*`, `src/agent_session_control/{__init__,models,errors,runtime,store,correlation}.py`, `tests/test_core*.py`, `scripts/check.py`, `docs/INTEGRATION.md` | Contract only |
| engine | `src/agent_session_control/{controller,terminal,history,supervisor}.py`, `tests/test_engine*.py`, `tests/fixtures/engine/`, `docs/BACKENDS.md` | Core API in contract; use isolated ports/fakes |
| acp | `src/agent_session_control/{acp,cli,__main__}.py`, `tests/test_acp*.py`, `tests/fixtures/acp/`, `docs/ACP.md` | Controller port in contract, official SDK/released schema; no manifest edits |
| host integration | Host-side non-UI `control*.py`, bounded `runtime/*` changes, host dependency/install/lock plumbing, dedicated integration tests, `examples/`, `tests/test_consumer*.py` | Core contract and engine port |

## Delivery and test obligations

1. Pure launch plans preserve native argument semantics across the five currently supported extraction-source runtimes and explicit auto-approval/model policies. New adapters remain pluggable; no dormant runtime becomes a new visible product entry.
2. Terminal control proves pane ownership, readiness, multiline/Unicode paste and submit verification. Partial injection stays unknown and is not automatically replayed. Detached processes and sockets are cleaned only when owned by the test.
3. Durable intent and same-transaction audit precede side effects; repeated immutable idempotency keys converge, changed payloads conflict. A stopped caller does not lose accepted durable work. Restarts do not replay uncertain terminal writes.
4. Native identity, run/message correlation, generation resets, foreign input and inferred versus native finality remain observable. Completion means a turn ended, never that a semantic task passed acceptance.
5. Latest-stable ACP is tested over stdio using an actual client, with valid success/error envelopes, output ordering, lifecycle, optional capabilities, resource links, MCP configuration and cancellation cleanup. No fabricated capabilities, stop reasons, tool events, permissions or model observations.
6. Published optional features work end-to-end for their advertised runtime/profile. Physical limitations, unavailable login/quota and backend baseline gaps are reported per profile rather than counted as passes. Unsupported profiles are not labeled fully ACP-compatible.
7. Fixture confidentiality and external limits: allowlisted synthetic MCP
environment proofs instead of ambient environment dumps. The full library
suite, Ruff, one type checker, wheel/sdist, clean install and an external
consumer pass before release. Host integrations run their canonical
complete checks and isolated real-terminal behavior before local
installation and installed-path verification.
8. Deliver one authoritative shared implementation. A temporary source-host wrapper is acceptable only when it delegates; leaving a second controller, store or parser is not extraction acceptance.

The library ships versioned artifacts on GitHub Releases
(`agent_session_control-0.1.0-py3-none-any.whl` plus sdist); consumers pin
that immutable wheel (see `docs/INTEGRATION.md`), never an editable/path
dependency. Host product releases remain the host's own decision.

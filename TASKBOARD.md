# TASKBOARD

Active work only; see the shared taskboard guide. Workers use the coordinator's file partition in docs/DESIGN.md.

| Task | State | Scope | Started | Updated | Notes |
|---|---|---|---|---|---|
| Independent Pi reviews (Pi 831aecaa) | Complete | Read-only durable/extraction review and latest-stable ACP review; reports outside repository | 2026-10-30 16:00 | 2026-10-30 17:20 | Owner-selected external Pi sessions, requested codex-1/gpt-6.1-sol:xhigh. Durable runs run_24db70712f34433696b183315232e7f4 and run_a1e92204902c43399b6d680d0f411543; task-local callback watcher uses pinned installed Corral. No source edits/full shared test jobs. |
| Reusable control and stable ACP (Pi 831aecaa) | Complete, coordinator-verified | Foundation, engine, ACP adapter, source-host integration; disjoint OpenCode ownership | 2026-10-30 11:43 | 2026-10-30 16:35 | Independent Pi reviews identified 8 reliability and 6 ACP P1 repros; original owners resumed/steered. Full gate green 2026-10-30 18:00: 242 passed, 3 skipped (all quota/auth honest skips), 30 subtests; ruff/pyrefly clean; wheel built; isolated clean-install probe ok. Codex/Claude live turns beyond quota remain explicit limits, not passes. |

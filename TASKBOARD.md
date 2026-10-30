# TASKBOARD

Active work only. Area ownership is listed in docs/DESIGN.md.

| Task | State | Scope | Started | Updated | Notes |
|---|---|---|---|---|---|
| 0.1.0 release | Complete | Core, engine, ACP adapter, host integration | 2026-10-30 | 2026-10-30 | Built in four parallel slices with independent read-only reviews (8 reliability + 6 ACP findings, all fixed with regressions). Release gate: 242 passed, 3 quota/auth honest skips, 30 subtests; ruff/pyrefly clean; wheel + sdist; isolated clean-install probe ok. Live model turns beyond quota remain explicit limits, not passes. |

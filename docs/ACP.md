# ACP compatibility

Status: implementation pending; no compatibility pass is claimed.

## Released baseline

The coordinator verified [schema-v1.27.0](https://github.com/agentclientprotocol/agent-client-protocol/releases/tag/schema-v1.27.0), commit `97fcb0f5b88567dd91d585ea6885761365a6aa7b`, released 2026-10-27. Stable wire major is v1. [v2 remains draft](https://agentclientprotocol.com/announcements/acp-v2-draft).

Published official Python SDK: [0.12.1](https://github.com/agentclientprotocol/python-sdk/releases/tag/0.12.1), Python >=3.10,<3.15 according to PyPI metadata checked 2026-10-30. A schema release number is not a protocol wire major; SDK coverage must be checked against released stable schema rather than inferred from its version.

## Required verification

Use the official SDK and inspect its reference implementation before adapting it. Record a method/capability matrix, released-schema/SDK differences, and exact tests of initialization, creation, prompt/update/result ordering, resource links, per-session MCP, cancellation/cleanup, optional load/resume/list/close, errors and malformed input. A baseline feature may not be hidden behind an optional capability. Preserve unavailable/unknown evidence and fail honestly rather than invent success.

Run an actual stdio client against the packaged production entry, plus deterministic controller fixtures for adverse paths. A fixture-only pass is not evidence that all real runtime profiles support all advertised capabilities. Backend/profile gaps and authentication/quota limits must be explicit.

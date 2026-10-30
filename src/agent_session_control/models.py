"""Shared public models and the controller port.

Typed dataclasses with explicit JSON serialization. Implementations may add
optional fields without requiring callers to supply them. Prompt content uses
ACP-shaped content blocks as a plain interchange structure; this module never
imports the ACP SDK.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from agent_session_control.errors import ControlError

TEXT_BLOCK = "text"
RESOURCE_LINK_BLOCK = "resource_link"
#: Legacy neutral alias: a URI link only. Accepted explicitly as compatibility
#: input and normalized to the canonical ``resource_link`` form. It must not
#: be confused with ACP embedded ``resource`` content, which is rejected.
LEGACY_RESOURCE_BLOCK = "resource"
RESOURCE_BLOCK = LEGACY_RESOURCE_BLOCK
_SUPPORTED_BLOCKS = frozenset({TEXT_BLOCK, RESOURCE_LINK_BLOCK, LEGACY_RESOURCE_BLOCK})
#: Keys that carry embedded resource *content* rather than a link. Their
#: presence on any resource block is rejected, never flattened into text.
_EMBEDDED_RESOURCE_KEYS = frozenset({"data", "content", "bytes", "text"})


@dataclass(frozen=True)
class SessionHandle:
    """One controlled execution instance.

    ``id`` is the opaque local control ID. ``native_id`` is the runtime's exact
    conversation identity and is never replaced by a shortened local ID.
    """

    id: str
    runtime: str
    cwd: str
    native_id: str | None = None
    instance_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "runtime": self.runtime,
            "cwd": self.cwd,
            "native_id": self.native_id,
            "instance_id": self.instance_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SessionHandle:
        return cls(
            id=str(data["id"]),
            runtime=str(data["runtime"]),
            cwd=str(data["cwd"]),
            native_id=data.get("native_id"),
            instance_id=data.get("instance_id"),
        )


@dataclass(frozen=True)
class ControlEvent:
    """One observable control event.

    ``seq`` is meaningful only within ``generation``; a replay or reset starts
    a new generation explicitly. ``evidence`` is ``unknown`` unless native or
    verified evidence established the record.
    """

    seq: int
    kind: str
    session_id: str
    run_id: str | None = None
    data: dict = field(default_factory=dict)
    evidence: str = "unknown"
    generation: str = ""

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "data": dict(self.data),
            "evidence": self.evidence,
            "generation": self.generation,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ControlEvent:
        return cls(
            seq=int(data["seq"]),
            kind=str(data["kind"]),
            session_id=str(data["session_id"]),
            run_id=data.get("run_id"),
            data=dict(data.get("data") or {}),
            evidence=str(data.get("evidence", "unknown")),
            generation=str(data.get("generation", "")),
        )


RUN_STATES = frozenset({"completed", "failed", "interrupted", "unknown"})


@dataclass(frozen=True)
class RunResult:
    """The settled (or explicitly unknown) outcome of one run.

    ``state`` reflects actual evidence only: ``unknown`` never masquerades as
    a native stop reason. A terminal run stays terminal; follow-ups start new
    runs.
    """

    run_id: str
    state: str
    final_response: str | None = None
    stop_reason: str | None = None
    error: dict | None = None
    evidence: str = "unknown"

    def __post_init__(self) -> None:
        if self.state not in RUN_STATES:
            raise ValueError(f"Unknown run state: {self.state!r}")

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "state": self.state,
            "final_response": self.final_response,
            "stop_reason": self.stop_reason,
            "error": dict(self.error) if self.error is not None else None,
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RunResult:
        return cls(
            run_id=str(data["run_id"]),
            state=str(data["state"]),
            final_response=data.get("final_response"),
            stop_reason=data.get("stop_reason"),
            error=dict(data["error"]) if data.get("error") is not None else None,
            evidence=str(data.get("evidence", "unknown")),
        )


def text_block(text: str) -> dict:
    if not isinstance(text, str):
        raise ControlError("usage_error", "A text block needs a string.", 2)
    return {"type": TEXT_BLOCK, "text": text}


def resource_link(
    uri: str,
    *,
    title: str | None = None,
    name: str | None = None,
    mime_type: str | None = None,
) -> dict:
    """Build the canonical ``resource_link`` block.

    ``name`` is the ACP SDK's link label and maps to ``title``; an explicit
    ``title`` wins when both are given.
    """
    if not isinstance(uri, str) or not uri:
        raise ControlError("usage_error", "A resource link needs a nonempty URI.", 2)
    block: dict = {"type": RESOURCE_LINK_BLOCK, "uri": uri}
    label = title if title is not None else name
    if label is not None:
        block["title"] = label
    if mime_type is not None:
        block["mimeType"] = mime_type
    return block


def _normalize_link(block: dict, *, legacy: bool) -> dict:
    uri = block.get("uri")
    if not uri or not isinstance(uri, str):
        raise ControlError("usage_error", "A resource link needs a string 'uri' field.", 2)
    embedded = sorted(key for key in _EMBEDDED_RESOURCE_KEYS if key in block)
    if embedded:
        raise ControlError(
            "unsupported",
            f"Embedded resource content ({', '.join(embedded)}) is not a link and is never flattened into text.",
            2,
            hint="Send a resource_link with a 'uri' instead of inline content.",
        )
    item: dict = {"type": RESOURCE_LINK_BLOCK, "uri": uri}
    label = block.get("title", block.get("name"))
    if label is not None:
        item["title"] = label
    if block.get("mimeType") is not None:
        item["mimeType"] = block["mimeType"]
    if legacy and any(key not in {"type", "uri", "title", "name", "mimeType"} for key in block):
        raise ControlError(
            "usage_error",
            "A legacy 'resource' block carries only link fields (uri plus optional title).",
            2,
        )
    return item


def normalize_prompt(prompt: list[dict]) -> list[dict]:
    """Validate ACP-shaped prompt blocks without importing the ACP SDK.

    Canonical block types are ``text`` and ``resource_link`` (``uri`` plus
    optional ``name``/``title``/``mimeType`` link metadata). A legacy neutral
    ``resource`` block is accepted explicitly as URI-link compatibility input
    and normalized to the canonical form. Rich content (images, audio) and
    embedded resource data are explicitly rejected here; a richer profile may
    accept them through its own capability-gated path, never by silently
    flattening them into text.
    """
    if not isinstance(prompt, list) or not prompt:
        raise ControlError("usage_error", "A turn needs a nonempty list of content blocks.", 2)
    normalized = []
    for block in prompt:
        if not isinstance(block, dict):
            raise ControlError("usage_error", "Each prompt block must be an object.", 2)
        kind = block.get("type")
        if kind == TEXT_BLOCK:
            if not isinstance(block.get("text"), str):
                raise ControlError("usage_error", "A text block needs a string 'text' field.", 2)
            normalized.append({"type": TEXT_BLOCK, "text": block["text"]})
        elif kind == RESOURCE_LINK_BLOCK:
            normalized.append(_normalize_link(block, legacy=False))
        elif kind == LEGACY_RESOURCE_BLOCK:
            normalized.append(_normalize_link(block, legacy=True))
        else:
            raise ControlError(
                "unsupported",
                f"Unsupported prompt block type: {kind!r}. Only 'text' and 'resource_link' blocks are accepted.",
                2,
            )
    return normalized


def prompt_text(prompt: list[dict]) -> str:
    """Human-readable projection of normalized blocks.

    Resource links stay visible as ``[resource: <uri>]`` (with the link
    title when present) so they are never silently flattened in ways that
    lose meaning.
    """
    parts = []
    for block in normalize_prompt(prompt):
        if block["type"] == TEXT_BLOCK:
            parts.append(block["text"])
        elif block.get("title"):
            parts.append(f"[resource: {block['title']} <{block['uri']}>]")
        else:
            parts.append(f"[resource: {block['uri']}]")
    return "\n".join(part for part in parts if part)


@runtime_checkable
class SessionController(Protocol):
    """Async controller port shared by the CLI, the ACP adapter and hosts.

    The ACP adapter accepts any object satisfying this port; the production
    CLI uses the engine's ``LocalController``. A backend used only in tests
    must not become the undocumented production implementation.
    """

    def capabilities(self, runtime: str) -> dict: ...

    async def create(
        self,
        runtime: str,
        cwd: str,
        *,
        model: str | None = None,
        mcp_servers: Any = (),
        actor: str = "local-user",
    ) -> SessionHandle: ...

    async def resume(
        self,
        session_id: str,
        *,
        cwd: str | None = None,
        mcp_servers: Any = (),
        actor: str = "local-user",
    ) -> SessionHandle: ...

    async def start_turn(
        self,
        session_id: str,
        prompt: list[dict],
        *,
        request_id: str,
        on_busy: str = "queue",
        actor: str = "local-user",
    ) -> str: ...

    def watch(self, run_id: str, *, after: int = 0) -> AsyncIterator[ControlEvent]: ...

    async def wait(self, run_id: str, *, timeout: float | None = None) -> RunResult: ...

    async def interrupt(self, run_id: str, *, actor: str = "local-user") -> None: ...

    async def close_session(self, session_id: str, *, actor: str = "local-user") -> None: ...

    async def list_sessions(self, *, cwd: str | None = None, cursor: str | None = None) -> dict: ...

    async def load_history(self, session_id: str) -> list[ControlEvent]: ...

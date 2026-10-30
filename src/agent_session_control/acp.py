"""ACP agent adapter over the shared ``SessionController`` port.

This module is an outward-facing protocol adapter only. It never launches,
hosts or observes runtimes itself; every operation delegates to the injected
controller (production: engine ``LocalController``). One ``ControlAgent``
serves exactly one explicitly selected runtime so ``initialize`` capability
claims stay truthful for every session on the connection.

The official ``agent-client-protocol`` SDK owns stdio framing, the JSON-RPC
engine, interfaces and wire validation; nothing here re-implements them.

Capability policy: optional protocol features need an explicit verified
backend value (``True`` or ``{"supported": True}``) plus an implemented
controller method. A failed probe, a missing key or an explicit opt-out
advertises nothing. MCP ``http``/``sse`` stay unadvertised without an
explicit opt-in. Link block forms follow the backend's ``prompt_blocks``,
defaulting to the canonical form.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import uuid
from typing import Any

from agent_session_control.errors import ControlError

log = logging.getLogger("agent_session_control.acp")

AGENT_NAME = "agent-session-control"
AGENT_VERSION_FALLBACK = "0.1.0"

#: Canonical baseline block type on the wire. The legacy neutral ``resource``
#: alias is only a normalization fallback, never confused with ACP embedded
#: ``resource`` content (which this agent does not advertise).
CANONICAL_LINK_BLOCK = "resource_link"
LEGACY_LINK_BLOCK = "resource"

#: Assistant text accepted for live streaming. Thinking replays as thought
#: chunks; user input, diagnostics, state and unknown kinds are counted,
#: never converted into agent text on the live turn.
_ASSISTANT_TEXT_TYPES = frozenset({"assistant_message"})

#: Terminal run states the controller reports; anything else is surfaced as
#: an internal protocol error rather than invented into a stop reason.
_COMPLETED = "completed"
_FAILED = "failed"
_INTERRUPTED = "interrupted"
_UNKNOWN = "unknown"

#: Stop reasons the controller may report that map 1:1 onto the protocol.
_PASSTHROUGH_STOP = frozenset({"end_turn", "max_tokens", "max_turn_requests", "refusal"})

#: How long ``session/close`` waits for a tracked run to settle before it
#: reports the (successful) release with an unconfirmed cancellation flag.
CLOSE_CONFIRM_TIMEOUT = 10.0

#: Compact summaries append this marker; stripping it recovers the true
#: prefix for tail reconciliation against the terminal result.
_TRUNC_SUFFIX_RE = re.compile(r"… \[(\d+) more chars\]$")


def _pkg_version() -> str:
    try:
        from importlib.metadata import version

        return version("agent-session-control")
    except Exception:
        return AGENT_VERSION_FALLBACK


def _sdk() -> Any:
    """Import the optional official SDK lazily so core stays dependency-free."""
    try:
        import acp  # type: ignore[import-not-found]

        return acp
    except ImportError as exc:
        raise ControlError(
            "unavailable",
            "The optional 'acp' extra is not installed; "
            "install agent-session-control[acp] (agent-client-protocol>=0.12.1).",
            hint="Use a Python >=3.10,<3.15 environment for this extra.",
        ) from exc


def _schema() -> Any:
    """Official SDK wire models (``acp.schema``), imported lazily."""
    try:
        from acp import schema  # type: ignore[import-not-found]

        return schema
    except ImportError as exc:
        raise ControlError(
            "unavailable",
            "The optional 'acp' extra is not installed; "
            "install agent-session-control[acp] (agent-client-protocol>=0.12.1).",
            hint="Use a Python >=3.10,<3.15 environment for this extra.",
        ) from exc


def _request_error(code: int, message: str, data: Any = None) -> Any:
    return _sdk().RequestError(code, message, data)


def control_error_to_request_error(exc: BaseException, *, session_id: str | None = None) -> Any:
    """Map controller failures onto ACP JSON-RPC errors without inventing facts.

    Unknown sessions become -32002, unsupported values -32602, authentication
    -32000, everything without a known terminal meaning becomes -32603 with
    structured data. Secret material never enters these messages; only codes,
    names and non-sensitive details travel.
    """
    sdk = _sdk()
    data: dict[str, Any] = {}
    if session_id is not None:
        data["sessionId"] = session_id
    if isinstance(exc, ControlError):
        message = exc.message or exc.code
        if exc.details is not None:
            data["details"] = exc.details
        if exc.code in ("not_found", "history_missing"):
            return sdk.RequestError.resource_not_found(session_id)
        if exc.code in ("not_authorized", "auth_required"):
            return sdk.RequestError.auth_required(data or None)
        if exc.code in (
            "usage_error",
            "unsupported",
            "unsupported_content",
            "steer_unsupported",
            "model_selection_unsupported",
            "invalid_directory",
            "terminal_run",
            "idempotency_conflict",
            "conflict",
        ):
            return sdk.RequestError.invalid_params({"code": exc.code, "message": message, **data})
        data.update({"code": exc.code, "message": message})
        return sdk.RequestError.internal_error(data)
    data.update({"error_type": type(exc).__name__, "message": str(exc)})
    return sdk.RequestError.internal_error(data)


def _block_to_wire(block: Any) -> dict:
    """Translate one ACP prompt block into the canonical interchange shape.

    Canonical ``resource_link`` carries ``uri`` plus optional ``name``/
    ``title``/``mimeType`` metadata. Anything beyond text and resource links
    (image, audio, embedded ``resource``) is refused: the agent advertises no
    capability for it.
    """
    if isinstance(block, dict):
        kind = block.get("type")
        text = block.get("text")
        uri = block.get("uri")
        title = block.get("title", block.get("name"))
        mime = block.get("mimeType", block.get("mime_type"))
    else:
        kind = getattr(block, "type", None)
        text = getattr(block, "text", None)
        uri = getattr(block, "uri", None)
        title = getattr(block, "title", None) or getattr(block, "name", None)
        mime = getattr(block, "mimeType", None) or getattr(block, "mime_type", None)
    if kind == "text":
        if not isinstance(text, str):
            raise _request_error(-32602, "Invalid params", {"message": "A text block needs a string 'text' field."})
        return {"type": "text", "text": text}
    if kind == CANONICAL_LINK_BLOCK:
        if not isinstance(uri, str) or not uri:
            raise _request_error(
                -32602, "Invalid params", {"message": "A resource_link block needs a string 'uri' field."}
            )
        item: dict[str, Any] = {"type": CANONICAL_LINK_BLOCK, "uri": uri}
        name = title if title is not None else None
        if name is not None:
            item["name"] = name
            item["title"] = title
        if mime is not None:
            item["mimeType"] = mime
        return item
    raise _request_error(
        -32602,
        "Invalid params",
        {
            "message": f"Unsupported prompt block type: {kind!r}. "
            "This agent accepts 'text' and 'resource_link' only "
            "(no image/audio/embedded capability is advertised)."
        },
    )


def _legacy_link_alias(item: dict) -> dict:
    """Normalize a canonical link block to the legacy neutral alias.

    Used only when the selected backend's ``prompt_blocks`` advertises the
    legacy ``resource`` form but not the canonical one. ``uri`` plus the
    human title travel; nothing is silently flattened.
    """
    alias: dict[str, Any] = {"type": LEGACY_LINK_BLOCK, "uri": item["uri"]}
    if item.get("title") is not None:
        alias["title"] = item["title"]
    if item.get("mimeType") is not None:
        alias["mimeType"] = item["mimeType"]
    return alias


def _mcp_to_wire(server: Any) -> dict:
    """Translate one ACP MCP server entry into the neutral interchange shape.

    Transport choice is decided here (only stdio is advertised, so anything
    else is refused). Names and duplicate detection happen here because the
    list-to-mapping translation would otherwise silently collapse entries.
    Strict value validation (absolute command, arg/env shapes, unknown keys)
    belongs to the foundation boundary, whose errors are authoritative and
    already secret-safe (names only, never values). Unknown keys travel
    through so the foundation rejects them loudly instead of dropping them.
    """
    if hasattr(server, "model_dump"):
        raw = server.model_dump(by_alias=True, exclude_none=True)
    elif isinstance(server, dict):
        raw = dict(server)
    else:
        raise _request_error(-32602, "Invalid params", {"message": "An MCP server entry must be an object."})
    kind = raw.get("type")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise _request_error(-32602, "Invalid params", {"message": "An MCP server entry needs a nonempty 'name'."})
    if kind in ("http", "sse", "acp"):
        raise _request_error(
            -32602,
            "Invalid params",
            {"message": f"MCP {kind} transport is not advertised by this agent; stdio only.", "server": name},
        )
    if kind is not None and kind != "stdio":
        raise _request_error(
            -32602, "Invalid params", {"message": f"Unknown MCP server type: {kind!r}.", "server": name}
        )
    env = raw.get("env") or []
    if isinstance(env, dict):
        wire_env = dict(env)
    elif isinstance(env, list):
        wire_env = {}
        for entry in env:
            if isinstance(entry, dict):
                ename, evalue = entry.get("name"), entry.get("value")
            elif isinstance(entry, (list, tuple)) and len(entry) == 2:
                ename, evalue = entry
            else:
                raise _request_error(
                    -32602,
                    "Invalid params",
                    {"message": "MCP env entries must be {name, value} objects.", "server": name},
                )
            if not isinstance(ename, str) or not ename:
                raise _request_error(
                    -32602, "Invalid params", {"message": "MCP env entries need nonempty string names.", "server": name}
                )
            if ename in wire_env:
                raise _request_error(
                    -32602, "Invalid params", {"message": f"Duplicate MCP env name: {ename!r}.", "server": name}
                )
            wire_env[ename] = evalue
    else:
        raise _request_error(
            -32602, "Invalid params", {"message": "MCP 'env' must be a list or mapping.", "server": name}
        )
    wire: dict[str, Any] = {"name": name, "command": raw.get("command"), "args": raw.get("args") or [], "env": wire_env}
    for key, value in raw.items():
        if key in wire or key == "field_meta" or key.startswith("_"):
            continue
        wire[key] = value
    return wire


def check_mcp_list(servers: Any) -> tuple[dict, ...]:
    """Validate a whole MCP server list, including duplicate server names."""
    wired = [_mcp_to_wire(s) for s in (servers or ())]
    names = [s["name"] for s in wired]
    if len(set(names)) != len(names):
        dupes = sorted({n for n in names if names.count(n) > 1})
        raise _request_error(
            -32602, "Invalid params", {"message": f"Duplicate MCP server names: {dupes}. Only names are reported."}
        )
    return tuple(wired)


def _strip_truncation_suffix(text: str) -> tuple[str, int | None]:
    """Recover the true prefix when a summary appended its omission marker.

    Returns ``(prefix, claimed_remainder)``; ``claimed_remainder`` is None
    when no marker is present. Lets the adapter reconcile bounded snapshots
    against the fuller terminal result. Markers are transport artifacts and
    are never streamed as native content.
    """
    match = _TRUNC_SUFFIX_RE.search(text)
    if not match:
        return text, None
    return text[: match.start()], int(match.group(1))


def _clean_text(text: str) -> str:
    """Renderable native text: a known omission marker is not content."""
    cleaned, _ = _strip_truncation_suffix(text)
    return cleaned


def _event_text(data: dict) -> str | None:
    text = data.get("text")
    return text if isinstance(text, str) and text else None


_TOOL_RESULT_STATUS = {"ok": "completed", "error": "failed", "unknown": "in_progress"}


class ControlAgent:
    """Outward-facing ACP agent over one controller and one runtime."""

    def __init__(self, controller: Any, runtime: str, *, actor: str = "acp-client") -> None:
        if not runtime or not isinstance(runtime, str):
            raise ControlError("usage_error", "ControlAgent needs an explicit runtime id.", 2)
        self._controller = controller
        self._runtime = runtime
        self._actor = actor or "acp-client"
        self._conn: Any = None
        self._sessions: dict[str, Any] = {}
        # Per-session turn lifecycle: {"phase": "starting"|"active",
        # "run_id": str|None, "cancelled": bool}. Reserved before the first
        # await; bound to the run id once known; released only by the owning
        # prompt's settlement or a successful close. Cancel and close scope to
        # this reservation, never to a future sibling.
        self._turns: dict[str, dict] = {}
        self._turn_locks: dict[str, asyncio.Lock] = {}
        self._cancelled_runs: set[str] = set()

    # -- connection ------------------------------------------------------

    def on_connect(self, conn: Any) -> None:
        self._conn = conn

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._turn_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._turn_locks[session_id] = lock
        return lock

    # -- capabilities -----------------------------------------------------

    def _probe_caps(self) -> tuple[dict, bool]:
        """Return (capabilities, probed). A failed probe verifies nothing."""
        try:
            caps = self._controller.capabilities(self._runtime)
        except Exception:
            return {}, False
        return caps if isinstance(caps, dict) else {}, isinstance(caps, dict)

    @staticmethod
    def _explicitly_on(value: Any) -> bool:
        """Only ``True`` or an agreed ``{"supported": true}`` advertises."""
        if value is True:
            return True
        return isinstance(value, dict) and value.get("supported") is True

    def _advertised(self) -> dict[str, Any]:
        """Optional gates from explicit verified backend values only.

        Each lifecycle feature needs an explicit opt-in plus an implemented
        controller method. A failed probe, a missing key or an explicit
        opt-out advertises nothing. MCP transports need an explicit opt-in.
        Link block forms follow the backend's ``prompt_blocks``, defaulting
        to the canonical form.
        """
        caps, probed = self._probe_caps()
        off = {
            "load": False,
            "resume": False,
            "close": False,
            "list": False,
            "mcp_http": False,
            "mcp_sse": False,
            "link_blocks": {CANONICAL_LINK_BLOCK},
        }
        if not probed:
            return off
        has = {
            "load": callable(getattr(self._controller, "load_history", None)),
            "resume": callable(getattr(self._controller, "resume", None)),
            "close": callable(getattr(self._controller, "close_session", None)),
            "list": callable(getattr(self._controller, "list_sessions", None)),
        }

        def enabled(name: str, *keys: str) -> bool:
            if not has[name]:
                return False
            return any(self._explicitly_on(caps.get(key)) for key in keys)

        blocks = caps.get("prompt_blocks")
        link_blocks = (
            {b for b in blocks if b in (CANONICAL_LINK_BLOCK, LEGACY_LINK_BLOCK)}
            if isinstance(blocks, (list, tuple))
            else {CANONICAL_LINK_BLOCK}
        )
        return {
            "load": enabled("load", "load_history"),
            "resume": enabled("resume", "resume"),
            "close": enabled("close", "close"),
            "list": enabled("list", "list"),
            "mcp_http": bool(probed and caps.get("mcp_http") is True),
            "mcp_sse": bool(probed and caps.get("mcp_sse") is True),
            "link_blocks": link_blocks,
        }

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any = None,
        client_info: Any = None,
        **kwargs: Any,
    ) -> Any:
        schema = _schema()
        negotiated = 1 if protocol_version != 1 else protocol_version
        gates = self._advertised()
        caps = schema.AgentCapabilities(
            load_session=gates["load"],
            prompt_capabilities=schema.PromptCapabilities(image=False, audio=False, embedded_context=False),
            mcp_capabilities=schema.McpCapabilities(http=gates["mcp_http"], sse=gates["mcp_sse"]),
            session_capabilities=schema.SessionCapabilities(
                list=schema.SessionListCapabilities() if gates["list"] else None,
                resume=schema.SessionResumeCapabilities() if gates["resume"] else None,
                close=schema.SessionCloseCapabilities() if gates["close"] else None,
            ),
        )
        return schema.InitializeResponse(
            protocol_version=negotiated,
            agent_capabilities=caps,
            agent_info=schema.Implementation(name=AGENT_NAME, version=_pkg_version()),
        )

    async def authenticate(self, method_id: str, **kwargs: Any) -> Any:
        raise _sdk().RequestError.method_not_found("authenticate")

    # -- sessions ----------------------------------------------------------

    def _remember(self, handle: Any) -> Any:
        self._sessions[handle.id] = handle
        return handle

    @staticmethod
    def _check_cwd(cwd: Any) -> str:
        if not isinstance(cwd, str) or not cwd:
            raise _request_error(-32602, "Invalid params", {"message": "cwd must be a nonempty absolute path."})
        return cwd

    async def new_session(
        self,
        cwd: str,
        additional_directories: Any = None,
        mcp_servers: Any = None,
        **kwargs: Any,
    ) -> Any:
        schema = _schema()
        self._check_cwd(cwd)
        if additional_directories:
            raise _request_error(
                -32602,
                "Invalid params",
                {"message": "additionalDirectories is not advertised; omit it or send an empty list."},
            )
        servers = check_mcp_list(mcp_servers)
        try:
            handle = await self._controller.create(self._runtime, cwd, mcp_servers=servers, actor=self._actor)
        except ControlError as exc:
            raise control_error_to_request_error(exc) from exc
        self._remember(handle)
        return schema.NewSessionResponse(session_id=handle.id)

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: Any = None,
        additional_directories: Any = None,
        **kwargs: Any,
    ) -> Any:
        schema = _schema()
        if not self._advertised()["load"]:
            raise _sdk().RequestError.method_not_found("session/load")
        self._check_cwd(cwd)
        if additional_directories:
            raise _request_error(
                -32602,
                "Invalid params",
                {"message": "additionalDirectories is not advertised; omit it or send an empty list."},
            )
        servers = check_mcp_list(mcp_servers)
        try:
            handle = await self._controller.resume(session_id, cwd=cwd, mcp_servers=servers, actor=self._actor)
        except ControlError as exc:
            raise control_error_to_request_error(exc, session_id=session_id) from exc
        self._remember(handle)
        try:
            events = await self._controller.load_history(handle.id)
        except ControlError as exc:
            raise control_error_to_request_error(exc, session_id=session_id) from exc
        await self._replay(handle.id, events or [])
        return schema.LoadSessionResponse()

    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: Any = None,
        mcp_servers: Any = None,
        **kwargs: Any,
    ) -> Any:
        schema = _schema()
        if not self._advertised()["resume"]:
            raise _sdk().RequestError.method_not_found("session/resume")
        self._check_cwd(cwd)
        if additional_directories:
            raise _request_error(
                -32602,
                "Invalid params",
                {"message": "additionalDirectories is not advertised; omit it or send an empty list."},
            )
        servers = check_mcp_list(mcp_servers)
        try:
            handle = await self._controller.resume(session_id, cwd=cwd, mcp_servers=servers, actor=self._actor)
        except ControlError as exc:
            raise control_error_to_request_error(exc, session_id=session_id) from exc
        self._remember(handle)
        # Resume reconnects without replaying history; load_session replays.
        return schema.ResumeSessionResponse()

    async def list_sessions(self, cwd: str | None = None, cursor: str | None = None, **kwargs: Any) -> Any:
        schema = _schema()
        if not self._advertised()["list"]:
            raise _sdk().RequestError.method_not_found("session/list")
        try:
            page = await self._controller.list_sessions(cwd=cwd, cursor=cursor)
        except ControlError as exc:
            raise control_error_to_request_error(exc) from exc
        items = []
        for entry in (page or {}).get("sessions", []):
            if not isinstance(entry, dict):
                continue
            sid = entry.get("session_id", entry.get("id"))
            scwd = entry.get("cwd", cwd or "")
            if not sid:
                continue
            items.append(schema.SessionInfo(session_id=str(sid), cwd=str(scwd)))
        return schema.ListSessionsResponse(sessions=items, next_cursor=(page or {}).get("next_cursor"))

    async def close_session(self, session_id: str, **kwargs: Any) -> Any:
        """Cancel owned work and release resources; failed close retains state.

        Attachment and turn maps are committed (popped) only after the
        controller reports success, so cancel and prompt can still address
        live work after a failed or uncertain close. Unknown sessions still
        succeed per protocol.
        """
        schema = _schema()
        if not self._advertised()["close"]:
            raise _sdk().RequestError.method_not_found("session/close")
        lock = self._lock_for(session_id)
        async with lock:
            reservation = self._turns.get(session_id)
            run_id = reservation.get("run_id") if reservation else None
            if reservation is not None:
                reservation["cancelled"] = True
            if run_id is not None:
                self._cancelled_runs.add(run_id)
        if run_id is not None:
            try:
                await self._controller.interrupt(run_id, actor=self._actor)
            except Exception:
                log.debug("close: best-effort interrupt failed", exc_info=True)
        try:
            await self._controller.close_session(session_id, actor=self._actor)
        except ControlError as exc:
            if exc.code == "not_found":
                pass  # idempotent: fall through to commit a successful close
            else:
                raise control_error_to_request_error(exc, session_id=session_id) from exc
        async with lock:
            self._turns.pop(session_id, None)
            self._sessions.pop(session_id, None)
        confirmed = run_id is None
        if run_id is not None:
            try:
                result = await self._controller.wait(run_id, timeout=CLOSE_CONFIRM_TIMEOUT)
                confirmed = getattr(result, "state", _UNKNOWN) in (_COMPLETED, _FAILED, _INTERRUPTED)
            except ControlError:
                confirmed = False
        return schema.CloseSessionResponse(field_meta={"cancelConfirmed": confirmed})

    # -- prompt / cancel ----------------------------------------------------

    async def _run_settled(self, run_id: str) -> bool:
        """Non-blocking liveness probe for a tracked run (stale reservations)."""
        try:
            result = await self._controller.wait(run_id, timeout=0)
        except ControlError as exc:
            return exc.code in ("not_found",)
        except Exception:
            return False
        return getattr(result, "state", None) in (_COMPLETED, _FAILED, _INTERRUPTED, _UNKNOWN)

    async def _reserve_turn(self, session_id: str) -> dict:
        """Reserve starting state before the first await; reject live turns.

        A stale reservation (settled or vanished run) is reaped and replaced;
        a live one rejects the prompt. The lock is held only for the
        synchronous check-and-set, never across controller awaits.
        """
        lock = self._lock_for(session_id)
        async with lock:
            existing = self._turns.get(session_id)
            if existing is None:
                reservation: dict = {"phase": "starting", "run_id": None, "cancelled": False}
                self._turns[session_id] = reservation
                return reservation
        if existing.get("run_id") is not None and await self._run_settled(existing["run_id"]):
            async with lock:
                if self._turns.get(session_id) is existing:
                    reservation = {"phase": "starting", "run_id": None, "cancelled": False}
                    self._turns[session_id] = reservation
                    return reservation
        raise _request_error(
            -32602,
            "Invalid params",
            {
                "message": "One v1 turn is already active on this session; "
                "wait for its response before prompting again.",
                "code": "turn_active",
                "sessionId": session_id,
            },
        )

    async def prompt(self, session_id: str, prompt: list, **kwargs: Any) -> Any:
        handle = self._sessions.get(session_id)
        if handle is None:
            # No auto-resume or auto-create: use session/new, load or resume first.
            raise _sdk().RequestError.resource_not_found(session_id)
        reservation = await self._reserve_turn(session_id)
        try:
            blocks = [_block_to_wire(b) for b in (prompt or [])]
            if not blocks:
                raise _request_error(
                    -32602, "Invalid params", {"message": "session/prompt needs a nonempty prompt list."}
                )
            link_blocks = self._advertised()["link_blocks"]
            if CANONICAL_LINK_BLOCK not in link_blocks:
                blocks = [_legacy_link_alias(b) if b.get("type") == CANONICAL_LINK_BLOCK else b for b in blocks]
            request_id = f"acp-{uuid.uuid4().hex}"
            try:
                run_id = await self._controller.start_turn(
                    handle.id, blocks, request_id=request_id, on_busy="queue", actor=self._actor
                )
            except ControlError as exc:
                raise control_error_to_request_error(exc, session_id=session_id) from exc
            lock = self._lock_for(session_id)
            async with lock:
                if self._turns.get(session_id) is not reservation:
                    # A successful close consumed the reservation while
                    # starting: the session is gone, do not track this run.
                    raise _sdk().RequestError.resource_not_found(session_id)
                reservation["phase"] = "active"
                reservation["run_id"] = run_id
                armed = reservation["cancelled"]
            if armed:
                # Cancel arrived while start_turn was in flight; honor it now.
                self._cancelled_runs.add(run_id)
                try:
                    await self._controller.interrupt(run_id, actor=self._actor)
                except ControlError:
                    log.debug("armed cancel interrupt reported an error", exc_info=True)
            try:
                stats = await self._stream_run(session_id, run_id)
                try:
                    result = await self._controller.wait(run_id, timeout=None)
                except ControlError as exc:
                    raise control_error_to_request_error(exc, session_id=session_id) from exc
                await self._announce_terminal(session_id, run_id, result, stats)
                return self._prompt_response(run_id, result, stats)
            finally:
                if self._turns.get(session_id) is reservation:
                    self._turns.pop(session_id, None)
        except BaseException:
            if self._turns.get(session_id) is reservation:
                self._turns.pop(session_id, None)
            raise

    async def _stream_run(self, session_id: str, run_id: str) -> dict:
        """Forward live run evidence as session/update notifications, in order.

        Identity is the stable native message (generation + native seq or
        call id), so repeated polls of the same snapshot never duplicate and
        cumulative text arrives as deltas under one message id. Only assistant
        text and tool call/result updates stream; user input, thinking,
        diagnostics, state and unknown kinds are counted, never converted
        into agent text. Assistant and tool delivery are tracked separately.
        """
        stats: dict[str, Any] = {
            "assistant": 0,
            "tool": 0,
            "seen": 0,
            "omitted": 0,
            "watch_error": None,
            "last_text": {},
        }
        if self._conn is None:
            return stats
        try:
            watcher = self._controller.watch(run_id, after=0)
        except ControlError as exc:
            log.debug("watch unavailable; settling from wait()", exc_info=True)
            stats["watch_error"] = exc.code
            return stats
        sent_text: dict[tuple, str] = {}
        seen_tools: set[str] = set()
        try:
            async for event in watcher:
                await self._stream_event(session_id, run_id, event, sent_text, seen_tools, stats)
        except ControlError as exc:
            # The store died mid-stream; wait() still decides the outcome.
            log.debug("watch failed mid-stream: %s", exc.code)
            stats["watch_error"] = exc.code
        stats["last_text"] = dict(sent_text)
        return stats

    async def _stream_event(
        self,
        session_id: str,
        run_id: str,
        event: Any,
        sent_text: dict,
        seen_tools: set[str],
        stats: dict,
    ) -> None:
        generation = getattr(event, "generation", "") or ""
        seq = getattr(event, "seq", 0)
        data = getattr(event, "data", None) or {}
        if not isinstance(data, dict):
            stats["omitted"] += 1
            return
        stats["seen"] += 1
        inner = data.get("type", "") or ""
        native = data.get("native_seq", data.get("nativeSeq"))
        native_key = native if isinstance(native, int) else seq
        key = (generation, native_key)
        if inner == "user_message":
            return  # Own submitted input needs no echo back to its sender.
        if inner == "tool_call":
            await self._stream_tool_call(session_id, run_id, key, data, seen_tools, stats)
            return
        if inner == "tool_result":
            await self._stream_tool_result(session_id, run_id, key, data, seen_tools, stats)
            return
        if inner in _ASSISTANT_TEXT_TYPES:
            text = _event_text(data)
            if text:
                await self._stream_text(session_id, run_id, key, text, False, sent_text, stats)
            return
        if inner == "thinking":
            text = _event_text(data)
            if text:
                await self._stream_text(session_id, run_id, key, text, True, sent_text, stats)
            return
        # State, message, compaction, lifecycle and unknown kinds are
        # diagnostics, not turn content: counted, never agent text.
        stats["omitted"] += 1

    async def _stream_text(
        self, session_id: str, run_id: str, key: tuple, text: str, thought: bool, sent_text: dict, stats: dict
    ) -> None:
        sdk = _sdk()
        text = _clean_text(text)
        previous = sent_text.get((thought, key), "")
        if text == previous:
            return  # identical snapshot poll: never duplicate
        delta = text[len(previous) :] if previous and text.startswith(previous) else text
        if not delta:
            return
        sent_text[(thought, key)] = text
        chunk = (
            sdk.update_agent_thought(sdk.text_block(delta))
            if thought
            else sdk.update_agent_message(sdk.text_block(delta))
        )
        chunk.message_id = f"{run_id}:{'thought' if thought else 'msg'}:{key[0]}:{key[1]}"
        try:
            await self._conn.session_update(session_id=session_id, update=chunk)
            stats["assistant"] += 1
        except Exception:
            log.warning("session/update delivery failed; settling from wait()", exc_info=True)

    def _tool_status(self, data: dict) -> tuple[str, list]:
        sdk = _sdk()
        raw_result = data.get("result")
        if isinstance(raw_result, dict):
            raw_status = raw_result.get("status")
        else:
            raw_status = raw_result
        status = _TOOL_RESULT_STATUS.get(str(raw_status or ""), "in_progress")
        if data.get("error") is not None:
            status = "failed"
        content = []
        text = _event_text(data)
        if text:
            content = [sdk.tool_content(sdk.text_block(_clean_text(text)))]
        return status, content

    async def _stream_tool_call(
        self, session_id: str, run_id: str, key: tuple, data: dict, seen_tools: set[str], stats: dict
    ) -> None:
        schema = _schema()
        call_id = data.get("call_id") or f"{run_id}:{key[0]}:{key[1]}"
        name = data.get("tool") or data.get("name") or "tool"
        try:
            if call_id not in seen_tools:
                seen_tools.add(call_id)
                start = schema.ToolCallStart(session_update="tool_call", tool_call_id=str(call_id), title=str(name))
                await self._conn.session_update(session_id=session_id, update=start)
                stats["tool"] += 1
            if data.get("result") is not None or data.get("error") is not None:
                status, content = self._tool_status(data)
                progress = schema.ToolCallProgress(
                    session_update="tool_call_update",
                    tool_call_id=str(call_id),
                    status=status,
                    content=content or None,
                )
                await self._conn.session_update(session_id=session_id, update=progress)
                stats["tool"] += 1
        except Exception:
            log.warning("tool update delivery failed", exc_info=True)

    async def _stream_tool_result(
        self, session_id: str, run_id: str, key: tuple, data: dict, seen_tools: set[str], stats: dict
    ) -> None:
        """Map the native tool_result variant to a call-scoped progress update.

        The result carries the call's stable id; when its start was missed
        the call is announced first so the result keeps its identity.
        """
        schema = _schema()
        call_id = data.get("call_id") or f"{run_id}:{key[0]}:{key[1]}"
        name = data.get("tool") or data.get("name") or "tool"
        try:
            if call_id not in seen_tools:
                seen_tools.add(call_id)
                start = schema.ToolCallStart(session_update="tool_call", tool_call_id=str(call_id), title=str(name))
                await self._conn.session_update(session_id=session_id, update=start)
                stats["tool"] += 1
            status, content = self._tool_status(data)
            progress = schema.ToolCallProgress(
                session_update="tool_call_update",
                tool_call_id=str(call_id),
                status=status,
                content=content or None,
            )
            await self._conn.session_update(session_id=session_id, update=progress)
            stats["tool"] += 1
        except Exception:
            log.warning("tool result delivery failed", exc_info=True)

    async def _announce_terminal(self, session_id: str, run_id: str, result: Any, stats: dict) -> None:
        """Surface terminal evidence the live stream could not carry.

        A failed or unknown settlement always gets an adapter-marked note with
        the controller's error (never invented native text). A completed turn
        whose stream missed the final response gets exactly the missing tail
        (or the whole response when no assistant text streamed), reconciled
        against truncation markers — never a resend.
        """
        if self._conn is None:
            return
        sdk = _sdk()
        state = getattr(result, "state", _UNKNOWN)
        if state in (_FAILED, _UNKNOWN):
            error = getattr(result, "error", None)
            if isinstance(error, dict):
                code = error.get("code") or error.get("kind") or "error"
                detail = f"{code}: {error.get('message', '')}".strip()
            else:
                detail = str(error) if error else "no recorded error"
            note = sdk.update_agent_message(sdk.text_block(f"[run {state}: {detail}]"))
            try:
                await self._conn.session_update(session_id=session_id, update=note)
            except Exception:
                log.debug("terminal note undeliverable", exc_info=True)
            return
        if state != _COMPLETED:
            return
        final = getattr(result, "final_response", None)
        if not isinstance(final, str) or not final:
            return
        assistant_texts = {
            (generation, native_key): text
            for (thought, (generation, native_key)), text in (stats.get("last_text") or {}).items()
            if not thought
        }
        for (generation, native_key), text in assistant_texts.items():
            stripped, claimed = _strip_truncation_suffix(text)
            for candidate, exact in ((stripped, claimed), (text, None)):
                if final == candidate:
                    return
                if final.startswith(candidate) and len(final) > len(candidate):
                    if exact is None or len(final) - len(candidate) == exact:
                        tail = final[len(candidate) :]
                        chunk = sdk.update_agent_message(sdk.text_block(tail))
                        chunk.message_id = f"{run_id}:{generation}:{native_key}"
                        try:
                            await self._conn.session_update(session_id=session_id, update=chunk)
                        except Exception:
                            log.debug("terminal tail undeliverable", exc_info=True)
                        return
        if not stats.get("assistant"):
            chunk = sdk.update_agent_message(sdk.text_block(final))
            chunk.message_id = f"{run_id}:result"
            try:
                await self._conn.session_update(session_id=session_id, update=chunk)
            except Exception:
                log.debug("terminal response undeliverable", exc_info=True)

    def _prompt_response(self, run_id: str, result: Any, stats: dict) -> Any:
        schema = _schema()
        state = getattr(result, "state", _UNKNOWN)
        evidence = getattr(result, "evidence", "unknown") or "unknown"
        stop_reason = getattr(result, "stop_reason", None)
        cancelled = run_id in self._cancelled_runs
        self._cancelled_runs.discard(run_id)
        meta = {"runId": run_id, "evidence": evidence, "cancelRequested": cancelled}
        if state == _COMPLETED:
            if evidence not in ("native", "inferred"):
                raise _sdk().RequestError.internal_error(
                    {
                        "message": "Run completed with no confirmed evidence; refusing to report end_turn.",
                        "runId": run_id,
                        "evidence": evidence,
                        "assistantUpdates": stats.get("assistant", 0),
                        "toolUpdates": stats.get("tool", 0),
                    }
                )
            if stop_reason in _PASSTHROUGH_STOP:
                return schema.PromptResponse(stop_reason=stop_reason, field_meta=meta)
            if stop_reason:
                raise _sdk().RequestError.internal_error(
                    {
                        "message": f"Run completed with an unmapped stop reason: {stop_reason!r}.",
                        "runId": run_id,
                        "evidence": evidence,
                    }
                )
            return schema.PromptResponse(stop_reason="end_turn", field_meta=meta)
        if state == _INTERRUPTED:
            # 'cancelled' is the only protocol stop reason for stopped work,
            # whether this connection requested it or another actor did.
            meta["cancelRequested"] = True
            return schema.PromptResponse(stop_reason="cancelled", field_meta=meta)
        error = getattr(result, "error", None)
        raise _sdk().RequestError.internal_error(
            {
                "message": f"Run settled as {state} with no successful turn.",
                "runId": run_id,
                "evidence": evidence,
                "error": dict(error) if isinstance(error, dict) else error,
            }
        )

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        """Request cancellation of the session's reserved turn only.

        Scoped to the current reservation: with no reservation this is a
        no-op and never arms a future prompt. Never touches unrelated runs.
        """
        lock = self._lock_for(session_id)
        async with lock:
            reservation = self._turns.get(session_id)
            if reservation is None:
                return
            reservation["cancelled"] = True
            run_id = reservation.get("run_id")
            if run_id is not None:
                self._cancelled_runs.add(run_id)
        if run_id is None:
            # Still starting: the owning prompt applies the cancel once its
            # run id is known. Nothing else to do here.
            return
        try:
            await self._controller.interrupt(run_id, actor=self._actor)
        except ControlError as exc:
            # A settled run no longer needs interruption; anything else is
            # observed at the pending prompt's wait, never swallowed here.
            log.debug("cancel interrupt reported %s", exc.code)
        # The optional permission-resolution hook runs only when provided.
        resolve = getattr(self._controller, "resolve_permissions", None)
        if callable(resolve):
            try:
                await resolve(run_id)
            except Exception:
                log.debug("permission resolution failed", exc_info=True)

    # -- replay --------------------------------------------------------------

    async def _replay(self, session_id: str, events: list) -> None:
        """Replay full history for session/load (resume never replays).

        User, assistant, thinking and tool events map by kind with stable
        ids; tool results resolve to their call ids. Anything carrying
        replayable content that cannot be mapped fails the load instead of
        succeeding with silent loss. Content-free records are skipped.
        """
        if self._conn is None or not events:
            return
        sdk = _sdk()
        schema = _schema()
        for event in events:
            data = getattr(event, "data", None) or {}
            if not isinstance(data, dict):
                raise _sdk().RequestError.internal_error(
                    {"message": "Cannot faithfully replay a non-mapping history event.", "sessionId": session_id}
                )
            kind = getattr(event, "kind", "") or ""
            inner = data.get("type", kind)
            generation = getattr(event, "generation", "") or ""
            seq = getattr(event, "seq", 0)
            message_id = f"{session_id}:load:{generation}:{seq}"
            text = _event_text(data)
            update = None
            if inner == "user_message" and text:
                update = sdk.update_user_message(sdk.text_block(_clean_text(text)))
            elif inner == "assistant_message" and text:
                update = sdk.update_agent_message(sdk.text_block(_clean_text(text)))
            elif inner == "thinking" and text:
                thought = sdk.update_agent_thought(sdk.text_block(_clean_text(text)))
                update = thought
            elif inner == "tool_call":
                call_id = data.get("call_id") or f"replay:{generation}:{seq}"
                update = schema.ToolCallStart(
                    session_update="tool_call", tool_call_id=str(call_id), title=str(data.get("tool") or "tool")
                )
            elif inner == "tool_result":
                call_id = data.get("call_id") or f"replay:{generation}:{seq}"
                status, content = self._tool_status(data)
                start = schema.ToolCallStart(
                    session_update="tool_call", tool_call_id=str(call_id), title=str(data.get("tool") or "tool")
                )
                progress = schema.ToolCallProgress(
                    session_update="tool_call_update",
                    tool_call_id=str(call_id),
                    status=status,
                    content=content or None,
                )
                try:
                    await self._conn.session_update(session_id=session_id, update=start)
                    await self._conn.session_update(session_id=session_id, update=progress)
                except Exception:
                    log.warning("load replay interrupted by delivery failure", exc_info=True)
                    return
                continue
            elif inner in ("compaction",) and text:
                # System-generated context, not agent speech: thought chunks
                # keep it visible without misattributing the speaker.
                update = sdk.update_agent_thought(sdk.text_block(_clean_text(text)))
            elif (
                text
                or data.get("call_id") is not None
                or data.get("result") is not None
                or data.get("error") is not None
                or data.get("interaction") is not None
            ):
                raise _sdk().RequestError.internal_error(
                    {
                        "message": f"Cannot faithfully replay history event type {inner!r}; refusing lossy load.",
                        "sessionId": session_id,
                    }
                )
            else:
                continue  # content-free record: nothing to replay
            if update is not None:
                try:
                    update.message_id = message_id
                except Exception:
                    pass
                try:
                    await self._conn.session_update(session_id=session_id, update=update)
                except Exception:
                    log.warning("load replay interrupted by delivery failure", exc_info=True)
                    return

    # -- unsupported surface (honest method_not_found) ------------------------

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise _sdk().RequestError.method_not_found(f"_{method}")

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        return None


async def serve_stdio(controller: Any, runtime: str, *, actor: str = "acp-client") -> None:
    """Run ``ControlAgent`` over stdio using the official SDK framing.

    ``use_unstable_protocol=True`` is required with SDK 0.12.1 because that
    SDK gates ``session/resume`` and ``session/close`` behind the unstable
    flag even though released schema v1.27.0 declares both stable. No draft
    method is implemented, so enabling the flag changes nothing observable
    beyond unblocking the two stable routes. Wire payloads are validated in
    tests against the released stable schema, not the SDK's draft surface.
    """
    sdk = _sdk()
    agent = ControlAgent(controller, runtime, actor=actor)
    await sdk.run_agent(agent, use_unstable_protocol=True)


def main(argv: list[str] | None = None) -> int:
    """``agent-session-control acp`` entry: protocol-only stdout."""
    import argparse

    parser = argparse.ArgumentParser(prog="agent-session-control acp")
    parser.add_argument("--runtime", required=True, help="The single runtime served on this connection.")
    parser.add_argument("--root", default=None, help="Isolated state root for this consumer.")
    parser.add_argument("--namespace", default="default", help="Namespace inside the state root.")
    parser.add_argument("--actor", default="acp-client", help="Audit actor for operations on this connection.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(name)s: %(levelname)s: %(message)s")
    try:
        from agent_session_control.controller import LocalController
    except ImportError as exc:
        print(f"acp: engine controller unavailable: {exc}", file=sys.stderr)
        return 1
    from pathlib import Path

    root = Path(args.root).expanduser() if args.root else None
    controller = LocalController(root=root, namespace=args.namespace)
    asyncio.run(serve_stdio(controller, args.runtime, actor=args.actor))
    return 0

"""Agent-friendly JSON CLI for the shared session controller.

Machine contract: non-TTY or ``--json`` prints exactly one
``{ok, data, error, meta}`` envelope on stdout with a stable exit code;
logs go to stderr. ``run events --follow`` is the only streaming command
and speaks JSONL. Every subcommand registers only flags it really reads.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from agent_session_control.errors import (
    EXIT_ERROR,
    ControlError,
    ok_envelope,
)

PACKAGE_NAME = "agent-session-control"
MAX_PROMPT_CHARS = 100_000
MAX_REQUEST_KEY = 200
WAIT_DEFAULT_TIMEOUT = 300.0
WAIT_MAX_TIMEOUT = 3600.0
FOLLOW_DEFAULT_TIMEOUT = 60.0
FOLLOW_DEFAULT_LIMIT = 200


def _exe_argv() -> list[str]:
    return [sys.executable, "-m", "agent_session_control"]


def _next(*parts: str) -> list[str]:
    return [" ".join([*_exe_argv(), *parts])]


def _pkg_version() -> str:
    try:
        from importlib.metadata import version

        return version(PACKAGE_NAME)
    except Exception:
        return "0.1.0"


def _meta_extra() -> dict:
    return {"version": _pkg_version(), "package": PACKAGE_NAME}


# -- command table (single source for argparse and describe) ------------------

ARG_ROOT = {"flags": ("--root",), "help": "Isolated state root for this consumer."}
ARG_NAMESPACE = {"flags": ("--namespace",), "default": "default", "help": "Namespace inside the state root."}
ARG_JSON = {"flags": ("--json",), "action": "store_true", "help": "Force machine JSON output even on a TTY."}
ARG_CWD = {"flags": ("--cwd",), "required": True, "help": "Absolute project directory."}
ARG_MODEL = {
    "flags": ("--model",),
    "default": None,
    "help": "Optional model for this launch only; omit to inherit runtime defaults.",
}
ARG_DRY_RUN = {
    "flags": ("--dry-run",),
    "action": "store_true",
    "help": "Validate and preview without side effects; creates no state.",
}
ARG_MCP = {
    "flags": ("--mcp-servers",),
    "default": None,
    "help": "JSON array of stdio MCP servers for this session (default: none).",
}
ARG_SESSION_ID = {"flags": ("--session-id",), "required": True, "help": "Exact local control session id."}
ARG_RUN_ID = {"flags": ("--run-id",), "required": True, "help": "Durable run id returned by run submit."}

COMMANDS: dict[str, dict] = {
    "version": {
        "summary": "Print package and interpreter versions. Offline.",
        "risk": "read",
        "args": [ARG_JSON],
        "examples": ["version --json"],
    },
    "describe": {
        "summary": "Machine-readable command/parameter reference from the live parser.",
        "risk": "read",
        "args": [ARG_JSON, {"flags": ("--command",), "default": None, "help": "Describe one command only."}],
        "examples": ["describe --json", "describe --command run"],
    },
    "doctor": {
        "summary": "Read-only environment checks: interpreter, extras, runtimes, state root.",
        "risk": "read",
        "args": [ARG_JSON, ARG_ROOT, ARG_NAMESPACE],
        "examples": ["doctor --json"],
    },
    "plan create": {
        "summary": "Pure fresh-launch plan (argv/cwd); never launches or writes state.",
        "risk": "read",
        "args": [ARG_JSON, {"flags": ("--runtime",), "required": True, "help": "Runtime id."}, ARG_CWD, ARG_MODEL],
        "examples": ["plan create --runtime codex --cwd /tmp/proj"],
    },
    "plan resume": {
        "summary": "Pure native-resume plan for an exact session mapping; never launches.",
        "risk": "read",
        "args": [
            ARG_JSON,
            {"flags": ("--runtime",), "required": True},
            {"flags": ("--session-id",), "required": True},
            {"flags": ("--path",), "required": True, "help": "Exact native history path."},
            ARG_CWD,
        ],
        "examples": ["plan resume --runtime claude --session-id abc --path /h --cwd /tmp/proj"],
    },
    "session create": {
        "summary": "Create and host a fresh controlled session for one runtime/project.",
        "risk": "write_local",
        "args": [
            ARG_JSON,
            ARG_ROOT,
            ARG_NAMESPACE,
            {"flags": ("--runtime",), "required": True},
            ARG_CWD,
            ARG_MODEL,
            ARG_DRY_RUN,
            ARG_MCP,
        ],
        "examples": ["session create --runtime pi --cwd /tmp/proj"],
    },
    "session resume": {
        "summary": "Resume an exact known session without rescanning or guessing.",
        "risk": "write_local",
        "args": [
            ARG_JSON,
            ARG_ROOT,
            ARG_NAMESPACE,
            ARG_SESSION_ID,
            {"flags": ("--cwd",), "default": None, "help": "Optional cwd; must match the stored session when given."},
            ARG_DRY_RUN,
            ARG_MCP,
        ],
        "examples": ["session resume --session-id ses_abc"],
    },
    "session close": {
        "summary": "Cancel owned work and release resources; never deletes history.",
        "risk": "write_local",
        "args": [ARG_JSON, ARG_ROOT, ARG_NAMESPACE, ARG_SESSION_ID],
        "examples": ["session close --session-id ses_abc"],
    },
    "session list": {
        "summary": "List controlled sessions, optionally filtered by cwd.",
        "risk": "read",
        "args": [
            ARG_JSON,
            ARG_ROOT,
            ARG_NAMESPACE,
            {"flags": ("--cwd",), "default": None},
            {"flags": ("--cursor",), "default": None},
        ],
        "examples": ["session list --json"],
    },
    "run submit": {
        "summary": "Submit one prompt turn; returns a durable run id immediately.",
        "risk": "write_local",
        "args": [
            ARG_JSON,
            ARG_ROOT,
            ARG_NAMESPACE,
            ARG_SESSION_ID,
            {"flags": ("--prompt",), "default": None},
            {"flags": ("--prompt-file",), "default": None, "help": "Path to a brief file; '-' reads piped stdin."},
            {"flags": ("--request-id",), "default": None, "help": "Immutable idempotency key (default: generated)."},
            {
                "flags": ("--on-busy",),
                "default": "queue",
                "choices": ("queue", "steer", "reject"),
                "help": "Disposition when the session is busy.",
            },
            ARG_DRY_RUN,
        ],
        "examples": ["run submit --session-id ses_abc --prompt 'fix the login retry'"],
    },
    "run wait": {
        "summary": "Block until the run settles. A timeout never cancels the work.",
        "risk": "read",
        "args": [
            ARG_JSON,
            ARG_ROOT,
            ARG_NAMESPACE,
            ARG_RUN_ID,
            {
                "flags": ("--timeout",),
                "default": WAIT_DEFAULT_TIMEOUT,
                "type": float,
                "help": f"Seconds to wait (default {WAIT_DEFAULT_TIMEOUT:g}, max {WAIT_MAX_TIMEOUT:g}).",
            },
        ],
        "examples": ["run wait --run-id run_abc --timeout 120"],
    },
    "run events": {
        "summary": "Read buffered run events, or follow them as JSONL.",
        "risk": "read",
        "args": [
            ARG_JSON,
            ARG_ROOT,
            ARG_NAMESPACE,
            ARG_RUN_ID,
            {"flags": ("--after",), "default": 0, "type": int},
            {"flags": ("--limit",), "default": 50, "type": int},
            {
                "flags": ("--follow",),
                "action": "store_true",
                "help": "Stream JSONL until settled or the budget expires.",
            },
            {"flags": ("--timeout",), "default": FOLLOW_DEFAULT_TIMEOUT, "type": float},
            {"flags": ("--unbounded",), "action": "store_true", "help": "Lift the follow budget (rows/time)."},
        ],
        "examples": ["run events --run-id run_abc --after 0", "run events --run-id run_abc --follow --timeout 30"],
    },
    "run interrupt": {
        "summary": "Request cancellation of exactly this run; never unrelated work.",
        "risk": "write_local",
        "args": [ARG_JSON, ARG_ROOT, ARG_NAMESPACE, ARG_RUN_ID],
        "examples": ["run interrupt --run-id run_abc"],
    },
    "acp": {
        "summary": "Serve one runtime as an ACP agent over stdio (protocol-only stdout).",
        "risk": "serve",
        "args": [
            {"flags": ("--runtime",), "required": True},
            ARG_ROOT,
            ARG_NAMESPACE,
            {"flags": ("--actor",), "default": "acp-client"},
        ],
        "examples": ["acp --runtime codex"],
    },
}

COMMAND_ORDER = [
    "version",
    "describe",
    "doctor",
    "plan create",
    "plan resume",
    "session create",
    "session resume",
    "session close",
    "session list",
    "run submit",
    "run wait",
    "run events",
    "run interrupt",
    "acp",
]

EXIT_BY_CODE = {"usage_error": 2, "not_found": 3, "not_authorized": 4, "conflict": 5, "wait_timeout": 6}


# -- output --------------------------------------------------------------------


def _wants_json(args: Any) -> bool:
    return bool(getattr(args, "json", False)) or not sys.stdout.isatty()


def _human(data: Any) -> str:
    if isinstance(data, dict):
        lines = []
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
            else:
                lines.append(f"{key}: {value}")
        return "\n".join(lines)
    return json.dumps(data, ensure_ascii=False)


def emit(data: Any, args: Any) -> int:
    if _wants_json(args):
        json.dump(ok_envelope(data, meta=_meta_extra()), sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        print(_human(data))
    return 0


def emit_error(exc: BaseException) -> int:
    if isinstance(exc, ControlError):
        # Exit by stable error code first so one code always means one exit;
        # fall back to the carried exit code for codes without a fixed exit.
        if exc.code in EXIT_BY_CODE:
            code = EXIT_BY_CODE[exc.code]
        elif isinstance(exc.exit_code, int) and 1 <= exc.exit_code <= 6:
            code = exc.exit_code
        else:
            code = EXIT_ERROR
        envelope = exc.to_envelope(meta=_meta_extra())
    else:
        code = EXIT_ERROR
        envelope = {
            "ok": False,
            "data": None,
            "error": {
                "code": "internal",
                "message": f"{type(exc).__name__}: {exc}",
                "hint": None,
                "next_commands": [],
                "details": None,
            },
            "meta": _meta_extra(),
        }
    json.dump(envelope, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return code


# -- validation -----------------------------------------------------------------

_PROTECTED = ("", "Desktop", "Documents")


def _validate_cwd(value: str, *, what: str = "--cwd") -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ControlError("usage_error", f"{what} must be an absolute project path.", 2)
    resolved = path.resolve()
    if not resolved.is_dir():
        raise ControlError("not_found", f"The project directory does not exist: {resolved}.", 3)
    return str(resolved)


def _guard_create_directory(cwd: str) -> None:
    home = Path.home().resolve()
    protected = {Path("/").resolve(), home, *(home / name for name in _PROTECTED[1:])}
    if Path(cwd) in protected:
        raise ControlError(
            "usage_error",
            "Agents are not launched in /, the home root, Desktop or Documents.",
            2,
            hint="Pass the project's own directory.",
        )


def _validate_key(value: str | None, *, name: str) -> str:
    text = (value or "").strip()
    if not text or len(text) > MAX_REQUEST_KEY or any(ord(c) < 32 or c.isspace() for c in text):
        raise ControlError(
            "usage_error", f"{name} must be 1-{MAX_REQUEST_KEY} printable characters without whitespace.", 2
        )
    return text


def _read_prompt(args: Any) -> str:
    inline, path = getattr(args, "prompt", None), getattr(args, "prompt_file", None)
    if bool(inline) == bool(path):
        raise ControlError("usage_error", "Pass exactly one of --prompt or --prompt-file.", 2)
    if inline is not None:
        text = inline
    elif path == "-":
        if sys.stdin.isatty():
            raise ControlError("usage_error", "--prompt-file - needs piped stdin.", 2)
        text = sys.stdin.read()
    else:
        prompt_path = path if isinstance(path, str) else str(path)
        try:
            with open(prompt_path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            raise ControlError("usage_error", f"Cannot read --prompt-file: {exc}.", 2) from exc
    if not text or not text.strip():
        raise ControlError("usage_error", "The prompt is empty.", 2)
    if "\x00" in text:
        raise ControlError("usage_error", "The prompt contains a NUL byte.", 2)
    if len(text) > MAX_PROMPT_CHARS:
        raise ControlError(
            "usage_error",
            f"The prompt exceeds {MAX_PROMPT_CHARS} characters.",
            2,
            hint="Write long material to a file in the project and reference its path.",
        )
    return text


def _parse_mcp_servers(raw: str | None) -> tuple:
    if not raw:
        return ()
    try:
        servers = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ControlError("usage_error", f"--mcp-servers is not valid JSON: {exc}.", 2) from exc
    if not isinstance(servers, list):
        raise ControlError("usage_error", "--mcp-servers must be a JSON array.", 2)
    for entry in servers:
        if not isinstance(entry, dict) or not entry.get("command"):
            raise ControlError("usage_error", "Each --mcp-servers entry needs an object with a 'command' path.", 2)
    return tuple(servers)


# -- controller ------------------------------------------------------------------


def _default_controller(args: Any) -> Any:
    from agent_session_control.controller import LocalController

    root = Path(args.root).expanduser() if getattr(args, "root", None) else None
    return LocalController(root=root, namespace=getattr(args, "namespace", "default"))


def _session_dict(handle: Any) -> dict:
    to_dict = getattr(handle, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return {
        "id": getattr(handle, "id", None),
        "runtime": getattr(handle, "runtime", None),
        "cwd": getattr(handle, "cwd", None),
        "native_id": getattr(handle, "native_id", None),
        "instance_id": getattr(handle, "instance_id", None),
    }


# -- reads ------------------------------------------------------------------------


def cmd_version(args: Any, _factory: Any = None) -> dict:
    return {"package": PACKAGE_NAME, "version": _pkg_version(), "python": sys.version.split()[0]}


def cmd_describe(args: Any, _factory: Any = None) -> dict:
    wanted = getattr(args, "command", None)
    if wanted is not None and wanted not in COMMANDS:
        raise ControlError(
            "usage_error", f"Unknown command: {wanted}.", 2, hint=f"Available: {', '.join(COMMAND_ORDER)}."
        )
    names = [wanted] if wanted else COMMAND_ORDER
    commands = []
    for name in names:
        spec = COMMANDS[name]
        params = []
        for arg in spec["args"]:
            flags = list(arg["flags"])
            default = arg.get("default", False if arg.get("action") == "store_true" else None)
            params.append(
                {
                    "flags": flags,
                    "required": bool(arg.get("required", False)),
                    "choices": list(arg.get("choices", ())) or None,
                    "default": default,
                    "help": arg.get("help", ""),
                }
            )
        commands.append(
            {
                "command": name,
                "summary": spec["summary"],
                "risk": spec["risk"],
                "params": params,
                "examples": list(spec.get("examples", [])),
            }
        )
    return {"commands": commands if wanted is None else commands[0]}


def cmd_doctor(args: Any, _factory: Any = None) -> dict:
    from agent_session_control import runtime as runtime_mod

    checks: dict[str, Any] = {
        "package": {"name": PACKAGE_NAME, "version": _pkg_version()},
        "python": sys.version.split()[0],
        "extras": {},
        "engine": None,
        "runtimes": [],
        "root": None,
    }
    for extra, module in (("acp", "acp"), ("history", "sesskit")):
        try:
            __import__(module)
            checks["extras"][extra] = "installed"
        except ImportError:
            checks["extras"][extra] = "missing"
    try:
        from agent_session_control import controller as _controller_mod  # noqa: F401

        checks["engine"] = "available"
    except ImportError as exc:
        checks["engine"] = f"missing: {exc}"
    try:
        checks["runtimes"] = runtime_mod.describe_runtimes()
    except ControlError as exc:
        checks["runtimes"] = f"unavailable: {exc.code}"
    try:
        from agent_session_control.controller import default_root

        root = Path(args.root).expanduser() if getattr(args, "root", None) else default_root() / "default"
        checks["root"] = {
            "path": str(root),
            "exists": root.exists(),
            "writable": os.access(root, os.W_OK) if root.exists() else os.access(root.parent, os.W_OK),
        }
    except ImportError:
        checks["root"] = {"path": None, "note": "engine unavailable"}
    return checks


def cmd_plan_create(args: Any, _factory: Any = None) -> dict:
    from agent_session_control import runtime as runtime_mod

    cwd = _validate_cwd(args.cwd)
    try:
        plan = runtime_mod.build_create_plan(args.runtime, cwd, model=args.model)
    except ControlError:
        raise
    except Exception as exc:
        raise ControlError("usage_error", f"Cannot plan for runtime {args.runtime!r}: {exc}.", 2) from exc
    return {
        "dry_run": True,
        "status": "planned",
        "launch": plan.to_dict(),
    }


def cmd_plan_resume(args: Any, _factory: Any = None) -> dict:
    from agent_session_control import runtime as runtime_mod

    mapping = {"id": args.session_id, "path": args.path, "cwd": args.cwd}
    try:
        plan = runtime_mod.build_resume_plan(args.runtime, mapping)
    except ControlError:
        raise
    except Exception as exc:
        raise ControlError("usage_error", f"Cannot plan resume for {args.runtime!r}: {exc}.", 2) from exc
    return {
        "dry_run": True,
        "status": "planned",
        "launch": plan.to_dict(),
    }


# -- session writes ------------------------------------------------------------------


def cmd_session_create(args: Any, factory: Any = None) -> dict:
    cwd = _validate_cwd(args.cwd)
    _guard_create_directory(cwd)
    servers = _parse_mcp_servers(args.mcp_servers)
    if args.dry_run:
        from agent_session_control import runtime as runtime_mod

        try:
            plan = runtime_mod.build_create_plan(args.runtime, cwd, model=args.model, mcp_servers=servers)
        except ControlError:
            raise
        except Exception as exc:
            raise ControlError("usage_error", f"Cannot plan for runtime {args.runtime!r}: {exc}.", 2) from exc
        return {
            "dry_run": True,
            "status": "planned",
            "session": {"id": None, "runtime": args.runtime, "cwd": cwd, "model": args.model},
            "launch": plan.to_dict(),
            "target": {"runtime": args.runtime, "cwd": cwd},
        }
    controller = (factory or _default_controller)(args)
    handle = _run_coro(controller.create(args.runtime, cwd, model=args.model, mcp_servers=servers, actor="local-user"))
    return {
        "dry_run": False,
        "status": "created",
        "session": _session_dict(handle),
        "next_commands": [
            *_next("session", "list"),
            *_next("run", "submit", "--session-id", handle.id, "--prompt", "<brief>"),
        ],
    }  # type: ignore[union-attr]


def cmd_session_resume(args: Any, factory: Any = None) -> dict:
    session_id = _validate_key(args.session_id, name="--session-id")
    servers = _parse_mcp_servers(args.mcp_servers)
    if args.dry_run:
        return {"dry_run": True, "status": "planned", "session": {"id": session_id, "cwd": args.cwd}}
    controller = (factory or _default_controller)(args)
    handle = _run_coro(controller.resume(session_id, cwd=args.cwd, mcp_servers=servers, actor="local-user"))
    return {"dry_run": False, "status": "resumed", "session": _session_dict(handle)}


def cmd_session_close(args: Any, factory: Any = None) -> dict:
    session_id = _validate_key(args.session_id, name="--session-id")
    controller = (factory or _default_controller)(args)
    _run_coro(controller.close_session(session_id, actor="local-user"))
    return {"status": "closed", "session_id": session_id}


def cmd_session_list(args: Any, factory: Any = None) -> dict:
    controller = (factory or _default_controller)(args)
    page = _run_coro(controller.list_sessions(cwd=args.cwd, cursor=args.cursor))
    sessions = (page or {}).get("sessions", [])
    return {"sessions": sessions, "returned": len(sessions), "next_cursor": (page or {}).get("next_cursor")}


# -- run writes/reads ---------------------------------------------------------------


def _blocks(prompt: str) -> list[dict]:
    return [{"type": "text", "text": prompt}]


def cmd_run_submit(args: Any, factory: Any = None) -> dict:
    session_id = _validate_key(args.session_id, name="--session-id")
    prompt = _read_prompt(args)
    request_id = _validate_key(args.request_id, name="--request-id") if args.request_id else uuid.uuid4().hex
    if args.on_busy not in ("queue", "steer", "reject"):
        raise ControlError("usage_error", "Unknown --on-busy mode.", 2)
    if args.dry_run:
        return {
            "dry_run": True,
            "status": "planned",
            "run": {
                "run_id": None,
                "session_id": session_id,
                "request_id": request_id,
                "on_busy": args.on_busy,
                "blocks": _blocks(prompt),
                "prompt_chars": len(prompt),
            },
        }
    controller = (factory or _default_controller)(args)
    run_id = _run_coro(
        controller.start_turn(
            session_id, _blocks(prompt), request_id=request_id, on_busy=args.on_busy, actor="local-user"
        )
    )
    return {
        "dry_run": False,
        "status": "submitted",
        "run": {"run_id": run_id, "session_id": session_id, "request_id": request_id, "on_busy": args.on_busy},
        "next_commands": _next("run", "wait", "--run-id", run_id, "--timeout", "120"),
    }


def _result_dict(result: Any) -> dict:
    to_dict = getattr(result, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return {
        "run_id": getattr(result, "run_id", None),
        "state": getattr(result, "state", None),
        "final_response": getattr(result, "final_response", None),
        "stop_reason": getattr(result, "stop_reason", None),
        "error": getattr(result, "error", None),
        "evidence": getattr(result, "evidence", None),
    }


def cmd_run_wait(args: Any, factory: Any = None) -> dict:
    run_id = _validate_key(args.run_id, name="--run-id")
    timeout = max(0.0, min(float(args.timeout), WAIT_MAX_TIMEOUT))
    controller = (factory or _default_controller)(args)
    result = _run_coro(controller.wait(run_id, timeout=timeout))
    return {
        "run": _result_dict(result),
        "note": "state=completed means the turn ended; review the work before treating the task as done.",
    }


def _event_dict(event: Any) -> dict:
    to_dict = getattr(event, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return {
        "seq": getattr(event, "seq", None),
        "kind": getattr(event, "kind", None),
        "session_id": getattr(event, "session_id", None),
        "run_id": getattr(event, "run_id", None),
        "data": getattr(event, "data", None),
        "evidence": getattr(event, "evidence", None),
        "generation": getattr(event, "generation", None),
    }


def _collect_events(controller: Any, run_id: str, after: int, limit: int) -> list[dict]:
    async def _drain() -> list[dict]:
        out = []
        async for event in controller.watch(run_id, after=after):
            out.append(_event_dict(event))
            if len(out) >= limit:
                break
        return out

    return _run_coro(_drain())


def cmd_run_events(args: Any, factory: Any = None) -> dict | None:
    run_id = _validate_key(args.run_id, name="--run-id")
    after = max(0, int(args.after))
    limit = max(1, min(int(args.limit), 500))
    controller = (factory or _default_controller)(args)
    if not args.follow:
        events = _collect_events(controller, run_id, after, limit)
        return {
            "run_id": run_id,
            "events": events,
            "next_cursor": events[-1]["seq"] if events else after,
            "has_more": len(events) >= limit,
        }
    _follow_jsonl(
        controller, run_id, after=after, limit=limit, timeout=float(args.timeout), unbounded=bool(args.unbounded)
    )
    return None


def _follow_jsonl(controller: Any, run_id: str, *, after: int, limit: int, timeout: float, unbounded: bool) -> int:
    import time as _time

    deadline = None if unbounded and timeout <= 0 else _time.monotonic() + max(0.0, timeout)
    budget = 10_000_000 if unbounded else max(1, limit)

    def line(record: dict) -> None:
        sys.stdout.write(json.dumps(record, ensure_ascii=False) + "\n")

    line({"type": "start", "run_id": run_id})
    count = 0

    async def _run() -> str:
        nonlocal count
        async for event in controller.watch(run_id, after=after):
            line({"type": "record", "run_id": run_id, **_event_dict(event)})
            count += 1
            if count >= budget:
                return "budget_exhausted"
            if deadline is not None and _time.monotonic() >= deadline:
                return "timeout"
        return "settled"

    try:
        reason = _run_coro(_run())
    except ControlError as exc:
        line({"type": "end", "run_id": run_id, "reason": "error", "error": exc.to_dict()})
        raise
    line(
        {
            "type": "end",
            "run_id": run_id,
            "reason": reason,
            "note": "A timeout/budget end never cancels the run; read events again to continue.",
        }
    )
    return 0


def cmd_run_interrupt(args: Any, factory: Any = None) -> dict:
    run_id = _validate_key(args.run_id, name="--run-id")
    controller = (factory or _default_controller)(args)
    _run_coro(controller.interrupt(run_id, actor="local-user"))
    return {
        "status": "interrupt_requested",
        "run_id": run_id,
        "note": "Interruption is confirmed by run wait/events settling to interrupted.",
        "next_commands": _next("run", "wait", "--run-id", run_id, "--timeout", "60"),
    }


def _run_coro(awaitable: Any) -> Any:
    return asyncio.run(_await_result(awaitable))


async def _await_result(awaitable: Any) -> Any:
    return await awaitable


HANDLERS = {
    "version": cmd_version,
    "describe": cmd_describe,
    "doctor": cmd_doctor,
    "plan create": cmd_plan_create,
    "plan resume": cmd_plan_resume,
    "session create": cmd_session_create,
    "session resume": cmd_session_resume,
    "session close": cmd_session_close,
    "session list": cmd_session_list,
    "run submit": cmd_run_submit,
    "run wait": cmd_run_wait,
    "run events": cmd_run_events,
    "run interrupt": cmd_run_interrupt,
}


# -- parser --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-session-control")
    sub = parser.add_subparsers(dest="command", required=True)
    groups: dict[str, Any] = {}

    def group(name: str) -> Any:
        if name not in groups:
            words = name.split()
            if len(words) == 1:
                groups[name] = sub.add_parser(words[0], help=f"{words[0]} commands")
            else:
                parent = group(words[0])
                if not hasattr(parent, "_sub"):
                    parent._sub = parent.add_subparsers(dest="subcommand", required=True)  # noqa: SLF001
                groups[name] = parent._sub.add_parser(words[1], help=COMMANDS[name]["summary"])
        return groups[name]

    for name in COMMAND_ORDER:
        if name == "acp":
            continue  # acp owns protocol stdout; registered separately below
        spec = COMMANDS[name]
        node = group(name)
        node.set_defaults(handler=HANDLERS[name], command_name=name)
        for arg in spec["args"]:
            flags = list(arg["flags"])
            kwargs: dict[str, Any] = {"help": arg.get("help", "")}
            if arg.get("required"):
                kwargs["required"] = True
            if "default" in arg:
                kwargs["default"] = arg["default"]
            if arg.get("action"):
                kwargs["action"] = arg["action"]
            if arg.get("type"):
                kwargs["type"] = arg["type"]
            if arg.get("choices"):
                kwargs["choices"] = tuple(arg["choices"])
            node.add_argument(*flags, **kwargs)
    acp_node = sub.add_parser("acp", help=COMMANDS["acp"]["summary"])
    from agent_session_control.acp import main as acp_main  # noqa: PLC0415

    acp_node.set_defaults(handler="acp", command_name="acp", acp_main=acp_main)
    for arg in COMMANDS["acp"]["args"]:
        flags = list(arg["flags"])
        kwargs = {"help": arg.get("help", "")}
        if arg.get("required"):
            kwargs["required"] = True
        if "default" in arg:
            kwargs["default"] = arg["default"]
        acp_node.add_argument(*flags, **kwargs)
    return parser


def _split_command(args: Any) -> str:
    base = args.command
    sub = getattr(args, "subcommand", None)
    return f"{base} {sub}" if sub else base


def main(argv: list[str] | None = None, *, controller_factory: Any = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "handler", None) == "acp":
        # Protocol-only stdout: the SDK owns it from here; envelopes stay out.
        acp_argv = ["--runtime", args.runtime]
        if getattr(args, "root", None):
            acp_argv += ["--root", args.root]
        if getattr(args, "namespace", None):
            acp_argv += ["--namespace", args.namespace]
        acp_argv += ["--actor", getattr(args, "actor", "acp-client")]
        return int(args.acp_main(acp_argv))
    try:
        data = args.handler(args, controller_factory)
    except ControlError as exc:
        return emit_error(exc)
    except BrokenPipeError:
        return 1
    if data is None:
        return 0  # streaming command already wrote its own records
    try:
        return emit(data, args)
    except BrokenPipeError:
        return 1
    except ControlError as exc:
        return emit_error(exc)

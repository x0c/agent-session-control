"""Stable control errors with CLI exit semantics.

Adapted from Corral ``src/corral/control_store.py`` (MIT, corral contributors):
the ``ControlError`` constructor, observable fields and exit codes are kept
identical so existing callers and tests observe the same shape.
"""

from __future__ import annotations

from typing import Any

EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_AUTH = 4
EXIT_CONFLICT = 5
EXIT_TIMEOUT = 6

CODE_USAGE = "usage_error"
CODE_NOT_FOUND = "not_found"
CODE_CONFLICT = "conflict"
CODE_IDEMPOTENCY_CONFLICT = "idempotency_conflict"
CODE_TERMINAL_RUN = "terminal_run"
CODE_UNSUPPORTED = "unsupported"
CODE_UNAVAILABLE = "unavailable"
CODE_NOT_AUTHORIZED = "not_authorized"
CODE_TIMEOUT = "wait_timeout"
CODE_UNCERTAIN_DELIVERY = "uncertain_delivery"
CODE_LOST_OWNERSHIP = "lost_ownership"
CODE_UPSTREAM_FAILURE = "upstream_failure"
CODE_STORE_TOO_NEW = "store_too_new"
CODE_MODEL_UNSUPPORTED = "model_selection_unsupported"


class ControlError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        exit_code: int = EXIT_ERROR,
        *,
        hint: str | None = None,
        next_commands: list[str] | None = None,
        details: dict | None = None,
    ):
        super().__init__(message)
        self.code, self.message, self.exit_code = code, message, exit_code
        self.hint, self.next_commands, self.details = hint, next_commands or [], details

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "next_commands": list(self.next_commands),
            "details": dict(self.details) if self.details is not None else None,
        }

    def to_envelope(self, *, meta: dict | None = None) -> dict:
        """Agent-friendly ``{ok, data, error, meta}`` envelope for CLI surfaces."""
        return {"ok": False, "data": None, "error": self.to_dict(), "meta": dict(meta or {})}


def ok_envelope(data: Any, *, meta: dict | None = None) -> dict:
    return {"ok": True, "data": data, "error": None, "meta": dict(meta or {})}


def usage_error(message: str, **kwargs: Any) -> ControlError:
    return ControlError(CODE_USAGE, message, EXIT_USAGE, **kwargs)


def not_found(message: str, **kwargs: Any) -> ControlError:
    return ControlError(CODE_NOT_FOUND, message, EXIT_NOT_FOUND, **kwargs)


def conflict(message: str, **kwargs: Any) -> ControlError:
    return ControlError(CODE_CONFLICT, message, EXIT_CONFLICT, **kwargs)


def idempotency_conflict(message: str, **kwargs: Any) -> ControlError:
    return ControlError(CODE_IDEMPOTENCY_CONFLICT, message, EXIT_CONFLICT, **kwargs)


def unsupported(message: str, **kwargs: Any) -> ControlError:
    return ControlError(CODE_UNSUPPORTED, message, EXIT_USAGE, **kwargs)


def unavailable(message: str, **kwargs: Any) -> ControlError:
    return ControlError(CODE_UNAVAILABLE, message, EXIT_ERROR, **kwargs)


def wait_timeout(message: str, **kwargs: Any) -> ControlError:
    return ControlError(CODE_TIMEOUT, message, EXIT_TIMEOUT, **kwargs)


def uncertain_delivery(message: str, **kwargs: Any) -> ControlError:
    return ControlError(CODE_UNCERTAIN_DELIVERY, message, EXIT_ERROR, **kwargs)


def lost_ownership(message: str, **kwargs: Any) -> ControlError:
    return ControlError(CODE_LOST_OWNERSHIP, message, EXIT_CONFLICT, **kwargs)


def upstream_failure(message: str, **kwargs: Any) -> ControlError:
    return ControlError(CODE_UPSTREAM_FAILURE, message, EXIT_ERROR, **kwargs)

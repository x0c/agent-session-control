"""Run/message identity and native-evidence correlation.

Adapted from Corral ``src/corral/control.py`` (MIT, corral contributors):
``marker``/``wire_prompt``/``own_message``/``compact_event``/``correlate``/
``build_result`` and prompt validation keep their semantics, but the marker
namespace is explicit with a neutral default. Compatibility hosts select
their legacy namespace explicitly (e.g. ``namespace="Corral"``) instead of
forking the logic. No downstream product name is hardcoded anywhere here.

Only native user-message evidence can establish input recording; markers
quoted in assistant or tool output never count. SessKit events and turns are
consumed through their public attributes, never reparsed from raw native
files. Usage observations are keyed by native event identity plus usage
evidence origin, never by token values; an observation repeated by
overlapping evidence ranges is counted once and reported, never presented
as a billing total. Totals and costs are not aggregated.
"""

from __future__ import annotations

import re
from typing import Any

from agent_session_control.errors import usage_error

DEFAULT_NAMESPACE = "session-control"

MAX_PROMPT_CHARS = 100_000
_EVENT_TEXT_LIMIT = 1_200
_RESULT_TEXT_LIMIT = 20_000
_ERROR_TEXT_LIMIT = 400


def _marker_pattern(namespace: str) -> re.Pattern[str]:
    return re.compile(r"\[" + re.escape(namespace) + r" run=(run_[0-9a-f]+) message=([\w-]+)\]")


def marker(run_id: str, message_id: str, *, namespace: str = DEFAULT_NAMESPACE) -> str:
    return f"[{namespace} run={run_id} message={message_id}]"


def wire_prompt(run_id: str, message_id: str, prompt: str, *, namespace: str = DEFAULT_NAMESPACE) -> str:
    """The visible envelope lets native evidence prove which run an input belongs to."""
    return f"{prompt}\n\n{marker(run_id, message_id, namespace=namespace)}"


def validate_prompt(text: str, *, max_chars: int = MAX_PROMPT_CHARS) -> str:
    if not text or not text.strip():
        raise usage_error("The prompt is empty.")
    if "\x00" in text:
        raise usage_error("The prompt contains a NUL byte.")
    if len(text) > max_chars:
        raise usage_error(
            f"The prompt exceeds {max_chars} characters.",
            hint="Write long material to a file in the project and reference its path in the brief.",
        )
    return text


def own_message(text: str | None, run_id: str, *, namespace: str = DEFAULT_NAMESPACE) -> str | None:
    pattern = _marker_pattern(namespace)
    return next((match.group(2) for match in pattern.finditer(text or "") if match.group(1) == run_id), None)


def _field(event: Any, name: str, default: Any = None) -> Any:
    if hasattr(event, name):
        return getattr(event, name)
    if isinstance(event, dict):
        return event.get(name, default)
    return default


def _lossless(value: Any) -> Any:
    """Plain-data projection of nested native records without loss.

    Frozen dataclasses (SessKit evidence, outcomes, errors, interactions,
    usage, compaction) become plain dicts recursively, including dataclasses
    nested inside dicts/lists/tuples (e.g. genuine native records inside
    ``raw_input``/``raw_output``). Anything else passes through untouched:
    fields are never parsed, summarized or fabricated.
    """
    import dataclasses as _dataclasses
    from types import SimpleNamespace as _SimpleNamespace

    if _dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _lossless(v) for k, v in _dataclasses.asdict(value).items()}
    if isinstance(value, _SimpleNamespace):
        return {k: _lossless(v) for k, v in vars(value).items()}
    if isinstance(value, dict):
        return {k: _lossless(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_lossless(v) for v in value]
    return value


def project_event(event: Any, *, text_limit: int | None = None) -> dict:
    """Lossless shared projection of one native event.

    Retains native ``message_id``/``call_id``/``type``/provenance and all
    available tool result/error/interaction/usage content with full text by
    default. ``text_limit`` caps only the text field for explicitly bounded
    callers; ``None`` (default) keeps the complete text. Native events are
    SessKit objects consumed through public attributes; nothing is reparsed
    from raw history and no field is fabricated.
    """
    out: dict = {
        "type": _field(event, "type"),
        "seq": _field(event, "seq"),
        "ts": _field(event, "ts"),
    }
    text = _field(event, "text")
    if text is not None:
        out["text"] = text if text_limit is None else _trim(text, text_limit)
    for key in ("message_id", "turn_id", "name", "call_id", "origin", "stop_reason"):
        value = _field(event, key)
        if value is not None:
            out[key] = value
    for key in ("evidence", "result", "error", "interaction", "usage", "compaction", "raw_input", "raw_output"):
        value = _field(event, key)
        if value is not None:
            out[key] = _lossless(value)
    return out


def _trim(text: Any, limit: int) -> str | None:
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= limit else text[:limit] + f"… [{len(text) - limit} more chars]"


def compact_event(event: Any) -> dict:
    """Bounded compatibility projection of a SessKit typed event.

    Text is capped at 1,200 chars and nested records are summarized. This is
    the historical Corral query shape: cheap to store and display, but NOT
    lossless. Consumers that deliver native content (ACP responses, load
    replay) must use :func:`project_event`, which keeps everything.
    """
    out: dict = {"type": _field(event, "type"), "native_seq": _field(event, "seq"), "ts": _field(event, "ts")}
    text = _field(event, "text")
    if text:
        out["text"] = _trim(text, _EVENT_TEXT_LIMIT)
    name = _field(event, "name")
    if name:
        out["tool"] = name
    call_id = _field(event, "call_id")
    if call_id:
        out["call_id"] = call_id
    result = _field(event, "result")
    if result is not None:
        out["result"] = getattr(result, "status", None)
    error = _field(event, "error")
    if error is not None:
        out["error"] = {
            "kind": getattr(error, "kind", None),
            "message": _trim(getattr(error, "message", None), _ERROR_TEXT_LIMIT),
        }
    interaction = _field(event, "interaction")
    if interaction is not None:
        out["interaction"] = {
            "purpose": interaction.purpose,
            "resolution": interaction.resolution,
            "request_id": interaction.request_id,
        }
    return out


def correlate(
    events: list,
    turns: list,
    run_id: str,
    baseline_seq: int,
    *,
    namespace: str = DEFAULT_NAMESPACE,
) -> dict:
    """Bind this run's own inputs (marker after the baseline) to native turns.

    Inputs from anyone else inside the bound range make attribution ``mixed``.
    Markers quoted in assistant or tool output never count as delivery.
    """
    fresh = [e for e in events if _field(e, "seq", 0) > baseline_seq]
    own = {}
    for event in fresh:
        if _field(event, "type") != "user_message":
            continue
        message_id = own_message(_field(event, "text"), run_id, namespace=namespace)
        if message_id:
            own[_field(event, "seq")] = message_id
    if not own:
        return {"recorded": False}
    first = min(own)
    last_own = max(own)
    turn = next((t for t in turns if _field(t, "start_seq", 0) <= last_own <= _field(t, "end_seq", -1)), None)
    end = _field(turn, "end_seq") if turn is not None else max(_field(e, "seq") for e in fresh)
    selected = [e for e in fresh if first <= _field(e, "seq", 0) <= end]
    foreign = [
        e
        for e in selected
        if _field(e, "type") == "user_message" and _field(e, "seq") not in own and (_field(e, "text") or "").strip()
    ]
    pending = [
        e for e in selected if _field(e, "interaction") is not None and _field(e, "interaction").resolution == "pending"
    ]
    outcome = _field(turn, "outcome") if turn is not None else None
    outcome_error = getattr(outcome, "error", None) if outcome is not None else None
    outcome_origin = getattr(getattr(outcome, "evidence", None), "origin", None)
    stop_reason = getattr(outcome, "stop_reason", None) if outcome is not None else None
    return {
        "recorded": True,
        "messages": sorted(set(own.values())),
        "first_seq": first,
        "end_seq": end,
        "turn_id": _field(turn, "turn_id") if turn is not None else None,
        "outcome": getattr(outcome, "status", None) if outcome is not None else "in_progress",
        "outcome_evidence": outcome_origin if outcome is not None else "unknown",
        "outcome_error": _trim(getattr(outcome_error, "message", None), _ERROR_TEXT_LIMIT)
        if outcome_error is not None
        else None,
        "stop_reason": stop_reason,
        "attribution": "mixed" if foreign else "correlated",
        "current_turn": not any(_field(e, "type") == "user_message" and _field(e, "seq", 0) > end for e in fresh),
        "pending_interaction": bool(pending),
        "events": selected,
    }


def _usage_identity(event: Any, usage: Any) -> tuple:
    """Native identity of one usage observation.

    Usage records carry no identity of their own; they are identified by the
    native event that carries them (``seq``/``message_id``/``turn_id``) plus
    the usage evidence origin. Token values are deliberately NOT part of the
    identity: two distinct native events with equal token counts are two
    observations, while the same event repeated by overlapping evidence ranges
    is one. Events without any native identity fall back to object identity
    so a repeated object never double-counts.
    """
    seq = _field(event, "seq", None)
    if seq is None:
        seq = _field(event, "native_seq", None)
    origin = getattr(getattr(usage, "evidence", None), "origin", None)
    identity: tuple = (_field(event, "message_id", None), _field(event, "turn_id", None), origin)
    if seq is None and identity == (None, None, None):
        return ("anonymous", id(event), id(usage))
    return (seq, *identity)


def build_result(observation: dict, *, text_limit: int | None = _RESULT_TEXT_LIMIT) -> dict:
    """Summarize the correlated range. Completion means the turn ended.

    ``text_limit`` caps the final response for explicitly bounded callers
    (Corral query compatibility); ``None`` returns the complete native final
    response for lossless delivery paths. Error samples follow the same
    rule: bounded callers keep the historical compact summaries, lossless
    callers receive the full shared projection. Usage aggregation, and the
    ``unverified`` acceptance marker are unchanged.
    """
    events = observation.get("events") or []
    replies = [e for e in events if _field(e, "type") == "assistant_message" and (_field(e, "text") or "").strip()]
    tools = [e for e in events if _field(e, "type") == "tool_call"]
    if text_limit is None:
        errors = [project_event(e, text_limit=None) for e in events if _field(e, "error") is not None]
    else:
        errors = [compact_event(e) for e in events if _field(e, "error") is not None]
    usage: dict = {}
    models = []
    seen_usage = set()
    duplicates_skipped = 0
    for event in events:
        event_usage = _field(event, "usage")
        if event_usage is None:
            continue
        identity = _usage_identity(event, event_usage)
        if identity in seen_usage:
            # The same native observation repeated by overlapping evidence
            # ranges; counting it again would fabricate a larger total.
            duplicates_skipped += 1
            continue
        seen_usage.add(identity)
        if getattr(event_usage, "model", None):
            models.append(event_usage.model)
        for field_name in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            value = getattr(event_usage, field_name, None)
            if isinstance(value, (int, float)):
                usage[field_name] = usage.get(field_name, 0) + value
    stamps = [s for s in (_field(e, "ts") for e in events) if s]
    final_text = _field(replies[-1], "text") if replies else None
    result = {
        "final_response": final_text if text_limit is None else _trim(final_text, text_limit) if replies else None,
        "assistant_messages": len(replies),
        "tool_calls": len(tools),
        "tools_used": sorted({n for n in (_field(e, "name") for e in tools) if n}),
        "errors": errors[-10:],
        "usage": usage or None,
        "observed_models": sorted(set(models)),
        "started_at": min(stamps) if stamps else None,
        "finished_at": max(stamps) if stamps else None,
        "turn_id": observation.get("turn_id"),
        "attribution": observation.get("attribution"),
        "outcome_evidence": observation.get("outcome_evidence"),
        "acceptance": "unverified",
    }
    if duplicates_skipped:
        result["usage_duplicates_skipped"] = duplicates_skipped
    return result


__all__ = [
    "DEFAULT_NAMESPACE",
    "MAX_PROMPT_CHARS",
    "build_result",
    "compact_event",
    "correlate",
    "marker",
    "own_message",
    "project_event",
    "validate_prompt",
    "wire_prompt",
]

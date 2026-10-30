"""SessKit observation for controlled runs.

SessKit owns native-history discovery, parsing, activity projections,
incremental reading and outcome evidence. This module only *adapts* its
public readers (``load_activity``, ``open_activity_reader``,
``derive_turns``, exact-ID session scans) and wraps the sole shared
correlation implementation (``agent_session_control.correlation``) with the
engine's ``ActivityObservation`` type. No native file is parsed here and no
correlation, marker-matching or usage/result aggregation algorithm is
duplicated here.

The wire namespace defaults to the foundation neutral namespace. A
compatibility host injects its legacy namespace explicitly (observer
``namespaces``); generic code never hardcodes a downstream product name.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any

from agent_session_control import correlation as _correlation
from agent_session_control.correlation import (
    build_result as _build_result,
)
from agent_session_control.correlation import (
    compact_event as _compact_event,
)
from agent_session_control.correlation import (
    correlate as _correlate,
)
from agent_session_control.correlation import (
    marker as _marker,
)
from agent_session_control.correlation import (
    own_message as _own_message,
)
from agent_session_control.correlation import (
    wire_prompt as _wire_prompt,
)

DEFAULT_NAMESPACE = _correlation.DEFAULT_NAMESPACE


def make_marker(run_id: str, message_id: str, *, namespace: str = DEFAULT_NAMESPACE) -> str:
    """Visible correlation marker appended to (or following) the brief."""
    return _marker(run_id, message_id, namespace=namespace)


def wire_prompt(run_id: str, message_id: str, prompt: str, *, namespace: str = DEFAULT_NAMESPACE) -> str:
    """Envelope the brief so SessKit evidence can prove which run it belongs to.

    The brief stays first and verbatim: some runtimes classify a leading
    bracketed marker as a local-only note and the first turn would lose its
    binding. Matching accepts either position (see ``own_message``).
    """
    return _wire_prompt(run_id, message_id, prompt, namespace=namespace)


def own_message(
    text: str | None,
    run_id: str,
    *,
    namespaces: tuple[str, ...] | None = None,
) -> str | None:
    """Return this run's message id when ``text`` carries its marker, else None.

    ``namespaces`` lists the accepted wire namespaces in priority order; the
    default is the neutral namespace only. A compatibility host passes its
    legacy namespace explicitly.
    """
    for namespace in namespaces or (DEFAULT_NAMESPACE,):
        found = _own_message(text, run_id, namespace=namespace)
        if found is not None:
            return found
    return None


def _with_seq(event: Any) -> Any:
    """Normalize dict-shaped events to the shared ``seq`` field convention."""
    if isinstance(event, dict) and "seq" not in event and isinstance(event.get("native_seq"), int):
        return {**event, "seq": event["native_seq"]}
    return event


def compact_event(event: Any) -> dict:
    """Small JSON-safe summary of one normalized activity event."""
    return _compact_event(_with_seq(event))


_text_limit_support: dict[str, bool] = {}


def _takes_text_limit(func_name: str) -> bool:
    """Whether the shared implementation already supports ``text_limit``."""
    if func_name not in _text_limit_support:
        fn = getattr(_correlation, func_name, None)
        try:
            _text_limit_support[func_name] = fn is not None and "text_limit" in inspect.signature(fn).parameters
        except (TypeError, ValueError):
            _text_limit_support[func_name] = False
    return _text_limit_support[func_name]


def project_event(event: Any, text_limit: int | None = None) -> dict:
    """Neutral event projection over the shared implementation.

    Uses ``correlation.project_event`` with the requested limit when the
    foundation provides it (``text_limit=None`` means complete replay).
    Until that function lands, the bounded ``compact_event`` applies and
    replay stays summary-capped; no second projection algorithm lives here
    (see the losslessness gap note in BACKENDS.md).
    """
    project = getattr(_correlation, "project_event", None)
    if project is None or not _takes_text_limit("project_event"):
        if project is not None:
            try:
                return project(_with_seq(event))
            except TypeError:
                pass
        return _compact_event(_with_seq(event))
    return project(_with_seq(event), text_limit=text_limit)


@dataclass
class ActivityObservation:
    """One correlation of this run's inputs to native turns."""

    recorded: bool = False
    message_ids: list[str] = field(default_factory=list)
    first_seq: int = -1
    end_seq: int = -1
    turn_id: str | None = None
    outcome: str = "in_progress"
    outcome_evidence: str = "unknown"  # native | inferred | unknown
    outcome_error: str | None = None
    stop_reason: str | None = None
    attribution: str = "correlated"  # correlated | mixed
    current_turn: bool = True
    pending_interaction: bool = False
    events: list[Any] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "recorded": self.recorded,
            "messages": list(self.message_ids),
            "first_seq": self.first_seq,
            "end_seq": self.end_seq,
            "turn_id": self.turn_id,
            "outcome": self.outcome,
            "outcome_evidence": self.outcome_evidence,
            "outcome_error": self.outcome_error,
            "stop_reason": self.stop_reason,
            "attribution": self.attribution,
            "current_turn": self.current_turn,
            "pending_interaction": self.pending_interaction,
            "events": list(self.events),
        }

    @classmethod
    def from_dict(cls, data: dict, *, stop_reason: str | None = None) -> ActivityObservation:
        return cls(
            recorded=bool(data.get("recorded", False)),
            message_ids=list(data.get("messages", [])),
            first_seq=int(data.get("first_seq", -1)),
            end_seq=int(data.get("end_seq", -1)),
            turn_id=data.get("turn_id"),
            outcome=str(data.get("outcome", "in_progress")),
            outcome_evidence=str(data.get("outcome_evidence", "unknown")),
            outcome_error=data.get("outcome_error"),
            stop_reason=data.get("stop_reason", stop_reason),
            attribution=str(data.get("attribution", "correlated")),
            current_turn=bool(data.get("current_turn", True)),
            pending_interaction=bool(data.get("pending_interaction", False)),
            events=list(data.get("events", [])),
        )


def _stop_reason_for(turns: list[Any], turn_id: str | None) -> str | None:
    """Native stop reason for one turn, without inventing any.

    The shared correlation dict carries no stop reason; the engine reads it
    from the matched turn's public outcome attributes. Missing evidence
    stays None and must never become a fabricated stop reason downstream.
    """
    if turn_id is None:
        return None
    for turn in turns:
        if getattr(turn, "turn_id", None) != turn_id:
            continue
        reason = getattr(getattr(turn, "outcome", None), "stop_reason", None)
        return reason if isinstance(reason, str) and reason.strip() else None
    return None


def correlate(
    events: list[Any],
    turns: list[Any],
    run_id: str,
    baseline_seq: int,
    *,
    namespaces: tuple[str, ...] | None = None,
) -> ActivityObservation:
    """Bind this run's own inputs (marker after the baseline) to native turns.

    Thin typed wrapper: each accepted namespace is tried through the shared
    implementation and the first recorded binding wins. A run's marker uses a
    single namespace, so exactly one can match; foreign-namespace markers for
    other runs still count as foreign input inside the winner's range.
    """
    for namespace in namespaces or (DEFAULT_NAMESPACE,):
        result = _correlate(events, turns, run_id, baseline_seq, namespace=namespace)
        if result.get("recorded"):
            return ActivityObservation.from_dict(result, stop_reason=_stop_reason_for(turns, result.get("turn_id")))
    return ActivityObservation(recorded=False)


def build_result(observation: ActivityObservation | dict, *, text_limit: int | None = None) -> dict:
    """Summarize the correlated range. Completion means the turn ended.

    ``text_limit=None`` requests the lossless final response; it takes
    effect once the shared implementation supports it (see
    :func:`project_event`).
    """
    if isinstance(observation, ActivityObservation):
        observation = observation.as_dict()
    else:
        observation = dict(observation)
        events = observation.get("events") or []
        observation["events"] = [_with_seq(e) for e in events]
    if _takes_text_limit("build_result"):
        return _correlation.build_result(observation, text_limit=text_limit)
    return _build_result(observation)


class HistoryError(RuntimeError):
    """Native history is missing or unreadable; outcome stays unknown."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _sesskit():
    try:
        import sesskit  # pyrefly: ignore[missing-import]
    except ImportError as exc:
        raise HistoryError(
            "history_unavailable", "SessKit is not installed; native observation needs sesskit>=0.2.21."
        ) from exc
    return sesskit


class HistoryObserver:
    """Thin adapter over SessKit's public activity API.

    ``session`` is the host's normalized session mapping (with ``path`` and
    ``source``/runtime fields). All SessKit access is lazy so importing this
    module never requires SessKit. ``namespaces`` lists the accepted wire
    namespaces; the neutral default is used unless a compatibility host
    injects its legacy namespace here.
    """

    def __init__(self, runtime: str = "", namespaces: tuple[str, ...] | None = None):
        self.runtime = runtime
        self.namespaces = namespaces or (DEFAULT_NAMESPACE,)

    def load_snapshot(self, session: dict) -> Any:
        sesskit = _sesskit()
        try:
            snapshot = sesskit.load_activity(dict(session))
        except Exception as exc:
            raise HistoryError("history_unavailable", f"the session's native history is unreadable: {exc}") from exc
        if getattr(snapshot, "state", None) not in ("available", "empty"):
            raise HistoryError(
                "history_unavailable",
                f"the session's native history is {getattr(snapshot, 'state', '?')}: {session.get('path')}",
            )
        return snapshot

    def baseline_seq(self, session: dict) -> int:
        try:
            snapshot = self.load_snapshot(session)
        except HistoryError:
            return -1
        return max((getattr(e, "seq", -1) for e in snapshot.events), default=-1)

    def open_reader(self, session: dict) -> Any:
        sesskit = _sesskit()
        try:
            return sesskit.open_activity_reader(dict(session))
        except Exception as exc:
            raise HistoryError("history_unavailable", f"the session's native history is unreadable: {exc}") from exc

    def observe(
        self, reader: Any, run_id: str, baseline_seq: int, events: dict[int, Any], runtime: str = ""
    ) -> tuple[Any, bool]:
        """Poll once; returns ``(observation, reset)``.

        ``reset`` means the native history was replaced (truncated/rotated):
        attribution built so far is void and the run must become unknown,
        never silently rebound.
        """
        sesskit = _sesskit()
        poll = reader.poll()
        if poll.reset and events:
            return ActivityObservation(recorded=bool(events)), True
        for event in poll.events:
            events[event.seq] = event
        ordered = [events[k] for k in sorted(events)]
        try:
            from sesskit.models import ActivitySnapshot  # pyrefly: ignore[missing-import]

            snapshot = ActivitySnapshot(
                state=poll.state,
                events=tuple(ordered),
                outcome=poll.outcome,
                cursor=poll.cursor,
                generation=poll.generation,
            )
            turns = list(sesskit.derive_turns(snapshot, runtime=runtime or self.runtime))
        except Exception as exc:
            raise HistoryError("history_unavailable", f"native turns could not be derived: {exc}") from exc
        return correlate(ordered, turns, run_id, baseline_seq, namespaces=self.namespaces), False

    def correlate_run(
        self, snapshot_or_events: Any, run_id: str, baseline_seq: int, runtime: str = ""
    ) -> ActivityObservation:
        """Correlate a loaded snapshot (or event list) with one run."""
        sesskit = _sesskit()
        if isinstance(snapshot_or_events, (list, tuple)):
            events = list(snapshot_or_events)
            try:
                from sesskit.models import ActivitySnapshot  # pyrefly: ignore[missing-import]

                snapshot = ActivitySnapshot(
                    state="available", events=tuple(events), outcome=None, cursor="", generation=""
                )
            except Exception as exc:
                raise HistoryError("history_unavailable", f"native turns could not be derived: {exc}") from exc
        else:
            snapshot = snapshot_or_events
            events = list(snapshot.events)
        try:
            turns = list(sesskit.derive_turns(snapshot, runtime=runtime or self.runtime))
        except Exception as exc:
            raise HistoryError("history_unavailable", f"native turns could not be derived: {exc}") from exc
        return correlate(events, turns, run_id, baseline_seq, namespaces=self.namespaces)

    def find_exact(self, runtime_id: str, native_id: str, *, limit: int = 50) -> dict | None:
        """Find one native session by exact id through public SessKit scans.

        Only an exact ``id`` equality binds; recency, cwd, title or mtime
        never do. Returns the session mapping (with ``path``) or None when no
        session carries exactly this id. Scan failures raise HistoryError so
        the caller stays unknown instead of guessing.
        """
        sesskit = _sesskit()
        try:
            parser = sesskit.registry.default_registry().get(runtime_id)
        except Exception as exc:
            raise HistoryError(
                "history_unavailable", f"no native history provider for runtime {runtime_id!r}: {exc}"
            ) from exc
        try:
            sessions = parser.scan_sessions(limit, keep_ids={native_id})
        except Exception as exc:
            raise HistoryError("history_unavailable", f"native history scan failed: {exc}") from exc
        for session in sessions:
            try:
                ident = session.get("id") if isinstance(session, dict) else getattr(session, "id", None)
            except Exception:
                continue
            if ident == native_id:
                return dict(session) if isinstance(session, dict) else dict(vars(session))
        return None

"""Per-turn evidence from the audit-grade messages view: provider calls, tool calls, time and history shedding.

Every figure here comes from what ``GET /api/sessions/{id}/messages`` returns
with ``include_llm_audit=true&include_tool_rows=true``: the public
``llm_call_audit`` envelopes on ``role="audit"`` rows (``latency_ms``,
``started_at``, ``finished_at``, ``status`` and the ``context_budget`` shed
record), the assistant rows' ``tool_calls`` envelopes with their server-stamped
``outcome``, and each row's ``created_at``. Nothing is estimated: a figure
the rows do not carry is reported as ``None``.

Time figures, all in milliseconds:

* ``span_ms`` -- from the operation's first stored row to its last stored row
  or last provider call finish, whichever is later. Rows are written by the
  server, so this is the server-recorded duration of the turn. Whether the
  first row is written at admission or when the worker starts is not visible
  through the API, so queue wait is not separated out.
* ``provider_busy_ms`` -- the union of the provider calls'
  ``[started_at, finished_at]`` intervals; overlapping calls count once.
* ``provider_latency_ms_sum`` -- the sum of the calls' ``latency_ms``.
* ``server_ms`` -- ``span_ms - provider_busy_ms``: time in the turn with no
  provider call in flight.

The client's own wall time for the turn (submission to the poll that saw it
finish) is measured by the battery, not here; its resolution is the poll
interval.

History shedding comes from each budgeted call's ``context_budget`` record:
``shed_history_indices`` (history messages left out), ``stubbed_tool_call_ids``
(tool results replaced by stubs) and ``shed_hatch_event_indices``. Calls with a
null ``context_budget`` (advisor, auto-title) carry no shed state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from scripts.staging.opslib import JsonObject, OperationFailed

LLM_CALL_AUDIT = "llm_call_audit"
LONG_PROVIDER_CALL_MS = 300_000

UNAVAILABLE_FROM_API = (
    "queue wait before the worker started the turn (the operation status carries no start or admission timestamp)",
    "where server time went inside a turn (tool execution, validation, persistence): no per-phase timing is exposed",
    "time spent in the advisor versus the planner when their calls overlap: calls are timed, phases are not",
)


def _instant(value: object, what: str) -> datetime:
    if type(value) is not str:
        raise OperationFailed(f"{what} is not a timestamp string")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise OperationFailed(f"{what} is not an ISO timestamp") from None


def _ms(start: datetime, end: datetime) -> int:
    return round((end - start).total_seconds() * 1000)


def busy_ms(intervals: Sequence[tuple[datetime, datetime]]) -> int:
    """Total length of the union of time intervals."""
    total = 0
    current: tuple[datetime, datetime] | None = None
    for start, end in sorted(intervals):
        if current is None:
            current = (start, end)
        elif start <= current[1]:
            current = (current[0], max(current[1], end))
        else:
            total += _ms(*current)
            current = (start, end)
    if current is not None:
        total += _ms(*current)
    return total


def operation_rows(rows: Sequence[Mapping[str, Any]], operation_id: str) -> list[Mapping[str, Any]]:
    return [row for row in rows if row["operation_id"] == operation_id]


def shed_state(rows: Sequence[Mapping[str, Any]]) -> list[int] | None:
    """Shed history indices of the newest budgeted call in ``rows``; ``None`` when there is none."""
    for row in reversed(rows):
        if row["role"] != "audit" or row["tool_calls"] is None:
            continue
        for entry in reversed(row["tool_calls"]):
            if "_kind" in entry and entry["_kind"] == LLM_CALL_AUDIT and "context_budget" in entry["call"]:
                budget = entry["call"]["context_budget"]
                if budget is not None:
                    return sorted(budget["shed_history_indices"])
    return None


def turn_evidence(rows: Sequence[Mapping[str, Any]], operation_id: str, *, shed_before: Sequence[int] | None) -> JsonObject:
    """Provider, tool, time and shedding figures for one operation's rows."""
    own = operation_rows(rows, operation_id)
    calls: list[Mapping[str, Any]] = []
    tool_counts: dict[str, int] = {}
    tool_outcomes: dict[str, dict[str, int]] = {}
    for row in own:
        if row["tool_calls"] is None:
            continue
        for entry in row["tool_calls"]:
            if row["role"] == "audit" and "_kind" in entry and entry["_kind"] == LLM_CALL_AUDIT:
                calls.append(entry["call"])
            elif row["role"] == "assistant" and "function" in entry:
                name = entry["function"]["name"]
                outcome = entry["outcome"] if "outcome" in entry and entry["outcome"] is not None else "unstamped"
                tool_counts[name] = tool_counts[name] + 1 if name in tool_counts else 1
                if name not in tool_outcomes:
                    tool_outcomes[name] = {}
                per_tool = tool_outcomes[name]
                per_tool[outcome] = per_tool[outcome] + 1 if outcome in per_tool else 1
    latencies = [call["latency_ms"] for call in calls if type(call["latency_ms"]) is int]
    intervals = [
        (_instant(call["started_at"], "started_at"), _instant(call["finished_at"], "finished_at"))
        for call in calls
        if "started_at" in call and "finished_at" in call and call["started_at"] is not None and call["finished_at"] is not None
    ]
    stamps = [_instant(row["created_at"], "created_at") for row in own if "created_at" in row and row["created_at"] is not None]
    span: int | None = None
    if stamps:
        end = max([*stamps, *(finish for _, finish in intervals)])
        span = _ms(min(stamps), end)
    busy = busy_ms(intervals) if len(intervals) == len(calls) else None
    budgets = [call["context_budget"] for call in calls if "context_budget" in call and call["context_budget"] is not None]
    shed_after = shed_state(own)
    newly_shed = sorted(set(shed_after) - set(shed_before or [])) if shed_after is not None else []
    return {
        "operation_rows": len(own),
        "provider_calls": len(calls),
        "successful_provider_calls": sum(1 for call in calls if call["status"] == "success"),
        "failed_provider_statuses": sorted(str(call["status"]) for call in calls if call["status"] != "success"),
        "budgeted_provider_calls": len(budgets),
        "tool_calls": sum(tool_counts.values()),
        "tool_calls_by_name": dict(sorted(tool_counts.items())),
        "tool_outcomes": {name: dict(sorted(counts.items())) for name, counts in sorted(tool_outcomes.items())},
        "span_ms": span,
        "provider_busy_ms": busy,
        "provider_latency_ms_sum": sum(latencies) if len(latencies) == len(calls) else None,
        "provider_latency_ms_max": max(latencies) if latencies else None,
        "provider_call_over_300_seconds": any(value > LONG_PROVIDER_CALL_MS for value in latencies),
        "server_ms": span - busy if span is not None and busy is not None else None,
        "history": {
            "calls_with_shedding": sum(
                1
                for budget in budgets
                if budget["shed_history_indices"] or budget["stubbed_tool_call_ids"] or budget["shed_hatch_event_indices"]
            ),
            "shed_history_after": shed_after,
            "newly_shed_history": newly_shed,
            "max_shed_history_messages": max((len(budget["shed_history_indices"]) for budget in budgets), default=0),
            "max_stubbed_tool_results": max((len(budget["stubbed_tool_call_ids"]) for budget in budgets), default=0),
            "max_shed_hatch_events": max((len(budget["shed_hatch_event_indices"]) for budget in budgets), default=0),
            "sent_over_estimate": any("over_estimate" in budget and budget["over_estimate"] is True for budget in budgets),
            "max_request_wire_bytes": max((budget["request_wire_bytes"] for budget in budgets), default=None),
            "max_estimated_tokens_before": max((budget["estimated_tokens_before"] for budget in budgets), default=None),
        },
    }


def shed_this_turn(evidence: Mapping[str, Any]) -> bool:
    history: Mapping[str, Any] = evidence["history"]
    return bool(history["calls_with_shedding"]) or bool(history["newly_shed_history"])

"""Per-turn provider, tool, time and history-shedding figures read from the audit-grade messages view."""

from __future__ import annotations

from typing import Any

from scripts.staging.turn_evidence import shed_state, shed_this_turn, turn_evidence

OP = "op-1"


def call(started: str, finished: str, latency: int, *, status: str = "success", shed: list[int] | None = None) -> dict[str, Any]:
    budget = None
    if shed is not None:
        budget = {
            "shed_history_indices": shed,
            "stubbed_tool_call_ids": [],
            "shed_hatch_event_indices": [],
            "request_wire_bytes": 1000,
            "estimated_tokens_before": 400,
            "over_estimate": False,
        }
    return {
        "_kind": "llm_call_audit",
        "call": {"status": status, "latency_ms": latency, "started_at": started, "finished_at": finished, "context_budget": budget},
    }


def tool(name: str, outcome: str | None) -> dict[str, Any]:
    entry: dict[str, Any] = {"id": name + "-1", "type": "function", "function": {"name": name, "arguments": "{}"}}
    if outcome is not None:
        entry["outcome"] = outcome
    return entry


def rows(*audit: dict[str, Any], tools: list[dict[str, Any]] | None = None, operation: str = OP) -> list[dict[str, Any]]:
    result = [{"role": "user", "operation_id": operation, "tool_calls": None, "created_at": "2026-01-01T00:00:00+00:00"}]
    if tools:
        result.append({"role": "assistant", "operation_id": operation, "tool_calls": tools, "created_at": "2026-01-01T00:00:01+00:00"})
    result.append({"role": "audit", "operation_id": operation, "tool_calls": list(audit), "created_at": "2026-01-01T00:00:10+00:00"})
    return result


def test_provider_time_is_the_union_of_call_intervals_and_server_time_the_rest_of_the_span() -> None:
    evidence = turn_evidence(
        rows(
            call("2026-01-01T00:00:01+00:00", "2026-01-01T00:00:04+00:00", 3000),
            call("2026-01-01T00:00:03+00:00", "2026-01-01T00:00:05+00:00", 2000),
        ),
        OP,
        shed_before=None,
    )
    assert evidence["span_ms"] == 10_000
    assert evidence["provider_busy_ms"] == 4000
    assert evidence["provider_latency_ms_sum"] == 5000
    assert evidence["server_ms"] == 6000
    assert evidence["provider_calls"] == 2


def test_a_call_finishing_after_the_last_row_extends_the_span() -> None:
    evidence = turn_evidence(rows(call("2026-01-01T00:00:08+00:00", "2026-01-01T00:00:12+00:00", 4000)), OP, shed_before=None)
    assert evidence["span_ms"] == 12_000
    assert evidence["server_ms"] == 8000


def test_a_span_shorter_than_provider_time_gives_no_server_time_rather_than_a_negative_one() -> None:
    # The call began 30 s before the turn's first stored row, so the stored span cannot contain it.
    evidence = turn_evidence(rows(call("2025-12-31T23:59:30+00:00", "2026-01-01T00:00:02+00:00", 32_000)), OP, shed_before=None)
    assert evidence["span_ms"] == 10_000
    assert evidence["provider_busy_ms"] == 32_000
    assert evidence["server_ms"] is None


def test_untimed_calls_leave_the_figures_unavailable_rather_than_estimated() -> None:
    untimed = {"_kind": "llm_call_audit", "call": {"status": "timeout", "latency_ms": None}}
    evidence = turn_evidence(rows(untimed), OP, shed_before=None)
    assert evidence["provider_busy_ms"] is None
    assert evidence["provider_latency_ms_sum"] is None
    assert evidence["server_ms"] is None
    assert evidence["successful_provider_calls"] == 0
    assert evidence["failed_provider_statuses"] == ["timeout"]


def test_tools_are_counted_by_name_and_server_stamped_outcome() -> None:
    evidence = turn_evidence(
        rows(
            tools=[
                tool("inspect_prompt_targets", "completed"),
                tool("change_substring", "rejected"),
                tool("change_substring", "applied"),
                tool("upsert_node", None),
            ]
        ),
        OP,
        shed_before=None,
    )
    assert evidence["tool_calls"] == 4
    assert evidence["tool_calls_by_name"] == {"change_substring": 2, "inspect_prompt_targets": 1, "upsert_node": 1}
    assert evidence["tool_outcomes"]["change_substring"] == {"applied": 1, "rejected": 1}
    assert evidence["tool_outcomes"]["upsert_node"] == {"unstamped": 1}


def test_only_the_operations_own_rows_count() -> None:
    other = rows(
        call("2026-01-01T00:00:01+00:00", "2026-01-01T00:00:02+00:00", 1000), tools=[tool("set_pipeline", "applied")], operation="op-0"
    )
    evidence = turn_evidence([*other, *rows(call("2026-01-01T00:00:01+00:00", "2026-01-01T00:00:02+00:00", 1000))], OP, shed_before=None)
    assert evidence["provider_calls"] == 1
    assert evidence["tool_calls_by_name"] == {}


def test_history_shed_in_the_turn_is_reported_with_what_was_newly_shed() -> None:
    evidence = turn_evidence(
        rows(call("2026-01-01T00:00:01+00:00", "2026-01-01T00:00:02+00:00", 1000, shed=[0, 1, 2])), OP, shed_before=[0, 1]
    )
    assert shed_this_turn(evidence)
    assert evidence["history"]["newly_shed_history"] == [2]
    assert evidence["history"]["shed_history_after"] == [0, 1, 2]
    assert evidence["history"]["max_shed_history_messages"] == 3
    unshed = turn_evidence(rows(call("2026-01-01T00:00:01+00:00", "2026-01-01T00:00:02+00:00", 1000, shed=[])), OP, shed_before=None)
    assert not shed_this_turn(unshed)
    assert unshed["budgeted_provider_calls"] == 1


def test_shed_state_reads_the_newest_budgeted_call_and_skips_unbudgeted_ones() -> None:
    history = [
        *rows(call("2026-01-01T00:00:01+00:00", "2026-01-01T00:00:02+00:00", 1000, shed=[3]), operation="op-0"),
        *rows(call("2026-01-01T00:00:01+00:00", "2026-01-01T00:00:02+00:00", 1000), operation="op-1"),
    ]
    assert shed_state(history) == [3]
    assert shed_state(rows()) is None

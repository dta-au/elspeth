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


# ── sanitised per-call telemetry, end reviews and tool time ───────────────


def full_call() -> dict[str, Any]:
    """A provider call carrying every public field, text and identifiers included."""
    return {
        "_kind": "llm_call_audit",
        "call": {
            "call_id": "call-secret-id",
            "provider_request_id": "req_123",
            "messages_hash": "a" * 64,
            "error_message": "the prompt said: classify the patient",
            "status": "success",
            "finish_reason": "tool_calls",
            "prompt_tokens": 238_385,
            "completion_tokens": 5165,
            "total_tokens": 243_550,
            "cached_prompt_tokens": None,
            "latency_ms": 47_340,
            "provider_cost": 0.58,
            "model_requested": "openai/sonnet",
            "model_returned": "https://models.invalid/sonnet",
            "pricing_model": "/srv/models/pricing.json",
            "error_class": "a reply with spaces is not a token",
            "started_at": "2026-01-01T00:00:01+00:00",
            "finished_at": "2026-01-01T00:00:04+00:00",
            "request_byte_split": {"message_bytes": [146_288, 12], "tools_bytes": 79_078, "total_bytes": 808_785, "raw": "x"},
            "context_budget": {
                "authored_bytes": 296_976,
                "estimated_tokens_before": 637_608,
                "authored_refuted": False,
                "anchor_status": "no_previous_call",
                "bytes_per_token": "3.0",
                "shed_history_indices": [0, 2],
                "stubbed_tool_call_ids": ["call_a", "call_b", "call_c"],
                "shed_hatch_event_indices": [],
                "request_wire_bytes": 815_782,
            },
        },
    }


def test_each_call_keeps_usage_bytes_and_budget_figures_and_no_text_or_identifiers() -> None:
    evidence = turn_evidence(rows(full_call()), OP, shed_before=None)
    (record,) = evidence["calls"]
    assert record["prompt_tokens"] == 238_385
    assert record["finish_reason"] == "tool_calls"
    assert record["model_requested"] == "openai/sonnet"
    assert record["cached_prompt_tokens"] is None
    assert record["reasoning_tokens"] is None  # absent fields are listed as null
    # A URL, a path and free text are dropped; identifiers and hashes are never copied.
    for kept_out in (
        "model_returned",
        "pricing_model",
        "error_class",
        "call_id",
        "provider_request_id",
        "messages_hash",
        "error_message",
        "started_at",
        "finished_at",
    ):
        assert kept_out not in record
    assert record["request_byte_split"] == {"message_bytes": [146_288, 12], "tools_bytes": 79_078, "total_bytes": 808_785}
    budget = record["context_budget"]
    assert budget["authored_bytes"] == 296_976
    assert budget["authored_refuted"] is False
    assert budget["shed_history_indices"] == [0, 2]
    assert budget["stubbed_tool_call_count"] == 3
    assert "stubbed_tool_call_ids" not in budget
    assert "anchor_status" not in budget
    assert "bytes_per_token" not in budget
    assert "call_a" not in repr(evidence)


def test_end_reviews_are_the_end_phase_checkpoint_records_reduced_to_their_verdicts() -> None:
    def review(phase: str, verdict: str, publication: str | None) -> dict[str, Any]:
        return {
            "_kind": "advisor_checkpoint_pass_audit",
            "pass": {
                "phase": phase,
                "verdict": verdict,
                "publication_verdict": publication,
                "source": "model",
                "findings_hash": "f" * 64,
                "publication_binding": "binding-secret",
            },
        }

    evidence = turn_evidence(
        rows(review("start", "clean", None), review("end", "flagged", "ADMIT"), review("end", "clean", "ADMIT")), OP, shed_before=None
    )
    assert evidence["end_reviews"] == [
        {"phase": "end", "verdict": "flagged", "publication_verdict": "ADMIT", "source": "model"},
        {"phase": "end", "verdict": "clean", "publication_verdict": "ADMIT", "source": "model"},
    ]
    assert "binding-secret" not in repr(evidence)


def tool_row(name: str, started: str | None, finished: str | None, *, latency: int | None = None) -> dict[str, Any]:
    invocation: dict[str, Any] = {
        "tool_name": name,
        "status": "success",
        "started_at": started,
        "finished_at": finished,
        "arguments_canonical": '{"secret": "argument text"}',
        "result_canonical": '{"secret": "result text"}',
    }
    if latency is not None:
        invocation["latency_ms"] = latency
    return {
        "role": "tool",
        "operation_id": OP,
        "tool_calls": [{"_kind": "audit", "invocation": invocation}],
        "created_at": "2026-01-01T00:00:06+00:00",
    }


def test_timed_tool_rows_give_tool_time_and_the_server_time_outside_provider_and_tool_calls() -> None:
    turn = rows(call("2026-01-01T00:00:01+00:00", "2026-01-01T00:00:04+00:00", 3000), tools=[tool("upsert_node", "applied")])
    turn.insert(2, tool_row("upsert_node", "2026-01-01T00:00:05+00:00", "2026-01-01T00:00:07+00:00"))
    evidence = turn_evidence(turn, OP, shed_before=None)
    assert evidence["tool_timings"] == [{"tool": "upsert_node", "status": "success", "latency_ms": 2000}]
    assert evidence["tool_timings_complete"] is True
    assert evidence["tool_busy_ms"] == 2000
    # span 10 s - provider 3 s - tool 2 s
    assert evidence["server_ms_outside_provider_and_tools"] == 5000
    assert evidence["server_ms"] == 7000
    assert "argument text" not in repr(evidence)
    assert "result text" not in repr(evidence)


def test_a_tool_call_without_a_timed_record_leaves_tool_time_unknown_rather_than_zero() -> None:
    # Two calls, one timed record: the primary tool row of the other carries no timing.
    turn = rows(
        call("2026-01-01T00:00:01+00:00", "2026-01-01T00:00:04+00:00", 3000),
        tools=[tool("upsert_node", "applied"), tool("remove_node", "applied")],
    )
    turn.insert(2, tool_row("upsert_node", "2026-01-01T00:00:05+00:00", "2026-01-01T00:00:07+00:00", latency=1999))
    evidence = turn_evidence(turn, OP, shed_before=None)
    assert evidence["tool_timings"] == [{"tool": "upsert_node", "status": "success", "latency_ms": 1999}]
    assert evidence["tool_timings_complete"] is False
    assert evidence["tool_busy_ms"] is None
    assert evidence["server_ms_outside_provider_and_tools"] is None
    assert evidence["server_ms"] == 7000

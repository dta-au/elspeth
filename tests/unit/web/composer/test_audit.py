"""Unit tests for the web composer's BufferingRecorder + audit envelope helper.

Pin the invariants:
- record() / invocations snapshot semantics (mutation after snapshot doesn't affect snapshot)
- audit_envelope produces the correct {_kind: "audit", invocation: {...}} shape
- thread-safe append under concurrent record() calls
- resolve_session is a no-op (Protocol conformance)
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus
from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.composer_planner_audit import (
    ComposerPlannerAttempt,
    ComposerPlannerAttemptLedTo,
    ComposerPlannerAttemptOutcome,
    ComposerPlannerAttemptPhase,
    ComposerPlannerInformationClass,
)
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.audit import (
    BufferingRecorder,
    audit_envelope,
    begin_dispatch,
    dispatch_with_audit,
    interleave_planner_audit_records,
    llm_call_audit_envelope,
    planner_attempt_audit_envelope,
    planner_attempt_audit_summary,
)


def _make_invocation(seq: int) -> ComposerToolInvocation:
    payload = {"seq": seq}
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    h = hashlib.sha256(canon.encode("utf-8")).hexdigest()
    t = datetime(2026, 5, 4, 12, 0, seq % 60, tzinfo=UTC)
    return ComposerToolInvocation(
        tool_call_id=f"tc-{seq}",
        tool_name="upsert_node",
        arguments_canonical=canon,
        arguments_hash=h,
        result_canonical=canon,
        result_hash=h,
        status=ComposerToolStatus.SUCCESS,
        error_class=None,
        error_message=None,
        version_before=seq,
        version_after=seq + 1,
        started_at=t,
        finished_at=t,
        latency_ms=1,
        actor="test",
    )


def _make_planner_attempt(ordinal: int, planner_call_ordinal: int) -> ComposerPlannerAttempt:
    return ComposerPlannerAttempt(
        ordinal=ordinal,
        planner_call_ordinal=planner_call_ordinal,
        phase=ComposerPlannerAttemptPhase.DISCOVERY,
        outcome=ComposerPlannerAttemptOutcome.DISCOVERY_EXECUTED,
        planner_code=None,
        selected_tools=("get_plugin_schema",),
        requested_information=(ComposerPlannerInformationClass.PLUGIN_SCHEMA,),
        new_information=(ComposerPlannerInformationClass.PLUGIN_SCHEMA,),
        rejection_codes=(),
        candidate_shape_hash=None,
        repeated_fingerprint=False,
        led_to=ComposerPlannerAttemptLedTo.CONTINUE,
    )


def _make_planner_llm_call(
    planner_call_ordinal: int,
    *,
    status: ComposerLLMCallStatus = ComposerLLMCallStatus.SUCCESS,
) -> ComposerLLMCall:
    t = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
    failed = status is not ComposerLLMCallStatus.SUCCESS
    return ComposerLLMCall(
        model_requested="test/planner",
        model_returned=None if failed else "test/planner-v1",
        status=status,
        prompt_tokens=None if failed else 13,
        completion_tokens=None if failed else 8,
        total_tokens=None if failed else 21,
        latency_ms=42,
        provider_request_id=None,
        messages_hash="m" * 64,
        tools_spec_hash="t" * 64,
        declared_tool_names=("emit_pipeline_proposal",),
        started_at=t,
        finished_at=t,
        error_class="APIError" if failed else None,
        error_message="APIError" if failed else None,
        temperature=0.0,
        seed=42,
        max_completion_tokens_requested=800,
        planner_policy_hash="a" * 64,
        planner_call_ordinal=planner_call_ordinal,
    )


def test_record_appends_in_order() -> None:
    rec = BufferingRecorder()
    for i in range(5):
        rec.record(_make_invocation(i))
    invs = rec.invocations
    assert len(invs) == 5
    assert [inv.tool_call_id for inv in invs] == [f"tc-{i}" for i in range(5)]


def test_invocations_returns_immutable_snapshot() -> None:
    """A snapshot taken via .invocations should not reflect later mutations."""
    rec = BufferingRecorder()
    rec.record(_make_invocation(0))
    snapshot = rec.invocations
    rec.record(_make_invocation(1))
    rec.record(_make_invocation(2))
    # Original snapshot unchanged.
    assert len(snapshot) == 1
    assert snapshot[0].tool_call_id == "tc-0"
    # New snapshot reflects later state.
    assert len(rec.invocations) == 3


def test_concurrent_records_all_landed() -> None:
    """Thread-safe append: 100 concurrent records land all 100 entries."""
    rec = BufferingRecorder()
    threads = [threading.Thread(target=rec.record, args=(_make_invocation(i),)) for i in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(rec.invocations) == 100


def test_planner_attempts_are_buffered_as_immutable_ordered_snapshots() -> None:
    recorder = BufferingRecorder()
    recorder.record_planner_attempt(_make_planner_attempt(1, 2))
    snapshot = recorder.planner_attempts
    recorder.record_planner_attempt(_make_planner_attempt(2, 4))

    assert [attempt.planner_call_ordinal for attempt in snapshot] == [2]
    assert [attempt.planner_call_ordinal for attempt in recorder.planner_attempts] == [2, 4]


def test_planner_attempt_envelope_and_summary_are_value_free() -> None:
    attempt = _make_planner_attempt(1, 2)

    envelope = planner_attempt_audit_envelope(attempt)
    summary = json.loads(planner_attempt_audit_summary(attempt))

    assert envelope == {"_kind": "planner_attempt_audit", "attempt": attempt.to_dict()}
    assert summary == {
        "_kind": "planner_attempt_audit",
        "ordinal": 1,
        "planner_call_ordinal": 2,
        "phase": "discovery",
        "outcome": "discovery_executed",
        "planner_code": None,
        "led_to": "continue",
    }
    serialized = json.dumps(envelope)
    assert "argument" not in serialized
    assert "content" not in serialized


def test_planner_rows_interleave_logical_attempt_after_matching_physical_call() -> None:
    failed_call = _make_planner_llm_call(1, status=ComposerLLMCallStatus.API_ERROR)
    response_call = _make_planner_llm_call(2)
    attempt = _make_planner_attempt(1, 2)

    records = interleave_planner_audit_records((failed_call, response_call), (attempt,))

    assert records == (failed_call, response_call, attempt)


def test_planner_rows_reject_a_semantic_response_without_exactly_one_attempt() -> None:
    response_call = _make_planner_llm_call(1)

    with pytest.raises(AuditIntegrityError, match="semantic response has no planner attempt"):
        interleave_planner_audit_records((response_call,), ())


def test_resolve_session_is_no_op() -> None:
    """The web recorder's session_id is known up front; resolve is no-op."""
    rec = BufferingRecorder()
    # Should not raise; returns None.
    result = rec.resolve_session("abc123def456")
    assert result is None
    # No record was created by resolve.
    assert rec.invocations == ()


def test_audit_envelope_kind_discriminator() -> None:
    """Every audit envelope must carry the _kind: 'audit' discriminator."""
    inv = _make_invocation(1)
    env = audit_envelope(inv)
    assert env["_kind"] == "audit"
    assert "invocation" in env
    inv_payload = env["invocation"]
    assert isinstance(inv_payload, dict)
    assert inv_payload["tool_call_id"] == "tc-1"
    assert inv_payload["status"] == "success"
    assert "authority_arguments_canonical" not in inv_payload
    assert "authority_arguments_hash" not in inv_payload


def test_audit_envelope_invocation_is_json_serializable() -> None:
    """The envelope must round-trip through json.dumps without raising."""
    inv = _make_invocation(1)
    env = audit_envelope(inv)
    serialized = json.dumps(env)
    round_tripped = json.loads(serialized)
    assert round_tripped["_kind"] == "audit"
    assert round_tripped["invocation"]["tool_call_id"] == "tc-1"


def test_llm_call_audit_envelope_omits_provider_reasoning_artifacts() -> None:
    t = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
    call = ComposerLLMCall(
        model_requested="openrouter/openai/gpt-5.5",
        model_returned="openai/gpt-5.5-2026-05-01",
        status=ComposerLLMCallStatus.SUCCESS,
        prompt_tokens=13,
        completion_tokens=8,
        total_tokens=21,
        latency_ms=42,
        provider_request_id="chatcmpl-reasoning",
        messages_hash="m" * 64,
        tools_spec_hash="t" * 64,
        declared_tool_names=("set_source", "splice_transform"),
        started_at=t,
        finished_at=t,
        error_class=None,
        error_message=None,
        temperature=0.0,
        seed=42,
        reasoning_tokens=5,
        reasoning_content="hidden prompt and row details",
        reasoning_details=[{"type": "reasoning.text", "text": "hidden chain detail"}],
        thinking_blocks=[{"type": "thinking", "thinking": "hidden provider thought"}],
        provider_cost=0.0037,
        provider_cost_source="response_usage.cost",
        max_completion_tokens_requested=800,
        planner_policy_hash="a" * 64,
        planner_call_ordinal=2,
    )

    env = llm_call_audit_envelope(call)

    assert env["_kind"] == "llm_call_audit"
    payload = env["call"]
    assert isinstance(payload, dict)
    assert payload["provider_request_id"] == "chatcmpl-reasoning"
    assert payload["declared_tool_names"] == ["set_source", "splice_transform"]
    assert payload["reasoning_tokens"] == 5
    assert payload["provider_cost"] == 0.0037
    assert payload["max_completion_tokens_requested"] == 800
    assert payload["planner_policy_hash"] == "a" * 64
    assert payload["planner_call_ordinal"] == 2
    assert "reasoning_content" not in payload
    assert "reasoning_details" not in payload
    assert "thinking_blocks" not in payload
    assert "hidden" not in json.dumps(env)


@pytest.mark.asyncio
async def test_dispatch_with_audit_records_plugin_crash_when_success_payload_extraction_fails() -> None:
    """A bad successful result shape must not bypass the audit recorder."""

    @dataclass(frozen=True, slots=True)
    class _UpdatedState:
        version: int

    class _BadResult:
        updated_state = _UpdatedState(version=2)

        def to_dict(self) -> list[str]:
            return ["not", "a", "mapping"]

    async def _dispatch() -> _BadResult:
        return _BadResult()

    recorder = BufferingRecorder()
    audit = begin_dispatch(
        "tc-bad-result",
        "set_metadata",
        {"patch": {"name": "bad-result"}},
        version_before=1,
        actor="assistant",
    )

    with pytest.raises(TypeError, match=r"result.to_dict\(\) returned list"):
        await dispatch_with_audit(
            recorder=recorder,
            audit=audit,
            do_dispatch=_dispatch,
            version_after_provider=lambda result: result.updated_state.version,
            arg_error_payload_factory=lambda _exc: {"error": "unused"},
        )

    invocations = recorder.invocations
    assert len(invocations) == 1
    inv = invocations[0]
    assert inv.status == ComposerToolStatus.PLUGIN_CRASH
    assert inv.tool_call_id == "tc-bad-result"
    assert inv.error_class == "TypeError"
    assert inv.error_message == "TypeError"
    assert inv.version_before == 1
    assert inv.version_after is None

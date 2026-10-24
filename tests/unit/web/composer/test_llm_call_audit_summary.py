"""The surviving LLM audit writer uses the shared operator summary.

Literal legacy JSON pins preserve field order and bytes independently of the
writer test. Routine reasons are omitted; abnormal and unknown reasons remain
visible. The actual surviving writer is exercised with each reason family.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from elspeth.contracts.composer_llm_audit import (
    PROVIDER_COST_SOURCE_RESPONSE_USAGE_COST,
    ComposerLLMCall,
    ComposerLLMCallStatus,
)
from elspeth.web.composer.audit import llm_call_audit_summary
from elspeth.web.coordination.contracts import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.sessions._persist_payload import AuditMessageDraft
from elspeth.web.sessions.protocol import SessionServiceProtocol
from elspeth.web.sessions.routes._helpers import _persist_llm_calls


def _compose_context(session_id: UUID) -> SessionOperationContext:
    """The COMPOSE operation the audited turn ran under (P4-D6 family A2b).

    The LLM-sidecar and planner-audit writers take the caller's operation as a
    required argument; these tests measure the audit summary text, and carry
    the fence exactly as the live compose turn does.
    """
    return SessionOperationContext(
        fence=SessionOperationFence(
            session_id=str(session_id),
            operation_id=f"llm-call-audit-{session_id}",
            lease_token=f"llm-call-audit-token-{session_id}",
            operation_epoch=1,
        ),
        operation_kind=SessionOperationKind.COMPOSE,
    )


# Terminal states that mean "the turn ended normally". ``tool_calls`` is the
# ordinary ending of every healthy iteration of a tool-using loop, which is
# why making a non-``stop`` reason fatal was rejected.
ROUTINE = ("stop", "tool_calls")

# Reasons an operator needs on the summary line. ``length`` renders as a
# half-finished answer (the fix is ``max_tokens``, not a different model);
# ``content_filter`` is a provider refusal that otherwise reads as our bug.
ABNORMAL = ("length", "content_filter")


def _call(
    *,
    finish_reason: str | None = None,
    status: ComposerLLMCallStatus = ComposerLLMCallStatus.SUCCESS,
    planner_call_ordinal: int | None = None,
) -> ComposerLLMCall:
    now = datetime(2026, 7, 31, tzinfo=UTC)
    failed = status is not ComposerLLMCallStatus.SUCCESS
    return ComposerLLMCall(
        model_requested="openrouter/anthropic/claude-sonnet-4.6",
        model_returned="anthropic/claude-sonnet-4.6",
        status=status,
        prompt_tokens=13,
        completion_tokens=8,
        total_tokens=21,
        reasoning_tokens=4,
        latency_ms=42,
        provider_request_id="chatcmpl-summary",
        messages_hash="m" * 64,
        tools_spec_hash="t" * 64,
        declared_tool_names=("set_pipeline",),
        started_at=now,
        finished_at=now,
        error_class="TimeoutError" if failed else None,
        error_message="upstream timeout" if failed else None,
        temperature=0.0,
        seed=42,
        provider_cost=0.0037,
        provider_cost_source=PROVIDER_COST_SOURCE_RESPONSE_USAGE_COST,
        finish_reason=finish_reason,
        max_completion_tokens_requested=8192 if planner_call_ordinal is not None else None,
        planner_policy_hash="b" * 64 if planner_call_ordinal is not None else None,
        planner_call_ordinal=planner_call_ordinal,
    )


# The exact string the three hand-built dicts produced before consolidation,
# written out literally. Deriving it from the helper (or from a dict built by
# the same code the helper uses) would make the no-regression test tautological.
_LEGACY_SUMMARY_JSON = (
    '{"_kind": "llm_call_audit", '
    '"status": "success", '
    '"model_requested": "openrouter/anthropic/claude-sonnet-4.6", '
    '"model_returned": "anthropic/claude-sonnet-4.6", '
    '"total_tokens": 21, '
    '"reasoning_tokens": 4, '
    '"provider_cost": 0.0037}'
)


class TestNoRegressionAgainstTheHandBuiltDicts:
    """The consolidated helper must not have changed what gets written."""

    def test_call_without_a_finish_reason_serialises_exactly_as_before(self) -> None:
        assert llm_call_audit_summary(_call()) == _LEGACY_SUMMARY_JSON

    def test_routine_stop_serialises_exactly_as_before(self) -> None:
        """``stop`` is the overwhelmingly common case — it must be untouched."""
        assert llm_call_audit_summary(_call(finish_reason="stop")) == _LEGACY_SUMMARY_JSON

    def test_key_order_is_preserved_not_merely_the_key_set(self) -> None:
        summary = json.loads(llm_call_audit_summary(_call()))

        assert list(summary) == [
            "_kind",
            "status",
            "model_requested",
            "model_returned",
            "total_tokens",
            "reasoning_tokens",
            "provider_cost",
        ]


class TestAbnormalFinishReasonsSurface:
    @pytest.mark.parametrize("finish_reason", ABNORMAL)
    def test_abnormal_reason_appears_in_the_summary(self, finish_reason: str) -> None:
        summary = json.loads(llm_call_audit_summary(_call(finish_reason=finish_reason)))

        assert summary["finish_reason"] == finish_reason

    def test_truncation_is_distinguishable_from_a_completed_answer(self) -> None:
        """The operator-facing point: two success rows must not read alike."""
        completed = llm_call_audit_summary(_call(finish_reason="stop"))
        truncated = llm_call_audit_summary(_call(finish_reason="length"))

        assert completed != truncated
        assert "length" in truncated

    def test_an_abnormal_reason_is_additive_only(self) -> None:
        """Surfacing must not disturb the fields a reader already relies on."""
        baseline = json.loads(llm_call_audit_summary(_call()))
        surfaced = json.loads(llm_call_audit_summary(_call(finish_reason="length")))

        assert {key: surfaced[key] for key in baseline} == baseline
        assert set(surfaced) - set(baseline) == {"finish_reason"}


class TestRoutineFinishReasonsAreOmitted:
    @pytest.mark.parametrize("finish_reason", ROUTINE)
    def test_routine_reason_is_absent_from_the_summary(self, finish_reason: str) -> None:
        summary = json.loads(llm_call_audit_summary(_call(finish_reason=finish_reason)))

        assert "finish_reason" not in summary

    def test_tool_calls_is_routine_because_the_loop_ends_that_way(self) -> None:
        """A tool-using turn ends on ``tool_calls`` every healthy iteration.

        Badging every such row would train the reader to ignore the badge —
        and it is why an abnormal reason is surfaced, never made fatal.
        """
        assert llm_call_audit_summary(_call(finish_reason="tool_calls")) == _LEGACY_SUMMARY_JSON


class TestUnknownIsFailVisible:
    """An unrecognised value surfaces rather than being hidden."""

    @pytest.mark.parametrize(
        "finish_reason",
        ("provider_specific_halt", "MAX_TOKENS", "guardrail_intervened", "STOP"),
    )
    def test_unrecognised_value_surfaces(self, finish_reason: str) -> None:
        summary = json.loads(llm_call_audit_summary(_call(finish_reason=finish_reason)))

        assert summary["finish_reason"] == finish_reason

    def test_the_value_is_rendered_verbatim_not_normalised(self) -> None:
        """No mapping onto a house vocabulary: summary and envelope agree."""
        summary = json.loads(llm_call_audit_summary(_call(finish_reason="MAX_TOKENS")))

        assert summary["finish_reason"] == "MAX_TOKENS"
        assert summary["finish_reason"] != "length"

    def test_uppercase_stop_is_not_folded_into_the_routine_set(self) -> None:
        """Case-folding here would silently hide an unfamiliar provider term."""
        summary = json.loads(llm_call_audit_summary(_call(finish_reason="STOP")))

        assert summary["finish_reason"] == "STOP"


class TestAbsenceOmitsTheKey:
    def test_none_omits_the_key_entirely(self) -> None:
        summary = json.loads(llm_call_audit_summary(_call()))

        assert "finish_reason" not in summary

    def test_none_does_not_emit_a_null(self) -> None:
        """``"finish_reason": null`` on every legacy row would be noise."""
        assert "finish_reason" not in llm_call_audit_summary(_call())

    def test_absence_still_omits_on_a_failed_call(self) -> None:
        summary = json.loads(
            llm_call_audit_summary(_call(status=ComposerLLMCallStatus.TIMEOUT)),
        )

        assert summary["status"] == "timeout"
        assert "finish_reason" not in summary


@dataclass
class _CapturedMessage:
    role: str
    content: str
    kwargs: dict[str, Any]


@dataclass
class _CapturingSessionService:
    """Records what a drain site actually asked to persist."""

    messages: list[_CapturedMessage] = field(default_factory=list)
    atomic_calls: int = 0

    async def add_message(
        self,
        session_id: UUID,
        role: str,
        content: str,
        **kwargs: Any,
    ) -> None:
        del session_id
        self.messages.append(_CapturedMessage(role=role, content=content, kwargs=kwargs))

    async def add_messages_atomic(
        self,
        session_id: UUID,
        drafts: Sequence[AuditMessageDraft],
        *,
        writer_principal: str,
        composition_state_id: UUID | None = None,
        **_fenced_kwargs: object,
    ) -> None:
        # In-memory double: the cohort is trivially atomic, so record one
        # captured message per draft with the same kwargs shape the old
        # per-row add_message calls carried.
        self.atomic_calls += 1
        for draft in drafts:
            await self.add_message(
                session_id,
                draft.role,
                draft.content,
                writer_principal=writer_principal,
                tool_calls=draft.tool_calls,
                composition_state_id=composition_state_id,
                tool_call_id=draft.tool_call_id,
                parent_assistant_id=draft.parent_assistant_id,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", (None, *ROUTINE, *ABNORMAL, "provider_specific_halt"))
async def test_surviving_writer_uses_shared_summary(finish_reason: str | None) -> None:
    service = _CapturingSessionService()
    session_id = uuid4()
    call = _call(finish_reason=finish_reason)
    await _persist_llm_calls(
        cast(SessionServiceProtocol, service),
        session_id,
        (call,),
        None,
        plugin_crash_pending=False,
        session_operation_context=_compose_context(session_id),
    )
    (saved,) = service.messages
    assert saved.content == llm_call_audit_summary(call)

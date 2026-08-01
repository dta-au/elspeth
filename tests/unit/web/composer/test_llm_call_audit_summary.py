"""The operator-facing summary line on an LLM-call audit row.

Two things are pinned here.

**The consolidation.** ``content`` for an LLM-call audit row used to be
hand-built byte-identically at three drain sites
(``sessions/routes/_helpers._persist_llm_calls``,
``composer/service._persist_pipeline_planner_audit``,
``sessions/guided_audit.prepare_guided_audit_rows``). They now share one
projection, :func:`llm_call_audit_summary`. The no-regression proof compares
the helper against a JSON string written out literally in this file — not
re-derived from the helper, which would pass vacuously — and the
three-sites-agree property is asserted by driving the three real drain
sites, not by calling the helper three times.

**The behaviour.** The envelope (``chat_messages.tool_calls``) already
carries ``finish_reason`` for every call; that is the forensic record, and
it is only visible to someone who opens the JSON column. This summary is
what the message-list view renders, so it surfaces a finish reason exactly
when the reason is worth an operator's attention: never for the routine
terminal states, always otherwise — including values nobody has triaged.

A note on reach, not a defect: ``litellm.types.utils.Choices`` types
``finish_reason`` as a ``Literal`` and rewrites unmapped values to
``"stop"`` before we see them (documented in
``test_llm_finish_reason_audit.py``). An unrecognised reason therefore only
reaches this projection through the Mapping-shaped provider response. The
unknown-surfaces rule is still the right one — it is what makes a new
provider term visible the day it starts arriving — but it is not universal
across every response shape.
"""

from __future__ import annotations

import json
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
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.sessions.guided_audit import prepare_guided_audit_rows
from elspeth.web.sessions.protocol import SessionServiceProtocol
from elspeth.web.sessions.routes._helpers import _persist_llm_calls

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

    async def add_message(
        self,
        session_id: UUID,
        role: str,
        content: str,
        **kwargs: Any,
    ) -> None:
        del session_id
        self.messages.append(_CapturedMessage(role=role, content=content, kwargs=kwargs))


class _PlannerAuditHost:
    """Minimal host for the real ``ComposerServiceImpl`` drain body.

    ``_persist_pipeline_planner_audit`` reaches ``self`` for exactly one
    thing — the sessions service. Binding the real method to this host runs
    the production body rather than a re-implementation of it.
    """

    def __init__(self, sessions: _CapturingSessionService) -> None:
        self._sessions = sessions

    def _require_sessions_service(self) -> SessionServiceProtocol:
        return cast(SessionServiceProtocol, self._sessions)


class TestAllThreeDrainSitesShareOneProjection:
    """The property the consolidation exists to guarantee.

    Asserted by driving the three real sites — calling the helper three
    times would prove nothing about whether the sites still use it.
    """

    @staticmethod
    async def _helpers_site_content(call: ComposerLLMCall) -> str:
        service = _CapturingSessionService()

        await _persist_llm_calls(
            cast(SessionServiceProtocol, service),
            uuid4(),
            (call,),
            None,
            plugin_crash_pending=False,
        )

        (message,) = service.messages
        return message.content

    @staticmethod
    async def _composer_service_site_content(call: ComposerLLMCall) -> str:
        service = _CapturingSessionService()
        host = _PlannerAuditHost(service)

        await ComposerServiceImpl._persist_pipeline_planner_audit(
            cast(Any, host),
            session_id=uuid4(),
            current_state_id=None,
            llm_calls=(call,),
            invocations=(),
        )

        (message,) = service.messages
        return message.content

    @staticmethod
    def _guided_audit_site_content(call: ComposerLLMCall) -> str:
        (row,) = prepare_guided_audit_rows(invocations=(), llm_calls=(call,), chat_turns=())

        assert row.kind == "llm"
        return row.content

    async def _all_three(self, call: ComposerLLMCall) -> tuple[str, str, str]:
        return (
            await self._helpers_site_content(call),
            await self._composer_service_site_content(call),
            self._guided_audit_site_content(call),
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("finish_reason", (None, *ROUTINE, *ABNORMAL, "provider_specific_halt"))
    async def test_the_three_sites_emit_the_same_summary(self, finish_reason: str | None) -> None:
        call = _call(finish_reason=finish_reason)

        helpers, composer_service, guided = await self._all_three(call)

        assert helpers == composer_service == guided

    @pytest.mark.asyncio
    async def test_the_three_sites_all_surface_an_abnormal_reason(self) -> None:
        """Not just equal to each other — equal to the right thing."""
        helpers, composer_service, guided = await self._all_three(_call(finish_reason="length"))

        for content in (helpers, composer_service, guided):
            assert json.loads(content)["finish_reason"] == "length"

    @pytest.mark.asyncio
    async def test_the_three_sites_are_unchanged_for_a_routine_call(self) -> None:
        """Byte-identical to the pre-consolidation output at every site."""
        helpers, composer_service, guided = await self._all_three(_call(finish_reason="stop"))

        for content in (helpers, composer_service, guided):
            assert content == _LEGACY_SUMMARY_JSON

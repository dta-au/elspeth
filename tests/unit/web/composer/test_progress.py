"""Tests for session-scoped composer progress snapshots."""

from __future__ import annotations

import inspect
from typing import Any, cast

import pytest
from pydantic import ValidationError

from elspeth.contracts.composer_progress import (
    COMPOSER_PROGRESS_MAX_EVIDENCE,
    ComposerProgressEvent,
)
from elspeth.web.composer.progress import (
    ComposerProgressRegistry,
    _live_request_count_expression,
    client_cancelled_progress_event,
    convergence_progress_event,
)


class TestComposerProgressEvent:
    def test_rejects_unknown_phase(self) -> None:
        """Progress phases are a closed, typed surface for frontend handling."""
        with pytest.raises(ValidationError):
            ComposerProgressEvent(
                phase=cast(Any, "thinking"),
                headline="Thinking about hidden details",
                evidence=(),
            )

    def test_evidence_is_bounded(self) -> None:
        event = ComposerProgressEvent(
            phase="using_tools",
            headline="Checking available tools",
            evidence=tuple(f"visible boundary {index}" for index in range(COMPOSER_PROGRESS_MAX_EVIDENCE + 3)),
        )

        assert len(event.evidence) == COMPOSER_PROGRESS_MAX_EVIDENCE
        assert event.evidence[-1] == f"visible boundary {COMPOSER_PROGRESS_MAX_EVIDENCE - 1}"

    def test_rejects_unknown_reason(self) -> None:
        """Reason codes are a closed Literal so the frontend can branch safely."""
        with pytest.raises(ValidationError):
            ComposerProgressEvent(
                phase="failed",
                headline="The composer could not finish this request.",
                evidence=("Some boundary.",),
                reason=cast(Any, "totally_made_up_reason"),
            )

    def test_failed_phase_requires_reason(self) -> None:
        """phase='failed' MUST carry a stable reason code so UX text drift is impossible."""
        with pytest.raises(ValidationError) as exc_info:
            ComposerProgressEvent(
                phase="failed",
                headline="The composer could not finish this request.",
                evidence=("The bounded composer loop stopped before a final answer.",),
            )
        assert "reason" in str(exc_info.value).lower()

    def test_non_failed_phases_allow_omitted_reason(self) -> None:
        """Non-failed events may omit the reason — they default to None."""
        event = ComposerProgressEvent(
            phase="using_tools",
            headline="The model is updating the pipeline graph.",
            evidence=("A pipeline-editing tool was requested.",),
        )
        assert event.reason is None

    def test_accepts_each_documented_reason_code(self) -> None:
        """Every documented reason value must round-trip through the model."""
        valid_reasons = (
            "convergence_composition_budget",
            "convergence_discovery_budget",
            "convergence_wall_clock_timeout",
            "provider_auth_failed",
            "provider_unavailable",
            "plugin_crash",
            "runtime_preflight_failed",
            "service_setup_failed",
            "client_cancelled",
            "composer_idle",
            "composer_complete",
        )
        for reason in valid_reasons:
            # client_cancelled is paired with phase="cancelled"; everything else
            # currently pairs with phase="failed". The validator only requires
            # *some* reason on terminal-non-success phases, so this is a
            # round-trip check, not a phase/reason coherence check.
            phase: str = "cancelled" if reason == "client_cancelled" else "failed"
            event = ComposerProgressEvent(
                phase=cast(Any, phase),
                headline="A safely bounded terminal event.",
                evidence=("Boundary text.",),
                reason=cast(Any, reason),
            )
            assert event.reason == reason

    def test_cancelled_phase_requires_reason(self) -> None:
        """phase='cancelled' MUST carry a stable reason — same rule as 'failed'.

        Without this, a future operator-initiated cancel reason could be
        added that collapses with client disconnect into one generic
        ``cancelled`` event — exactly the elspeth-5030f7373d failure mode
        but on the cancellation axis.
        """
        with pytest.raises(ValidationError) as exc_info:
            ComposerProgressEvent(
                phase="cancelled",
                headline="The request was cancelled.",
                evidence=("The connection closed.",),
            )
        assert "reason" in str(exc_info.value).lower()

    def test_client_cancelled_event_pairs_with_cancelled_phase(self) -> None:
        """The helper must emit the discriminated cancellation event."""
        event = client_cancelled_progress_event()
        assert event.phase == "cancelled"
        assert event.reason == "client_cancelled"
        assert event.likely_next is not None
        # Recovery copy is for the user, not the operator.
        assert "resubmit" in event.likely_next.lower()


class TestConvergenceProgressEvent:
    """The discriminator that fixes elspeth-5030f7373d.

    The original symptom was that wall-clock timeout, mutation-turn budget,
    and discovery-turn budget all collapsed into one generic ``phase: failed``
    event. These tests pin the three distinct events the helper must emit,
    so any future refactor that re-collapses them fails immediately.
    """

    def test_wall_clock_timeout_emits_distinct_event(self) -> None:
        event = convergence_progress_event(budget_exhausted="timeout")
        assert event.phase == "failed"
        assert event.reason == "convergence_wall_clock_timeout"
        assert "timed out" in event.headline.lower()
        assert event.likely_next is not None
        assert "wall-clock" in event.likely_next.lower()

    def test_composition_budget_emits_distinct_event(self) -> None:
        event = convergence_progress_event(budget_exhausted="composition")
        assert event.phase == "failed"
        assert event.reason == "convergence_composition_budget"
        assert "mutation turn budget" in event.headline.lower()
        assert event.likely_next is not None
        assert "smaller turns" in event.likely_next.lower()

    def test_discovery_budget_emits_distinct_event(self) -> None:
        event = convergence_progress_event(budget_exhausted="discovery")
        assert event.phase == "failed"
        assert event.reason == "convergence_discovery_budget"
        assert "discovery turn budget" in event.headline.lower()
        assert event.likely_next is not None
        assert "narrow" in event.likely_next.lower()

    def test_three_sub_causes_produce_three_distinct_reason_codes(self) -> None:
        """Regression guard: the three causes must NOT collapse to one code."""
        codes = {
            convergence_progress_event(budget_exhausted="timeout").reason,
            convergence_progress_event(budget_exhausted="composition").reason,
            convergence_progress_event(budget_exhausted="discovery").reason,
        }
        assert len(codes) == 3, (
            "Three convergence sub-causes collapsed back into fewer reason codes — "
            "elspeth-5030f7373d regression. Codes seen: " + repr(codes)
        )


def test_durable_reads_bind_snapshot_and_liveness_in_one_exact_statement() -> None:
    """A takeover cannot label snapshot A live using request/fence B."""
    for method in (ComposerProgressRegistry.get_latest, ComposerProgressRegistry.list_active):
        source = inspect.getsource(method)
        assert source.count("connection.execute(") == 1
        assert "_live_request_count_expression(connection)" in source
        assert "_database_now" not in source
        assert "_live_request_count(" not in source

    correlation = inspect.getsource(_live_request_count_expression)
    for exact_binding in (
        "composer_inflight_requests_table.c.session_id == composer_progress_snapshots_table.c.session_id",
        "composer_inflight_requests_table.c.request_id == composer_progress_snapshots_table.c.request_id",
        "composer_inflight_requests_table.c.user_id == composer_progress_snapshots_table.c.user_id",
        "composer_inflight_requests_table.c.operation_id == composer_progress_snapshots_table.c.operation_id",
        "composer_inflight_requests_table.c.operation_epoch == composer_progress_snapshots_table.c.operation_epoch",
        "composer_inflight_requests_table.c.operation_id == session_operation_fences_table.c.operation_id",
        "composer_inflight_requests_table.c.operation_epoch == session_operation_fences_table.c.operation_epoch",
    ):
        assert exact_binding in correlation
    assert ".correlate(composer_progress_snapshots_table)" in correlation
    assert "func.current_timestamp()" in correlation

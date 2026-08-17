"""Tests for session-scoped composer progress snapshots."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pydantic import ValidationError

from elspeth.contracts.composer_progress import (
    COMPOSER_PROGRESS_MAX_EVIDENCE,
    NON_TERMINAL_PROGRESS_PHASES,
    ComposerProgressEvent,
)
from elspeth.web.composer.progress import (
    ComposerProgressRegistry,
    advisor_checkpoint_progress_event,
    client_cancelled_progress_event,
    convergence_progress_event,
)
from elspeth.web.sessions.routes._helpers import _composer_progress_sink


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

    def test_end_advisor_progress_is_evidence_scoped_without_approval_claim(self) -> None:
        event = advisor_checkpoint_progress_event("end")

        assert event.headline == "I'm asking the advisor model to review the completion evidence."
        assert event.likely_next == ("The advisor may flag a blocker visible in the supplied evidence before the composer finalizes.")
        assert event.evidence == ("A second, model-distinct advisor is reviewing the bounded completion evidence.",)
        rendered = " ".join((event.headline, event.likely_next))
        assert "sign off" not in rendered
        assert "approve" not in rendered

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


class TestComposerProgressRegistry:
    @pytest.mark.parametrize(
        ("older_request_id", "newer_request_id"),
        (
            ("freeform-older", "guided-newer"),
            ("guided-older", "freeform-newer"),
        ),
    )
    @pytest.mark.asyncio
    async def test_shared_route_sink_rejects_late_cross_surface_publishers_in_either_direction(
        self,
        older_request_id: str,
        newer_request_id: str,
    ) -> None:
        """Freeform and guided surfaces participate in one latest-request domain."""
        registry = ComposerProgressRegistry()
        older = _composer_progress_sink(
            registry,
            session_id="session-1",
            request_id=older_request_id,
            user_id="user-1",
        )
        newer = _composer_progress_sink(
            registry,
            session_id="session-1",
            request_id=newer_request_id,
            user_id="user-1",
        )

        await newer(
            ComposerProgressEvent(
                phase="calling_model",
                headline="The newer composer request is active.",
                evidence=("The newer request owns progress custody.",),
            )
        )
        await older(
            ComposerProgressEvent(
                phase="failed",
                headline="The older composer request settled late.",
                evidence=("The superseded request must not regain custody.",),
                reason="provider_unavailable",
            )
        )

        latest = await registry.get_latest("session-1")
        assert latest.request_id == newer_request_id
        assert latest.phase == "calling_model"

    @pytest.mark.asyncio
    async def test_returns_idle_snapshot_when_session_has_no_progress(self) -> None:
        registry = ComposerProgressRegistry()

        snapshot = await registry.get_latest("session-1")

        assert snapshot.session_id == "session-1"
        assert snapshot.request_id is None
        assert snapshot.phase == "idle"
        assert snapshot.headline == "No active composer work."
        assert snapshot.evidence == ()
        assert snapshot.reason == "composer_idle"

    @pytest.mark.asyncio
    async def test_keeps_only_latest_snapshot_per_session(self) -> None:
        registry = ComposerProgressRegistry()

        first = await registry.publish(
            session_id="session-1",
            request_id="message-1",
            user_id="user-1",
            event=ComposerProgressEvent(
                phase="starting",
                headline="I'm reading your request and current pipeline.",
                evidence=("The request was accepted.",),
            ),
        )
        second = await registry.publish(
            session_id="session-1",
            request_id="message-1",
            user_id="user-1",
            event=ComposerProgressEvent(
                phase="calling_model",
                headline="I'm asking the model to choose the next pipeline change.",
                evidence=("The composer prompt was built.",),
            ),
        )

        latest = await registry.get_latest("session-1")

        assert first.updated_at < second.updated_at
        assert latest == second
        assert latest.phase == "calling_model"

    @pytest.mark.asyncio
    async def test_inflight_request_count_enriches_snapshots(self) -> None:
        """Snapshots report the session's live in-flight compose request count.

        The count is the SPA's correlated settlement signal after a client
        abort: the phase alone cannot distinguish "the aborted route is
        still running (queued on the compose lock / not yet published)"
        from "everything settled" — the registry may hold the PREVIOUS
        turn's terminal snapshot in both cases. Zero in-flight requests is
        the only reliable quiescence condition.
        """
        registry = ComposerProgressRegistry()

        assert (await registry.get_latest("session-1")).inflight_requests == 0

        registry.begin_request("session-1")
        assert (await registry.get_latest("session-1")).inflight_requests == 1

        # A second tab's request queued on the compose lock counts too.
        registry.begin_request("session-1")
        assert (await registry.get_latest("session-1")).inflight_requests == 2
        # Sessions are independent.
        assert (await registry.get_latest("session-2")).inflight_requests == 0

        registry.end_request("session-1")
        registry.end_request("session-1")
        assert (await registry.get_latest("session-1")).inflight_requests == 0

        # Published snapshots are enriched at read time with the live count,
        # not the count at publish time.
        await registry.publish(
            session_id="session-1",
            request_id="message-1",
            user_id="user-1",
            event=ComposerProgressEvent(
                phase="complete",
                headline="The requested pipeline change is finished.",
                evidence=("The state was saved.",),
            ),
        )
        registry.begin_request("session-1")
        enriched = await registry.get_latest("session-1")
        assert enriched.phase == "complete"
        assert enriched.inflight_requests == 1
        registry.end_request("session-1")

    def test_end_request_rejects_unmatched_teardown(self) -> None:
        """A missing begin_request is an owned lifecycle-contract defect."""
        registry = ComposerProgressRegistry()

        with pytest.raises(KeyError, match="session-1"):
            registry.end_request("session-1")

    def test_end_request_rejects_double_teardown(self) -> None:
        registry = ComposerProgressRegistry()
        registry.begin_request("session-1")
        registry.end_request("session-1")

        with pytest.raises(KeyError, match="session-1"):
            registry.end_request("session-1")

    @pytest.mark.asyncio
    async def test_list_active_snapshots_carry_live_inflight_count(self) -> None:
        """The operator /_active view reports the LIVE count, not publish-time zero.

        Stored snapshots are created by publish() where inflight_requests
        defaults to 0; serving them raw from list_active() would show an
        actively composing session with a zero count, contradicting the
        live-count contract the per-session GET provides.
        """
        registry = ComposerProgressRegistry()
        await registry.publish(
            session_id="session-1",
            request_id="message-1",
            user_id="user-1",
            event=ComposerProgressEvent(
                phase="using_tools",
                headline="I'm applying the requested pipeline changes.",
                evidence=("A tool call is running.",),
            ),
        )
        registry.begin_request("session-1")
        try:
            active = await registry.list_active(user_id="user-1")
            assert len(active) == 1
            assert active[0].inflight_requests == 1
        finally:
            registry.end_request("session-1")

    @pytest.mark.asyncio
    async def test_clear_removes_session_snapshot(self) -> None:
        registry = ComposerProgressRegistry()
        await registry.publish(
            session_id="session-1",
            request_id="message-1",
            user_id="user-1",
            event=ComposerProgressEvent(
                phase="failed",
                headline="The composer could not finish this request.",
                evidence=("The safe failure path was reached.",),
                reason="service_setup_failed",
            ),
        )

        await registry.clear("session-1")
        snapshot = await registry.get_latest("session-1")

        assert snapshot.phase == "idle"
        assert snapshot.updated_at <= datetime.now(UTC)

    @pytest.mark.asyncio
    async def test_clear_revokes_a_bound_request_sink(self) -> None:
        registry = ComposerProgressRegistry()
        progress = registry.bind_request(
            session_id="session-1",
            request_id="operation-1",
            user_id="user-1",
        )
        await progress(
            ComposerProgressEvent(
                phase="calling_model",
                headline="The guided planner is active.",
                evidence=("A bounded request is running.",),
            )
        )

        await registry.clear("session-1")
        await progress(
            ComposerProgressEvent(
                phase="failed",
                headline="A late operation attempted to publish.",
                evidence=("The cleared request sink completed late.",),
                reason="service_setup_failed",
            )
        )

        assert (await registry.get_latest("session-1")).phase == "idle"
        assert await registry.list_active(user_id="user-1") == ()

        replacement = registry.bind_request(
            session_id="session-1",
            request_id="operation-2",
            user_id="user-2",
        )
        await replacement(
            ComposerProgressEvent(
                phase="calling_model",
                headline="A replacement guided planner is active.",
                evidence=("A new bounded request owns the session.",),
            )
        )
        await progress(
            ComposerProgressEvent(
                phase="failed",
                headline="The archived operation completed too late.",
                evidence=("The old request sink no longer has custody.",),
                reason="service_setup_failed",
            )
        )

        latest = await registry.get_latest("session-1")
        assert latest.request_id == "operation-2"
        assert latest.phase == "calling_model"
        assert await registry.list_active(user_id="user-1") == ()
        assert [snapshot.request_id for snapshot in await registry.list_active(user_id="user-2")] == ["operation-2"]

    @pytest.mark.asyncio
    async def test_list_active_returns_only_non_terminal_phases(self) -> None:
        """list_active is the cross-session enumeration primitive used by /_active."""
        registry = ComposerProgressRegistry()
        # Two non-terminal sessions for user-1 and one terminated session.
        await registry.publish(
            session_id="session-running-1",
            request_id="msg-1",
            user_id="user-1",
            event=ComposerProgressEvent(
                phase="calling_model",
                headline="The model is composing.",
                evidence=("Prompt was built.",),
            ),
        )
        await registry.publish(
            session_id="session-running-2",
            request_id="msg-2",
            user_id="user-1",
            event=ComposerProgressEvent(
                phase="using_tools",
                headline="The model is using tools.",
                evidence=("A tool call started.",),
            ),
        )
        await registry.publish(
            session_id="session-done",
            request_id="msg-3",
            user_id="user-1",
            event=ComposerProgressEvent(
                phase="complete",
                headline="The composer response is ready.",
                evidence=("The assistant response was saved.",),
                reason="composer_complete",
            ),
        )

        active = await registry.list_active(user_id="user-1")

        assert {snap.session_id for snap in active} == {"session-running-1", "session-running-2"}
        assert all(snap.phase in NON_TERMINAL_PROGRESS_PHASES for snap in active)

    @pytest.mark.asyncio
    async def test_list_active_scopes_to_user_id(self) -> None:
        """A caller cannot enumerate other users' in-flight sessions.

        The internal user index is the only mechanism enforcing this — there
        is no DB lookup at the endpoint, so this scoping must be airtight.
        """
        registry = ComposerProgressRegistry()
        await registry.publish(
            session_id="session-mine",
            request_id="msg-1",
            user_id="user-alice",
            event=ComposerProgressEvent(
                phase="calling_model",
                headline="The model is composing.",
                evidence=("Prompt built.",),
            ),
        )
        await registry.publish(
            session_id="session-yours",
            request_id="msg-2",
            user_id="user-bob",
            event=ComposerProgressEvent(
                phase="calling_model",
                headline="Different user's request.",
                evidence=("Prompt built.",),
            ),
        )

        alice_active = await registry.list_active(user_id="user-alice")
        bob_active = await registry.list_active(user_id="user-bob")

        assert {snap.session_id for snap in alice_active} == {"session-mine"}
        assert {snap.session_id for snap in bob_active} == {"session-yours"}

    @pytest.mark.asyncio
    async def test_list_active_orders_oldest_first(self) -> None:
        """Triage order: longest-running request at the top, like a DB lock list."""
        registry = ComposerProgressRegistry()
        await registry.publish(
            session_id="session-old",
            request_id="msg-old",
            user_id="user-1",
            event=ComposerProgressEvent(
                phase="calling_model",
                headline="Older request.",
                evidence=("Already in flight.",),
            ),
        )
        await registry.publish(
            session_id="session-new",
            request_id="msg-new",
            user_id="user-1",
            event=ComposerProgressEvent(
                phase="calling_model",
                headline="Newer request.",
                evidence=("Just started.",),
            ),
        )

        active = await registry.list_active(user_id="user-1")

        assert [snap.session_id for snap in active] == ["session-old", "session-new"]

    @pytest.mark.asyncio
    async def test_list_active_excludes_cancelled_phase(self) -> None:
        """A cancelled session is no longer in flight — pin this regression guard."""
        registry = ComposerProgressRegistry()
        await registry.publish(
            session_id="session-cancelled",
            request_id="msg-1",
            user_id="user-1",
            event=client_cancelled_progress_event(),
        )

        active = await registry.list_active(user_id="user-1")

        assert active == ()

    @pytest.mark.asyncio
    async def test_clear_purges_user_index(self) -> None:
        """Clearing a session must drop its user-index entry too.

        Otherwise a re-published snapshot under the same session_id but a
        different user_id (e.g., session ownership transfer in a future
        feature) would still surface to the original user via list_active.
        """
        registry = ComposerProgressRegistry()
        await registry.publish(
            session_id="session-shared-id",
            request_id="msg-1",
            user_id="user-alice",
            event=ComposerProgressEvent(
                phase="calling_model",
                headline="Alice's request.",
                evidence=("Prompt built.",),
            ),
        )
        await registry.clear("session-shared-id")
        await registry.publish(
            session_id="session-shared-id",
            request_id="msg-2",
            user_id="user-bob",
            event=ComposerProgressEvent(
                phase="calling_model",
                headline="Bob's request after Alice's was cleared.",
                evidence=("Prompt built.",),
            ),
        )

        alice_active = await registry.list_active(user_id="user-alice")
        bob_active = await registry.list_active(user_id="user-bob")

        assert alice_active == ()
        assert {snap.session_id for snap in bob_active} == {"session-shared-id"}


def test_composer_progress_reason_typescript_mirror_is_complete() -> None:
    """The SPA's ``ComposerProgressReason`` union is a HAND-WRITTEN mirror with no pin — and it had drifted.

    Python carried ``tool_call_cap_exceeded``; the TypeScript union did not, so the type asserted a value the
    server could send could not occur. It went unnoticed because the only surface reaching that code was the
    guided one, and freeform hardcoded ``provider_unavailable`` over every planner outcome — fixing that
    attribution (elspeth-ad5628ecda) made the missing member reachable on a second surface. The sibling
    vocabulary (``GuidedOperationFailureCode``) is pinned both by a test and by a pre-commit mirror check
    (``scripts/cicd/check_slot_type_cross_language.py``); this one was not.
    """
    import re
    from pathlib import Path
    from typing import get_args

    from elspeth.contracts.composer_progress import ComposerProgressReason

    ts_path = Path(__file__).resolve().parents[4] / "src/elspeth/web/frontend/src/types/index.ts"
    body = re.search(r"export type ComposerProgressReason =(.*?);", ts_path.read_text(), re.S)
    assert body is not None, "the TypeScript union was renamed or removed; update this pin"
    # Only the union arms — a bare `"..."` on a `|` line. Comment prose is skipped by construction.
    declared = {m.group(1) for line in body.group(1).splitlines() if (m := re.match(r'\s*\|\s*"([a-z_]+)"\s*$', line))}

    assert declared == set(get_args(ComposerProgressReason.__value__)), (
        f"missing from TypeScript: {sorted(set(get_args(ComposerProgressReason.__value__)) - declared)}; "
        f"absent from Python: {sorted(declared - set(get_args(ComposerProgressReason.__value__)))}"
    )

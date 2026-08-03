"""Production route contracts for retry-safe guided-full planning."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import event, func, select, text

from elspeth.contracts.blobs import BlobIntegrityError, BlobStateError
from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.composer_progress import ComposerProgressEvent
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.composer.pipeline_planner import PipelinePlannerError
from elspeth.web.composer.pipeline_proposal import PipelineProposal, PresentBase, composition_content_hash
from elspeth.web.middleware.rate_limit import ComposerRateLimiter
from elspeth.web.sessions.guided_replay import project_composition_proposal
from elspeth.web.sessions.models import (
    chat_messages_table,
    composition_proposals_table,
    composition_states_table,
    guided_operation_events_table,
    guided_operations_table,
    proposal_events_table,
)
from elspeth.web.sessions.protocol import (
    CompositionStateData,
    GuidedOperationCompleted,
    GuidedOperationFenceLostError,
)
from elspeth.web.sessions.routes._helpers import (
    _composition_proposal_response,
    _state_from_record,
)
from elspeth.web.sessions.routes.composer import guided_plan as guided_plan_route
from elspeth.web.sessions.routes.composer.guided_plan import (
    _guided_full_failure_code,
)
from elspeth.web.sessions.routes.guided_operations import (
    GuidedOperationExpired,
    GuidedOperationLease,
    reserve_or_replay_guided_operation,
)
from elspeth.web.sessions.schemas import CompositionProposalResponse
from elspeth.web.sessions.service import _composition_state_data_content_hash


def _record_failed_llm_call(recorder, *, status: ComposerLLMCallStatus, secret: str) -> None:
    now = datetime.now(UTC)
    recorder.record_llm_call(
        ComposerLLMCall(
            model_requested="test/provider-model",
            model_returned=None,
            status=status,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            latency_ms=1,
            provider_request_id=None,
            messages_hash="a" * 64,
            tools_spec_hash=None,
            declared_tool_names=(),
            started_at=now,
            finished_at=now,
            error_class="ProviderFailure",
            error_message=secret,
            temperature=None,
            seed=None,
        )
    )


def _assert_guided_plan_terminal_progress(
    composer_test_client,
    *,
    session: dict[str, object],
    operation_id: str,
    phase: str,
    reason: str,
) -> None:
    registry = composer_test_client.app.state.composer_progress_registry
    session_id = str(session["id"])
    snapshot = asyncio.run(registry.get_latest(session_id))

    assert snapshot.request_id == operation_id
    assert snapshot.phase == phase
    assert snapshot.reason == reason
    assert snapshot.inflight_requests == 0
    active = asyncio.run(registry.list_active(user_id=str(session["user_id"])))
    assert all(candidate.session_id != session_id for candidate in active)


def test_guided_full_stages_one_atomic_replayable_cohort(composer_test_client) -> None:
    session = composer_test_client.post("/api/sessions", json={"title": "guided full"}).json()
    response = composer_test_client.post(
        f"/api/sessions/{session['id']}/guided/plan",
        json={
            "operation_id": "00000000-0000-4000-8000-000000000001",
            "intent": "Build a complete pipeline.",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "pending"
    assert payload["pipeline_metadata"]["surface"] == "guided_full"

    records = asyncio.run(composer_test_client.app.state.session_service.list_composition_proposals(UUID(session["id"])))
    assert len(records) == 1
    expected_projection = CompositionProposalResponse.model_validate_json(
        response.text,
        strict=True,
    )
    assert project_composition_proposal(records[0]) == expected_projection
    assert _composition_proposal_response(records[0]) == expected_projection

    replay = composer_test_client.post(
        f"/api/sessions/{session['id']}/guided/plan",
        json={
            "operation_id": "00000000-0000-4000-8000-000000000001",
            "intent": "Build a complete pipeline.",
        },
    )
    assert replay.status_code == 200
    assert replay.json() == payload

    engine = composer_test_client.app.state.session_engine
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(composition_states_table)) == 1
        assert conn.scalar(select(func.count()).select_from(composition_proposals_table)) == 1
        assert conn.scalar(select(func.count()).select_from(proposal_events_table)) == 1
        user_rows = conn.execute(
            select(chat_messages_table.c.content, chat_messages_table.c.writer_principal).where(chat_messages_table.c.role == "user")
        ).all()
        assert user_rows == [("Build a complete pipeline.", "route_user_message")]
        operation = conn.execute(select(guided_operations_table)).one()
        assert operation.kind == "guided_plan"
        assert operation.status == "completed"
        assert operation.result_kind == "pipeline_proposal"
        assert operation.proposal_id == payload["id"]
        assert operation.result_state_id == payload["base_state_id"]
        assert operation.originating_message_id is not None
    _assert_guided_plan_terminal_progress(
        composer_test_client,
        session=session,
        operation_id="00000000-0000-4000-8000-000000000001",
        phase="complete",
        reason="composer_complete",
    )


@pytest.mark.parametrize("reason", ("operator_rejected", "superseded"))
def test_guided_full_completed_replay_is_exact_after_proposal_terminal_settlement(
    composer_test_client,
    reason: str,
) -> None:
    session = composer_test_client.post("/api/sessions", json={"title": f"guided full replay {reason}"}).json()
    body = {
        "operation_id": (
            "00000000-0000-4000-8000-000000000051" if reason == "operator_rejected" else "00000000-0000-4000-8000-000000000052"
        ),
        "intent": "Preserve the creation-time response exactly.",
    }
    first = composer_test_client.post(f"/api/sessions/{session['id']}/guided/plan", json=body)
    assert first.status_code == 200, first.text
    payload = first.json()
    service = composer_test_client.app.state.session_service
    asyncio.run(
        service.reject_pipeline_composition_proposal(
            session_id=UUID(session["id"]),
            proposal_id=UUID(payload["id"]),
            draft_hash=payload["pipeline_metadata"]["draft_hash"],
            reviewed_facts={},
            reason=reason,
            dispatch=None,
            actor="test",
        )
    )

    replay = composer_test_client.post(f"/api/sessions/{session['id']}/guided/plan", json=body)

    assert replay.status_code == 200, replay.text
    assert replay.json() == payload


def test_guided_full_completed_replay_after_restart_restores_terminal_progress(
    composer_test_client,
) -> None:
    session = composer_test_client.post("/api/sessions", json={"title": "guided restart replay progress"}).json()
    operation_id = "00000000-0000-4000-8000-000000000077"
    body = {"operation_id": operation_id, "intent": "Persist and replay this exact proposal."}
    first = composer_test_client.post(f"/api/sessions/{session['id']}/guided/plan", json=body)
    assert first.status_code == 200, first.text

    restarted = composer_test_client.app.state.restart_test_client()
    replay = restarted.post(f"/api/sessions/{session['id']}/guided/plan", json=body)

    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    _assert_guided_plan_terminal_progress(
        restarted,
        session=session,
        operation_id=operation_id,
        phase="complete",
        reason="composer_complete",
    )


def test_guided_full_completed_replay_cannot_overwrite_a_newer_active_operation(
    composer_test_client,
) -> None:
    original_planner = composer_test_client.app.state.composer_service
    session = composer_test_client.post("/api/sessions", json={"title": "guided replay progress custody"}).json()
    older_operation_id = "00000000-0000-4000-8000-000000000078"
    older_body = {"operation_id": older_operation_id, "intent": "Build the already completed proposal."}
    completed = composer_test_client.post(f"/api/sessions/{session['id']}/guided/plan", json=older_body)
    assert completed.status_code == 200, completed.text

    class _BlockingNewerPlanner:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def plan_guided_full_pipeline(self, **kwargs):
            await kwargs["progress"](
                ComposerProgressEvent(
                    phase="calling_model",
                    headline="The newer guided operation is active.",
                    evidence=("The exact newer request owns progress custody.",),
                )
            )
            self.started.set()
            await self.release.wait()
            return await original_planner.plan_guided_full_pipeline(**kwargs)

    planner = _BlockingNewerPlanner()
    composer_test_client.app.state.composer_service = planner
    newer_operation_id = "00000000-0000-4000-8000-000000000079"

    async def replay_during_newer_operation() -> None:
        async with AsyncClient(transport=ASGITransport(app=composer_test_client.app), base_url="http://test") as client:
            newer_task = asyncio.create_task(
                client.post(
                    f"/api/sessions/{session['id']}/guided/plan",
                    json={"operation_id": newer_operation_id, "intent": "Build the newer active proposal."},
                )
            )
            await asyncio.wait_for(planner.started.wait(), timeout=3)
            replay = await client.post(f"/api/sessions/{session['id']}/guided/plan", json=older_body)
            assert replay.status_code == 200, replay.text
            latest = await composer_test_client.app.state.composer_progress_registry.get_latest(session["id"])
            assert latest.request_id == newer_operation_id
            assert latest.phase == "calling_model"
            planner.release.set()
            newer = await asyncio.wait_for(newer_task, timeout=3)
            assert newer.status_code == 200, newer.text

    asyncio.run(replay_during_newer_operation())


def test_guided_full_reserved_setup_failure_terminalizes_the_exact_operation(
    composer_test_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = composer_test_client.post("/api/sessions", json={"title": "guided full setup failure"}).json()
    operation_id = "00000000-0000-4000-8000-000000000053"

    def fail_snapshot(*_args, **_kwargs):
        raise RuntimeError("snapshot setup failed")

    monkeypatch.setattr(guided_plan_route, "_request_plugin_policy_context", fail_snapshot)
    response = composer_test_client.post(
        f"/api/sessions/{session['id']}/guided/plan",
        json={"operation_id": operation_id, "intent": "Reserve, then fail setup."},
    )

    assert response.status_code == 500, response.text
    with composer_test_client.app.state.session_engine.connect() as conn:
        operation = (
            conn.execute(select(guided_operations_table).where(guided_operations_table.c.operation_id == operation_id)).mappings().one()
        )
    assert operation["status"] == "failed"
    assert operation["failure_code"] == "operation_failed"
    _assert_guided_plan_terminal_progress(
        composer_test_client,
        session=session,
        operation_id=operation_id,
        phase="failed",
        reason="service_setup_failed",
    )


def test_guided_full_provider_owned_cancelled_error_is_operation_failed(
    composer_test_client,
) -> None:
    class _ProviderCancelledPlanner:
        async def plan_guided_full_pipeline(self, **_kwargs):
            raise asyncio.CancelledError()

    composer_test_client.app.state.composer_service = _ProviderCancelledPlanner()
    session = composer_test_client.post("/api/sessions", json={"title": "provider cancelled"}).json()
    operation_id = "00000000-0000-4000-8000-000000000054"

    response = composer_test_client.post(
        f"/api/sessions/{session['id']}/guided/plan",
        json={"operation_id": operation_id, "intent": "Classify provider cancellation honestly."},
    )

    assert response.status_code == 500, response.text
    assert response.json()["detail"]["failure_code"] == "operation_failed"
    with composer_test_client.app.state.session_engine.connect() as conn:
        operation = (
            conn.execute(select(guided_operations_table).where(guided_operations_table.c.operation_id == operation_id)).mappings().one()
        )
    assert operation["status"] == "failed"
    assert operation["failure_code"] == "operation_failed"
    _assert_guided_plan_terminal_progress(
        composer_test_client,
        session=session,
        operation_id=operation_id,
        phase="failed",
        reason="service_setup_failed",
    )


def test_guided_full_escape_hatch_decline_is_an_ordinary_assistant_message_not_a_failure(
    composer_test_client,
) -> None:
    """PlannerDeclined on the guided-full surface must not become operation_failed.

    Mirrors the freeform surface's PlannerDeclined handling
    (ComposerServiceImpl.compose): an honest decline from the escape-hatch
    advisor is a conversational outcome, so the guided-full operation
    completes with the advisor's own words persisted as an ordinary
    assistant chat message — never routed through GuidedOperationFailureCode.
    """
    from elspeth.web.composer.pipeline_planner import GuidedPlannerDecline

    decline_text = "I could not find a source plugin that reads this format."

    class _DecliningPlanner:
        async def plan_guided_full_pipeline(self, **_kwargs):
            return GuidedPlannerDecline(decline_text=decline_text)

    composer_test_client.app.state.composer_service = _DecliningPlanner()
    session = composer_test_client.post("/api/sessions", json={"title": "guided full decline"}).json()
    operation_id = "00000000-0000-4000-8000-000000000056"

    response = composer_test_client.post(
        f"/api/sessions/{session['id']}/guided/plan",
        json={"operation_id": operation_id, "intent": "Build a pipeline from an unsupported format."},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["outcome"] == "declined"
    assert payload["message"]["role"] == "assistant"
    assert payload["message"]["content"] == decline_text

    with composer_test_client.app.state.session_engine.connect() as conn:
        operation = (
            conn.execute(select(guided_operations_table).where(guided_operations_table.c.operation_id == operation_id)).mappings().one()
        )
        assistant_rows = conn.execute(
            select(chat_messages_table.c.id, chat_messages_table.c.content, chat_messages_table.c.writer_principal).where(
                chat_messages_table.c.role == "assistant"
            )
        ).all()
        user_rows = conn.execute(
            select(chat_messages_table.c.content, chat_messages_table.c.writer_principal).where(chat_messages_table.c.role == "user")
        ).all()
        assert conn.scalar(select(func.count()).select_from(composition_proposals_table)) == 0
        assert conn.scalar(select(func.count()).select_from(proposal_events_table)) == 0

    # Not a failure: completed, no failure_code, no proposal.
    assert operation.status == "completed"
    assert operation.failure_code is None
    assert operation.result_kind == "declined"
    assert operation.proposal_id is None
    assert operation.result_state_id is not None
    assert len(assistant_rows) == 1
    decline_message_id = assistant_rows[0].id
    assert assistant_rows[0].content == decline_text
    assert assistant_rows[0].writer_principal == "compose_loop"
    assert user_rows == [("Build a pipeline from an unsupported format.", "route_user_message")]

    # A later assistant turn may legitimately retain the same unchanged
    # composition-state association. Replay must use the immutable decline
    # message locator, not scan by that non-unique state.
    asyncio.run(
        composer_test_client.app.state.session_service.add_message(
            UUID(session["id"]),
            "assistant",
            "Later assistant turn with unchanged composition.",
            writer_principal="compose_loop",
            composition_state_id=UUID(operation.result_state_id),
        )
    )
    replay = composer_test_client.post(
        f"/api/sessions/{session['id']}/guided/plan",
        json={"operation_id": operation_id, "intent": "Build a pipeline from an unsupported format."},
    )
    assert replay.status_code == 200
    assert replay.json() == payload
    with composer_test_client.app.state.session_engine.connect() as conn:
        replay_operation = (
            conn.execute(select(guided_operations_table).where(guided_operations_table.c.operation_id == operation_id)).mappings().one()
        )
    assert replay_operation.result_message_id == decline_message_id
    _assert_guided_plan_terminal_progress(
        composer_test_client,
        session=session,
        operation_id=operation_id,
        phase="complete",
        reason="composer_complete",
    )


def test_guided_full_failure_atomically_retains_sanitized_audit_without_a_checkpoint(
    composer_test_client,
) -> None:
    secret = "tier-three-provider-secret"

    class _AuditedFailurePlanner:
        async def plan_guided_full_pipeline(self, **kwargs):
            _record_failed_llm_call(kwargs["recorder"], status=ComposerLLMCallStatus.API_ERROR, secret=secret)
            raise PipelinePlannerError("safe provider failure", code="PROVIDER_ERROR")

    composer_test_client.app.state.composer_service = _AuditedFailurePlanner()
    session = composer_test_client.post("/api/sessions", json={"title": "audited guided failure"}).json()
    operation_id = "00000000-0000-4000-8000-000000000055"

    response = composer_test_client.post(
        f"/api/sessions/{session['id']}/guided/plan",
        json={"operation_id": operation_id, "intent": "Retain bounded failure evidence."},
    )

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["failure_code"] == "provider_unavailable"
    with composer_test_client.app.state.session_engine.connect() as conn:
        audit_rows = conn.execute(
            select(
                chat_messages_table.c.role,
                chat_messages_table.c.content,
                chat_messages_table.c.tool_calls,
                chat_messages_table.c.composition_state_id,
            ).where(chat_messages_table.c.session_id == session["id"])
        ).all()
        operation = (
            conn.execute(select(guided_operations_table).where(guided_operations_table.c.operation_id == operation_id)).mappings().one()
        )
    assert len(audit_rows) == 1
    assert audit_rows[0].role == "audit"
    assert audit_rows[0].composition_state_id is None
    assert audit_rows[0].tool_calls[0]["_kind"] == "llm_call_audit"
    assert audit_rows[0].tool_calls[0]["call"]["error_message"] is None
    assert secret not in str(audit_rows[0])
    assert operation["status"] == "failed"
    assert operation["failure_code"] == "provider_unavailable"
    _assert_guided_plan_terminal_progress(
        composer_test_client,
        session=session,
        operation_id=operation_id,
        phase="failed",
        reason="provider_unavailable",
    )


def test_guided_full_failure_cleanup_error_preserves_the_primary_provider_outcome(
    composer_test_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from structlog.testing import capture_logs

    class _PrimaryFailurePlanner:
        async def plan_guided_full_pipeline(self, **_kwargs):
            raise PipelinePlannerError("safe primary failure", code="PROVIDER_ERROR")

    secondary_secret = "secondary-cleanup-secret-must-not-be-logged"  # secret-scan: allow-this-line

    async def fail_cleanup(_command):
        raise RuntimeError(secondary_secret)

    composer_test_client.app.state.composer_service = _PrimaryFailurePlanner()
    monkeypatch.setattr(
        composer_test_client.app.state.session_service,
        "fail_guided_operation_with_audit",
        fail_cleanup,
    )
    session = composer_test_client.post("/api/sessions", json={"title": "guided cleanup primacy"}).json()
    operation_id = "00000000-0000-4000-8000-000000000074"

    with capture_logs() as logs:
        response = composer_test_client.post(
            f"/api/sessions/{session['id']}/guided/plan",
            json={"operation_id": operation_id, "intent": "Preserve the provider failure."},
        )

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["failure_code"] == "provider_unavailable"
    secondary_events = [event for event in logs if event.get("event") == "guided.plan_failure_settlement_secondary_failure"]
    assert len(secondary_events) == 1
    assert secondary_events[0]["primary_failure_code"] == "provider_unavailable"
    assert secondary_events[0]["secondary_exc_class"] == "RuntimeError"
    assert secondary_secret not in str(secondary_events)
    _assert_guided_plan_terminal_progress(
        composer_test_client,
        session=session,
        operation_id=operation_id,
        phase="failed",
        reason="provider_unavailable",
    )


def test_guided_full_no_winner_after_failure_fence_loss_preserves_primary_outcome(
    composer_test_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from structlog.testing import capture_logs

    class _PrimaryFailurePlanner:
        async def plan_guided_full_pipeline(self, **_kwargs):
            raise PipelinePlannerError("safe primary failure", code="PROVIDER_ERROR")

    real_reserve = reserve_or_replay_guided_operation

    async def lose_failure_fence(command):
        raise GuidedOperationFenceLostError(command.fence)

    async def no_winner_lookup(**kwargs):
        if kwargs.get("reserve_if_absent") is False:
            return None
        return await real_reserve(**kwargs)

    composer_test_client.app.state.composer_service = _PrimaryFailurePlanner()
    monkeypatch.setattr(
        composer_test_client.app.state.session_service,
        "fail_guided_operation_with_audit",
        lose_failure_fence,
    )
    monkeypatch.setattr(guided_plan_route, "reserve_or_replay_guided_operation", no_winner_lookup)
    session = composer_test_client.post("/api/sessions", json={"title": "guided no failure winner"}).json()
    operation_id = "00000000-0000-4000-8000-000000000075"

    with capture_logs() as logs:
        response = composer_test_client.post(
            f"/api/sessions/{session['id']}/guided/plan",
            json={"operation_id": operation_id, "intent": "Preserve the primary failure without a winner."},
        )

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["failure_code"] == "provider_unavailable"
    secondary_events = [event for event in logs if event.get("event") == "guided.plan_failure_settlement_secondary_failure"]
    assert len(secondary_events) == 1
    assert secondary_events[0]["site"] == "fence_lost_no_winner"
    _assert_guided_plan_terminal_progress(
        composer_test_client,
        session=session,
        operation_id=operation_id,
        phase="failed",
        reason="provider_unavailable",
    )


@pytest.mark.parametrize(
    ("lookup_outcome", "expected_log_site"),
    (
        ("none", "main_fence_lost_no_winner"),
        ("lease", "main_fence_lost_no_winner"),
        ("expired", "main_fence_lost_no_winner"),
        ("lookup_error", "main_fence_winner_lookup"),
    ),
)
def test_guided_full_main_fence_loss_without_a_replayable_winner_preserves_the_primary_and_terminalizes_progress(
    composer_test_client,
    monkeypatch: pytest.MonkeyPatch,
    lookup_outcome: str,
    expected_log_site: str,
) -> None:
    from structlog.testing import capture_logs

    class _FenceLosingPlanner:
        def __init__(self) -> None:
            self.fence = None

        async def plan_guided_full_pipeline(self, **kwargs):
            self.fence = kwargs["operation_fence"]
            await kwargs["progress"](
                ComposerProgressEvent(
                    phase="calling_model",
                    headline="The guided planner is preparing a proposal.",
                    evidence=("A bounded test provider call is active.",),
                )
            )
            raise GuidedOperationFenceLostError(self.fence)

    planner = _FenceLosingPlanner()
    real_reserve = reserve_or_replay_guided_operation

    async def no_replayable_winner(**kwargs):
        if kwargs.get("reserve_if_absent") is not False:
            return await real_reserve(**kwargs)
        if lookup_outcome == "lease":
            assert planner.fence is not None
            return GuidedOperationLease(fence=planner.fence)
        if lookup_outcome == "expired":
            return GuidedOperationExpired(attempt=1)
        if lookup_outcome == "lookup_error":
            raise RuntimeError("SENSITIVE_LOOKUP_DETAIL")
        return None

    composer_test_client.app.state.composer_service = planner
    monkeypatch.setattr(guided_plan_route, "reserve_or_replay_guided_operation", no_replayable_winner)
    session = composer_test_client.post("/api/sessions", json={"title": f"guided main fence {lookup_outcome}"}).json()
    operation_id = "00000000-0000-4000-8000-000000000081"

    async def lose_fence() -> None:
        async with AsyncClient(transport=ASGITransport(app=composer_test_client.app), base_url="http://test") as client:
            with pytest.raises(GuidedOperationFenceLostError, match="fence is no longer current"):
                await client.post(
                    f"/api/sessions/{session['id']}/guided/plan",
                    json={"operation_id": operation_id, "intent": "Lose the main operation fence."},
                )

    with capture_logs() as logs:
        asyncio.run(lose_fence())

    secondary_events = [event for event in logs if event.get("event") == "guided.plan_failure_settlement_secondary_failure"]
    assert len(secondary_events) == 1
    assert secondary_events[0]["primary_failure_code"] == "operation_failed"
    assert secondary_events[0]["site"] == expected_log_site
    assert "SENSITIVE_LOOKUP_DETAIL" not in str(secondary_events)
    _assert_guided_plan_terminal_progress(
        composer_test_client,
        session=session,
        operation_id=operation_id,
        phase="failed",
        reason="service_setup_failed",
    )


def test_guided_full_preserves_an_existing_canonical_state_as_the_checkpoint_base(
    composer_test_client,
) -> None:
    session = composer_test_client.post("/api/sessions", json={"title": "guided full existing"}).json()
    service = composer_test_client.app.state.session_service
    existing = asyncio.run(
        service.save_composition_state(
            UUID(session["id"]),
            CompositionStateData(
                sources={
                    "existing-source": {
                        "plugin": "csv",
                        "options": {"path": "/data/existing.csv"},
                        "on_success": "existing-output",
                        "on_validation_failure": "discard",
                    }
                },
                nodes=[],
                edges=[],
                outputs=[
                    {
                        "name": "existing-output",
                        "plugin": "json",
                        "options": {"path": "/data/existing.jsonl"},
                        "on_write_failure": "discard",
                    }
                ],
                metadata_={"name": "Existing pipeline", "description": "Preserve exactly"},
                is_valid=True,
            ),
            provenance="session_seed",
        )
    )
    original_state = _state_from_record(existing)
    original_hash = composition_content_hash(original_state)

    response = composer_test_client.post(
        f"/api/sessions/{session['id']}/guided/plan",
        json={
            "operation_id": "00000000-0000-4000-8000-000000000018",
            "intent": "Replace this with the proposed complete pipeline.",
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    checkpoint = asyncio.run(service.get_state(UUID(payload["base_state_id"])))
    assert checkpoint.id != existing.id
    assert checkpoint.version == existing.version + 1
    assert checkpoint.derived_from_state_id == existing.id
    assert composition_content_hash(_state_from_record(checkpoint)) == original_hash
    unchanged = asyncio.run(service.get_state(existing.id))
    assert unchanged == existing
    authority = asyncio.run(
        service.get_authoritative_pipeline_proposal(
            session_id=UUID(session["id"]),
            proposal_id=UUID(payload["id"]),
            reviewed_facts={},
        )
    )
    assert type(authority.proposal.base) is PresentBase
    assert authority.proposal.base.state_id == checkpoint.id
    assert authority.proposal.base.composition_content_hash == original_hash
    with composer_test_client.app.state.session_engine.connect() as conn:
        origin_state_id = conn.scalar(
            select(chat_messages_table.c.composition_state_id).where(chat_messages_table.c.id == str(authority.row.user_message_id))
        )
    assert origin_state_id == str(checkpoint.id)


def test_guided_full_settlement_rejects_command_state_that_differs_from_the_observed_head(
    composer_test_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = composer_test_client.post("/api/sessions", json={"title": "guided full head binding"}).json()
    session_id = UUID(session["id"])
    service = composer_test_client.app.state.session_service
    existing = asyncio.run(
        service.save_composition_state(
            session_id,
            CompositionStateData(
                sources={},
                nodes=[],
                edges=[],
                outputs=[],
                metadata_={"name": "authoritative head", "description": "Bind the checkpoint to this exact head."},
                is_valid=False,
            ),
            provenance="session_seed",
        )
    )
    real_stage = service.stage_guided_full_pipeline_proposal

    async def stage_mismatched_state(command):
        mismatched_state = replace(command.state, metadata_={"name": "different checkpoint bytes"})
        mismatched_hash = _composition_state_data_content_hash(mismatched_state)
        mismatched_proposal = PipelineProposal.create(
            pipeline=command.plan.proposal.pipeline,
            base=PresentBase(
                state_id=command.checkpoint_state_id,
                composition_content_hash=mismatched_hash,
            ),
            reviewed_facts={},
            surface=command.plan.proposal.surface,
            repair_count=command.plan.proposal.repair_count,
            skill_hash=command.plan.proposal.skill_hash,
            covered_deferred_intent_ids=command.plan.proposal.covered_deferred_intent_ids,
            supersedes_draft_hash=command.plan.proposal.supersedes_draft_hash,
        )
        return await real_stage(
            replace(
                command,
                state=mismatched_state,
                plan=replace(command.plan, proposal=mismatched_proposal),
            )
        )

    monkeypatch.setattr(service, "stage_guided_full_pipeline_proposal", stage_mismatched_state)
    response = composer_test_client.post(
        f"/api/sessions/{session['id']}/guided/plan",
        json={
            "operation_id": "00000000-0000-4000-8000-000000000056",
            "intent": "Reject a mismatched checkpoint before publication.",
        },
    )

    assert response.status_code == 500, response.text
    assert response.json()["detail"]["failure_code"] == "integrity_error"
    with composer_test_client.app.state.session_engine.connect() as conn:
        states = conn.execute(select(composition_states_table.c.id).where(composition_states_table.c.session_id == session["id"])).all()
        assert states == [(str(existing.id),)]
        assert conn.scalar(select(func.count()).select_from(composition_proposals_table)) == 0
        assert conn.scalar(select(func.count()).select_from(proposal_events_table)) == 0
        assert conn.scalar(select(func.count()).select_from(chat_messages_table)) == 0


def test_guided_full_requires_authentication_before_operation_reservation(
    composer_test_client,
) -> None:
    session = composer_test_client.post("/api/sessions", json={"title": "guided full auth"}).json()
    app = composer_test_client.app

    class _AuthAuditRecorder:
        def record_auth_failure(self, *_args, **_kwargs) -> None:
            return None

    app.state.auth_rate_limiter = ComposerRateLimiter(limit=100)
    app.state.auth_audit_recorder = _AuthAuditRecorder()
    override = app.dependency_overrides.pop(get_current_user)
    try:
        response = composer_test_client.post(
            f"/api/sessions/{session['id']}/guided/plan",
            json={
                "operation_id": "00000000-0000-4000-8000-000000000019",
                "intent": "This request is unauthenticated.",
            },
        )
    finally:
        app.dependency_overrides[get_current_user] = override

    assert response.status_code == 401
    with app.state.session_engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(guided_operations_table)) == 0
        assert conn.scalar(select(func.count()).select_from(composition_states_table)) == 0
        assert conn.scalar(select(func.count()).select_from(chat_messages_table)) == 0


@pytest.mark.parametrize("tamper", ("response_hash", "origin_authority"))
def test_guided_full_replay_fails_closed_on_persisted_authority_tamper(
    composer_test_client,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    session = composer_test_client.post("/api/sessions", json={"title": f"guided full tamper {tamper}"}).json()
    operation_id = {
        "response_hash": "00000000-0000-4000-8000-000000000031",
        "origin_authority": "00000000-0000-4000-8000-000000000032",
    }[tamper]
    body = {"operation_id": operation_id, "intent": "Bind this exact authority."}
    first = composer_test_client.post(
        f"/api/sessions/{session['id']}/guided/plan",
        json=body,
    )
    assert first.status_code == 200, first.text
    engine = composer_test_client.app.state.session_engine
    service = composer_test_client.app.state.session_service
    if tamper == "response_hash":
        reserve = service.reserve_guided_operation

        async def tampered_reserve(*args, **kwargs):
            outcome = await reserve(*args, **kwargs)
            if isinstance(outcome, GuidedOperationCompleted):
                return replace(outcome, response_hash="0" * 64)
            return outcome

        monkeypatch.setattr(service, "reserve_guided_operation", tampered_reserve)
    else:
        get_messages = service.get_messages

        async def tampered_messages(*args, **kwargs):
            messages = await get_messages(*args, **kwargs)
            return [replace(message, content="tampered origin") if message.role == "user" else message for message in messages]

        monkeypatch.setattr(service, "get_messages", tampered_messages)

    class _ForbiddenPlanner:
        async def plan_guided_full_pipeline(self, **_kwargs):
            raise AssertionError("replay must not call the planner")

    composer_test_client.app.state.composer_service = _ForbiddenPlanner()
    with pytest.raises(AuditIntegrityError):
        composer_test_client.post(
            f"/api/sessions/{session['id']}/guided/plan",
            json=body,
        )
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(composition_proposals_table)) == 1
        assert conn.scalar(select(func.count()).select_from(composition_states_table)) == 1


def test_cold_guided_chat_rejects_without_creating_an_operation(composer_test_client) -> None:
    session = composer_test_client.post("/api/sessions", json={"title": "cold guided"}).json()
    response = composer_test_client.post(
        f"/api/sessions/{session['id']}/guided/chat",
        json={
            "operation_id": "00000000-0000-4000-8000-000000000002",
            "turn_token": "0" * 64,
            "message": "Build a source.",
        },
    )
    assert response.status_code == 409
    with composer_test_client.app.state.session_engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(guided_operations_table)) == 0
        assert conn.scalar(select(func.count()).select_from(chat_messages_table)) == 0


def test_guided_full_hash_mismatch_conflicts_without_new_provider_call(composer_test_client) -> None:
    session = composer_test_client.post("/api/sessions", json={"title": "guided full conflict"}).json()
    first = composer_test_client.post(
        f"/api/sessions/{session['id']}/guided/plan",
        json={
            "operation_id": "00000000-0000-4000-8000-000000000003",
            "intent": "Build pipeline A.",
        },
    )
    assert first.status_code == 200
    conflict = composer_test_client.post(
        f"/api/sessions/{session['id']}/guided/plan",
        json={
            "operation_id": "00000000-0000-4000-8000-000000000003",
            "intent": "Build pipeline B.",
        },
    )
    assert conflict.status_code == 409
    with composer_test_client.app.state.session_engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(composition_proposals_table)) == 1


def test_guided_full_provider_timeout_fails_closed_before_atomic_staging(composer_test_client) -> None:
    class _TimedOutPlanner:
        async def plan_guided_full_pipeline(self, **_kwargs):
            raise PipelinePlannerError("bounded provider timeout", code="TIMEOUT")

    composer_test_client.app.state.composer_service = _TimedOutPlanner()
    session = composer_test_client.post("/api/sessions", json={"title": "guided full timeout"}).json()
    operation_id = "00000000-0000-4000-8000-000000000004"

    response = composer_test_client.post(
        f"/api/sessions/{session['id']}/guided/plan",
        json={"operation_id": operation_id, "intent": "Build before the deadline."},
    )

    assert response.status_code == 504, response.text
    assert response.json()["detail"]["failure_code"] == "provider_timeout"
    with composer_test_client.app.state.session_engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(composition_states_table)) == 0
        assert conn.scalar(select(func.count()).select_from(composition_proposals_table)) == 0
        assert conn.scalar(select(func.count()).select_from(proposal_events_table)) == 0
        assert conn.scalar(select(func.count()).select_from(chat_messages_table)) == 0
        operation = (
            conn.execute(select(guided_operations_table).where(guided_operations_table.c.operation_id == operation_id)).mappings().one()
        )
    assert operation["status"] == "failed"
    assert operation["failure_code"] == "provider_timeout"
    assert operation["originating_message_id"] is None
    assert operation["proposal_id"] is None
    assert operation["result_state_id"] is None


def test_guided_full_blob_failures_keep_custody_and_integrity_distinct() -> None:
    assert _guided_full_failure_code(BlobStateError("00000000-0000-4000-8000-000000000001", message="not ready")) == "custody_error"
    assert (
        _guided_full_failure_code(
            BlobIntegrityError(
                "00000000-0000-4000-8000-000000000001",
                expected="0" * 64,
                actual="1" * 64,
            )
        )
        == "integrity_error"
    )


@pytest.mark.parametrize(
    ("code", "expected"),
    (
        ("TIMEOUT", "provider_timeout"),
        ("PROVIDER_ERROR", "provider_unavailable"),
        ("MALFORMED_RESPONSE", "invalid_provider_response"),
        ("VALIDATION_FAILED", "invalid_provider_response"),
        ("COST_UNAVAILABLE", "invalid_provider_response"),
        ("COMPLETION_TOKENS_EXCEEDED", "invalid_provider_response"),
        ("PROVIDER_CALLS_EXHAUSTED", "invalid_provider_response"),
        ("TOOL_CALLS_EXHAUSTED", "invalid_provider_response"),
        ("COMPOSITION_EXHAUSTED", "invalid_provider_response"),
        ("REPAIR_EXHAUSTED", "invalid_provider_response"),
        ("DISCOVERY_ONLY", "invalid_provider_response"),
        ("DISCOVERY_EXHAUSTED", "invalid_provider_response"),
        ("DISCOVERY_CYCLE", "invalid_provider_response"),
        ("REQUEST_BYTES_EXHAUSTED", "operation_failed"),
        ("COST_CAP_EXCEEDED", "operation_failed"),
        ("FUTURE_UNKNOWN_CODE", "operation_failed"),
    ),
)
def test_guided_full_planner_failure_mapping_is_closed(
    code: str,
    expected: str,
) -> None:
    assert _guided_full_failure_code(PipelinePlannerError("safe", code=code)) == expected


@pytest.mark.parametrize("code", ("VALIDATION_FAILED", "REPAIR_EXHAUSTED", "COMPOSITION_EXHAUSTED"))
@pytest.mark.parametrize("policy_code", ("aws_s3_source_not_allowed", "plugin_not_allowed_on_web"))
def test_guided_full_policy_refusal_outranks_the_planner_code(code: str, policy_code: str) -> None:
    """A categorical policy refusal is permanent, whatever code it exhausted under.

    The same refusal arrives as VALIDATION_FAILED from the server-derived
    pass-through gate and as REPAIR_EXHAUSTED once the model has burnt its repair
    budget re-authoring the prohibited component. Both are permanent, so the
    classification is keyed on the rejection's closed detail codes rather than the
    planner code — otherwise the user is told to retry a request that can never
    succeed (guided S3, 2026-07-31).
    """
    exc = PipelinePlannerError("safe", code=code, detail_codes=(policy_code,))

    assert _guided_full_failure_code(exc) == "policy_blocked"
    # Without the policy code the same planner code stays a retryable provider
    # fault — the split must not swallow ordinary exhaustion.
    assert _guided_full_failure_code(PipelinePlannerError("safe", code=code)) == "invalid_provider_response"


@pytest.mark.parametrize(
    "fault_point",
    ("checkpoint", "origin", "proposal_event", "proposal", "operation_complete"),
)
def test_guided_full_atomic_stage_fault_rolls_back_the_entire_cohort(
    composer_test_client,
    fault_point: str,
) -> None:
    session = composer_test_client.post("/api/sessions", json={"title": f"fault {fault_point}"}).json()
    operation_id = {
        "checkpoint": "00000000-0000-4000-8000-000000000011",
        "origin": "00000000-0000-4000-8000-000000000012",
        "proposal_event": "00000000-0000-4000-8000-000000000013",
        "proposal": "00000000-0000-4000-8000-000000000014",
        "operation_complete": "00000000-0000-4000-8000-000000000015",
    }[fault_point]
    engine = composer_test_client.app.state.session_engine
    armed = True

    def inject_fault(_conn, _cursor, statement, _parameters, context, _executemany):
        nonlocal armed
        if not armed:
            return
        normalized = " ".join(statement.lower().split())
        compiled = getattr(context, "compiled", None)
        target_table = getattr(getattr(compiled, "statement", None), "table", None)
        label: str | None = None
        if target_table is composition_states_table:
            label = "checkpoint"
        elif target_table is chat_messages_table:
            label = "origin"
        elif target_table is proposal_events_table:
            label = "proposal_event"
        elif target_table is composition_proposals_table:
            label = "proposal"
        elif normalized.startswith("update guided_operations set status"):
            label = "operation_complete"
        if label == fault_point:
            armed = False
            raise RuntimeError(f"injected {fault_point}")

    event.listen(engine, "before_cursor_execute", inject_fault)
    try:
        response = composer_test_client.post(
            f"/api/sessions/{session['id']}/guided/plan",
            json={"operation_id": operation_id, "intent": "All or nothing."},
        )
    finally:
        event.remove(engine, "before_cursor_execute", inject_fault)

    assert response.status_code == 500, response.text
    assert not armed, f"fault point {fault_point} was not reached"
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(composition_states_table)) == 0
        assert conn.scalar(select(func.count()).select_from(chat_messages_table)) == 0
        assert conn.scalar(select(func.count()).select_from(proposal_events_table)) == 0
        assert conn.scalar(select(func.count()).select_from(composition_proposals_table)) == 0
        operation = (
            conn.execute(select(guided_operations_table).where(guided_operations_table.c.operation_id == operation_id)).mappings().one()
        )
        operation_events = (
            conn.execute(
                select(guided_operation_events_table.c.event_kind)
                .where(guided_operation_events_table.c.operation_id == operation_id)
                .order_by(guided_operation_events_table.c.sequence)
            )
            .scalars()
            .all()
        )
    assert operation["status"] == "failed"
    assert operation["failure_code"] == "operation_failed"
    assert operation["originating_message_id"] is None
    assert operation["proposal_id"] is None
    assert operation["result_state_id"] is None
    assert operation_events[-1] == "failed"


def test_guided_full_cancel_before_staging_leaves_no_partial_cohort(composer_test_client) -> None:
    secret = "cancelled-provider-secret"

    class _BlockingPlanner:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def plan_guided_full_pipeline(self, **kwargs):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                _record_failed_llm_call(
                    kwargs["recorder"],
                    status=ComposerLLMCallStatus.CANCELLED,
                    secret=secret,
                )
                raise

    planner = _BlockingPlanner()
    composer_test_client.app.state.composer_service = planner
    session = composer_test_client.post("/api/sessions", json={"title": "guided full cancelled"}).json()
    operation_id = "00000000-0000-4000-8000-000000000016"

    async def cancel_request() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=composer_test_client.app),
            base_url="http://test",
        ) as client:
            request_task = asyncio.create_task(
                client.post(
                    f"/api/sessions/{session['id']}/guided/plan",
                    json={"operation_id": operation_id, "intent": "Cancel before staging."},
                )
            )
            await asyncio.wait_for(planner.started.wait(), timeout=3)
            request_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request_task

    asyncio.run(cancel_request())

    with composer_test_client.app.state.session_engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(composition_states_table)) == 0
        audit_rows = conn.execute(
            select(chat_messages_table.c.content, chat_messages_table.c.tool_calls, chat_messages_table.c.composition_state_id)
        ).all()
        assert conn.scalar(select(func.count()).select_from(proposal_events_table)) == 0
        assert conn.scalar(select(func.count()).select_from(composition_proposals_table)) == 0
        operation = (
            conn.execute(select(guided_operations_table).where(guided_operations_table.c.operation_id == operation_id)).mappings().one()
        )
    assert operation["status"] == "failed"
    assert operation["failure_code"] == "request_cancelled"
    assert operation["originating_message_id"] is None
    assert operation["proposal_id"] is None
    assert operation["result_state_id"] is None
    assert len(audit_rows) == 1
    assert audit_rows[0].composition_state_id is None
    assert audit_rows[0].tool_calls[0]["_kind"] == "llm_call_audit"
    assert audit_rows[0].tool_calls[0]["call"]["error_message"] is None
    assert secret not in str(audit_rows[0])
    _assert_guided_plan_terminal_progress(
        composer_test_client,
        session=session,
        operation_id=operation_id,
        phase="cancelled",
        reason="client_cancelled",
    )


def test_guided_full_cancel_after_atomic_settlement_still_publishes_terminal_progress(
    composer_test_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = composer_test_client.app.state.session_service
    real_stage = service.stage_guided_full_pipeline_proposal
    settlement_committed = asyncio.Event()
    release_settlement = asyncio.Event()

    async def committed_then_paused(command):
        result = await real_stage(command)
        settlement_committed.set()
        await release_settlement.wait()
        return result

    monkeypatch.setattr(service, "stage_guided_full_pipeline_proposal", committed_then_paused)
    session = composer_test_client.post("/api/sessions", json={"title": "guided settled cancellation"}).json()
    operation_id = "00000000-0000-4000-8000-000000000073"

    async def cancel_after_commit() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=composer_test_client.app),
            base_url="http://test",
        ) as client:
            request_task = asyncio.create_task(
                client.post(
                    f"/api/sessions/{session['id']}/guided/plan",
                    json={"operation_id": operation_id, "intent": "Settle before cancellation is observed."},
                )
            )
            await asyncio.wait_for(settlement_committed.wait(), timeout=3)
            request_task.cancel()
            await asyncio.sleep(0)
            release_settlement.set()
            with pytest.raises(asyncio.CancelledError):
                await request_task

    asyncio.run(cancel_after_commit())

    with composer_test_client.app.state.session_engine.connect() as conn:
        operation = (
            conn.execute(select(guided_operations_table).where(guided_operations_table.c.operation_id == operation_id)).mappings().one()
        )
    assert operation["status"] == "completed"
    _assert_guided_plan_terminal_progress(
        composer_test_client,
        session=session,
        operation_id=operation_id,
        phase="complete",
        reason="composer_complete",
    )


def test_guided_full_cancellation_cleanup_error_preserves_cancelled_error_and_progress(
    composer_test_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from structlog.testing import capture_logs

    secondary_secret = "cancel-cleanup-secret-must-not-be-logged"  # secret-scan: allow-this-line

    class _BlockingPlanner:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def plan_guided_full_pipeline(self, **_kwargs):
            self.started.set()
            await asyncio.Event().wait()

    async def fail_cleanup(_command):
        raise RuntimeError(secondary_secret)

    planner = _BlockingPlanner()
    composer_test_client.app.state.composer_service = planner
    monkeypatch.setattr(
        composer_test_client.app.state.session_service,
        "fail_guided_operation_with_audit",
        fail_cleanup,
    )
    session = composer_test_client.post("/api/sessions", json={"title": "guided cancel cleanup"}).json()
    operation_id = "00000000-0000-4000-8000-000000000076"

    async def cancel_request() -> None:
        async with AsyncClient(transport=ASGITransport(app=composer_test_client.app), base_url="http://test") as client:
            request_task = asyncio.create_task(
                client.post(
                    f"/api/sessions/{session['id']}/guided/plan",
                    json={"operation_id": operation_id, "intent": "Cancel while preserving the primary outcome."},
                )
            )
            await asyncio.wait_for(planner.started.wait(), timeout=3)
            request_task.cancel("primary caller cancellation")
            with pytest.raises(asyncio.CancelledError, match="primary caller cancellation"):
                await request_task

    with capture_logs() as logs:
        asyncio.run(cancel_request())

    secondary_events = [event for event in logs if event.get("event") == "guided.plan_failure_settlement_secondary_failure"]
    assert len(secondary_events) == 1
    assert secondary_events[0]["primary_failure_code"] == "request_cancelled"
    assert secondary_events[0]["secondary_exc_class"] == "RuntimeError"
    assert secondary_secret not in str(secondary_events)
    _assert_guided_plan_terminal_progress(
        composer_test_client,
        session=session,
        operation_id=operation_id,
        phase="cancelled",
        reason="client_cancelled",
    )


def test_guided_full_cancellation_fence_loss_checks_for_a_winner_before_preserving_cancellation(
    composer_test_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from structlog.testing import capture_logs

    class _BlockingPlanner:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def plan_guided_full_pipeline(self, **_kwargs):
            self.started.set()
            await asyncio.Event().wait()

    planner = _BlockingPlanner()
    real_reserve = reserve_or_replay_guided_operation
    winner_lookups = 0

    async def lose_failure_fence(command):
        raise GuidedOperationFenceLostError(command.fence)

    async def no_winner_lookup(**kwargs):
        nonlocal winner_lookups
        if kwargs.get("reserve_if_absent") is False:
            winner_lookups += 1
            return None
        return await real_reserve(**kwargs)

    composer_test_client.app.state.composer_service = planner
    monkeypatch.setattr(
        composer_test_client.app.state.session_service,
        "fail_guided_operation_with_audit",
        lose_failure_fence,
    )
    monkeypatch.setattr(guided_plan_route, "reserve_or_replay_guided_operation", no_winner_lookup)
    session = composer_test_client.post("/api/sessions", json={"title": "guided cancel no winner"}).json()
    operation_id = "00000000-0000-4000-8000-000000000080"

    async def cancel_request() -> None:
        async with AsyncClient(transport=ASGITransport(app=composer_test_client.app), base_url="http://test") as client:
            request_task = asyncio.create_task(
                client.post(
                    f"/api/sessions/{session['id']}/guided/plan",
                    json={"operation_id": operation_id, "intent": "Cancel after the failure fence is lost."},
                )
            )
            await asyncio.wait_for(planner.started.wait(), timeout=3)
            request_task.cancel("primary cancellation survives fence loss")
            with pytest.raises(asyncio.CancelledError, match="primary cancellation survives fence loss"):
                await request_task

    with capture_logs() as logs:
        asyncio.run(cancel_request())

    assert winner_lookups == 1
    secondary_events = [event for event in logs if event.get("event") == "guided.plan_failure_settlement_secondary_failure"]
    assert len(secondary_events) == 1
    assert secondary_events[0]["site"] == "fence_lost_no_winner"
    _assert_guided_plan_terminal_progress(
        composer_test_client,
        session=session,
        operation_id=operation_id,
        phase="cancelled",
        reason="client_cancelled",
    )


def test_guided_full_takeover_fences_stale_worker_and_joins_one_winner(
    composer_test_client,
) -> None:
    original_planner = composer_test_client.app.state.composer_service

    class _ControlledPlanner:
        def __init__(self) -> None:
            self.calls = 0
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()
            self.takeover_started = asyncio.Event()

        async def plan_guided_full_pipeline(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                self.first_started.set()
                await self.release_first.wait()
            else:
                self.takeover_started.set()
            return await original_planner.plan_guided_full_pipeline(**kwargs)

    planner = _ControlledPlanner()
    composer_test_client.app.state.composer_service = planner
    session = composer_test_client.post("/api/sessions", json={"title": "guided full takeover"}).json()
    operation_id = "00000000-0000-4000-8000-000000000017"
    body = {"operation_id": operation_id, "intent": "One exact winner."}
    engine = composer_test_client.app.state.session_engine

    async def race() -> tuple[Response, Response]:
        async with AsyncClient(
            transport=ASGITransport(app=composer_test_client.app),
            base_url="http://test",
        ) as client:
            stale = asyncio.create_task(client.post(f"/api/sessions/{session['id']}/guided/plan", json=body))
            await asyncio.wait_for(planner.first_started.wait(), timeout=3)
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE guided_operations SET lease_expires_at = :expired "
                        "WHERE session_id = :session_id AND operation_id = :operation_id"
                    ),
                    {
                        "expired": datetime.now(UTC) - timedelta(seconds=1),
                        "session_id": session["id"],
                        "operation_id": operation_id,
                    },
                )
            winner = asyncio.create_task(client.post(f"/api/sessions/{session['id']}/guided/plan", json=body))
            await asyncio.wait_for(planner.takeover_started.wait(), timeout=3)
            winner_response = await asyncio.wait_for(winner, timeout=3)
            planner.release_first.set()
            stale_response = await asyncio.wait_for(stale, timeout=3)
            return stale_response, winner_response

    stale_response, winner_response = asyncio.run(race())

    assert stale_response.status_code == winner_response.status_code == 200
    assert stale_response.json() == winner_response.json()
    assert planner.calls == 2
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(composition_states_table)) == 1
        assert conn.scalar(select(func.count()).select_from(chat_messages_table)) == 1
        assert conn.scalar(select(func.count()).select_from(proposal_events_table)) == 1
        assert conn.scalar(select(func.count()).select_from(composition_proposals_table)) == 1
        operation = (
            conn.execute(select(guided_operations_table).where(guided_operations_table.c.operation_id == operation_id)).mappings().one()
        )
    assert operation["status"] == "completed"
    assert operation["attempt"] == 2


def test_late_older_guided_plan_progress_cannot_overwrite_the_newer_operation(
    composer_test_client,
) -> None:
    original_planner = composer_test_client.app.state.composer_service

    class _OrderedPlanner:
        def __init__(self) -> None:
            self.calls = 0
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()

        async def plan_guided_full_pipeline(self, **kwargs):
            self.calls += 1
            call = self.calls
            await kwargs["progress"](
                ComposerProgressEvent(
                    phase="calling_model",
                    headline="The guided planner is preparing a proposal.",
                    evidence=("A bounded test provider call is active.",),
                )
            )
            if call == 1:
                self.first_started.set()
                await self.release_first.wait()
            return await original_planner.plan_guided_full_pipeline(**kwargs)

    planner = _OrderedPlanner()
    composer_test_client.app.state.composer_service = planner
    session = composer_test_client.post("/api/sessions", json={"title": "guided progress custody"}).json()
    older_operation_id = "00000000-0000-4000-8000-000000000071"
    newer_operation_id = "00000000-0000-4000-8000-000000000072"

    async def race() -> tuple[int, int]:
        async with AsyncClient(
            transport=ASGITransport(app=composer_test_client.app),
            base_url="http://test",
        ) as client:
            older = asyncio.create_task(
                client.post(
                    f"/api/sessions/{session['id']}/guided/plan",
                    json={"operation_id": older_operation_id, "intent": "Build the older proposal."},
                )
            )
            await asyncio.wait_for(planner.first_started.wait(), timeout=3)
            newer = await asyncio.wait_for(
                client.post(
                    f"/api/sessions/{session['id']}/guided/plan",
                    json={"operation_id": newer_operation_id, "intent": "Build the newer proposal."},
                ),
                timeout=3,
            )
            latest_after_newer = await composer_test_client.app.state.composer_progress_registry.get_latest(session["id"])
            assert latest_after_newer.request_id == newer_operation_id
            assert latest_after_newer.phase == "complete"

            planner.release_first.set()
            older_response = await asyncio.wait_for(older, timeout=3)
            return older_response.status_code, newer.status_code

    older_status, newer_status = asyncio.run(race())

    assert newer_status == 200
    assert older_status != 200
    _assert_guided_plan_terminal_progress(
        composer_test_client,
        session=session,
        operation_id=newer_operation_id,
        phase="complete",
        reason="composer_complete",
    )

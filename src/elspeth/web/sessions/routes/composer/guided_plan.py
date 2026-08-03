"""Retry-safe guided-full pipeline planning controller."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
from uuid import UUID, uuid4

from elspeth.contracts.blobs import (
    BlobContentMissingError,
    BlobError,
    BlobGuidedOperationFenceLostError,
    BlobIntegrityError,
    BlobQuotaExceededError,
)
from elspeth.contracts.composer_progress import ComposerProgressEvent, ComposerProgressSink
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.pipeline_planner import GuidedPlannerDecline, PipelinePlannerError, PlannerOriginatingMessage
from elspeth.web.composer.pipeline_proposal import PlannerSurface, PresentBase, composition_content_hash
from elspeth.web.composer.progress import client_cancelled_progress_event
from elspeth.web.composer.proposals import build_tool_proposal_summary
from elspeth.web.composer.protocol import ComposerServiceError
from elspeth.web.composer.redaction import redact_tool_call_arguments
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.sessions.guided_replay import project_composition_proposal, project_guided_full_decline
from elspeth.web.sessions.protocol import (
    CompositionStateData,
    GuidedAuditEvidence,
    GuidedDeclinedResult,
    GuidedFullPipelineDeclineCommand,
    GuidedFullPipelineProposalStageCommand,
    GuidedOperationFailed,
    GuidedOperationFailureCode,
    GuidedOperationFailureCommand,
    GuidedOperationFenceLostError,
    GuidedOperationSettlementConflictError,
    GuidedOriginatingUserMessageDraft,
    GuidedPipelineProposalResult,
)
from elspeth.web.sessions.schemas import CompositionProposalResponse, GuidedPlanDeclinedResponse, GuidedPlanRequest

from .._helpers import (
    APIRouter,
    ComposerRateLimiter,
    Depends,
    HTTPException,
    Request,
    SessionServiceProtocol,
    UserIdentity,
    _cancel_on_client_disconnect,
    _failure_log_request_id,
    _get_composer_progress_registry,
    _get_session_compose_lock_registry,
    _is_client_disconnect_cancel,
    _request_plugin_policy_context,
    _safe_frame_strings,
    _state_from_record,
    _track_compose_inflight,
    _verify_session_ownership,
    get_current_user,
    get_rate_limiter,
    planner_failure_is_policy_blocked,
    slog,
)
from ..guided_operations import (
    GuidedOperationExpired,
    GuidedOperationLease,
    raise_guided_operation_failure,
    reserve_or_replay_guided_operation,
)
from .pipeline_settlement import (
    _GUIDED_ATOMIC_SETTLEMENT_COMPLETED,
    _GUIDED_ATOMIC_SETTLEMENT_FAILURE,
    _await_guided_atomic_settlement,
    _await_with_deferred_cancellation,
)

router = APIRouter()

# Escape-hatch decline text is the advisor's own free-text reply; an empty
# reply is theoretically possible (a hatch-turn response with no tool calls
# and blank content) but chat_messages forbids a contentless assistant row
# with no raw_content/tool_calls (_assert_assistant_row_has_audit_content).
# Mirrors the freeform surface's identical fallback in
# ComposerServiceImpl.compose (service.py, PlannerDeclined handler).
_EMPTY_DECLINE_FALLBACK = "I could not find a way to build this pipeline with the available components."


def _guided_full_complete_progress_event(*, declined: bool = False) -> ComposerProgressEvent:
    if declined:
        return ComposerProgressEvent(
            phase="complete",
            headline="The guided planner finished without proposing a pipeline.",
            evidence=("The guided decline was saved as an assistant response.",),
            likely_next="Review the reply and revise the request if you want to try again.",
            reason="composer_complete",
        )
    return ComposerProgressEvent(
        phase="complete",
        headline="The guided pipeline proposal is ready for review.",
        evidence=("The proposal and its audit evidence were settled atomically.",),
        likely_next="Review the proposed pipeline before accepting it.",
        reason="composer_complete",
    )


def _guided_full_failed_progress_event(failure_code: GuidedOperationFailureCode) -> ComposerProgressEvent:
    provider_failure = failure_code in {
        "invalid_provider_response",
        "provider_timeout",
        "provider_unavailable",
    }
    if failure_code == "policy_blocked":
        likely_next = "Change the policy-blocked pipeline component before submitting a new guided request."
    elif failure_code == "quota_exceeded":
        likely_next = "Remove stored session data or ask an operator to raise the configured storage quota."
    elif failure_code == "stale_conflict":
        likely_next = "Refresh the current session state before submitting a new guided request."
    elif failure_code in {"custody_error", "integrity_error"}:
        likely_next = "Restore the authoritative input or session state before retrying the guided request."
    elif provider_failure:
        likely_next = "Retry when the composer provider is available, or revise the request if the response remains unusable."
    else:
        likely_next = "Review the error response before retrying the guided request."
    return ComposerProgressEvent(
        phase="failed",
        headline=(
            "The guided planner could not produce a usable pipeline proposal."
            if provider_failure
            else "The guided pipeline request could not be completed."
        ),
        evidence=("The guided operation was settled with a safe failure classification.",),
        likely_next=likely_next,
        reason="provider_unavailable" if provider_failure else "service_setup_failed",
    )


def _note_guided_full_secondary_failure(
    *,
    request: Request,
    primary_failure_code: str,
    secondary: BaseException,
    site: str,
) -> None:
    """Record bounded cleanup diagnostics without replacing the primary outcome."""
    with contextlib.suppress(Exception):
        slog.error(
            "guided.plan_failure_settlement_secondary_failure",
            primary_failure_code=primary_failure_code,
            secondary_exc_class=type(secondary).__name__,
            site=site,
            frames=_safe_frame_strings(secondary),
            request_id=_failure_log_request_id(request),
        )


async def _publish_guided_full_terminal_preserving_primary(
    *,
    request: Request,
    progress: ComposerProgressSink,
    primary_outcome: str,
    event: ComposerProgressEvent,
) -> None:
    """Terminalize progress without allowing UI cleanup to replace the primary.

    This runs only after a fence failure or durable winner has established the
    authoritative outcome. A ``CancelledError`` raised by this secondary sink
    is therefore cleanup failure, not the route's primary cancellation. Keep
    the catch explicit: other ``BaseException`` subclasses must still escape.
    """
    try:
        await progress(event)
    except (asyncio.CancelledError, Exception) as progress_exc:
        _note_guided_full_secondary_failure(
            request=request,
            primary_failure_code=primary_outcome,
            secondary=progress_exc,
            site="terminal_progress",
        )


def _guided_full_failure_code(exc: BaseException) -> GuidedOperationFailureCode:
    if isinstance(exc, GuidedOperationSettlementConflictError):
        return "stale_conflict"
    if isinstance(exc, AuditIntegrityError):
        return "integrity_error"
    if isinstance(exc, BlobIntegrityError | BlobContentMissingError):
        return "integrity_error"
    if isinstance(exc, BlobQuotaExceededError):
        return "quota_exceeded"
    if isinstance(exc, BlobError):
        return "custody_error"
    if isinstance(exc, ComposerServiceError):
        return "provider_unavailable"
    if isinstance(exc, PipelinePlannerError):
        # Detail codes FIRST: a categorical deployment-policy refusal is
        # permanent and arrives under whichever planner code exhausted
        # (VALIDATION_FAILED from the server-derived gate, REPAIR_EXHAUSTED when
        # the model kept re-authoring the prohibited component). Collapsing it
        # into ``invalid_provider_response`` blamed the provider for a policy
        # decision and told the user to retry an operation that can never
        # succeed. Shared predicate with the freeform mirror so the two
        # surfaces cannot drift.
        if planner_failure_is_policy_blocked(exc):
            return "policy_blocked"
        if exc.code == "TIMEOUT":
            return "provider_timeout"
        if exc.code == "PROVIDER_ERROR":
            return "provider_unavailable"
        if exc.code in {
            "COMPLETION_TOKENS_EXCEEDED",
            "COMPOSITION_EXHAUSTED",
            "COST_UNAVAILABLE",
            "DISCOVERY_CYCLE",
            "DISCOVERY_EXHAUSTED",
            "DISCOVERY_ONLY",
            "MALFORMED_RESPONSE",
            "PROVIDER_CALLS_EXHAUSTED",
            "REPAIR_EXHAUSTED",
            "RESPONSE_TRUNCATED",
            "TOOL_CALLS_EXHAUSTED",
            "VALIDATION_FAILED",
        }:
            return "invalid_provider_response"
    return "operation_failed"


@router.post("/{session_id}/guided/plan", response_model=CompositionProposalResponse | GuidedPlanDeclinedResponse)
async def post_guided_plan(
    session_id: UUID,
    body: GuidedPlanRequest,
    request: Request,
    user: UserIdentity = Depends(get_current_user),  # noqa: B008
    rate_limiter: ComposerRateLimiter = Depends(get_rate_limiter),  # noqa: B008
    _inflight_tally: None = Depends(_track_compose_inflight),
) -> CompositionProposalResponse | GuidedPlanDeclinedResponse:
    """Plan and atomically stage one full guided proposal.

    Two outcomes: a reviewable ``CompositionProposalResponse`` (the ordinary
    case), or a ``GuidedPlanDeclinedResponse`` when the escape-hatch advisor
    answers in text instead of proposing a pipeline — an honest decline is a
    conversational outcome, not a provider failure, so it settles the
    operation as completed rather than failed (mirrors the freeform
    surface's identical PlannerDeclined handling).
    """

    await rate_limiter.check(user.user_id)
    await _verify_session_ownership(session_id, user, request)
    service: SessionServiceProtocol = request.app.state.session_service

    async def replay(result: object) -> CompositionProposalResponse | GuidedPlanDeclinedResponse:
        if type(result) is GuidedDeclinedResult:
            checkpoint = await service.get_state_in_session(result.checkpoint_state_id, session_id)
            decline_rows = [
                message for message in await service.get_messages(session_id, limit=None) if message.id == result.decline_message_id
            ]
            if (
                len(decline_rows) != 1
                or decline_rows[0].session_id != session_id
                or decline_rows[0].composition_state_id != checkpoint.id
                or decline_rows[0].role != "assistant"
                or decline_rows[0].writer_principal != "compose_loop"
            ):
                raise AuditIntegrityError("guided-full decline replay has a malformed assistant message locator")
            return project_guided_full_decline(decline_rows[0])
        if type(result) is not GuidedPipelineProposalResult:
            raise AuditIntegrityError("guided-full replay has an unsupported result locator")
        checkpoint = await service.get_state_in_session(result.checkpoint_state_id, session_id)
        authority = await service.get_authoritative_pipeline_proposal(
            session_id=session_id,
            proposal_id=result.proposal_id,
            reviewed_facts={},
        )
        if (
            authority.proposal.surface is not PlannerSurface.GUIDED_FULL
            or type(authority.proposal.base) is not PresentBase
            or authority.proposal.base.state_id != result.checkpoint_state_id
            or authority.proposal.base.composition_content_hash != composition_content_hash(_state_from_record(checkpoint))
            or authority.row.base_state_id != result.checkpoint_state_id
            or authority.row.user_message_id is None
        ):
            raise AuditIntegrityError("guided-full replay authority differs from its operation locator")
        origin_rows = [
            message for message in await service.get_messages(session_id, limit=None) if message.id == authority.row.user_message_id
        ]
        if (
            len(origin_rows) != 1
            or origin_rows[0].session_id != session_id
            or origin_rows[0].role != "user"
            or origin_rows[0].writer_principal != "route_user_message"
            or origin_rows[0].content != body.intent
            or origin_rows[0].composition_state_id != checkpoint.id
        ):
            raise AuditIntegrityError("guided-full replay originating message authority changed")
        if authority.row.pipeline_metadata is None or authority.row.pipeline_metadata.draft_hash != authority.proposal.draft_hash:
            raise AuditIntegrityError("guided-full replay proposal metadata changed")
        creation_record = replace(
            authority.row,
            status="pending",
            committed_state_id=None,
            audit_event_id=authority.creation_event_id,
            updated_at=authority.row.created_at,
        )
        return project_composition_proposal(creation_record)

    reserved = await reserve_or_replay_guided_operation(
        service=service,
        session_id=session_id,
        kind="guided_plan",
        request=body,
        replay=replay,
    )
    if reserved is None:  # pragma: no cover - reserve defaults to true
        raise AuditIntegrityError("guided-full operation was not reserved")
    if not isinstance(reserved, GuidedOperationLease):
        await _get_composer_progress_registry(request).publish_replay_if_unclaimed(
            session_id=str(session_id),
            request_id=body.operation_id,
            user_id=user.user_id,
            event=_guided_full_complete_progress_event(
                declined=type(reserved) is GuidedPlanDeclinedResponse,
            ),
        )
        return reserved

    recorder = BufferingRecorder()
    progress = _get_composer_progress_registry(request).bind_request(
        session_id=str(session_id),
        request_id=body.operation_id,
        user_id=user.user_id,
    )
    try:
        catalog, plugin_snapshot = _request_plugin_policy_context(request, user)
        compose_lock = await _get_session_compose_lock_registry(request).get_lock(str(session_id))
        async with compose_lock:
            observed_record = await service.get_current_state(session_id)
            observed_state = (
                _state_from_record(observed_record)
                if observed_record is not None
                else CompositionState(
                    source=None,
                    nodes=(),
                    edges=(),
                    outputs=(),
                    metadata=PipelineMetadata(),
                    version=1,
                )
            )
            expected_state_id = observed_record.id if observed_record is not None else None
            expected_state_version = observed_record.version if observed_record is not None else None
            expected_content_hash = composition_content_hash(observed_state) if observed_record is not None else None
            checkpoint_id = uuid4()
            proposal_id = uuid4()
            origin = GuidedOriginatingUserMessageDraft(message_id=uuid4(), content=body.intent)
            fence = await service.renew_guided_operation(
                reserved.fence,
                actor="composer_route",
                lease_seconds=300,
            )

        state_dict = observed_state.to_dict()
        checkpoint_data = CompositionStateData(
            sources=state_dict["sources"],
            nodes=state_dict["nodes"],
            edges=state_dict["edges"],
            outputs=state_dict["outputs"],
            metadata_=state_dict["metadata"],
            is_valid=observed_record.is_valid if observed_record is not None else False,
            validation_errors=observed_record.validation_errors if observed_record is not None else None,
            composer_meta=observed_record.composer_meta if observed_record is not None else None,
        )

        async with _cancel_on_client_disconnect(request):
            outcome = await request.app.state.composer_service.plan_guided_full_pipeline(
                intent=body.intent,
                current_state=observed_state,
                originating_message=PlannerOriginatingMessage(
                    session_id=str(session_id),
                    message_id=str(origin.message_id),
                    content=origin.content,
                    user_id=user.user_id,
                ),
                base=PresentBase(
                    state_id=checkpoint_id,
                    composition_content_hash=composition_content_hash(observed_state),
                ),
                policy_catalog=catalog,
                plugin_snapshot=plugin_snapshot,
                recorder=recorder,
                operation_fence=fence,
                progress=progress,
            )

        if isinstance(outcome, GuidedPlannerDecline):
            # Honest decline from the escape-hatch advisor turn: a
            # successful conversational outcome, not a planner failure.
            # Persist the advisor's own words as an ordinary assistant
            # message and complete the operation — never
            # GuidedOperationFailureCode (mirrors the freeform surface's
            # identical PlannerDeclined handling in ComposerServiceImpl).
            decline_text = outcome.decline_text.strip() or _EMPTY_DECLINE_FALLBACK
            async with compose_lock:
                renewed_fence = await service.renew_guided_operation(
                    fence,
                    actor="composer_route",
                    lease_seconds=300,
                )
                decline_settlement = await _await_guided_atomic_settlement(
                    service.decline_guided_full_pipeline_proposal(
                        GuidedFullPipelineDeclineCommand(
                            fence=renewed_fence,
                            expected_current_state_id=expected_state_id,
                            expected_current_state_version=expected_state_version,
                            expected_current_content_hash=expected_content_hash,
                            checkpoint_state_id=checkpoint_id,
                            state=checkpoint_data,
                            decline_text=decline_text,
                            actor="composer_route",
                            originating_message=origin,
                            audit_evidence=GuidedAuditEvidence(
                                invocations=recorder.invocations,
                                llm_calls=recorder.llm_calls,
                                chat_turns=recorder.chat_turns,
                            ),
                        )
                    )
                )
            decline_response = project_guided_full_decline(decline_settlement.decline_message)
            await progress(_guided_full_complete_progress_event(declined=True))
            return decline_response

        plan, _catalog_ids = outcome
        redacted = redact_tool_call_arguments(
            "set_pipeline",
            deep_thaw(plan.proposal.pipeline),
            telemetry=NoopRedactionTelemetry(),
        )
        summary = build_tool_proposal_summary(
            tool_name="set_pipeline",
            arguments=deep_thaw(plan.proposal.pipeline),
            redacted_arguments=redacted,
        )
        async with compose_lock:
            renewed_fence = await service.renew_guided_operation(
                fence,
                actor="composer_route",
                lease_seconds=300,
            )
            settlement = await _await_guided_atomic_settlement(
                service.stage_guided_full_pipeline_proposal(
                    GuidedFullPipelineProposalStageCommand(
                        fence=renewed_fence,
                        expected_current_state_id=expected_state_id,
                        expected_current_state_version=expected_state_version,
                        expected_current_content_hash=expected_content_hash,
                        checkpoint_state_id=checkpoint_id,
                        proposal_id=proposal_id,
                        state=checkpoint_data,
                        plan=plan,
                        summary=summary.summary,
                        rationale=summary.rationale,
                        affects=summary.affects,
                        arguments_redacted_json=summary.arguments_redacted_json,
                        actor="composer_route",
                        originating_message=origin,
                        audit_evidence=GuidedAuditEvidence(
                            invocations=recorder.invocations,
                            llm_calls=recorder.llm_calls,
                            chat_turns=recorder.chat_turns,
                        ),
                    )
                )
            )
        proposal_response = project_composition_proposal(settlement.proposal)
        await progress(_guided_full_complete_progress_event())
        return proposal_response
    except (GuidedOperationFenceLostError, BlobGuidedOperationFenceLostError) as exc:
        failure_code = _guided_full_failure_code(exc)
        lookup_failed = False
        try:
            joined = await reserve_or_replay_guided_operation(
                service=service,
                session_id=session_id,
                kind="guided_plan",
                request=body,
                replay=replay,
                reserve_if_absent=False,
                takeover_expired=False,
            )
        except Exception as lookup_exc:
            lookup_failed = True
            _note_guided_full_secondary_failure(
                request=request,
                primary_failure_code=failure_code,
                secondary=lookup_exc,
                site="main_fence_winner_lookup",
            )
            joined = None
        if lookup_failed:
            await _publish_guided_full_terminal_preserving_primary(
                request=request,
                progress=progress,
                primary_outcome=failure_code,
                event=_guided_full_failed_progress_event(failure_code),
            )
            raise
        if joined is None or isinstance(joined, (GuidedOperationLease, GuidedOperationExpired)):
            _note_guided_full_secondary_failure(
                request=request,
                primary_failure_code=failure_code,
                secondary=exc,
                site="main_fence_lost_no_winner",
            )
            await _publish_guided_full_terminal_preserving_primary(
                request=request,
                progress=progress,
                primary_outcome=failure_code,
                event=_guided_full_failed_progress_event(failure_code),
            )
            raise
        await _publish_guided_full_terminal_preserving_primary(
            request=request,
            progress=progress,
            primary_outcome="durable_complete",
            event=_guided_full_complete_progress_event(
                declined=type(joined) is GuidedPlanDeclinedResponse,
            ),
        )
        return joined
    except asyncio.CancelledError as exc:
        if exc.__dict__.get(_GUIDED_ATOMIC_SETTLEMENT_COMPLETED) is True:
            try:
                (joined, _cancelled_during_replay) = await _await_with_deferred_cancellation(
                    reserve_or_replay_guided_operation(
                        service=service,
                        session_id=session_id,
                        kind="guided_plan",
                        request=body,
                        replay=replay,
                        reserve_if_absent=False,
                        takeover_expired=False,
                    )
                )
            except Exception as replay_exc:
                _note_guided_full_secondary_failure(
                    request=request,
                    primary_failure_code="durable_complete",
                    secondary=replay_exc,
                    site="completed_cancellation_replay",
                )
                joined = None
            await _await_with_deferred_cancellation(
                progress(
                    _guided_full_complete_progress_event(
                        declined=type(joined) is GuidedPlanDeclinedResponse,
                    )
                )
            )
            raise
        settlement_failure = exc.__dict__.get(_GUIDED_ATOMIC_SETTLEMENT_FAILURE)
        caller_task = asyncio.current_task()
        caller_cancelled = caller_task is not None and caller_task.cancelling() > 0
        disconnected = _is_client_disconnect_cancel(exc)
        cancel_failure_code: GuidedOperationFailureCode = (
            _guided_full_failure_code(settlement_failure)
            if settlement_failure is not None
            else "request_cancelled"
            if disconnected or caller_cancelled
            else "operation_failed"
        )
        failed: GuidedOperationFailed | None = None
        joined_winner: CompositionProposalResponse | GuidedPlanDeclinedResponse | None = None
        try:
            (failed, _cancelled_during_failure_settlement) = await _await_with_deferred_cancellation(
                service.fail_guided_operation_with_audit(
                    GuidedOperationFailureCommand(
                        fence=reserved.fence,
                        failure_code=cancel_failure_code,
                        actor="composer_route",
                        audit_evidence=GuidedAuditEvidence(
                            invocations=recorder.invocations,
                            llm_calls=recorder.llm_calls,
                            chat_turns=recorder.chat_turns,
                        ),
                    )
                )
            )
        except GuidedOperationFenceLostError as fence_lost:
            try:
                (joined, _cancelled_during_winner_lookup) = await _await_with_deferred_cancellation(
                    reserve_or_replay_guided_operation(
                        service=service,
                        session_id=session_id,
                        kind="guided_plan",
                        request=body,
                        replay=replay,
                        reserve_if_absent=False,
                        takeover_expired=False,
                    )
                )
            except Exception as lookup_exc:
                _note_guided_full_secondary_failure(
                    request=request,
                    primary_failure_code=cancel_failure_code,
                    secondary=lookup_exc,
                    site="fence_lost_winner_lookup",
                )
            else:
                if joined is None or isinstance(joined, (GuidedOperationLease, GuidedOperationExpired)):
                    _note_guided_full_secondary_failure(
                        request=request,
                        primary_failure_code=cancel_failure_code,
                        secondary=fence_lost,
                        site="fence_lost_no_winner",
                    )
                else:
                    joined_winner = joined
        except Exception as cleanup_exc:
            _note_guided_full_secondary_failure(
                request=request,
                primary_failure_code=cancel_failure_code,
                secondary=cleanup_exc,
                site="failure_settlement",
            )
        terminal_event = (
            _guided_full_complete_progress_event(
                declined=type(joined_winner) is GuidedPlanDeclinedResponse,
            )
            if joined_winner is not None
            else client_cancelled_progress_event()
            if cancel_failure_code == "request_cancelled"
            else _guided_full_failed_progress_event(cancel_failure_code)
        )
        await _await_with_deferred_cancellation(progress(terminal_event))
        if settlement_failure is not None:
            raise exc from settlement_failure
        if disconnected:
            raise HTTPException(
                status_code=499,
                detail="Client disconnected while guided full planning was running.",
            ) from exc
        if caller_cancelled:
            raise
        if joined_winner is not None:
            return joined_winner
        raise_guided_operation_failure(failed or GuidedOperationFailed(failure_code=cancel_failure_code))
    except Exception as exc:
        failure_code = _guided_full_failure_code(exc)
        try:
            failed = await service.fail_guided_operation_with_audit(
                GuidedOperationFailureCommand(
                    fence=reserved.fence,
                    failure_code=failure_code,
                    actor="composer_route",
                    audit_evidence=GuidedAuditEvidence(
                        invocations=recorder.invocations,
                        llm_calls=recorder.llm_calls,
                        chat_turns=recorder.chat_turns,
                    ),
                )
            )
        except GuidedOperationFenceLostError as fence_lost:
            try:
                joined = await reserve_or_replay_guided_operation(
                    service=service,
                    session_id=session_id,
                    kind="guided_plan",
                    request=body,
                    replay=replay,
                    reserve_if_absent=False,
                    takeover_expired=False,
                )
            except Exception as lookup_exc:
                _note_guided_full_secondary_failure(
                    request=request,
                    primary_failure_code=failure_code,
                    secondary=lookup_exc,
                    site="fence_lost_winner_lookup",
                )
                await progress(_guided_full_failed_progress_event(failure_code))
                raise_guided_operation_failure(GuidedOperationFailed(failure_code=failure_code))
            if joined is None or isinstance(joined, (GuidedOperationLease, GuidedOperationExpired)):
                _note_guided_full_secondary_failure(
                    request=request,
                    primary_failure_code=failure_code,
                    secondary=fence_lost,
                    site="fence_lost_no_winner",
                )
                await progress(_guided_full_failed_progress_event(failure_code))
                raise_guided_operation_failure(GuidedOperationFailed(failure_code=failure_code))
            await progress(
                _guided_full_complete_progress_event(
                    declined=type(joined) is GuidedPlanDeclinedResponse,
                )
            )
            return joined
        except Exception as cleanup_exc:
            _note_guided_full_secondary_failure(
                request=request,
                primary_failure_code=failure_code,
                secondary=cleanup_exc,
                site="failure_settlement",
            )
            await progress(_guided_full_failed_progress_event(failure_code))
            raise_guided_operation_failure(GuidedOperationFailed(failure_code=failure_code))
        await progress(_guided_full_failed_progress_event(failure_code))
        raise_guided_operation_failure(failed)


__all__ = ["router"]

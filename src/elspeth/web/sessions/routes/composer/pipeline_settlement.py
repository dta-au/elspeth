"""Canonical pipeline settlement shared by manual and automatic approval.

Callers must already hold the session compose lock. Trust mode changes who
invokes this coordinator, never the prepare/audit/atomic-settlement sequence.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

import structlog

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.compartments import ChatIngressInput, compartment_ingress_record
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.pipeline_commit import (
    PipelineCommitConfig,
    PipelineCommitError,
    RecoveredPipelineCommit,
    prepare_pipeline_proposal_commit,
)
from elspeth.web.composer.protocol import PipelineCommitIntent
from elspeth.web.composer.state import ValidationSummary
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
    RequiredWorkSource,
    RequiredWorkTicket,
    required_failure_leaves,
)
from elspeth.web.sessions.composer_app_services import ComposerAppServices
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.pipeline_finish_once import (
    ComposerPipelineBusinessReturned,
    ComposerPipelineRaised,
    ComposerPipelineRevocationCompleted,
)
from elspeth.web.sessions.pipeline_rejection import PipelineRejectionExpected
from elspeth.web.sessions.pipeline_rejection_custody import original_outcome_group, reject_pipeline_with_required_custody
from elspeth.web.sessions.protocol import (
    AuthoritativePipelineProposal,
    ChatMessageRecord,
    ComposerTrustMode,
    CompositionProposalRecord,
    CompositionStateRecord,
    PipelineDispatchRecovery,
    PipelineProposalRejectionReason,
    PipelineProposalSettlementResult,
    TrustModeAutoCommitRevokedError,
)

from .._helpers import (
    HTTPException,
    SessionServiceProtocol,
    _chat_ingress_inputs,
    _initial_composition_state,
    _persist_tool_invocations,
    _state_data_from_composer_state,
    _state_from_record,
    asyncio,
    merge_composer_meta_updates,
)

slog = structlog.get_logger()


@dataclass(slots=True)
class _DeferredCancellationState:
    cancellations: tuple[asyncio.CancelledError, ...] = ()

    @property
    def requested(self) -> bool:
        return bool(self.cancellations)

    def raise_if_requested(self) -> None:
        if len(self.cancellations) == 1:
            raise self.cancellations[0]
        if self.cancellations:
            raise BaseExceptionGroup("Pipeline deferred cancellations", list(self.cancellations))

    def retain(self, error: asyncio.CancelledError) -> None:
        if all(error is not existing for existing in self.cancellations):
            self.cancellations += (error,)


@dataclass(frozen=True, slots=True)
class PipelineRouteSettlement:
    settlement: PipelineProposalSettlementResult
    validation: ValidationSummary | None


async def _await_with_deferred_cancellation[T](
    awaitable: Awaitable[T],
    *,
    state: _DeferredCancellationState | None = None,
) -> tuple[T, bool]:
    """Finish one dispatch+settlement critical section before cancelling.

    ``run_sync_in_worker`` leaves its worker running if the request task is
    cancelled. Once an approved execution starts, the route must retain the
    session lock until the side effect is paired with a terminal proposal
    transition.
    """
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    cancellation_state = state if state is not None else _DeferredCancellationState()
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError as error:
            cancelled = True
            cancellation_state.retain(error)
            if task.done():
                return task.result(), cancelled


async def _required_preparation_read[T](
    coordinator: RequiredWorkCoordinator,
    read: Callable[[RequiredWorkTicket], Awaitable[T]],
    project: Callable[[T], None],
    *,
    state: _DeferredCancellationState | None = None,
) -> T:
    sql, projection = coordinator.reserve_pair(
        RequiredWorkSource.PREPARATION_READ_SQL,
        RequiredWorkSource.PREPARATION_READ_PROJECTION,
        transition_ordinal=0,
        semantic_ordinal=0,
    )
    cancellations = state if state is not None else _DeferredCancellationState()
    try:
        operation = read(sql)
    except BaseException as error:
        sql.complete_without_submission(error)
        projection.complete_without_submission()
        raise
    try:
        result, _ = await _await_with_deferred_cancellation(operation, state=cancellations)
    except BaseException as error:
        if sql.complete:
            projection.complete_without_submission()
        if cancellations.requested:
            raise BaseExceptionGroup("Preparation SQL and original cancellation", [error, *cancellations.cancellations]) from None
        raise
    projection.begin_projection()
    try:
        project(result)
    except BaseException as error:
        projection.complete_owned(error)
        raise
    projection.complete_owned()
    if state is None:
        cancellations.raise_if_requested()
    return result


def _project_preparation_state(value: CompositionStateRecord | None, session_id: UUID, *, required: bool = False) -> None:
    if value is None and not required:
        return
    if type(value) is not CompositionStateRecord or value.session_id != session_id:
        raise AuditIntegrityError("Pipeline preparation state belongs to another authority")


def _project_preparation_messages(value: list[ChatMessageRecord], session_id: UUID) -> None:
    if type(value) is not list or any(type(message) is not ChatMessageRecord or message.session_id != session_id for message in value):
        raise AuditIntegrityError("Pipeline preparation messages belong to another authority")


def _project_preparation_recovery(value: PipelineDispatchRecovery | None, tool_call_id: str) -> None:
    if value is not None and (type(value) is not PipelineDispatchRecovery or value.binding.tool_call_id != tool_call_id):
        raise AuditIntegrityError("Pipeline preparation recovery belongs to another tool transition")


def _project_preparation_authority(value: AuthoritativePipelineProposal, session_id: UUID, proposal_id: UUID) -> None:
    if type(value) is not AuthoritativePipelineProposal or value.row.session_id != session_id or value.row.id != proposal_id:
        raise AuditIntegrityError("Pipeline preparation proposal belongs to another authority")


def _validate_pipeline_publication_result(settled: PipelineProposalSettlementResult, proposal: CompositionProposalRecord) -> None:
    if (
        type(settled) is not PipelineProposalSettlementResult
        or settled.proposal.id != proposal.id
        or settled.proposal.session_id != proposal.session_id
        or settled.proposal.status != "committed"
        or settled.state.session_id != proposal.session_id
        or settled.accepted_state is None
        or settled.proposal.committed_state_id != settled.accepted_state.id
        or settled.accepted_state.session_id != proposal.session_id
        or (
            settled.transition_message is not None
            and (
                settled.transition_message.session_id != proposal.session_id
                or settled.transition_message.composition_state_id != settled.state.id
            )
        )
    ):
        raise AuditIntegrityError("Pipeline publication returned inconsistent committed evidence")


async def _proposal_user_message_content(
    service: SessionServiceProtocol,
    proposal: CompositionProposalRecord,
    *,
    required_work: RequiredWorkCoordinator | None = None,
    required_binding: RequiredWorkBinding | None = None,
    cancellation_state: _DeferredCancellationState | None = None,
) -> str | None:
    """Recover the immutable originating user-message body for replay."""
    if proposal.user_message_id is None:
        return None
    if required_work is None:
        messages = await service.get_messages(proposal.session_id, limit=None)
    else:
        messages = await _required_preparation_read(
            required_work,
            lambda ticket: service.get_messages(proposal.session_id, limit=None, required_work=ticket),
            lambda value: _project_preparation_messages(value, proposal.session_id),
            state=cancellation_state,
        )
    for message in messages:
        if message.id != proposal.user_message_id:
            continue
        if message.role != "user":
            raise HTTPException(
                status_code=409,
                detail="Stored proposal references a non-user originating message; ask ELSPETH to regenerate the proposal.",
            )
        return message.content
    raise HTTPException(
        status_code=409,
        detail="Stored proposal references an originating message that could not be recovered; ask ELSPETH to regenerate the proposal.",
    )


async def _proposal_chat_ingress_inputs(
    service: SessionServiceProtocol,
    proposal: CompositionProposalRecord,
    *,
    own_compartment_id: str | None,
    required_work: RequiredWorkCoordinator | None = None,
    required_binding: RequiredWorkBinding | None = None,
    cancellation_state: _DeferredCancellationState | None = None,
) -> list[ChatIngressInput]:
    """Retain all durable human inputs through this proposal's originating turn."""
    if proposal.user_message_id is None:
        return []
    if required_work is None:
        messages = await service.get_messages(proposal.session_id, limit=None)
    else:
        messages = await _required_preparation_read(
            required_work,
            lambda ticket: service.get_messages(proposal.session_id, limit=None, required_work=ticket),
            lambda value: _project_preparation_messages(value, proposal.session_id),
            state=cancellation_state,
        )
    for index, message in enumerate(messages):
        if message.id == proposal.user_message_id:
            if message.role != "user":
                raise HTTPException(status_code=409, detail="Stored proposal references a non-user originating message.")
            return list(_chat_ingress_inputs(messages[: index + 1], own_compartment_id=own_compartment_id))
    raise HTTPException(status_code=409, detail="Stored proposal references an originating message that could not be recovered.")


async def _settle_pipeline_proposal_under_compose_lock(
    *,
    services: ComposerAppServices,
    user_id: str,
    authority: AuthoritativePipelineProposal,
    draft_hash: str,
    composer_meta: Mapping[str, object] | None = None,
    telemetry_source: Literal["compose", "recompose"] = "compose",
    required_trust_mode: ComposerTrustMode | None = None,
    session_operation_context: SessionOperationContext,
    commit_timeout_seconds: float,
    running: ComposerOperationRunning | None = None,
    required_work: RequiredWorkCoordinator | None = None,
    required_binding: RequiredWorkBinding | None = None,
) -> PipelineRouteSettlement:
    """Settle one exact canonical proposal while the caller holds the lock.

    ``required_trust_mode`` threads the commit-boundary trust check into the
    settlement write transaction (elspeth-01d4c6e683): auto-commit callers
    pass ``"auto_commit"`` and receive ``TrustModeAutoCommitRevokedError``
    when a durable preference downgrade beat the settlement — the proposal
    stays pending (with its durable dispatch audit recoverable by a later
    manual approval, the same crash-between-dispatch-and-settlement state
    the recovery path already supports). Manual approval passes ``None``.
    """
    if required_work is not None:
        if type(required_work) is not RequiredWorkCoordinator:
            raise AuditIntegrityError("Pipeline settlement requires an owned required-work coordinator")
        if required_work.authority.context != session_operation_context:
            raise AuditIntegrityError("Pipeline required-work scope disagrees with context")
    service: SessionServiceProtocol = services.session_service
    proposal = authority.row
    if required_work is not None:
        if required_work.authority.proposal_id != str(proposal.id) or required_work.authority.tool_call_id != proposal.tool_call_id:
            raise RuntimeError("Pipeline required-work scope is not bound to the exact proposal")
        proposal_work = required_work
    else:
        kind = (
            RequiredAuthorityKind.MANUAL_PROPOSAL
            if session_operation_context.operation_kind is SessionOperationKind.PROPOSAL
            else RequiredAuthorityKind.SYNCHRONOUS_COMPOSE
        )
        proposal_work = RequiredWorkCoordinator(
            RequiredWorkAuthority(
                kind,
                session_operation_context,
                proposal_id=str(proposal.id),
                invocation_id=str(uuid4()),
                tool_call_id=proposal.tool_call_id,
            )
        )
    if required_binding is not None and type(required_binding) is not RequiredWorkBinding:
        raise AuditIntegrityError("Settlement requires an owned rejection binding")
    rejection_binding = (
        RequiredWorkBinding(proposal_work, 0, 0, RequiredWorkRole.TURN, running) if required_binding is None else required_binding
    )
    if rejection_binding.coordinator is not proposal_work:
        raise AuditIntegrityError("Settlement rejection binding has another actual proposal owner")
    rejection_binding.validate_context(session_operation_context)
    cancellation_state = _DeferredCancellationState()
    if draft_hash != authority.proposal.draft_hash:
        raise HTTPException(status_code=409, detail="The pipeline proposal draft hash is stale or mismatched.")
    if proposal.status == "committed":
        committed_state_id = proposal.committed_state_id
        if committed_state_id is None:
            raise RuntimeError("committed pipeline proposal has no committed state id")
        state = await _required_preparation_read(
            proposal_work,
            lambda ticket: service.get_state(committed_state_id, required_work=ticket),
            lambda value: _project_preparation_state(value, proposal.session_id, required=True),
            state=cancellation_state,
        )
        validation_ticket = proposal_work.reserve(RequiredWorkSource.PREPARATION_VALIDATION_PRODUCER)
        try:
            prepared_interpretations = services.interpretation_surfacing.prepare_pending_interpretation_reviews(_state_from_record(state))
        except BaseException as exc:
            validation_ticket.complete_owned(exc)
            raise
        validation_ticket.complete_owned()
        read_ticket = proposal_work.reserve(RequiredWorkSource.POSTCOMMIT_REVIEW_READ_SQL)
        projection_ticket = proposal_work.reserve(RequiredWorkSource.POSTCOMMIT_REVIEW_PROJECTION)
        replay_cancellation = cancellation_state
        try:
            replayed, _ = await _await_with_deferred_cancellation(
                service.replay_pipeline_composition_proposal(
                    authority=authority, prepared_interpretations=prepared_interpretations, required_work=read_ticket
                ),
                state=replay_cancellation,
            )
        except BaseException:
            if read_ticket.complete:
                projection_ticket.complete_without_submission()
            raise
        projection_ticket.begin_projection()
        try:
            if (
                type(replayed) is not PipelineProposalSettlementResult
                or replayed.proposal.id != proposal.id
                or replayed.state.id != proposal.committed_state_id
                or replayed.state.session_id != proposal.session_id
            ):
                raise AuditIntegrityError("Pipeline replay returned inconsistent committed evidence")
            outcome = PipelineRouteSettlement(settlement=replayed, validation=None)
        except BaseException as error:
            projection_ticket.complete_owned(error)
            raise
        projection_ticket.complete_owned()
        replay_cancellation.raise_if_requested()
        return outcome
    if proposal.status != "pending":
        raise HTTPException(status_code=409, detail="Only pending proposals can be accepted.")

    current_record = await _required_preparation_read(
        proposal_work,
        lambda ticket: service.get_current_state(proposal.session_id, required_work=ticket),
        lambda value: _project_preparation_state(value, proposal.session_id),
        state=cancellation_state,
    )
    current_state = _state_from_record(current_record) if current_record is not None else _initial_composition_state()
    user_message_content = await _proposal_user_message_content(
        service, proposal, required_work=proposal_work, cancellation_state=cancellation_state
    )
    if composer_meta is None:
        previous_meta = current_record.composer_meta if current_record is not None else None
        chat_ingress_inputs = await _proposal_chat_ingress_inputs(
            service,
            proposal,
            own_compartment_id=services.settings.compartment_id,
            required_work=proposal_work,
            cancellation_state=cancellation_state,
        )
        composer_meta = merge_composer_meta_updates(
            previous_meta,
            {
                "ingress": compartment_ingress_record(user_message_content, own_compartment_id=services.settings.compartment_id),
                "chat_ingress_inputs": chat_ingress_inputs,
            }
            if user_message_content is not None
            else {},
        )
    plugin_snapshot = services.plugin_snapshot_for_user_id(user_id)
    policy_catalog = PolicyCatalogView(
        services.catalog_service,
        plugin_snapshot,
        services.operator_profile_registry,
    )
    recorder = BufferingRecorder()
    recovery = await _required_preparation_read(
        proposal_work,
        lambda ticket: service.get_pipeline_dispatch_recovery(authority=authority, required_work=ticket),
        lambda value: _project_preparation_recovery(value, proposal.tool_call_id),
        state=cancellation_state,
    )
    try:
        prepared, _ = await _await_with_deferred_cancellation(
            prepare_pipeline_proposal_commit(
                authority=authority,
                current_state=current_state,
                current_state_id=current_record.id if current_record is not None else None,
                policy_catalog=policy_catalog,
                plugin_snapshot=plugin_snapshot,
                config=PipelineCommitConfig(
                    data_dir=str(services.settings.data_dir),
                    session_engine=services.session_engine,
                    session_operation_context=session_operation_context,
                    session_operation_authority=service.session_operation_authority,
                    secret_service=services.scoped_secret_resolver,
                    user_id=str(user_id),
                    user_message_content=user_message_content,
                    max_blob_storage_per_session_bytes=services.settings.max_blob_storage_per_session_bytes,
                    runtime_preflight=None,
                    timeout_seconds=commit_timeout_seconds,
                ),
                recorder=recorder,
                actor=f"user:{user_id}",
                recovery_dispatch=recovery.binding if recovery is not None else None,
                recovery_executor_content_hash=recovery.executor_content_hash if recovery is not None else None,
            ),
            state=cancellation_state,
        )
    except PipelineCommitError as exc:
        try:
            persisted_dispatch = recovery.binding if recovery is not None else exc.dispatch if exc.invocation is None else None
            captured = (exc.invocation,) if exc.invocation is not None else tuple(recorder.invocations)
            if captured:
                bindings, _ = await _await_with_deferred_cancellation(
                    _persist_tool_invocations(
                        service,
                        proposal.session_id,
                        captured,
                        None,
                        plugin_crash_pending=True,
                        required_audit=True,
                        required_work=proposal_work,
                        session_operation_context=session_operation_context,
                        session_operation_kind=session_operation_context.operation_kind,
                    ),
                    state=cancellation_state,
                )
                if exc.invocation is not None:
                    if exc.dispatch is None or len(bindings) != 1:
                        raise RuntimeError("pipeline failure dispatch did not persist exactly one rebound binding") from exc
                    persisted_dispatch = bindings[0]
            reason_by_code: dict[str, PipelineProposalRejectionReason] = {
                "CANDIDATE_EXECUTOR_MISMATCH": "candidate_executor_mismatch",
                "VALIDATION_FAILED": "validation_failed",
                "BASE_CONFLICT": "base_conflict",
            }
            if exc.code in reason_by_code:
                reason = reason_by_code[exc.code]
                await reject_pipeline_with_required_custody(
                    service,
                    expected=PipelineRejectionExpected(
                        authority,
                        reason,
                        persisted_dispatch,
                        f"system:pipeline_commit:user:{user_id}",
                        user_id,
                        session_operation_context,
                        running,
                    ),
                    binding=rejection_binding,
                )
        except BaseException as cleanup_exc:
            raise original_outcome_group(
                "Pipeline body cleanup and original cancellations",
                exc,
                cleanup_exc,
                *cancellation_state.cancellations,
            ) from None
        if cancellation_state.requested:
            raise BaseExceptionGroup("Pipeline preparation and cancellation", [exc, *cancellation_state.cancellations]) from None
        if exc.code == "TIMEOUT":
            public_error = HTTPException(
                status_code=504,
                detail="Pipeline preparation timed out. Please retry this proposal.",
            )
        else:
            status_code = 409 if exc.code in {"BASE_CONFLICT", "NOT_PENDING"} else 422
            public_error = HTTPException(status_code=status_code, detail=str(exc))
        raise original_outcome_group("Pipeline original body and public projection", exc, public_error) from None
    except BaseException as exc:
        captured = tuple(recorder.invocations)
        if captured:
            await _await_with_deferred_cancellation(
                _persist_tool_invocations(
                    service,
                    proposal.session_id,
                    captured,
                    None,
                    plugin_crash_pending=True,
                    required_audit=True,
                    required_work=proposal_work,
                    session_operation_context=session_operation_context,
                    session_operation_kind=session_operation_context.operation_kind,
                ),
                state=cancellation_state,
            )
        if cancellation_state.requested:
            raise BaseExceptionGroup("Pipeline preparation and cancellation", [exc, *cancellation_state.cancellations]) from None
        raise

    publication_projection = None
    try:
        if isinstance(prepared, RecoveredPipelineCommit):
            bindings = (prepared.dispatch,)
        else:
            bindings, _ = await _await_with_deferred_cancellation(
                _persist_tool_invocations(
                    service,
                    proposal.session_id,
                    (prepared.invocation,),
                    None,
                    plugin_crash_pending=False,
                    required_audit=True,
                    required_work=proposal_work,
                    session_operation_context=session_operation_context,
                    session_operation_kind=session_operation_context.operation_kind,
                ),
                state=cancellation_state,
            )
        if len(bindings) != 1:
            raise RuntimeError("pipeline acceptance dispatch did not persist exactly one binding")
        (state_data, validation), _ = await _await_with_deferred_cancellation(
            _state_data_from_composer_state(
                prepared.result.updated_state,
                settings=services.settings,
                secret_service=services.scoped_secret_resolver,
                user_id=str(user_id),
                session_id=proposal.session_id,
                plugin_snapshot=plugin_snapshot,
                profile_registry=services.operator_profile_registry,
                catalog=services.catalog_service,
                runtime_preflight=prepared.result.runtime_preflight,
                preflight_exception_policy="raise",
                initial_version=current_state.version,
                telemetry_source=telemetry_source,
                composer_meta=composer_meta,
            ),
            state=cancellation_state,
        )
        validation_ticket = proposal_work.reserve(RequiredWorkSource.PREPARATION_VALIDATION_PRODUCER)
        try:
            prepared_interpretations = services.interpretation_surfacing.prepare_pending_interpretation_reviews(
                prepared.result.updated_state
            )
        except BaseException as exc:
            validation_ticket.complete_owned(exc)
            raise
        validation_ticket.complete_owned()
        publication_projection = proposal_work.reserve(RequiredWorkSource.PIPELINE_PUBLICATION_PROJECTION)
        publication_ticket = proposal_work.reserve(RequiredWorkSource.PIPELINE_PUBLICATION_SQL)
        revocation_ticket = proposal_work.reserve(RequiredWorkSource.TRUST_REVOCATION_SQL)
        revocation_projection = proposal_work.reserve(RequiredWorkSource.TRUST_REVOCATION_PROJECTION)
        handoff, _ = await _await_with_deferred_cancellation(
            service.settle_pipeline_composition_proposal_finish_once(
                session_id=proposal.session_id,
                proposal_id=proposal.id,
                draft_hash=draft_hash,
                state=state_data,
                candidate_content_hash=prepared.candidate_content_hash,
                executor_content_hash=prepared.executor_content_hash,
                final_composer_metadata=state_data.composer_meta,
                dispatch=bindings[0],
                actor=f"user:{user_id}",
                required_trust_mode=required_trust_mode,
                session_operation_context=session_operation_context,
                prepared_interpretations=prepared_interpretations,
                running=running,
                coordinator=proposal_work,
                required_work=publication_ticket,
                publication_projection_work=publication_projection,
                revocation_required_work=revocation_ticket,
                revocation_projection_work=revocation_projection,
            ),
            state=cancellation_state,
        )
    except BaseException as exc:
        # An unknown service outcome supplies no closed arm. Keep projection
        # work unresolved rather than asserting a publication branch.
        if cancellation_state.requested:
            raise BaseExceptionGroup("Pipeline publication and cancellation", [exc, *cancellation_state.cancellations]) from None
        raise
    if publication_projection is None:
        raise AuditIntegrityError("Pipeline publication projection was not registered")
    for cancellation in handoff.deferred_cancellations:
        cancellation_state.retain(cancellation)
    if isinstance(handoff, ComposerPipelineBusinessReturned):
        publication_projection.begin_projection()
        try:
            settled = handoff.result
            _validate_pipeline_publication_result(settled, proposal)
        except BaseException as exc:
            publication_projection.complete_owned(exc)
            raise
        publication_projection.complete_owned()
        cancellation_state.raise_if_requested()
        return PipelineRouteSettlement(settlement=settled, validation=validation)
    if isinstance(handoff, (ComposerPipelineRevocationCompleted, ComposerPipelineRaised)):
        if handoff.publication_projection_disposition is not handoff.publication_projection_unused.disposition:
            raise AuditIntegrityError("Pipeline publication unused disposition disagrees with its receipt")
        publication_projection.complete_unused(handoff.publication_projection_unused)
        if isinstance(handoff, ComposerPipelineRaised):
            if cancellation_state.requested:
                raise BaseExceptionGroup(
                    "Pipeline publication and cancellation", [handoff.error, *cancellation_state.cancellations]
                ) from None
            raise handoff.error
        cancellation_state.raise_if_requested()
        raise TrustModeAutoCommitRevokedError(session_id=str(proposal.session_id), required="auto_commit", current="explicit_approve")
    raise AuditIntegrityError("Pipeline publication returned an undeclared service handoff")


async def settle_pipeline_proposal_under_compose_lock(
    *,
    services: ComposerAppServices,
    user_id: str,
    authority: AuthoritativePipelineProposal,
    draft_hash: str,
    composer_meta: Mapping[str, object] | None = None,
    telemetry_source: Literal["compose", "recompose"] = "compose",
    required_trust_mode: ComposerTrustMode | None = None,
    session_operation_context: SessionOperationContext,
    commit_timeout_seconds: float,
    running: ComposerOperationRunning | None = None,
    required_work: RequiredWorkCoordinator | None = None,
    required_binding: RequiredWorkBinding | None = None,
) -> PipelineRouteSettlement:
    if required_binding is not None and type(required_binding) is not RequiredWorkBinding:
        raise AuditIntegrityError("Pipeline settlement requires an owned parent binding")
    if required_work is not None:
        if type(required_work) is not RequiredWorkCoordinator:
            raise AuditIntegrityError("Pipeline settlement requires an owned required-work coordinator")
        if required_work.authority.context != session_operation_context:
            raise AuditIntegrityError("Pipeline required-work scope disagrees with context")
    child: RequiredWorkCoordinator | None = None
    producer = None
    child_binding = None
    if required_work is not None:
        parent_binding = (
            RequiredWorkBinding(required_work, 0, 0, RequiredWorkRole.TURN, running) if required_binding is None else required_binding
        )
        if parent_binding.coordinator is not required_work:
            raise AuditIntegrityError("Pipeline parent binding has a foreign coordinator")
        parent_binding.validate_context(session_operation_context)
        child, producer = required_work.begin_proposal_child(
            str(authority.row.id),
            authority.row.tool_call_id,
            transition_ordinal=parent_binding.transition_ordinal,
            semantic_ordinal=parent_binding.semantic_ordinal,
        )
        child_binding = RequiredWorkBinding(
            child, parent_binding.transition_ordinal, parent_binding.semantic_ordinal, parent_binding.role, parent_binding.running
        )
    try:
        result = await _settle_pipeline_proposal_under_compose_lock(
            services=services,
            user_id=user_id,
            authority=authority,
            draft_hash=draft_hash,
            composer_meta=composer_meta,
            telemetry_source=telemetry_source,
            required_trust_mode=required_trust_mode,
            session_operation_context=session_operation_context,
            commit_timeout_seconds=commit_timeout_seconds,
            running=running,
            required_work=child,
            required_binding=child_binding,
        )
    except BaseException as original:
        if required_work is None or child is None or producer is None:
            raise
        try:
            child.assert_completed()
        except BaseException as incomplete:
            raise BaseExceptionGroup("Pipeline original failure and unresolved child custody", [original, incomplete]) from None
        roots = [original]
        original_leaves = required_failure_leaves(original)
        for receipt in child.failure_receipts():
            if any(all(w is not leaf for leaf in original_leaves) for w in receipt.original_category_witnesses):
                roots.append(receipt.original_root)
        retained = original if len(roots) == 1 else BaseExceptionGroup("Pipeline child original failures", roots)
        required_work.complete_proposal_child(child, producer, retained)
        if retained is original:
            raise
        raise retained from None
    if required_work is not None and child is not None and producer is not None:
        required_work.complete_proposal_child(child, producer)
    return result


@dataclass(frozen=True, slots=True)
class AutoCommitRevoked:
    """Auto-commit authority was durably revoked at the settlement boundary.

    Carries the trust facts from ``TrustModeAutoCommitRevokedError`` so the
    route can act on the revocation as an explicit outcome: the proposal
    remains pending and the turn becomes an ordinary review-path response.
    By the time a caller holds this value the revocation is already durable:
    the locked settlement transaction writes the ``auto_commit.revoked``
    proposal event before raising the outcome translated here.
    """

    required: str
    current: str


async def settle_auto_commit_intent(
    *,
    services: ComposerAppServices,
    user_id: str,
    service: SessionServiceProtocol,
    session_id: UUID,
    intent: PipelineCommitIntent,
    composer_meta: Mapping[str, object] | None,
    telemetry_source: Literal["compose", "recompose"],
    session_operation_context: SessionOperationContext,
    commit_timeout_seconds: float,
    running: ComposerOperationRunning | None = None,
    required_work: RequiredWorkCoordinator | None = None,
    required_binding: RequiredWorkBinding | None = None,
) -> PipelineRouteSettlement | AutoCommitRevoked:
    """Settle a planner-minted auto-commit intent, or report revocation.

    Shared by the send-message and recompose routes. ``AutoCommitRevoked``
    means the session's trust mode was durably downgraded before the
    settlement transaction could commit (elspeth-01d4c6e683): the proposal
    remains pending and the caller must fall back to the review-path
    response.
    """
    if required_work is not None:
        if type(required_work) is not RequiredWorkCoordinator:
            raise AuditIntegrityError("Pipeline settlement requires an owned required-work coordinator")
        if required_work.authority.context != session_operation_context:
            raise AuditIntegrityError("Pipeline required-work scope disagrees with context")
    if required_work is None:
        authority = await service.get_authoritative_pipeline_proposal(session_id=session_id, proposal_id=intent.proposal_id)
    else:
        authority = await _required_preparation_read(
            required_work,
            lambda ticket: service.get_authoritative_pipeline_proposal(
                session_id=session_id, proposal_id=intent.proposal_id, required_work=ticket
            ),
            lambda value: _project_preparation_authority(value, session_id, intent.proposal_id),
        )
    try:
        return await settle_pipeline_proposal_under_compose_lock(
            services=services,
            user_id=user_id,
            authority=authority,
            draft_hash=intent.draft_hash,
            composer_meta=composer_meta,
            telemetry_source=telemetry_source,
            commit_timeout_seconds=commit_timeout_seconds,
            required_trust_mode="auto_commit",
            session_operation_context=session_operation_context,
            running=running,
            required_work=required_work,
            required_binding=required_binding,
        )
    except TrustModeAutoCommitRevokedError as exc:
        # The locked settlement transaction committed this revocation before
        # raising, so cancellation or process failure cannot leave the
        # successful dispatch unexplained while the proposal stays pending.
        slog.info(
            "composer.auto_commit.revoked",
            session_id=str(session_id),
            proposal_id=str(intent.proposal_id),
            required_trust_mode=exc.required,
            current_trust_mode=exc.current,
            telemetry_source=telemetry_source,
        )
        return AutoCommitRevoked(required=exc.required, current=exc.current)

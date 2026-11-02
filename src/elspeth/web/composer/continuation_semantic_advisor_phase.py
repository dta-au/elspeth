"""Live semantic Advisor action and separately admitted SQL decision consumer.

The first PRIMARY stays with its original owner. A later unprescribed Compose
cut enters this actual guarded provider path; a retained paid result is consumed
by a subsequent SQL action. No result DATA grants a physical call or retry.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast
from uuid import UUID

from sqlalchemy import Connection

from elspeth.contracts.chargeable_admission import ChargeableAdmissionRefused
from elspeth.contracts.composer_content import ComposerContentScope
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer.advisor_output import PrimaryContinuationAction, ReadContinuationAction, ReviewContinuationAction
from elspeth.web.composer.continuation import AdvisorFrame, ComposeFrame, EndFrame, ToolBatchFrame
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_initialization import _registered
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_policy import (
    ContinuationAdmissionPolicy,
    ContinuationAdmissionPolicyV2,
    ContinuationAdmissionPolicyV3,
    ContinuationAdmissionPolicyV4,
    decode_continuation_policy,
)
from elspeth.web.composer.continuation_progress import decode_prescribed_continuation_action
from elspeth.web.composer.continuation_selection import SelectedContinuationState, load_selected_continuation
from elspeth.web.composer.continuation_semantic_advisor import SemanticAdvisorRetainedInputs, prepare_semantic_advisor_request
from elspeth.web.composer.continuation_semantic_advisor_outcome import (
    SemanticAdvisorInvocationRaised,
    SemanticAdvisorInvocationReturned,
    SemanticAdvisorInvocationV4Finalized,
    observe_semantic_advisor_invocation,
)
from elspeth.web.composer.continuation_semantic_invocation import make_semantic_advisor_invocation
from elspeth.web.composer.continuation_semantic_no_sdk import SemanticAdvisorNoSdkIssuer, SemanticAdvisorNoSdkRefusalReason
from elspeth.web.composer.continuation_semantic_observation import SemanticProviderResponseObservation
from elspeth.web.composer.continuation_semantic_source import capture_semantic_advisor_source
from elspeth.web.composer.continuation_tool_failure import combine_failure_objects
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.prompt_guard import PromptGuardRefused
from elspeth.web.composer.provider_cost import CommonProviderCostRefused
from elspeth.web.composer.provider_quota import V4PaidIncompleteCommitted
from elspeth.web.composer.provider_result_admission import V4ProviderResultIncomplete
from elspeth.web.continuation_retention_memo import legacy_advisor_preparation_memo_scope
from elspeth.web.required_sql_outcomes import RequiredSQLFinishOnce, RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredWorkSource, RequiredWorkTicket
from elspeth.web.sessions.composer_operations import ComposerOperationRecord, ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    PreparedContinuationProvider,
    StoredContinuationSnapshot,
    V3UnboundedTailRefusal,
    refuse_v3_retained_tail_before_paid,
)
from elspeth.web.sessions.continuation_paid_incomplete_terminal import VerifiedPaidIncompleteTerminal
from elspeth.web.sessions.continuation_semantic_advisor import (
    SemanticAdvisorProviderResult,
    _semantic_checkpoint_kind_data,
    begin_semantic_advisor_on_connection,
    close_semantic_advisor_no_sdk_on_connection,
    consume_committed_semantic_read_on_connection,
    consume_semantic_advisor_end_stop_on_connection,
    consume_semantic_advisor_primary_on_connection,
    consume_semantic_advisor_review_on_connection,
    consume_semantic_advisor_terminal_iteration_stop_on_connection,
    install_semantic_advisor_decision_on_connection,
    prepare_semantic_advisor_on_connection,
    read_current_paid_semantic_decision_on_connection,
    read_semantic_advisor_retained_inputs_on_connection,
    retain_semantic_advisor_outcome_on_connection,
    select_semantic_advisor_current_graph_on_connection,
)
from elspeth.web.sessions.continuation_semantic_paid_overflow_terminal import VerifiedSemanticPaidOverflowTerminal
from elspeth.web.sessions.continuation_semantic_v4_no_sdk_close import close_v4_semantic_no_sdk_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.service import SessionServiceImpl


@dataclass(frozen=True, slots=True)
class SemanticAdvisorNoSdkTerminal:
    terminal: ComposerOperationRecord
    original_error: BaseException = field(repr=False, compare=False)
    deferred_cancellations: tuple[asyncio.CancelledError, ...] = ()


@dataclass(frozen=True, slots=True)
class SemanticAdvisorPaidTerminal:
    verified: VerifiedSemanticPaidOverflowTerminal
    deferred_cancellations: tuple[asyncio.CancelledError, ...] = ()


@dataclass(frozen=True, slots=True)
class SemanticAdvisorPaidIncompleteTerminal:
    verified: VerifiedPaidIncompleteTerminal
    deferred_cancellations: tuple[asyncio.CancelledError, ...] = ()


@dataclass(frozen=True, slots=True)
class _PhysicalFinish:
    paid: SemanticAdvisorProviderResult | None = None
    terminal: ComposerOperationRecord | None = None

    def __post_init__(self) -> None:
        if (self.paid is None) == (self.terminal is None):
            raise AuditIntegrityError("Semantic physical finish lost its exact returned variant")


def _transaction[T](phases: ContinuationLivePhases, operation: Callable[[Connection], T]) -> T:
    running = phases.work.running
    if type(running) is not ComposerOperationRunning or running.session_operation_context is not phases.lease.context:
        raise AuditIntegrityError("Semantic transaction changed its actual physical owner")
    with locked_session_transaction(phases.authority._engine, str(running.claim.session_id)) as conn:
        return operation(conn)


def _native_cancellations(error: BaseException) -> tuple[asyncio.CancelledError, ...]:
    if isinstance(error, asyncio.CancelledError):
        return (error,)
    if isinstance(error, BaseExceptionGroup):
        result: list[asyncio.CancelledError] = []
        for member in error.exceptions:
            for cancellation in _native_cancellations(member):
                if not any(cancellation is previous for previous in result):
                    result.append(cancellation)
        return tuple(result)
    return ()


def _refusal_reason(error: BaseException) -> SemanticAdvisorNoSdkRefusalReason | None:
    if type(error) is PromptGuardRefused:
        return SemanticAdvisorNoSdkRefusalReason.GUARD_REFUSED
    if type(error) is CommonProviderCostRefused:
        return SemanticAdvisorNoSdkRefusalReason.COST_REFUSED
    if type(error) is ChargeableAdmissionRefused:
        return SemanticAdvisorNoSdkRefusalReason.CHARGEABLE_REFUSED
    if type(error) is TimeoutError:
        return SemanticAdvisorNoSdkRefusalReason.PRE_SDK_DEADLINE
    if type(error) is asyncio.CancelledError:
        return SemanticAdvisorNoSdkRefusalReason.CANCELLED_BEFORE_SDK
    return None


async def _finish_physical(
    phases: ContinuationLivePhases,
    sql: RequiredWorkTicket,
    projection: RequiredWorkTicket,
    operation: Callable[[], _PhysicalFinish],
) -> tuple[_PhysicalFinish, tuple[asyncio.CancelledError, ...]]:
    try:
        outcome = await run_required_sql_finish_once(sql, operation)
    except BaseException:
        projection.complete_without_submission()
        raise
    try:
        phases.work.coordinator.validate_continuation_sql_outcome(
            ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_STATE_SQL, actual_outcome=outcome
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    if type(outcome) is RequiredSQLRaised:
        projection.complete_without_submission()
        raise combine_failure_objects(outcome.error, *outcome.deferred_cancellations)
    if type(outcome) is not RequiredSQLReturned:
        projection.complete_without_submission()
        raise AuditIntegrityError("Semantic finish lost its actual physical SQL outcome")
    projection.begin_projection()
    try:
        if type(outcome.value) is not _PhysicalFinish:
            raise AuditIntegrityError("Semantic finish received a foreign SQL result")
        result = outcome.value
    except BaseException as error:
        projection.complete_owned(error)
        raise
    projection.complete_owned()
    return result, outcome.deferred_cancellations


async def _consume_current_decision(phases: ContinuationLivePhases, selected: SelectedContinuationState) -> None:
    running = phases.work.running
    frame = selected.stack.frames[-1]
    if type(running) is not ComposerOperationRunning or type(frame) is not AdvisorFrame or frame.provider_effect_key is None:
        raise AuditIntegrityError("Semantic consumer lost its exact current decision source")
    if frame.phase not in ("response_retained", "decision_ready"):
        raise AuditIntegrityError("Semantic consumer cannot replay unresolved prepared/begun provider work")
    effect_key = frame.provider_effect_key
    stored = StoredContinuationSnapshot(encode_continuation_snapshot(selected.stack), selected.stack.cursor, selected.checkpoint_id)

    def consume(conn: Connection) -> StoredContinuationSnapshot:
        from elspeth.web.sessions.continuation_semantic_actions import install_semantic_action_on_connection

        current = stored
        if frame.phase == "response_retained":
            paid = read_current_paid_semantic_decision_on_connection(conn, running, selected=current, effect_key=effect_key)
            if type(paid.decision.prescribed_action) is PrimaryContinuationAction:
                current = install_semantic_advisor_decision_on_connection(conn, running, selected=current, effect_key=effect_key)
            else:
                current = install_semantic_action_on_connection(conn, running, selected=current, effect_key=effect_key)
        root = decode_continuation_snapshot(current.payload_json)
        action = decode_prescribed_continuation_action(root.active_action_json) if root.active_action_json is not None else None
        if type(action) is PrimaryContinuationAction:
            return consume_semantic_advisor_primary_on_connection(conn, running, selected=current, effect_key=effect_key)
        if type(action) is ReviewContinuationAction:
            return consume_semantic_advisor_review_on_connection(conn, running, selected=current, effect_key=effect_key)
        if type(action) is ReadContinuationAction:
            # The following real read action receives the original read policy.
            return current
        if (
            _semantic_checkpoint_kind_data(conn, str(running.claim.session_id), running.claim.operation_id, current.cursor)
            == "semantic_read_committed"
        ):
            return consume_committed_semantic_read_on_connection(conn, running, selected=current, effect_key=effect_key)
        if len(root.frames) == 3 and type(root.frames[1]) is ToolBatchFrame:
            return consume_semantic_advisor_terminal_iteration_stop_on_connection(conn, running, selected=current, effect_key=effect_key)
        return consume_semantic_advisor_end_stop_on_connection(conn, running, selected=current, effect_key=effect_key)

    phases.latest_snapshot = await _registered(
        work=phases.work,
        source=RequiredWorkSource.CONTINUATION_STATE_SQL,
        operation=lambda: _transaction(phases, consume),
        expected=StoredContinuationSnapshot,
    )


async def run_prescribed_semantic_read(phases: ContinuationLivePhases, selected: SelectedContinuationState) -> None:
    """Execute the paid-admitted read under the driver's one original read clock."""
    from elspeth.web.composer.continuation_semantic_advisor_read import execute_semantic_advisor_read
    from elspeth.web.sessions.continuation_semantic_actions import prepare_semantic_read_on_connection

    running = phases.work.running
    frame = selected.stack.frames[-1]
    if (
        type(running) is not ComposerOperationRunning
        or type(frame) is not AdvisorFrame
        or frame.phase != "decision_ready"
        or frame.provider_effect_key is None
        or selected.stack.active_action_json is None
        or type(decode_prescribed_continuation_action(selected.stack.active_action_json)) is not ReadContinuationAction
    ):
        raise AuditIntegrityError("Semantic read requires its original installed paid action")
    effect_key = frame.provider_effect_key
    runtime, _inventory = await phases.current(selected)
    stored = StoredContinuationSnapshot(encode_continuation_snapshot(selected.stack), selected.stack.cursor, selected.checkpoint_id)
    prepared = await _registered(
        work=phases.work,
        source=RequiredWorkSource.CONTINUATION_STATE_SQL,
        operation=lambda: _transaction(
            phases, lambda conn: prepare_semantic_read_on_connection(conn, running, selected=stored, effect_key=effect_key)
        ),
        expected=StoredContinuationSnapshot,
    )
    execution = await execute_semantic_advisor_read(
        service=phases.service,
        authority=phases.authority,
        running=running,
        selected=prepared,
        effect_key=effect_key,
        runtime=runtime,
        required_work=phases.work,
    )
    # Its actual returned disposition has committed. Read DATA consumption is
    # the following SQL action, without renewing this spent read allowance.
    phases.latest_snapshot = execution.committed.snapshot


async def run_continuation_semantic_advisor(
    phases: ContinuationLivePhases, selected: SelectedContinuationState
) -> SemanticAdvisorNoSdkTerminal | SemanticAdvisorPaidTerminal | SemanticAdvisorPaidIncompleteTerminal | V3UnboundedTailRefusal | None:
    """Execute one real action; retain paid output before a new SQL action.

    Stop/expiry may finish an original physical outcome, but cannot install a
    new decision. The original V3 policy refuses before any paid preparation.
    """
    if type(phases) is not ContinuationLivePhases or type(selected) is not SelectedContinuationState:
        raise AuditIntegrityError("Semantic live action requires its actual phase/selection owners")
    running = phases.work.running
    if type(running) is not ComposerOperationRunning or phases.lease.context is not running.session_operation_context:
        raise AuditIntegrityError("Semantic live action lost its original current lease")
    frame = selected.stack.frames[-1]
    if type(frame) is AdvisorFrame:
        await _consume_current_decision(phases, selected)
        return None
    initial_compose = type(frame) is ComposeFrame and len(selected.stack.frames) == 1 and frame.phase == "action_ready"
    actual_end = (
        type(frame) is EndFrame
        and len(selected.stack.frames) == 2
        and frame.phase in ("review_needed", "repair_needed", "publication_ready")
    )
    actual_iteration_return = (
        type(frame) is ToolBatchFrame
        and len(selected.stack.frames) == 2
        and frame.phase == "result_retained"
        and frame.accumulator.codec == "composer.applied-p4-batch.v2"
    )
    if (
        (not initial_compose and not actual_end and not actual_iteration_return)
        or selected.stack.active_action_json is not None
        or selected.stack.active_action_source is not None
    ):
        raise AuditIntegrityError("Semantic live action cannot replace an original prescribed/unresolved owner")
    policy = decode_continuation_policy(selected.stack.admission_policy_json, selected.stack.policy_hash)
    if type(policy) is ContinuationAdmissionPolicyV3:
        inventory = await materialize_continuation_env_inventory(
            advisor=phases.service._advisor_checkpoint,
            authority=phases.authority,
            selected=selected,
            required_work=phases.work,
        )
        return await _registered(
            work=phases.work,
            source=RequiredWorkSource.CONTINUATION_STATE_SQL,
            operation=lambda: refuse_v3_retained_tail_before_paid(phases.authority, running, selected=selected, inventory=inventory),
            expected=V3UnboundedTailRefusal,
        )
    clock = phases.action_clock
    if clock is None:
        if phases.work.coordinator.sql_deadline_owner is not None:
            raise AuditIntegrityError("Semantic live action lost its original driver clock")
        clock = ContinuationActionClock(policy.actions.advisor)
    if type(clock) is not ContinuationActionClock or clock.policy != policy.actions.advisor:
        raise AuditIntegrityError("Semantic live action changed its original Advisor allowance")
    if type(policy) is ContinuationAdmissionPolicyV4:
        return await _run_semantic_advisor_preparation(phases, selected, running=running, policy=policy, clock=clock)
    if type(policy) not in (ContinuationAdmissionPolicy, ContinuationAdmissionPolicyV2):
        raise AuditIntegrityError("Semantic Advisor lost its original admitted policy")
    with legacy_advisor_preparation_memo_scope(clock):
        return await _run_semantic_advisor_preparation(phases, selected, running=running, policy=policy, clock=clock)


async def _run_semantic_advisor_preparation(
    phases: ContinuationLivePhases,
    selected: SelectedContinuationState,
    *,
    running: ComposerOperationRunning,
    policy: ContinuationAdmissionPolicy | ContinuationAdmissionPolicyV2 | ContinuationAdmissionPolicyV3 | ContinuationAdmissionPolicyV4,
    clock: ContinuationActionClock,
) -> SemanticAdvisorNoSdkTerminal | SemanticAdvisorPaidTerminal | SemanticAdvisorPaidIncompleteTerminal | None:
    runtime, inventory = await phases.current(selected)
    current = await _registered(
        work=phases.work,
        source=RequiredWorkSource.CONTINUATION_STATE_SQL,
        operation=lambda: _transaction(
            phases, lambda conn: select_semantic_advisor_current_graph_on_connection(conn, running, selected=selected, inventory=inventory)
        ),
        expected=StoredContinuationSnapshot,
    )
    if current.checkpoint_id != selected.checkpoint_id:
        selected = await load_selected_continuation(
            authority=phases.authority, running=running, selected=current, required_work=phases.work
        )
        runtime, inventory = await phases.current(selected)
    raw = capture_semantic_advisor_source(runtime)
    invocation = make_semantic_advisor_invocation(raw)
    stored = StoredContinuationSnapshot(encode_continuation_snapshot(selected.stack), selected.stack.cursor, selected.checkpoint_id)
    retained_inputs = await _registered(
        work=phases.work,
        source=RequiredWorkSource.CONTINUATION_READ_SQL,
        operation=lambda: _transaction(
            phases, lambda conn: read_semantic_advisor_retained_inputs_on_connection(conn, running, selected=stored)
        ),
        expected=SemanticAdvisorRetainedInputs,
    )
    request = prepare_semantic_advisor_request(
        owner=phases.service._advisor_checkpoint,
        provider_owner=phases.provider_owner,
        source=invocation.invocation,
        frontier=invocation.frontier,
        state=runtime.live.state,
        content_scope=ComposerContentScope(UUID(selected.stack.logical.session_id), composition_content_hash(runtime.live.state)),
        user_message=runtime.bindings.message,
        runtime=runtime,
        env_ref_names=runtime.env_ref_names,
        retained_inputs=retained_inputs,
    )
    issuer = SemanticAdvisorNoSdkIssuer(
        running=running,
        work=phases.work,
        selected_checkpoint_id=selected.checkpoint_id,
        selected_cursor=selected.stack.cursor,
        input_sha256=request.request_sha256,
        clock=clock,
    )
    prepared = await _registered(
        work=phases.work,
        source=RequiredWorkSource.CONTINUATION_STATE_SQL,
        operation=lambda: _transaction(
            phases,
            lambda conn: prepare_semantic_advisor_on_connection(
                conn,
                running,
                selected=selected,
                invocation=invocation,
                request=request,
                inventory=inventory,
                no_sdk_issuer=issuer,
            ),
        ),
        expected=PreparedContinuationProvider,
    )
    issuer.bind_prepared(prepared)
    if type(policy) is ContinuationAdmissionPolicyV4:
        if type(phases.service._sessions_service) is not SessionServiceImpl:
            raise AuditIntegrityError("V4 semantic Advisor requires the original session SQL owner")
        session_service = phases.service._sessions_service

        async def finalize_paid(
            observation: SemanticProviderResponseObservation, ticket: RequiredWorkTicket
        ) -> RequiredSQLFinishOnce[SemanticAdvisorProviderResult | VerifiedSemanticPaidOverflowTerminal]:
            deadline_owner = phases.work.coordinator.sql_deadline_owner
            if deadline_owner is not None:
                deadline_owner.begin_settlement()
            outcome = await run_required_sql_finish_once(
                ticket,
                lambda: session_service.complete_v4_semantic_result_sync(
                    prepared=prepared,
                    admission=issuer.require_original_v4_admission(prepared),
                    observation=observation,
                    recorder=runtime.live.recorder,
                    no_sdk_issuer=issuer,
                ),
            )
            phases.work.coordinator.validate_provider_settlement_sql_outcome(ticket=ticket, actual_outcome=outcome)
            return outcome

        async def finalize_incomplete(
            incomplete: V4ProviderResultIncomplete, ticket: RequiredWorkTicket
        ) -> RequiredSQLFinishOnce[VerifiedPaidIncompleteTerminal]:
            deadline_owner = phases.work.coordinator.sql_deadline_owner
            if deadline_owner is not None:
                deadline_owner.begin_settlement()
            outcome = await run_required_sql_finish_once(
                ticket,
                lambda: session_service.complete_v4_semantic_paid_incomplete_sync(
                    prepared=prepared,
                    admission=issuer.require_original_v4_admission(prepared),
                    incomplete=incomplete,
                    no_sdk_issuer=issuer,
                ),
            )
            phases.work.coordinator.validate_provider_settlement_sql_outcome(ticket=ticket, actual_outcome=outcome)
            return outcome

        observed_v4: SemanticAdvisorInvocationReturned | SemanticAdvisorInvocationV4Finalized | SemanticAdvisorInvocationRaised | None = (
            None
        )
        try:
            remaining = clock.deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("Semantic action exhausted its original allowance before physical dispatch")
            observed_v4 = await observe_semantic_advisor_invocation(
                request,
                provider_owner=phases.provider_owner,
                recorder=runtime.live.recorder,
                timeout=remaining,
                no_sdk_issuer=issuer,
                v4_prepared=prepared,
                v4_env_ref_names=runtime.env_ref_names,
                v4_finalizer=finalize_paid,
                v4_incomplete_finalizer=finalize_incomplete,
            )
            if type(observed_v4) is SemanticAdvisorInvocationV4Finalized:
                paid_finalization = observed_v4.finalization
                if type(paid_finalization.result) is VerifiedSemanticPaidOverflowTerminal:
                    return SemanticAdvisorPaidTerminal(paid_finalization.result, paid_finalization.deferred_cancellations)
                if type(paid_finalization.result) is not SemanticAdvisorProviderResult:
                    raise AuditIntegrityError("V4 semantic Advisor lost its exact paid result variant")
                phases.latest_snapshot = paid_finalization.result.snapshot
                retained_parent = decode_continuation_snapshot(paid_finalization.result.snapshot.payload_json).frames[0]
                if type(retained_parent) is not ComposeFrame:
                    raise AuditIntegrityError("V4 semantic paid finish lost its Compose counter owner")
                runtime.live.advisor_calls_used = retained_parent.advisor_calls
                if paid_finalization.deferred_cancellations:
                    raise combine_failure_objects(
                        paid_finalization.deferred_cancellations[0], *paid_finalization.deferred_cancellations[1:]
                    )
                return None
            if type(observed_v4) is not SemanticAdvisorInvocationRaised:
                raise AuditIntegrityError("V4 semantic Advisor lost its original physical observation")
            if type(observed_v4.error) is V4PaidIncompleteCommitted:
                finalized_incomplete = issuer.require_v4_paid_incomplete_committed(prepared, observed_v4)
                return SemanticAdvisorPaidIncompleteTerminal(finalized_incomplete.verified, finalized_incomplete.deferred_cancellations)
            if observed_v4.observation.sdk_entered:
                # A returned failure or unknown physical outcome has no approved
                # terminal policy. Keep its original attempt and H unresolved.
                raise observed_v4.error
            original = observed_v4.observation.original_failure
            reason = None if original is None else _refusal_reason(original)
            admission = issuer.original_v4_admission_or_none(prepared)
            if reason is None or original is None or admission is None:
                raise observed_v4.error
            no_sdk_reason = reason
            no_sdk_original = original
            sql, projection = phases.work.reserve_pair(
                RequiredWorkSource.CONTINUATION_STATE_SQL, RequiredWorkSource.CONTINUATION_STATE_PROJECTION
            )
            deadline_owner = phases.work.coordinator.sql_deadline_owner
            if deadline_owner is not None:
                deadline_owner.begin_settlement()
            closed, deferred = await _finish_physical(
                phases,
                sql,
                projection,
                lambda: _transaction(
                    phases,
                    lambda conn: _PhysicalFinish(
                        terminal=close_v4_semantic_no_sdk_on_connection(
                            conn,
                            service=session_service,
                            prepared=prepared,
                            admission=admission,
                            issuer=issuer,
                            reason=no_sdk_reason,
                            original_failure=no_sdk_original,
                        )
                    ),
                ),
            )
            if closed.terminal is None:
                raise AuditIntegrityError("V4 semantic no-SDK finish lost its failed terminal")
            cancellations = list(_native_cancellations(observed_v4.error))
            cancellations.extend(item for item in deferred if all(item is not prior for prior in cancellations))
            return SemanticAdvisorNoSdkTerminal(closed.terminal, observed_v4.error, tuple(cancellations))
        except BaseException as failure:
            if type(observed_v4) is SemanticAdvisorInvocationRaised:
                combined = combine_failure_objects(observed_v4.error, failure)
                if combined is not failure:
                    raise combined from combined.__cause__
            raise
    dispatched = await _registered(
        work=phases.work,
        source=RequiredWorkSource.CONTINUATION_STATE_SQL,
        operation=lambda: _transaction(phases, lambda conn: begin_semantic_advisor_on_connection(conn, prepared, no_sdk_issuer=issuer)),
        expected=StoredContinuationSnapshot,
    )
    issuer.bind_dispatched(dispatched)
    sql, projection = phases.work.reserve_pair(RequiredWorkSource.CONTINUATION_STATE_SQL, RequiredWorkSource.CONTINUATION_STATE_PROJECTION)
    observed: SemanticAdvisorInvocationReturned | SemanticAdvisorInvocationRaised | SemanticAdvisorInvocationV4Finalized | None = None
    try:
        remaining = clock.deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("Semantic action exhausted its original allowance before physical dispatch")
        observed = await observe_semantic_advisor_invocation(
            request,
            provider_owner=phases.provider_owner,
            recorder=runtime.live.recorder,
            timeout=remaining,
            no_sdk_issuer=issuer,
        )
        if type(observed) is SemanticAdvisorInvocationV4Finalized:
            raise AuditIntegrityError("Legacy semantic Advisor received a V4 finalization")
        legacy_observed = cast(SemanticAdvisorInvocationReturned | SemanticAdvisorInvocationRaised, observed)
        if not observed.observation.sdk_entered:
            if type(observed) is not SemanticAdvisorInvocationRaised:
                raise AuditIntegrityError("Semantic unentered observation cannot invent a returned paid result")
            original = observed.observation.original_failure
            reason = None if original is None else _refusal_reason(original)
            if reason is None or original is None:
                raise observed.error
            permit = issuer.reserve_refusal(reason=reason, original_failure=original)
            deadline_owner = phases.work.coordinator.sql_deadline_owner
            if deadline_owner is not None:
                deadline_owner.begin_settlement()
            finished, deferred = await _finish_physical(
                phases,
                sql,
                projection,
                lambda: _transaction(
                    phases, lambda conn: _PhysicalFinish(terminal=close_semantic_advisor_no_sdk_on_connection(conn, permit))
                ),
            )
            if finished.terminal is None:
                raise AuditIntegrityError("Semantic no-SDK finish lost its actual terminal")
            cancellations = list(_native_cancellations(observed.error))
            cancellations.extend(item for item in deferred if all(item is not prior for prior in cancellations))
            return SemanticAdvisorNoSdkTerminal(finished.terminal, observed.error, tuple(cancellations))
        deadline_owner = phases.work.coordinator.sql_deadline_owner
        if deadline_owner is not None:
            deadline_owner.begin_settlement()
        finished, deferred = await _finish_physical(
            phases,
            sql,
            projection,
            lambda: _transaction(
                phases,
                lambda conn: _PhysicalFinish(
                    paid=retain_semantic_advisor_outcome_on_connection(
                        conn,
                        prepared,
                        selected=dispatched,
                        outcome=legacy_observed,
                        recorder=runtime.live.recorder,
                        issuer=issuer,
                    )
                ),
            ),
        )
        if finished.paid is None:
            raise AuditIntegrityError("Semantic paid finish lost its actual original retained result")
        phases.latest_snapshot = finished.paid.snapshot
        retained_parent = decode_continuation_snapshot(finished.paid.snapshot.payload_json).frames[0]
        if type(retained_parent) is not ComposeFrame:
            raise AuditIntegrityError("Semantic paid finish lost its actual Compose counter owner")
        runtime.live.advisor_calls_used = retained_parent.advisor_calls
        if type(observed) is SemanticAdvisorInvocationRaised:
            raise combine_failure_objects(observed.error, *deferred)
        if deferred:
            raise combine_failure_objects(deferred[0], *deferred[1:])
        return None
    except BaseException as failure:
        if type(observed) is SemanticAdvisorInvocationRaised:
            combined = combine_failure_objects(observed.error, failure)
            if combined is not failure:
                raise combined from combined.__cause__
        raise
    finally:
        if not sql.complete:
            sql.complete_without_submission()
        if not projection.complete:
            projection.complete_without_submission()

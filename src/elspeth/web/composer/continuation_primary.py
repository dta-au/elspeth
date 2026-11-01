"""Actual primary provider publication using the existing gateway and SQL owner.

This action retains a provider result. The existing compose phase owners still
apply its completion, enforce the tool cap and confirm delivery afterward.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import Connection
from sqlalchemy.exc import SQLAlchemyError

from elspeth.contracts.chargeable_admission import ChargeableAdmissionRefused
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import freeze_fields
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.continuation import ComposeFrame
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors, decode_recorder_snapshot
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import encode_continuation_snapshot, owned_json
from elspeth.web.composer.continuation_message_shelf import decode_message_shelf
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV2, decode_continuation_policy
from elspeth.web.composer.continuation_primary_no_sdk import PrimaryNoSdkIssuer, PrimaryNoSdkRefusalReason
from elspeth.web.composer.continuation_prompt_producers import CurrentContinuationMessageProducers, _CurrentProducerInputs
from elspeth.web.composer.continuation_provider_input import continuation_provider_input_json
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.continuation_tool_batch import _state_from_tool_json
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.composer.provider_cost import (
    CommonProviderCostBudget,
    CommonProviderCostRefused,
    CommonProviderCostSnapshot,
    continuation_common_cost_policy,
)
from elspeth.web.composer.provider_gateway import PreparedContinuationPrimaryRequest
from elspeth.web.composer.provider_quota import ProviderInvocationFamily, ProviderInvocationOwner
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.continuation_retention_memo import retention_stage_memo_scope
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.required_sql_outcomes import RequiredSQLFinishOnce, RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkBinding, RequiredWorkSource, RequiredWorkTicket
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import PreparedContinuationProvider, StoredContinuationSnapshot
from elspeth.web.sessions.continuation_cost import read_continuation_cost_snapshot_on_connection
from elspeth.web.sessions.continuation_primary import PrimaryNoSdkRefusalReceipt, PrimaryProviderResult
from elspeth.web.sessions.locking import locked_session_transaction


@dataclass(frozen=True, slots=True)
class PreparedPrimaryInvocation:
    request: PreparedContinuationPrimaryRequest
    messages: Sequence[Mapping[str, Any]] = field(repr=False)
    tools: Sequence[Mapping[str, Any]] = field(repr=False)
    cost_budget: CommonProviderCostBudget | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        freeze_fields(self, "messages", "tools")


def _require_owners(
    authority: ComposerAsyncOperationAuthority,
    work: RequiredWorkBinding,
) -> None:
    if type(authority) is not ComposerAsyncOperationAuthority or type(work) is not RequiredWorkBinding or work.running is None:
        raise AuditIntegrityError("primary action requires actual current SQL and required-work owners")
    current = work.coordinator.authority
    if (
        current.authority_kind is not RequiredAuthorityKind.DURABLE_COMPOSE
        or current.durable_operation_id != work.running.claim.operation_id
        or current.claim_attempt != work.running.claim.attempt
    ):
        raise AuditIntegrityError("primary action changed its actual durable required-work claim")
    work.validate_context(work.running.session_operation_context)


def _project[T](
    work: RequiredWorkBinding,
    sql: RequiredWorkTicket,
    projection: RequiredWorkTicket,
    source: RequiredWorkSource,
    outcome: RequiredSQLFinishOnce[T],
) -> T:
    try:
        work.coordinator.validate_continuation_sql_outcome(ticket=sql, expected_source=source, actual_outcome=outcome)
    except BaseException:
        projection.complete_without_submission()
        raise
    errors: list[BaseException] = []
    value: T | None = None
    if type(outcome) is RequiredSQLRaised:
        projection.complete_without_submission()
        errors.append(outcome.error)
    elif type(outcome) is RequiredSQLReturned:
        projection.begin_projection()
        try:
            if type(outcome.value) not in (
                PreparedPrimaryInvocation,
                PreparedContinuationProvider,
                StoredContinuationSnapshot,
                PrimaryProviderResult,
            ):
                raise AuditIntegrityError("primary SQL returned an unfamiliar owned result")
            value = outcome.value
        except BaseException as error:
            projection.complete_owned(error)
            errors.append(error)
        else:
            projection.complete_owned()
    else:
        projection.complete_without_submission()
        raise AuditIntegrityError("primary SQL returned an unfamiliar finish-once outcome")
    for cancellation in outcome.deferred_cancellations:
        if all(cancellation is not original for original in errors):
            errors.append(cancellation)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("Primary continuation SQL outcomes", errors)
    if value is None:
        raise AuditIntegrityError("primary SQL lost its actual result")
    return value


def _project_no_sdk_sql[T](
    work: RequiredWorkBinding,
    sql: RequiredWorkTicket,
    projection: RequiredWorkTicket,
    outcome: RequiredSQLFinishOnce[Any],
    *,
    expected: type[T],
    allow_none: bool = False,
) -> tuple[T | None, BaseException | None, tuple[asyncio.CancelledError, ...]]:
    try:
        work.coordinator.validate_continuation_sql_outcome(
            ticket=sql,
            expected_source=RequiredWorkSource.CONTINUATION_STATE_SQL if not allow_none else RequiredWorkSource.CONTINUATION_READ_SQL,
            actual_outcome=outcome,
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    if type(outcome) is RequiredSQLRaised:
        projection.complete_without_submission()
        return None, outcome.error, outcome.deferred_cancellations
    if type(outcome) is not RequiredSQLReturned:
        projection.complete_without_submission()
        raise AuditIntegrityError("PRIMARY no-SDK SQL returned an unfamiliar finish-once result")
    projection.begin_projection()
    try:
        value = outcome.value
        if type(value) is not expected and (not allow_none or value is not None):
            raise AuditIntegrityError("PRIMARY no-SDK SQL returned a foreign receipt")
    except BaseException as error:
        projection.complete_owned(error)
        raise
    projection.complete_owned()
    return value, None, outcome.deferred_cancellations


async def _finish_primary_no_sdk(
    *,
    authority: ComposerAsyncOperationAuthority,
    required_work: RequiredWorkBinding,
    issuer: PrimaryNoSdkIssuer,
    reason: PrimaryNoSdkRefusalReason,
    original_failure: BaseException,
    original_cancellations: tuple[asyncio.CancelledError, ...] = (),
) -> PrimaryNoSdkRefusalReceipt:
    """Finish one proven unentered effect before original cancellation escapes."""
    deadline_owner = required_work.coordinator.sql_deadline_owner
    if deadline_owner is not None:
        deadline_owner.begin_settlement()
    permit = issuer.reserve_refusal(reason=reason, original_failure=original_failure)
    cancellations: list[asyncio.CancelledError] = []
    if type(original_failure) is asyncio.CancelledError:
        cancellations.append(original_failure)
    cancellations.extend(item for item in original_cancellations if all(item is not prior for prior in cancellations))
    last_failure: BaseException | None = None
    for attempt in range(2):
        sql, projection = required_work.reserve_pair(
            RequiredWorkSource.CONTINUATION_STATE_SQL, RequiredWorkSource.CONTINUATION_STATE_PROJECTION
        )
        try:
            outcome = await run_required_sql_finish_once(sql, authority.refuse_continuation_primary, permit)
        except BaseException:
            projection.complete_without_submission()
            raise
        receipt, failure, deferred = _project_no_sdk_sql(required_work, sql, projection, outcome, expected=PrimaryNoSdkRefusalReceipt)
        cancellations.extend(item for item in deferred if all(item is not prior for prior in cancellations))
        if receipt is not None:
            return replace(receipt, deferred_cancellations=tuple(cancellations))
        last_failure = failure
        read_sql, read_projection = required_work.reserve_pair(
            RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION
        )
        try:
            observed = await run_required_sql_finish_once(read_sql, authority.read_continuation_primary_no_sdk_refusal, permit)
        except BaseException:
            read_projection.complete_without_submission()
            raise
        readback, read_failure, read_deferred = _project_no_sdk_sql(
            required_work, read_sql, read_projection, observed, expected=PrimaryNoSdkRefusalReceipt, allow_none=True
        )
        cancellations.extend(item for item in read_deferred if all(item is not prior for prior in cancellations))
        if readback is not None:
            return replace(readback, deferred_cancellations=tuple(cancellations))
        if read_failure is not None:
            raise BaseExceptionGroup(
                "PRIMARY no-SDK refusal and readback failed", [failure, read_failure] if failure is not None else [read_failure]
            )
        if attempt == 0:
            continue  # Exact unchanged source permits retry of the refusal writer only.
    if last_failure is not None:
        raise last_failure
    raise AuditIntegrityError("PRIMARY no-SDK refusal lost its exact terminal receipt")


def _pre_sdk_failure(
    error: BaseException, *, preparing: bool
) -> tuple[PrimaryNoSdkRefusalReason, BaseException, tuple[asyncio.CancelledError, ...]] | None:
    """Recognize one nominal pre-entry failure and only joined cancellation tails.

    A group containing an undispatched-disposition failure cannot prove no SDK
    custody, even if its first member is a familiar timeout or refusal.
    """
    cancellations: tuple[asyncio.CancelledError, ...] = ()
    if type(error) is BaseExceptionGroup:
        parts = error.exceptions
        if len(parts) < 2 or any(type(item) is not asyncio.CancelledError for item in parts[1:]):
            return None
        error = parts[0]
        cancellations = tuple(item for item in parts[1:] if isinstance(item, asyncio.CancelledError))
    if type(error) is TimeoutError:
        reason = PrimaryNoSdkRefusalReason.PREPARATION_EXPIRED if preparing else PrimaryNoSdkRefusalReason.PRE_SDK_DEADLINE
        return reason, error, cancellations
    if type(error) is ChargeableAdmissionRefused:
        return PrimaryNoSdkRefusalReason.CHARGEABLE_REFUSED, error, cancellations
    if type(error) is CommonProviderCostRefused:
        return PrimaryNoSdkRefusalReason.COST_REFUSED, error, cancellations
    if isinstance(error, SQLAlchemyError):
        return PrimaryNoSdkRefusalReason.STORAGE_FAILURE, error, cancellations
    if isinstance(error, AuditIntegrityError):
        return PrimaryNoSdkRefusalReason.INTEGRITY_REFUSED, error, cancellations
    if type(error) is asyncio.CancelledError:
        return PrimaryNoSdkRefusalReason.CANCELLED_BEFORE_SDK, error, cancellations
    return None


async def _read_primary_candidate(
    *, authority: ComposerAsyncOperationAuthority, required_work: RequiredWorkBinding, issuer: PrimaryNoSdkIssuer, begin: bool
) -> tuple[PreparedContinuationProvider | StoredContinuationSnapshot | None, tuple[asyncio.CancelledError, ...]]:
    sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION)
    try:
        outcome = await run_required_sql_finish_once(
            sql,
            authority.read_continuation_primary_begin_outcome if begin else authority.read_continuation_primary_prepare_outcome,
            issuer,
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    result, failure, deferred = _project_no_sdk_sql(
        required_work,
        sql,
        projection,
        outcome,
        expected=StoredContinuationSnapshot if begin else PreparedContinuationProvider,
        allow_none=True,
    )
    if failure is not None:
        if deferred:
            raise BaseExceptionGroup("PRIMARY source readback and original cancellations", [failure, *deferred])
        raise failure
    if result is not None and not isinstance(result, (PreparedContinuationProvider, StoredContinuationSnapshot)):
        raise AuditIntegrityError("PRIMARY source readback returned a foreign candidate")
    return result, deferred


def _current_cost_budget(
    conn: Connection,
    selected: SelectedContinuationState,
    policy: ContinuationAdmissionPolicyV2,
    existing: CommonProviderCostBudget | None,
) -> CommonProviderCostBudget:
    source = read_continuation_cost_snapshot_on_connection(conn, selected.stack)
    if type(source) is not CommonProviderCostSnapshot:
        raise AuditIntegrityError("PRIMARY cost read lost its complete original SQL source")
    if existing is None:
        budget = CommonProviderCostBudget(source)
    else:
        if type(existing) is not CommonProviderCostBudget or existing.snapshot() != source:
            raise AuditIntegrityError("PRIMARY cost owner differs from its complete current SQL source")
        budget = existing
    budget.validate_binding(
        session_id=selected.stack.logical.session_id,
        request_id=selected.stack.logical.original_user_message_id,
        lifecycle_version=2,
    )
    if source.applicability != "continuation_v2" or source.policy != continuation_common_cost_policy(policy):
        raise AuditIntegrityError("PRIMARY cost owner differs from its immutable admitted policy")
    budget.require_supported_configuration()
    return budget


def _freeze_input(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    producers: CurrentContinuationMessageProducers,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    recorder: BufferingRecorder,
    drained: RecorderDrainCursors,
    cost_budget: CommonProviderCostBudget | None,
) -> PreparedPrimaryInvocation:
    if (
        type(service) is not ComposerServiceImpl
        or type(producers) is not CurrentContinuationMessageProducers
        or type(selected) is not SelectedContinuationState
        or producers.service is not service
        or producers.authority is not authority
        or producers.selected is not selected
        or type(recorder) is not BufferingRecorder
        or type(drained) is not RecorderDrainCursors
    ):
        raise AuditIntegrityError("primary input requires the actual current message/recorder producers")

    def prepared_from_current(
        current_inputs: _CurrentProducerInputs, budget: CommonProviderCostBudget | None, *, reprove_after: bool
    ) -> PreparedPrimaryInvocation:
        frame = selected.stack.frames[-1]
        if type(frame) is not ComposeFrame or frame.phase != "action_ready" or frame.pending_response is not None:
            raise AuditIntegrityError("primary input requires its actual action-ready compose owner")
        by_ref = {chunk.reference: chunk for chunk in selected.chunks}
        shelf = decode_message_shelf(by_ref[frame.transcript])
        recipes = shelf.producer_recipes or (None,) * len(shelf.messages_json)
        captured = tuple(producers.capture_message(message) for message in messages)
        expected = tuple((shelf.messages_json[index], recipes[index]) for index in shelf.current_indexes)
        if captured != expected:
            raise AuditIntegrityError("primary input changed its actual complete current message producers")
        if frame.tools is None:
            if tools:
                raise AuditIntegrityError("primary input added tools absent from its actual selected frame")
        elif canonical_json(owned_json(tools)) != by_ref[frame.tools].canonical:
            raise AuditIntegrityError("primary input changed its complete selected tool schemas")
        stored_messages, error = parse_json_strict(by_ref[frame.messages].canonical)
        if (
            error is not None
            or type(stored_messages) is not dict
            or set(stored_messages) != {"messages_json", "current_indexes"}
            or tuple(stored_messages["messages_json"]) != shelf.messages_json
            or tuple(stored_messages["current_indexes"]) != shelf.current_indexes
        ):
            raise AuditIntegrityError("primary input changed its selected complete message/shelf join")
        if recorder.snapshot_for_continuation(drained=drained) != decode_recorder_snapshot(by_ref[frame.audit_buffer]):
            raise AuditIntegrityError("primary input changed its complete current recorder prefix")
        # The SQL worker does not carry the async prompt ContextVar. The
        # current bootstrap owns the original authenticated human input.
        try:
            request = service._provider_gateway.prepare_continuation_primary(
                messages, tools, user_prompt=current_inputs.values.message, cost_budget=budget
            )
        except ComposerContextUnsupported:
            if reprove_after:
                producers._require_current()
            raise
        if reprove_after:
            producers._require_current()
        provider_input = request.provider_input
        return PreparedPrimaryInvocation(
            request, provider_input.evidence.messages, provider_input.tools if provider_input.tools is not None else (), budget
        )

    policy = decode_continuation_policy(selected.stack.admission_policy_json, selected.stack.policy_hash)
    if type(policy) is not ContinuationAdmissionPolicyV2:
        current_inputs = producers._require_current()
        return prepared_from_current(current_inputs, cost_budget, reprove_after=True)
    # This one fresh lock/memo brackets the current producer proof, full SQL
    # cost replay, and local input freeze. It closes before any DML or SDK.
    with (
        locked_session_transaction(authority._engine, str(producers.inventory.running.claim.session_id)) as conn,
        retention_stage_memo_scope(),
    ):
        current_inputs = producers._require_current_on_connection(conn)
        current_budget = _current_cost_budget(conn, selected, policy, cost_budget)
        return prepared_from_current(current_inputs, current_budget, reprove_after=False)


async def run_continuation_primary(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    producers: CurrentContinuationMessageProducers,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    recorder: BufferingRecorder,
    drained: RecorderDrainCursors,
    required_work: RequiredWorkBinding,
    provider_owner: ProviderInvocationOwner,
    timeout: float,
    on_sdk_entry: Callable[[], None] | None = None,
) -> PrimaryProviderResult | PrimaryNoSdkRefusalReceipt:
    _require_owners(authority, required_work)
    running = required_work.running
    if (
        running is None
        or type(service) is not ComposerServiceImpl
        or type(producers) is not CurrentContinuationMessageProducers
        or (
            type(provider_owner) is not ProviderInvocationOwner
            or provider_owner.required_work is not required_work
            or provider_owner.service is not service._require_sessions_service()
            or producers.inventory.running is not running
        )
    ):
        raise AuditIntegrityError("primary invocation crossed actual service/claim/provider owners")
    if type(selected) is not SelectedContinuationState or type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise AuditIntegrityError("primary invocation requires its actual selected input and finite positive allowance")
    policy = decode_continuation_policy(selected.stack.admission_policy_json, selected.stack.policy_hash)
    started = time.monotonic()
    clock = ContinuationActionClock(policy.actions.primary)
    budget = provider_owner.cost_budget
    if type(policy) is not ContinuationAdmissionPolicyV2 and budget is not None:
        raise AuditIntegrityError("legacy Continuation policy cannot activate a current cost allowance")
    if timeout > policy.actions.primary.total:
        raise AuditIntegrityError("primary invocation cannot renew its retained action allowance")
    deadline = started + timeout
    deadline_owner = required_work.coordinator.sql_deadline_owner
    if deadline_owner is not None:
        if deadline_owner.running is not running:
            raise AuditIntegrityError("primary invocation changed its actual original timing owner")
        deadline = min(deadline, started + deadline_owner.remaining_dispatch(monotonic_now=started))
    if deadline <= started:
        raise TimeoutError("primary action exhausted its original allowance before input preparation")
    sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION)
    try:
        captured = await run_required_sql_finish_once(
            sql,
            _freeze_input,
            service=service,
            authority=authority,
            selected=selected,
            producers=producers,
            messages=messages,
            tools=tools,
            recorder=recorder,
            drained=drained,
            cost_budget=budget,
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    invocation = _project(required_work, sql, projection, RequiredWorkSource.CONTINUATION_READ_SQL, captured)
    if type(invocation) is not PreparedPrimaryInvocation:
        raise AuditIntegrityError("primary input preparation lost its actual request")
    if type(policy) is ContinuationAdmissionPolicyV2:
        if type(invocation.cost_budget) is not CommonProviderCostBudget:
            raise AuditIntegrityError("PRIMARY input lost its original cost owner")
        if budget is None:
            provider_owner.bind_cost_budget(invocation.cost_budget)
        elif invocation.cost_budget is not budget:
            raise AuditIntegrityError("PRIMARY input replaced its original cost owner")
        if provider_owner.cost_budget is not invocation.cost_budget:
            raise AuditIntegrityError("PRIMARY could not bind its original cost owner")
    elif invocation.cost_budget is not budget:
        raise AuditIntegrityError("legacy PRIMARY input changed its cost applicability")
    if time.monotonic() >= deadline:
        raise TimeoutError("primary action allowance expired during actual input preparation")
    issuer = PrimaryNoSdkIssuer(
        running=running,
        work=required_work,
        selected_checkpoint_id=selected.checkpoint_id,
        selected_cursor=selected.stack.cursor,
        input_sha256=invocation.request.input_sha256,
        clock=clock,
    )
    sql, projection = required_work.reserve_pair(
        RequiredWorkSource.CONTINUATION_STATE_SQL, RequiredWorkSource.CONTINUATION_STATE_PROJECTION
    )
    try:
        prepared_outcome = await run_required_sql_finish_once(
            sql,
            authority.prepare_provider_action,
            running,
            family=0,
            input_sha256=invocation.request.input_sha256,
            snapshot_json=encode_continuation_snapshot(selected.stack),
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.stack.cursor,
            primary_input_json=continuation_provider_input_json(invocation.request.kwargs),
            no_sdk_issuer=issuer,
        )
    except BaseException as error:
        projection.complete_without_submission()
        if issuer.prepared_candidate is not None:
            reconciled, read_deferred = await _read_primary_candidate(
                authority=authority, required_work=required_work, issuer=issuer, begin=False
            )
            failure = _pre_sdk_failure(error, preparing=True)
            if reconciled is not None and failure is not None:
                return await _finish_primary_no_sdk(
                    authority=authority,
                    required_work=required_work,
                    issuer=issuer,
                    reason=failure[0],
                    original_failure=failure[1],
                    original_cancellations=(*failure[2], *read_deferred),
                )
            if read_deferred:
                raise BaseExceptionGroup("PRIMARY preparation and readback cancellations", [error, *read_deferred]) from None
        raise
    if type(prepared_outcome) is RequiredSQLReturned:
        required_work.coordinator.validate_continuation_sql_outcome(
            ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_STATE_SQL, actual_outcome=prepared_outcome
        )
        issuer.bind_prepared(prepared_outcome.value)
    elif type(prepared_outcome) is RequiredSQLRaised and issuer.prepared_candidate is not None:
        reconciled, read_deferred = await _read_primary_candidate(
            authority=authority, required_work=required_work, issuer=issuer, begin=False
        )
        failure = _pre_sdk_failure(prepared_outcome.error, preparing=True)
        if reconciled is not None and failure is not None:
            projection.complete_without_submission()
            return await _finish_primary_no_sdk(
                authority=authority,
                required_work=required_work,
                issuer=issuer,
                reason=failure[0],
                original_failure=failure[1],
                original_cancellations=(*failure[2], *prepared_outcome.deferred_cancellations, *read_deferred),
            )
        if read_deferred:
            projection.complete_without_submission()
            raise BaseExceptionGroup(
                "PRIMARY preparation and readback cancellations",
                [prepared_outcome.error, *prepared_outcome.deferred_cancellations, *read_deferred],
            )
    try:
        prepared = _project(required_work, sql, projection, RequiredWorkSource.CONTINUATION_STATE_SQL, prepared_outcome)
    except BaseException as error:
        failure = _pre_sdk_failure(error, preparing=True)
        if issuer.can_refuse() and failure is not None:
            return await _finish_primary_no_sdk(
                authority=authority,
                required_work=required_work,
                issuer=issuer,
                reason=failure[0],
                original_failure=failure[1],
                original_cancellations=failure[2],
            )
        raise
    if type(prepared) is not PreparedContinuationProvider:
        raise AuditIntegrityError("primary preparation lost its actual committed effect")
    try:
        clock.require_preparation_time()
        if time.monotonic() >= deadline:
            raise TimeoutError("primary action allowance expired after actual effect preparation")
    except TimeoutError as error:
        return await _finish_primary_no_sdk(
            authority=authority,
            required_work=required_work,
            issuer=issuer,
            reason=PrimaryNoSdkRefusalReason.PREPARATION_EXPIRED,
            original_failure=error,
        )
    sql, projection = required_work.reserve_pair(
        RequiredWorkSource.CONTINUATION_STATE_SQL, RequiredWorkSource.CONTINUATION_STATE_PROJECTION
    )
    try:
        dispatch_outcome = await run_required_sql_finish_once(sql, authority.begin_continuation_primary, prepared, no_sdk_issuer=issuer)
    except BaseException as error:
        projection.complete_without_submission()
        if issuer.dispatched_candidate is not None:
            reconciled, read_deferred = await _read_primary_candidate(
                authority=authority, required_work=required_work, issuer=issuer, begin=True
            )
            failure = _pre_sdk_failure(error, preparing=True)
            if reconciled is not None and failure is not None:
                return await _finish_primary_no_sdk(
                    authority=authority,
                    required_work=required_work,
                    issuer=issuer,
                    reason=failure[0],
                    original_failure=failure[1],
                    original_cancellations=(*failure[2], *read_deferred),
                )
            if read_deferred:
                raise BaseExceptionGroup("PRIMARY begin and readback cancellations", [error, *read_deferred]) from None
        raise
    if type(dispatch_outcome) is RequiredSQLReturned:
        required_work.coordinator.validate_continuation_sql_outcome(
            ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_STATE_SQL, actual_outcome=dispatch_outcome
        )
        issuer.bind_dispatched(dispatch_outcome.value)
    elif type(dispatch_outcome) is RequiredSQLRaised and issuer.dispatched_candidate is not None:
        reconciled, read_deferred = await _read_primary_candidate(
            authority=authority, required_work=required_work, issuer=issuer, begin=True
        )
        failure = _pre_sdk_failure(dispatch_outcome.error, preparing=True)
        if reconciled is not None and failure is not None:
            projection.complete_without_submission()
            return await _finish_primary_no_sdk(
                authority=authority,
                required_work=required_work,
                issuer=issuer,
                reason=failure[0],
                original_failure=failure[1],
                original_cancellations=(*failure[2], *dispatch_outcome.deferred_cancellations, *read_deferred),
            )
        if read_deferred:
            projection.complete_without_submission()
            raise BaseExceptionGroup(
                "PRIMARY begin and readback cancellations",
                [dispatch_outcome.error, *dispatch_outcome.deferred_cancellations, *read_deferred],
            )
    try:
        dispatched = _project(required_work, sql, projection, RequiredWorkSource.CONTINUATION_STATE_SQL, dispatch_outcome)
    except BaseException as error:
        failure = _pre_sdk_failure(error, preparing=True)
        if issuer.can_refuse() and failure is not None:
            return await _finish_primary_no_sdk(
                authority=authority,
                required_work=required_work,
                issuer=issuer,
                reason=failure[0],
                original_failure=failure[1],
                original_cancellations=failure[2],
            )
        raise
    if type(dispatched) is not StoredContinuationSnapshot:
        raise AuditIntegrityError("primary dispatch intent lost its actual complete root")
    try:
        clock.finish_preparation()
    except TimeoutError as error:
        return await _finish_primary_no_sdk(
            authority=authority,
            required_work=required_work,
            issuer=issuer,
            reason=PrimaryNoSdkRefusalReason.PREPARATION_EXPIRED,
            original_failure=error,
        )
    try:
        custody = provider_owner.mint(ProviderInvocationFamily.PRIMARY)
        custody.bind_continuation_primary_clock(clock)
        custody.bind_primary_no_sdk_issuer(issuer)
        frame = selected.stack.frames[-1]
        if type(frame) is not ComposeFrame:
            raise AuditIntegrityError("primary dispatch lost its actual compose state")
        # The original current-message owner supplies this decoder's admission.
        by_ref = {chunk.reference: chunk for chunk in selected.chunks}
        state = _state_from_tool_json(by_ref[frame.state].canonical, env_ref_names=producers.inventory.names)
        now = time.monotonic()
        remaining = deadline - now
        if deadline_owner is not None:
            remaining = min(remaining, deadline_owner.remaining_dispatch(monotonic_now=now))
        if remaining <= 0:
            raise TimeoutError("primary action allowance expired before actual provider entry")
        provider_input = invocation.request.provider_input
        service._provider_gateway.verify_input(provider_input)
        if canonical_json(invocation.messages) != canonical_json(provider_input.evidence.messages) or canonical_json(
            invocation.tools
        ) != canonical_json(provider_input.tools if provider_input.tools is not None else ()):
            raise AuditIntegrityError("Primary invocation changed its original admitted wire snapshot")
        actual = await service._provider_gateway._call_llm_outcome_with_audit(
            provider_input.evidence.messages,
            provider_input.tools if provider_input.tools is not None else [],
            timeout=remaining,
            recorder=recorder,
            provider_custody=custody,
            credential_state=state,
            continuation_input_sha256=invocation.request.input_sha256,
            prepared_input=provider_input,
            on_sdk_entry=on_sdk_entry,
        )
    except BaseException as error:
        failure = _pre_sdk_failure(error, preparing=False)
        if failure is not None and issuer.can_refuse():
            return await _finish_primary_no_sdk(
                authority=authority,
                required_work=required_work,
                issuer=issuer,
                reason=failure[0],
                original_failure=failure[1],
                original_cancellations=failure[2],
            )
        raise
    snapshot = recorder.snapshot_for_continuation(drained=drained)
    sql, projection = required_work.reserve_pair(
        RequiredWorkSource.CONTINUATION_STATE_SQL, RequiredWorkSource.CONTINUATION_STATE_PROJECTION
    )
    try:
        published = await clock.run(
            "retention",
            lambda: run_required_sql_finish_once(
                sql,
                authority.publish_continuation_primary,
                prepared.binding,
                selected=dispatched,
                outcome=actual,
                recorder=snapshot,
                env_ref_names=producers.inventory.names,
            ),
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    result = _project(required_work, sql, projection, RequiredWorkSource.CONTINUATION_STATE_SQL, published)
    if type(result) is not PrimaryProviderResult:
        raise AuditIntegrityError("primary publication lost its actual complete result")
    return result


async def read_continuation_primary(
    *,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    required_work: RequiredWorkBinding,
) -> PrimaryProviderResult:
    _require_owners(authority, required_work)
    running = required_work.running
    if type(running) is not ComposerOperationRunning:
        raise AuditIntegrityError("primary recovery requires its actual nominal running owner")
    if type(selected) is not SelectedContinuationState:
        raise AuditIntegrityError("primary recovery requires its actual complete nominal selection")
    selected.__post_init__()
    frame = selected.stack.frames[-1]
    if type(frame) is not ComposeFrame or frame.provider_effect_key is None or frame.provider_input_sha256 is None:
        raise AuditIntegrityError("primary recovery lost its actual selected provider locator")
    sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION)
    try:
        outcome = await run_required_sql_finish_once(
            sql,
            authority.read_continuation_primary,
            running,
            selected=selected,
            checkpoint_id=selected.checkpoint_id,
            cursor=selected.stack.cursor,
            effect_key=frame.provider_effect_key,
            input_sha256=frame.provider_input_sha256,
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    result = _project(required_work, sql, projection, RequiredWorkSource.CONTINUATION_READ_SQL, outcome)
    if type(result) is not PrimaryProviderResult:
        raise AuditIntegrityError("primary source read lost its actual committed result")
    return result

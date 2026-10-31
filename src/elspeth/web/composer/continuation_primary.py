"""Actual primary provider publication using the existing gateway and SQL owner.

This action retains a provider result. The existing compose phase owners still
apply its completion, enforce the tool cap and confirm delivery afterward.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import freeze_fields
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.continuation import ComposeFrame
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors, decode_recorder_snapshot
from elspeth.web.composer.continuation_codec import encode_continuation_snapshot, owned_json
from elspeth.web.composer.continuation_message_shelf import decode_message_shelf
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_prompt_producers import CurrentContinuationMessageProducers
from elspeth.web.composer.continuation_provider_input import continuation_provider_input_json
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.continuation_tool_batch import _state_from_tool_json
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.composer.provider_gateway import PreparedContinuationPrimaryRequest
from elspeth.web.composer.provider_quota import ProviderInvocationFamily, ProviderInvocationOwner
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.required_sql_outcomes import RequiredSQLFinishOnce, RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkBinding, RequiredWorkSource, RequiredWorkTicket
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import PreparedContinuationProvider, StoredContinuationSnapshot
from elspeth.web.sessions.continuation_primary import PrimaryProviderResult


@dataclass(frozen=True, slots=True)
class PreparedPrimaryInvocation:
    request: PreparedContinuationPrimaryRequest
    messages: Sequence[Mapping[str, Any]] = field(repr=False)
    tools: Sequence[Mapping[str, Any]] = field(repr=False)

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
    current_inputs = producers._require_current()
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
    # Source checks precede the new request preparation. The finish-once SQL
    # worker does not carry the async prompt ContextVar; its actual admitted
    # bootstrap owns the original authenticated human input explicitly.
    try:
        request = service._provider_gateway.prepare_continuation_primary(messages, tools, user_prompt=current_inputs.values.message)
    except ComposerContextUnsupported:
        producers._require_current()
        raise
    producers._require_current()
    provider_input = request.provider_input
    return PreparedPrimaryInvocation(
        request, provider_input.evidence.messages, provider_input.tools if provider_input.tools is not None else ()
    )


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
) -> PrimaryProviderResult:
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
    if timeout > policy.actions.primary.total:
        raise AuditIntegrityError("primary invocation cannot renew its retained action allowance")
    started = time.monotonic()
    deadline = started + timeout
    deadline_owner = required_work.coordinator.sql_deadline_owner
    if deadline_owner is not None:
        if deadline_owner.running is not running:
            raise AuditIntegrityError("primary invocation changed its actual original timing owner")
        deadline = min(deadline, started + deadline_owner.remaining_dispatch(monotonic_now=started))
    if deadline <= started:
        raise TimeoutError("primary action exhausted its original allowance before input preparation")
    # None is the retained lifecycle-v2 cost contract. An applicable monetary
    # owner needs its separately approved prepared-input/allowance join.
    if provider_owner.cost_budget is not None:
        raise AuditIntegrityError("primary continuation cannot replace an applicable monetary allowance owner")
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
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    invocation = _project(required_work, sql, projection, RequiredWorkSource.CONTINUATION_READ_SQL, captured)
    if type(invocation) is not PreparedPrimaryInvocation:
        raise AuditIntegrityError("primary input preparation lost its actual request")
    if time.monotonic() >= deadline:
        raise TimeoutError("primary action allowance expired during actual input preparation")
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
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    prepared = _project(required_work, sql, projection, RequiredWorkSource.CONTINUATION_STATE_SQL, prepared_outcome)
    if type(prepared) is not PreparedContinuationProvider:
        raise AuditIntegrityError("primary preparation lost its actual committed effect")
    if time.monotonic() >= deadline:
        raise TimeoutError("primary action allowance expired after actual effect preparation")
    sql, projection = required_work.reserve_pair(
        RequiredWorkSource.CONTINUATION_STATE_SQL, RequiredWorkSource.CONTINUATION_STATE_PROJECTION
    )
    try:
        dispatch_outcome = await run_required_sql_finish_once(sql, authority.begin_continuation_primary, prepared)
    except BaseException:
        projection.complete_without_submission()
        raise
    dispatched = _project(required_work, sql, projection, RequiredWorkSource.CONTINUATION_STATE_SQL, dispatch_outcome)
    if type(dispatched) is not StoredContinuationSnapshot:
        raise AuditIntegrityError("primary dispatch intent lost its actual complete root")
    custody = provider_owner.mint(ProviderInvocationFamily.PRIMARY)
    frame = selected.stack.frames[-1]
    if type(frame) is not ComposeFrame:
        raise AuditIntegrityError("primary dispatch lost its actual compose state")
    # This existing decoder's current inventory admission is supplied by the
    # actual current message owner; no environment value is saved or renewed.
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
    )
    snapshot = recorder.snapshot_for_continuation(drained=drained)
    sql, projection = required_work.reserve_pair(
        RequiredWorkSource.CONTINUATION_STATE_SQL, RequiredWorkSource.CONTINUATION_STATE_PROJECTION
    )
    try:
        published = await run_required_sql_finish_once(
            sql,
            authority.publish_continuation_primary,
            prepared.binding,
            selected=dispatched,
            outcome=actual,
            recorder=snapshot,
            env_ref_names=producers.inventory.names,
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

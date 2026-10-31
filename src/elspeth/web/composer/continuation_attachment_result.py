"""Finish-once entry points for actual attachment result/history ownership."""

from __future__ import annotations

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer.audit import DispatchAudit
from elspeth.web.composer.continuation_attachment_read import read_current_attachment
from elspeth.web.composer.continuation_dispatch_audit import capture_dispatch_audit
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory, materialize_continuation_env_inventory
from elspeth.web.composer.continuation_selection import SelectedContinuationState, load_selected_continuation
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.tool_batch import ContinuationCallState
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.required_sql_outcomes import RequiredSQLFinishOnce, RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkBinding, RequiredWorkSource, RequiredWorkTicket
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_attachment_result import (
    AttachmentEffectPreparation,
    AttachmentResultCheckpoint,
    _input,
    require_attachment_read_audit,
)
from elspeth.web.sessions.service import SessionServiceImpl


def _owners(
    authority: ComposerAsyncOperationAuthority, inventory: CurrentContinuationEnvInventory, work: RequiredWorkBinding
) -> ComposerOperationRunning:
    if (
        type(authority) is not ComposerAsyncOperationAuthority
        or type(inventory) is not CurrentContinuationEnvInventory
        or type(work) is not RequiredWorkBinding
        or type(work.running) is not ComposerOperationRunning
        or inventory.running is not work.running
    ):
        raise AuditIntegrityError("attachment operation lost current required-work ownership")
    running = work.running
    work.validate_context(running.session_operation_context)
    owner = work.coordinator.authority
    if (
        owner.authority_kind is not RequiredAuthorityKind.DURABLE_COMPOSE
        or owner.durable_operation_id != running.claim.operation_id
        or owner.claim_attempt != running.claim.attempt
    ):
        raise AuditIntegrityError("attachment operation changed its actual required-work claim")
    return running


def _project[T](
    work: RequiredWorkBinding,
    sql: RequiredWorkTicket,
    projection: RequiredWorkTicket,
    source: RequiredWorkSource,
    outcome: RequiredSQLFinishOnce[T],
) -> T:
    from elspeth.web.sessions.continuation_attachment_application import StoredAttachmentApplication

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
                AttachmentEffectPreparation,
                AttachmentResultCheckpoint,
                StoredContinuationSnapshot,
                StoredAttachmentApplication,
            ):
                raise AuditIntegrityError("attachment operation returned an unfamiliar result owner")
            value = outcome.value
        except BaseException as error:
            projection.complete_owned(error)
            errors.append(error)
        else:
            projection.complete_owned()
    else:
        projection.complete_without_submission()
        raise AuditIntegrityError("attachment operation returned an unfamiliar finish-once outcome")
    for cancellation in outcome.deferred_cancellations:
        if all(cancellation is not error for error in errors):
            errors.append(cancellation)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("Attachment result SQL outcomes", errors)
    if value is None:
        raise AuditIntegrityError("attachment operation lost its actual returned result")
    return value


async def run_attachment_result(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    required_work: RequiredWorkBinding,
    audit: DispatchAudit | None = None,
) -> StoredContinuationSnapshot:
    running = _owners(authority, inventory, required_work)
    if type(service) is not ComposerServiceImpl:
        raise AuditIntegrityError("attachment execution requires its actual Composer service")
    sessions = service._require_sessions_service()
    if type(sessions) is not SessionServiceImpl or sessions._engine is not authority._engine:
        raise AuditIntegrityError("attachment execution crossed actual session services")
    if audit is not None:
        _initial_compose, _initial_frame, initial_data, _initial_key, _initial_digest = _input(selected, inventory.names)
        require_attachment_read_audit(initial_data, capture_dispatch_audit(audit, env_ref_names=inventory.names), names=inventory.names)
    sql, projection = required_work.reserve_pair(
        RequiredWorkSource.CONTINUATION_STATE_SQL, RequiredWorkSource.CONTINUATION_STATE_PROJECTION
    )
    try:
        outcome = await run_required_sql_finish_once(
            sql, authority.prepare_attachment_result, running, selected=selected, inventory=inventory
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    prepared = _project(required_work, sql, projection, RequiredWorkSource.CONTINUATION_STATE_SQL, outcome)
    if type(prepared) is not AttachmentEffectPreparation:
        raise AuditIntegrityError("attachment preparation lost its actual checkpoint")
    current = await load_selected_continuation(
        authority=authority, running=running, selected=prepared.snapshot, required_work=required_work
    )
    current_inventory = await materialize_continuation_env_inventory(
        advisor=service._advisor_checkpoint, authority=authority, selected=current, required_work=required_work
    )
    _compose, _frame, data, _key, _digest = _input(current, current_inventory.names)
    call = data.batch.current_call
    if type(call) is not ContinuationCallState or call.arguments is None:
        raise AuditIntegrityError("attachment execution lost its actual prepared arguments")
    if audit is not None:
        require_attachment_read_audit(
            data, capture_dispatch_audit(audit, env_ref_names=current_inventory.names), names=current_inventory.names
        )
    actual = await read_current_attachment(
        service=service,
        authority=authority,
        selected=current,
        inventory=current_inventory,
        tool_name=call.tool_name,
        arguments=call.arguments,
        state=call.pre_state,
        required_work=required_work,
    )
    # Observe the original actual timer after the read finishes, before SQL
    # publication. Never derive elapsed time from a previous process anchor.
    read_audit = None if audit is None else capture_dispatch_audit(audit, env_ref_names=current_inventory.names)
    sql, projection = required_work.reserve_pair(
        RequiredWorkSource.CONTINUATION_STATE_SQL, RequiredWorkSource.CONTINUATION_STATE_PROJECTION
    )
    try:
        published = await run_required_sql_finish_once(
            sql,
            authority.publish_attachment_result,
            running,
            selected=current,
            inventory=current_inventory,
            actual=actual,
            pre_state=call.pre_state,
            read_audit=read_audit,
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    result = _project(required_work, sql, projection, RequiredWorkSource.CONTINUATION_STATE_SQL, published)
    if type(result) is not StoredContinuationSnapshot:
        raise AuditIntegrityError("attachment publication lost its actual checkpoint")
    return result


async def read_attachment_result(
    *,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    required_work: RequiredWorkBinding,
) -> AttachmentResultCheckpoint:
    running = _owners(authority, inventory, required_work)
    sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION)
    try:
        outcome = await run_required_sql_finish_once(sql, authority.read_attachment_result, running, selected=selected, inventory=inventory)
    except BaseException:
        projection.complete_without_submission()
        raise
    result = _project(required_work, sql, projection, RequiredWorkSource.CONTINUATION_READ_SQL, outcome)
    if type(result) is not AttachmentResultCheckpoint:
        raise AuditIntegrityError("attachment source read lost its complete result")
    return result


async def apply_attachment_history(
    *,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    required_work: RequiredWorkBinding,
) -> StoredContinuationSnapshot:
    running = _owners(authority, inventory, required_work)
    sql, projection = required_work.reserve_pair(
        RequiredWorkSource.CONTINUATION_STATE_SQL, RequiredWorkSource.CONTINUATION_STATE_PROJECTION
    )
    try:
        outcome = await run_required_sql_finish_once(
            sql, authority.apply_attachment_history, running, selected=selected, inventory=inventory
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    result = _project(required_work, sql, projection, RequiredWorkSource.CONTINUATION_STATE_SQL, outcome)
    if type(result) is not StoredContinuationSnapshot:
        raise AuditIntegrityError("attachment history application lost its exact checkpoint")
    return result

"""Actual TITLE producer and owed-accounting joins; no title/ready writer."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMCompletion
from elspeth.web.composer.continuation import InitializeFrame
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import _read_current_env_names, materialize_continuation_env_inventory
from elspeth.web.composer.continuation_initialization import _registered, _sessions
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.continuation_title_values import capture_title_provider_result
from elspeth.web.composer.provider_audit_source import provider_attempt_audit_drafts
from elspeth.web.composer.provider_quota import ProviderCallCustody, ProviderInvocationFamily, ProviderInvocationOwner
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredWorkBinding, RequiredWorkSource
from elspeth.web.sessions._auto_title import _maybe_auto_title_session_required
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import StoredContinuationSnapshot, load_snapshot
from elspeth.web.sessions.continuation_title_provider import (
    CurrentTitleNames,
    CurrentTitleResult,
    PreparedTitleProvider,
    TitleAuditSettlementRequest,
    _current,
    prepare_title_provider_on_connection,
    read_title_result_on_connection,
    require_title_creator,
    retain_title_response_on_connection,
)
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.schemas import SendMessageRequest


def _running(work: RequiredWorkBinding) -> ComposerOperationRunning:
    if type(work) is not RequiredWorkBinding or type(work.running) is not ComposerOperationRunning:
        raise AuditIntegrityError("TITLE requires its actual current durable work owner")
    work.validate_context(work.running.session_operation_context)
    return work.running


async def prepare_current_title_provider(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: StoredContinuationSnapshot,
    required_work: RequiredWorkBinding,
) -> PreparedTitleProvider:
    running = _running(required_work)
    if (
        type(service) is not ComposerServiceImpl
        or type(authority) is not ComposerAsyncOperationAuthority
        or _sessions(service)._engine is not authority._engine
    ):
        raise AuditIntegrityError("TITLE preparation requires the same actual current SQL store")
    root = decode_continuation_snapshot(selected.payload_json)
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    clock = ContinuationActionClock(policy.actions.title)
    before_read = asyncio.get_running_loop().time()
    current = await load_selected_continuation(authority=authority, running=running, selected=selected, required_work=required_work)
    inventory = await materialize_continuation_env_inventory(
        advisor=service._advisor_checkpoint, authority=authority, selected=current, required_work=required_work
    )

    def prepare() -> PreparedTitleProvider:
        with locked_session_transaction(authority._engine, str(running.claim.session_id)) as conn:
            return prepare_title_provider_on_connection(
                conn, service, running, selected=selected, clock=clock, authority=authority, inventory=inventory, work=required_work
            )

    result = await _registered(
        work=required_work, source=RequiredWorkSource.CONTINUATION_STATE_SQL, operation=prepare, expected=PreparedTitleProvider
    )
    # A slow read cannot renew either the action deadline or the real segment.
    clock.deadline = min(clock.deadline, before_read + result.timing.hard_remaining_seconds)
    clock.finish_preparation()
    return result


async def run_prepared_title_provider(
    *, prepared: PreparedTitleProvider, invocation_owner: ProviderInvocationOwner
) -> TitleAuditSettlementRequest | None:
    if type(prepared) is not PreparedTitleProvider or type(invocation_owner) is not ProviderInvocationOwner:
        raise AuditIntegrityError("TITLE execution requires its actual prepared/current invocation owners")
    if invocation_owner.required_work is not prepared.work or invocation_owner.service is not _sessions(prepared.composer):
        raise AuditIntegrityError("TITLE execution changed its actual original work/service")
    custody = invocation_owner.mint(ProviderInvocationFamily.TITLE)
    custody.bind_continuation_title(prepared)
    settings = prepared.composer._settings
    request = prepared.request
    from elspeth.web.composer.continuation_bootstrap import bootstrap_values, decode_continuation_bootstrap
    from elspeth.web.sessions.continuation_title_provider import _chunk

    def original_user() -> str:
        with locked_session_transaction(prepared.authority._engine, str(prepared.running.claim.session_id)) as conn:
            _current(conn, prepared.running, prepared.selected)
            frame = decode_continuation_snapshot(prepared.selected.payload_json).frames[0]
            if not isinstance(frame, InitializeFrame) or type(frame) is not InitializeFrame or frame.bootstrap is None:
                raise AuditIntegrityError("TITLE producer lost its original bootstrap")
            values = bootstrap_values(decode_continuation_bootstrap(_chunk(conn, prepared.running, frame.bootstrap)))
            original_request = values.request
            if not isinstance(original_request, SendMessageRequest) or type(original_request) is not SendMessageRequest:
                raise AuditIntegrityError("TITLE producer lost its actual original SendMessage request")
            return original_request.content

    user = await _registered(work=prepared.work, source=RequiredWorkSource.CONTINUATION_READ_SQL, operation=original_user, expected=str)
    await _maybe_auto_title_session_required(
        service=_sessions(prepared.composer),
        session_id=prepared.running.claim.session_id,
        model=request.kwargs["model"],
        temperature=request.kwargs.get("temperature"),
        seed=request.kwargs.get("seed"),
        session_operation_context=prepared.running.session_operation_context,
        kwargs=request.kwargs,
        audit_messages=request.audit_messages,
        provider_custody=custody,
        prompt_guard_mount=prepared.composer.composer_prompt_guard_mount,
        prompt_guard_mode=settings.composer_prompt_guard_mode,
        user_message=user,
    )
    return custody.continuation_title_result


def _read_selected(service: ComposerServiceImpl, running: ComposerOperationRunning) -> StoredContinuationSnapshot:
    with locked_session_transaction(_sessions(service)._engine, str(running.claim.session_id)) as conn:
        selected = load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)
        _current(conn, running, selected, owed=True)
        return selected


async def current_title_names(
    *, service: ComposerServiceImpl, running: ComposerOperationRunning, work: RequiredWorkBinding
) -> CurrentTitleNames:
    if _running(work) is not running:
        raise AuditIntegrityError("TITLE names read changed its actual required-work running owner")
    before = await _registered(
        work=work,
        source=RequiredWorkSource.CONTINUATION_READ_SQL,
        operation=lambda: _read_selected(service, running),
        expected=StoredContinuationSnapshot,
    )
    # Same installed names-only producer; the TITLE-only bracket permits owed
    # accounting after cancellation without granting any new action.
    names = await _read_current_env_names(service._advisor_checkpoint, str(running.claim.session_id))
    after = await _registered(
        work=work,
        source=RequiredWorkSource.CONTINUATION_READ_SQL,
        operation=lambda: _read_selected(service, running),
        expected=StoredContinuationSnapshot,
    )
    if before != after:
        raise AuditIntegrityError("TITLE names changed its complete current root during metadata read")
    return CurrentTitleNames(running, after, names)


async def retain_current_title_success(custody: ProviderCallCustody, completion: _AdmittedLLMCompletion) -> None:
    prepared = custody.continuation_title
    if type(prepared) is not PreparedTitleProvider:
        raise AuditIntegrityError("TITLE retention lost its original action clock")
    await prepared.clock.run("retention", lambda: _retain_current_title_success_once(custody, completion))


async def _retain_current_title_success_once(custody: ProviderCallCustody, completion: _AdmittedLLMCompletion) -> None:
    prepared = custody.continuation_title
    if type(prepared) is not PreparedTitleProvider or custody.call is None or custody.attempt is None:
        raise AuditIntegrityError("TITLE retained result requires its actual original physical producer")
    names = await current_title_names(service=prepared.composer, running=prepared.running, work=prepared.work)
    if custody.continuation_title_selected != names.selected:
        raise AuditIntegrityError("TITLE response lost its exact physical admission root")
    root = decode_continuation_snapshot(names.selected.payload_json)
    from elspeth.web.sessions.continuation_title_provider import _descriptor

    creator_sequence, creator_generation = require_title_creator(prepared.running)
    captured = capture_title_provider_result(
        logical=root.logical,
        actor_user_id=prepared.actor_user_id,
        effect_key="provider:3:0",
        input_sha256=_descriptor(prepared.input).input_sha256,
        creator_sequence=creator_sequence,
        creator_generation=creator_generation,
        completion=completion,
        audit_call=custody.call,
        context=custody.context,
        audit_drafts=provider_attempt_audit_drafts(custody.call, custody.context),
        env_ref_names=names.names,
    )

    def retain() -> TitleAuditSettlementRequest:
        with locked_session_transaction(prepared.authority._engine, str(prepared.running.claim.session_id)) as conn:
            names.require(conn)
            return retain_title_response_on_connection(conn, prepared, selected=names.selected, captured=captured, names=names.names)

    async def retain_once() -> None:
        sql, projection = prepared.work.reserve_pair(
            RequiredWorkSource.CONTINUATION_STATE_SQL, RequiredWorkSource.CONTINUATION_STATE_PROJECTION
        )
        try:
            outcome = await run_required_sql_finish_once(sql, retain)
        except BaseException:
            projection.complete_without_submission()
            raise
        try:
            prepared.work.coordinator.validate_continuation_sql_outcome(
                ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_STATE_SQL, actual_outcome=outcome
            )
        except BaseException:
            projection.complete_without_submission()
            raise
        errors: list[BaseException] = []
        if type(outcome) is RequiredSQLRaised:
            projection.complete_without_submission()
            errors.append(outcome.error)
        elif type(outcome) is RequiredSQLReturned:
            projection.begin_projection()
            try:
                if type(outcome.value) is not TitleAuditSettlementRequest:
                    raise AuditIntegrityError("TITLE retention lost its committed original result")
                # Install the committed locator before any original cancellation
                # is delivered to the original provider scope's one finisher.
                custody.continuation_title_result = outcome.value
                custody.continuation_title_selected = outcome.value.selected
            except BaseException as error:
                projection.complete_owned(error)
                errors.append(error)
            else:
                projection.complete_owned()
        else:
            projection.complete_without_submission()
            raise AuditIntegrityError("TITLE retention returned no owned SQL result")
        errors.extend(cancellation for cancellation in outcome.deferred_cancellations if all(cancellation is not error for error in errors))
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("TITLE retention original outcomes", errors)

    await retain_once()


async def read_current_title_result(
    *, service: ComposerServiceImpl, running: ComposerOperationRunning, required_work: RequiredWorkBinding
) -> CurrentTitleResult:
    names = await current_title_names(service=service, running=running, work=required_work)

    def read() -> CurrentTitleResult:
        with locked_session_transaction(_sessions(service)._engine, str(running.claim.session_id)) as conn:
            names.require(conn)
            return read_title_result_on_connection(conn, running, selected=names.selected, inventory=names)

    return await _registered(
        work=required_work, source=RequiredWorkSource.CONTINUATION_READ_SQL, operation=read, expected=CurrentTitleResult
    )


async def refresh_current_title_settlement(custody: ProviderCallCustody) -> TitleAuditSettlementRequest:
    prepared, original = custody.continuation_title, custody.continuation_title_result
    if type(prepared) is not PreparedTitleProvider or type(original) is not TitleAuditSettlementRequest:
        raise AuditIntegrityError("TITLE settlement lost its actual retained producer")
    current = await read_current_title_result(service=prepared.composer, running=prepared.running, required_work=prepared.work)
    if current.settlement.result != original.result or current.settlement.audit_call != original.audit_call:
        raise AuditIntegrityError("TITLE settlement changed its actual original response/audit")
    # Preserve the warm original object/cohort after complete actual SQL readmission.
    return replace(current.settlement, audit_call=original.audit_call, audit_drafts=original.audit_drafts)


async def settle_current_title_result(
    *, service: ComposerServiceImpl, running: ComposerOperationRunning, required_work: RequiredWorkBinding
) -> CurrentTitleResult:
    current = await read_current_title_result(service=service, running=running, required_work=required_work)
    if current.settled:
        return current
    sql = required_work.coordinator.reserve(
        RequiredWorkSource.TITLE_PROVIDER_SETTLEMENT_SQL,
        transition_ordinal=required_work.transition_ordinal,
        semantic_ordinal=6,
        recurrence_ordinal=0,
    )
    # Same original finisher/audit inserter/ledger, now using the fresh writer.
    await _sessions(service).finish_provider_attempt(
        session_operation_context=running.session_operation_context,
        call=current.settlement.audit_call,
        required_work=sql,
        continuation_title=current.settlement,
    )
    return await read_current_title_result(service=service, running=running, required_work=required_work)

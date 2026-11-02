"""Original issued preview producer and once-only returned-value adoption.

The SQL issue/hold and original action clock precede every producer. These
process-local markers are never recreated by cold decode. An unknown issued
effect cannot re-enter the producer after restart.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

from sqlalchemy import Connection, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_preview_observation import PreviewSourceData, require_preview_source_matches
from elspeth.web.composer.continuation_value_batch_preparation import PreparedValueBatchCall
from elspeth.web.composer.discovery_response import AdmittedDiscoveryResult, admit_discovery_result, serialize_admitted_discovery_result
from elspeth.web.composer.no_tool_policy import is_pending_interpretation_handoff
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.composer.tools._dispatch import execute_tool
from elspeth.web.composer.tools.generation import MalformedSourceProofClaim, _admit_source_proof_claims
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.execution.schemas import ValidationResult
from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn, ComposerOperationRunning
from elspeth.web.sessions.continuation import _require_current_continuation_running, load_snapshot
from elspeth.web.sessions.continuation_preview_result import (
    CompletePreviewReturn,
    PreviewReturned,
    preview_return_fields,
    project_original_preview_return,
    read_preview_terminal_hold_on_connection,
    require_issued_preview_return,
)
from elspeth.web.sessions.continuation_sql_deadline import ContinuationPreviewResultDeadline
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_effects_table as effects

if TYPE_CHECKING:
    from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome
    from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
    from elspeth.web.composer.continuation_runtime import RestoredContinuationRuntime
    from elspeth.web.composer.continuation_selection import SelectedContinuationState
    from elspeth.web.composer.tool_batch import BatchAccumulator, ContinuationBatchState, ContinuationCallState, ToolBatchContext
    from elspeth.web.required_sql_outcomes import RequiredSQLFinishOnce
    from elspeth.web.required_work import RequiredPreviewProduced, RequiredWorkSource, RequiredWorkTicket


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class IssuedPreviewDispatch:
    """One actual SQL issue, not a constructible/copyable dispatch capability."""


@dataclass(slots=True)
class _Issued:
    authority: ComposerAsyncOperationAuthority
    running: ComposerOperationRunning
    prepared: PreparedValueBatchCall
    source: PreviewSourceData
    clock: ContinuationPreviewResultDeadline
    preflight_started: bool = False
    preflight_completed: bool = False
    strict: ValidationResult | None = None
    tolerant: ValidationResult | None = None
    handler_started: bool = False
    raw_result: ToolResult | None = None
    admitted: AdmittedDiscoveryResult | None = None
    returned: PreviewReturned | None = None
    original_error: BaseException | None = None
    adopted: bool = False
    finish_ticket_identity: int | None = None


_ISSUED: WeakKeyDictionary[IssuedPreviewDispatch, _Issued] = WeakKeyDictionary()
_LOCK = threading.RLock()


def _issued(permit: IssuedPreviewDispatch) -> _Issued:
    with _LOCK:
        if type(permit) is not IssuedPreviewDispatch or permit not in _ISSUED:
            raise AuditIntegrityError("preview has no original issued dispatch identity")
        return _ISSUED[permit]


def _before_producer(record: _Issued) -> None:
    owner = record.clock.owner
    if owner.capture_preview_result_deadline() != record.clock:
        raise AuditIntegrityError("preview producer changed its original action clock")
    if owner.drain_signal.started_at is not None or owner.remaining_dispatch(monotonic_now=time.monotonic()) <= 0:
        raise TimeoutError("preview producer exhausted its original action/drain allowance")
    if record.prepared.context.cancellation_requested.is_set():
        raise ComposerOperationCancelledDuringTurn(record.running.claim)


def bind_issued_preview_dispatch_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    authority: ComposerAsyncOperationAuthority,
    prepared: PreparedValueBatchCall,
    source: PreviewSourceData,
    clock: ContinuationPreviewResultDeadline,
) -> IssuedPreviewDispatch:
    """Bind the genuine current issue once, before original async preflight."""
    if type(authority) is not ComposerAsyncOperationAuthority or conn.engine is not authority._engine:
        raise AuditIntegrityError("preview dispatch changed its actual session store owner")
    if type(clock) is not ContinuationPreviewResultDeadline or clock.owner.running is not running:
        raise AuditIntegrityError("preview dispatch requires its original pre-producer action clock")
    require_preview_source_matches(source, prepared)
    _require_current_continuation_running(conn, running)
    contract = read_preview_terminal_hold_on_connection(
        conn,
        session_id=source.session_id,
        operation_id=source.operation_id,
        effect_key=f"tool:{source.response.digest}:{source.tool_index}",
        env_ref_names=prepared.runtime.env_ref_names,
    )
    stored = load_snapshot(conn, session_id=source.session_id, operation_id=source.operation_id)
    if contract.source != source or stored.cursor != source.cursor + 1:
        raise AuditIntegrityError("preview dispatch lost its exact current reserved issue")
    record = _Issued(authority, running, prepared, source, clock)
    _before_producer(record)
    changed = conn.execute(
        update(effects)
        .where(
            effects.c.session_id == source.session_id,
            effects.c.operation_id == source.operation_id,
            effects.c.effect_key == f"tool:{source.response.digest}:{source.tool_index}",
            effects.c.owner == "tool_dispatch",
            effects.c.disposition == "dispatched",
            effects.c.response_started_at.is_(None),
            effects.c.creator_sequence == source.sequence,
            effects.c.creator_generation == source.generation,
        )
        .values(response_started_at=database_now(conn))
    )
    if changed.rowcount != 1:
        raise AuditIntegrityError("preview producer cannot repeat an entered or foreign issue")
    permit = IssuedPreviewDispatch()
    with _LOCK:
        _ISSUED[permit] = record
    return permit


async def prepare_original_preview_preflight(permit: IssuedPreviewDispatch) -> None:
    """Use the unchanged real coordinator, cache, principal and snapshot."""
    record = _issued(permit)
    _before_producer(record)
    require_preview_source_matches(record.source, record.prepared)
    with _LOCK:
        if record.preflight_started:
            raise AuditIntegrityError("preview cannot repeat its original async preflight")
        record.preflight_started = True
    ctx, state = record.prepared.context, record.prepared.call.pre_state
    try:
        try:
            _admit_source_proof_claims(state)
        except MalformedSourceProofClaim:
            # Preserve the original handler's coded source-claim refusal;
            # that branch never starts runtime validation.
            record.preflight_completed = True
            return
        record.strict = await ctx.preflight.cached_runtime_preflight(
            state,
            user_id=ctx.user_id,
            session_id=ctx.session_id,
            cache=ctx.runtime_preflight_cache,
            initial_version=ctx.initial_version,
            session_scope=ctx.session_scope,
            llm_calls=ctx.recorder.llm_calls,
            plugin_snapshot=ctx.plugin_snapshot,
            session_operation_context=ctx.session_operation_context,
        )
        if is_pending_interpretation_handoff(record.strict):
            record.tolerant = await ctx.preflight.cached_runtime_preflight(
                state,
                user_id=ctx.user_id,
                session_id=ctx.session_id,
                cache=ctx.runtime_preflight_cache,
                initial_version=ctx.initial_version,
                session_scope=ctx.session_scope,
                llm_calls=ctx.recorder.llm_calls,
                plugin_snapshot=ctx.plugin_snapshot,
                session_operation_context=ctx.session_operation_context,
                interpretation_tolerant=True,
            )
        record.preflight_completed = True
    except BaseException as original:
        record.original_error = original
        raise


def produce_original_preview(permit: IssuedPreviewDispatch) -> RequiredPreviewProduced:
    """Enter the real synchronous handler once under ordinary action timing."""
    from elspeth.web.required_work import RequiredPreviewProduced

    record = _issued(permit)
    _before_producer(record)
    prepared, source = record.prepared, record.source
    require_preview_source_matches(source, prepared)
    with _LOCK:
        if not record.preflight_completed or record.handler_started or record.original_error is not None:
            raise AuditIntegrityError("preview cannot repeat or bypass its original preflight/handler")
        record.handler_started = True
    ctx, call = prepared.context, prepared.call
    arguments = call.arguments
    if type(arguments) is not dict or arguments:
        raise AuditIntegrityError("preview handler lost its original admitted empty arguments")
    data = prepared.runtime.graph.batch
    if data is None or data.batch.current_call is not call:
        raise AuditIntegrityError("preview handler lost its actual prepared batch")
    with locked_session_transaction(record.authority._engine, source.session_id) as conn:
        _require_current_continuation_running(conn, record.running)
        row = conn.execute(
            select(effects).where(
                effects.c.session_id == source.session_id,
                effects.c.operation_id == source.operation_id,
                effects.c.effect_key == f"tool:{source.response.digest}:{source.tool_index}",
            )
        ).one()
        if row.disposition != "dispatched" or row.response_started_at is None or row.response_finished_at is not None:
            raise AuditIntegrityError("preview handler changed its original entered SQL issue")

    def strict_callback(state: CompositionState) -> ValidationResult:
        if state is not call.pre_state or record.strict is None:
            raise AuditIntegrityError("preview strict callback changed its original state/result")
        return record.strict

    def tolerant_callback(state: CompositionState) -> ValidationResult:
        if state is not call.pre_state or record.tolerant is None:
            raise AuditIntegrityError("preview tolerant callback changed its original state/result")
        return record.tolerant

    try:
        _before_producer(record)
        result = execute_tool(
            "preview_pipeline",
            arguments,
            call.pre_state,
            ctx.policy_catalog,
            plugin_snapshot=ctx.plugin_snapshot,
            data_dir=ctx.custody.data_dir,
            session_engine=ctx.custody.session_engine,
            session_id=ctx.session_id,
            session_operation_context=ctx.session_operation_context,
            session_operation_authority=ctx.session_operation_authority,
            secret_service=ctx.custody.secret_service,
            secret_wiring_policy=ctx.custody.secret_wiring_policy,
            user_id=ctx.user_id,
            prior_validation=data.batch.last_validation,
            runtime_preflight=strict_callback if record.strict is not None else None,
            structural_preflight=tolerant_callback if record.tolerant is not None else None,
            max_blob_storage_per_session_bytes=ctx.custody.max_blob_storage_per_session_bytes,
            user_message_id=ctx.user_message_id,
            user_message_content=ctx.user_message_content,
            composer_model_identifier=ctx.provenance.model_identifier,
            composer_model_version=data.batch.provider_model_version,
            composer_provider=ctx.provenance.provider or "unknown",
            composer_skill_hash=ctx.provenance.skill_hash,
            tool_arguments_hash=prepared.original_audit.binding_arguments_hash,
            mutation_ceiling=ctx.mutation_ceiling,
            validate_arguments=True,
            require_data_dir_for_paths=True,
            raise_schema_argument_errors=True,
        )
        record.raw_result = result
        record.admitted = admit_discovery_result("preview_pipeline", result)
        record.returned = project_original_preview_return(prepared, source=source, result=result, tolerant=record.tolerant)
        return RequiredPreviewProduced(permit, record.returned)
    except BaseException as original:
        record.original_error = original
        raise


def require_preview_produced(permit: IssuedPreviewDispatch, returned: PreviewReturned) -> ContinuationPreviewResultDeadline:
    record = _issued(permit)
    if record.returned is not returned or record.raw_result is not preview_return_fields(returned)[1]:
        raise AuditIntegrityError("preview produced carrier replaced its actual original result")
    if preview_return_fields(returned)[0] != record.source or record.original_error is not None:
        raise AuditIntegrityError("preview produced carrier changed its original source/outcome")
    require_issued_preview_return(returned)
    return record.clock


def claim_preview_finish(permit: IssuedPreviewDispatch, returned: PreviewReturned, ticket: RequiredWorkTicket) -> None:
    from elspeth.web.required_work import RequiredWorkSource, RequiredWorkTicket

    clock = require_preview_produced(permit, returned)
    if (
        type(ticket) is not RequiredWorkTicket
        or ticket.key.source is not RequiredWorkSource.CONTINUATION_PREVIEW_RESULT_SQL
        or ticket.sql_deadline_owner is not clock.owner
    ):
        raise AuditIntegrityError("preview finish changed its original returned-result ticket")
    clock.owner.validate_context(
        ticket.key.authority.context,
        operation_id=ticket.key.authority.durable_operation_id,
        attempt=ticket.key.authority.claim_attempt,
    )
    record = _issued(permit)
    with _LOCK:
        if record.finish_ticket_identity is not None and record.finish_ticket_identity != id(ticket):
            raise AuditIntegrityError("preview cannot submit a second disposition owner")
        record.finish_ticket_identity = id(ticket)


def require_preview_adoption(
    permit: IssuedPreviewDispatch,
    *,
    call_model: _CallModelOutcome,
    ctx: ToolBatchContext,
    acc: BatchAccumulator,
    batch: ContinuationBatchState,
    call: ContinuationCallState,
) -> None:
    """Prove the exact original native owners before pure returned adoption."""
    record = _issued(permit)
    prepared = record.prepared
    data = prepared.runtime.graph.batch
    if (
        data is None
        or call_model is not prepared.call_model
        or ctx is not prepared.context
        or acc is not data.accumulator
        or batch is not data.batch
        or call is not prepared.call
        or batch.current_call is not call
    ):
        raise AuditIntegrityError("preview adoption changed its actual original prepared owners")
    require_preview_source_matches(record.source, prepared)


def original_preview_preflight(permit: IssuedPreviewDispatch, *, tolerant: bool = False) -> ValidationResult:
    """Return the original coordinator value; this never invokes validation."""
    record = _issued(permit)
    if not record.preflight_completed:
        if record.original_error is not None and not record.handler_started:
            raise record.original_error
        raise AuditIntegrityError("preview has no original completed preflight")
    result = record.tolerant if tolerant else record.strict
    if result is None:
        raise AuditIntegrityError("preview substituted its original preflight branch")
    return result


def release_preview_dispatch(permit: IssuedPreviewDispatch) -> None:
    """Drop live owners only after their registered producer/finish joined."""
    _issued(permit)
    with _LOCK:
        del _ISSUED[permit]


def original_adopted_preview_result(permit: IssuedPreviewDispatch, *, prepared: PreparedValueBatchCall) -> AdmittedDiscoveryResult:
    """Read original returned DATA after native adoption changed batch phase."""
    record = _issued(permit)
    if record.prepared is not prepared or not record.adopted or type(record.returned) is not CompletePreviewReturn:
        raise AuditIntegrityError("preview message lost its original adopted producer")
    require_preview_produced(permit, record.returned)
    if record.admitted is None or record.admitted.result is not record.raw_result:
        raise AuditIntegrityError("preview message replaced its original discovery result")
    if serialize_admitted_discovery_result(record.admitted) != record.returned.feedback:
        raise AuditIntegrityError("preview message changed its complete original feedback")
    return record.admitted


async def try_admit_preview_tool(
    owner: ContinuationLivePhases,
    *,
    runtime: RestoredContinuationRuntime,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
) -> bool:
    """The actual ordinary singleton preview route, including its owed tail."""
    from dataclasses import replace
    from datetime import UTC, datetime

    from elspeth.web.composer.continuation import EffectFrontierEntry, ToolBatchFrame
    from elspeth.web.composer.continuation_batch_data import decode_prepared_batch_data
    from elspeth.web.composer.continuation_codec import PrivateChunk
    from elspeth.web.composer.continuation_dispatch_audit import capture_dispatch_audit
    from elspeth.web.composer.continuation_initialization import _registered
    from elspeth.web.composer.continuation_preview_observation import preview_source_data
    from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime, capture_continuation_runtime_owners
    from elspeth.web.composer.continuation_value_batch_builder import adopt_issued_preview_batch
    from elspeth.web.composer.continuation_value_batch_preparation import prepare_issued_preview_batch
    from elspeth.web.composer.tool_batch import ContinuationCallState, _prepare_continuation_call_live
    from elspeth.web.required_work import RequiredWorkSource
    from elspeth.web.sessions.continuation import StoredContinuationSnapshot
    from elspeth.web.sessions.continuation_preview_result import (
        OversizedPreviewReturn,
        PreviewValidationIncompleteTerminal,
        retain_preview_validation_incomplete_on_connection,
    )
    from elspeth.web.sessions.continuation_value_batch_tail import retain_value_batch_tail_on_connection
    from elspeth.web.sessions.service import SessionServiceImpl

    data = runtime.graph.batch
    if data is None or len(data.calls) != 1 or data.calls[0].function.name != "preview_pipeline":
        return False
    if data.batch.phase != "call_ready" or data.batch.current_call is not None or data.batch.next_tool_index != 0:
        raise AuditIntegrityError("preview cannot repeat an issued or completed producer")
    sessions = owner.service._sessions_service
    clock_owner = owner.work.coordinator.sql_deadline_owner
    if type(sessions) is not SessionServiceImpl or clock_owner is None:
        raise AuditIntegrityError("preview requires its actual publication and original action clock owners")
    if owner.action_clock is None:
        raise AuditIntegrityError("preview requires its original driver-begun action clock")
    clock_owner.require_action_clock(owner.action_clock)
    clock = clock_owner.capture_preview_result_deadline()
    primary = await owner._primary_for_phase(runtime, inventory, selected)
    context = await owner._tool_context(runtime)
    call = ContinuationCallState(0, data.calls[0], data.batch.state, data.batch.state.version)
    data.batch.current_call, data.batch.phase = call, "call_active"
    preparation = await _prepare_continuation_call_live(
        call_model=primary.outcome, ctx=context, llm_messages=runtime.live.llm_messages, batch=data.batch, call=call
    )
    if preparation != "prepared" or call.audit is None:
        raise AuditIntegrityError("preview requires its original successfully prepared native call")
    issued_runtime = replace(
        runtime,
        graph=runtime.graph.with_selected_dispatch(
            capture_dispatch_audit(call.audit, env_ref_names=inventory.names), context.tool_contract_dialect
        ),
    )
    captured = await capture_continuation_runtime_owners(issued_runtime, selected=selected, inventory=inventory)
    top = captured.stack.frames[-1]
    if type(top) is not ToolBatchFrame:
        raise AuditIntegrityError("preview preparation changed its actual tool frame")
    chunks = tuple(chunk for chunk in captured.chunks if chunk.reference == top.accumulator)
    if len(chunks) != 1 or type(chunks[0]) is not PrivateChunk:
        raise AuditIntegrityError("preview preparation lost its exact captured original prepared bytes")
    encoded = chunks[0].canonical
    portable = decode_prepared_batch_data(encoded, env_ref_names=inventory.names).audit
    prepared = PreparedValueBatchCall(primary.outcome, context, runtime, call, encoded, call.audit, replace(call.audit), portable)
    source = preview_source_data(prepared)
    key = f"tool:{top.response.digest}:0"
    root = captured.stack.model_copy(
        update={
            "frames": (*captured.stack.frames[:-1], top.model_copy(update={"phase": "effect_reconciling"})),
            "effect_frontier": (
                *captured.stack.effect_frontier,
                EffectFrontierEntry(effect_id=key, disposition="dispatched", receipt_id=None),
            ),
        }
    )
    await owner._retain(CapturedContinuationRuntime(selected, root, captured.chunks), inventory, "tool_issued", preview_source=source)
    current = owner.runtime
    if current is None:
        raise AuditIntegrityError("preview issue lost its actual current retained runtime")
    current_selected, current_inventory = current.source.selected, current.materialization.inventory

    bound_by_sql: IssuedPreviewDispatch | None = None

    def bind() -> IssuedPreviewDispatch:
        nonlocal bound_by_sql
        with locked_session_transaction(owner.authority._engine, source.session_id) as conn:
            bound = bind_issued_preview_dispatch_on_connection(
                conn, inventory.running, authority=owner.authority, prepared=prepared, source=source, clock=clock
            )
        bound_by_sql = bound
        return bound

    reserved: list[RequiredWorkTicket] = []
    original_failure: BaseException | None = None
    try:
        permit = await _registered(
            work=owner.work, source=RequiredWorkSource.CONTINUATION_STATE_SQL, operation=bind, expected=IssuedPreviewDispatch
        )
        producer_sql, producer_projection = owner.work.reserve_pair(
            RequiredWorkSource.CONTINUATION_PREVIEW_PRODUCER_SQL, RequiredWorkSource.CONTINUATION_PREVIEW_PRODUCER_PROJECTION
        )
        reserved.extend((producer_sql, producer_projection))
        finish_sql, finish_projection = owner.work.reserve_pair(
            RequiredWorkSource.CONTINUATION_PREVIEW_RESULT_SQL, RequiredWorkSource.CONTINUATION_PREVIEW_RESULT_PROJECTION
        )
        reserved.extend((finish_sql, finish_projection))
        await prepare_original_preview_preflight(permit)
        produced, cancellations = await _produce_preview_owned(owner, permit, producer_sql, producer_projection)

        async def dispose_returned() -> tuple[
            StoredContinuationSnapshot | PreviewValidationIncompleteTerminal, tuple[asyncio.CancelledError, ...]
        ]:
            if type(produced.returned) is CompletePreviewReturn:
                await adopt_issued_preview_batch(prepared=prepared, permit=permit)

            def finish(actual: RequiredPreviewProduced) -> StoredContinuationSnapshot | PreviewValidationIncompleteTerminal:
                with (
                    locked_session_transaction(owner.authority._engine, source.session_id) as conn,
                    sessions._session_write_lock(conn, source.session_id),
                ):
                    if type(actual.returned) is OversizedPreviewReturn:
                        terminal = retain_preview_validation_incomplete_on_connection(
                            conn, inventory.running, returned=actual.returned, env_ref_names=inventory.names, recorded_at=datetime.now(UTC)
                        )
                        value: StoredContinuationSnapshot | PreviewValidationIncompleteTerminal = terminal
                    else:
                        batch = prepare_issued_preview_batch(
                            owner,
                            prepared=prepared,
                            produced=actual,
                            runtime=current,
                            selected=current_selected,
                            inventory=current_inventory,
                        )
                        value = retain_value_batch_tail_on_connection(
                            conn, inventory.running, service=sessions, batch=batch, published_at=datetime.now(UTC), issued_preview=actual
                        ).snapshot
                # Preserve the actual committing return before bridge delivery.
                if type(value) is PreviewValidationIncompleteTerminal:
                    owner.preview_terminal = value
                    owner.latest_snapshot = value.snapshot
                elif type(value) is StoredContinuationSnapshot:
                    owner.latest_snapshot = value
                else:
                    raise AuditIntegrityError("Preview disposition returned another nominal snapshot")
                owner.runtime = None
                owner.primary = None
                owner.p4 = None
                owner.finished_batch = None
                return value

            return await _finish_preview_owned(owner, produced, finish_sql, finish_projection, finish)

        coroutine = dispose_returned()
        try:
            task = asyncio.create_task(coroutine, name="continuation-preview-original-result-disposition")
        except BaseException:
            coroutine.close()
            raise
        from elspeth.web.sessions.routes._helpers import _join_freeform_owned_task

        observed = list(cancellations)
        try:
            (_retained, deferred), _first = await _join_freeform_owned_task(task, cancellation_observations=observed)
        except BaseException as original:
            failures = [original, *(item for item in observed if item is not original)]
            if len(failures) == 1:
                raise
            raise BaseExceptionGroup("Preview original disposition and cancellation", failures) from None
        for cancellation in deferred:
            if all(cancellation is not item for item in observed):
                observed.append(cancellation)
        if len(observed) == 1:
            raise observed[0]
        if observed:
            raise BaseExceptionGroup("Preview original result cancellations", observed)
        return True
    except BaseException as original:
        original_failure = original
        raise
    finally:
        cleanup_failures: list[BaseException] = []
        for ticket in reserved:
            if not ticket.complete:
                try:
                    ticket.complete_without_submission()
                except BaseException as cleanup:
                    cleanup_failures.append(cleanup)
        if bound_by_sql is not None and all(ticket.complete for ticket in reserved):
            try:
                release_preview_dispatch(bound_by_sql)
            except BaseException as cleanup:
                cleanup_failures.append(cleanup)
        if cleanup_failures:
            if original_failure is not None:
                cleanup_failures.insert(0, original_failure)
            raise BaseExceptionGroup("Preview original outcome and required-work cleanup", cleanup_failures)


def _project_preview_outcome[T](
    owner: ContinuationLivePhases,
    *,
    sql: RequiredWorkTicket,
    projection: RequiredWorkTicket,
    source: RequiredWorkSource,
    outcome: RequiredSQLFinishOnce[T],
    allowed: tuple[type[object], ...],
) -> tuple[T, tuple[asyncio.CancelledError, ...]]:
    from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned

    try:
        owner.work.coordinator.validate_continuation_sql_outcome(ticket=sql, expected_source=source, actual_outcome=outcome)
    except BaseException as original:
        try:
            projection.complete_without_submission()
        except BaseException as cleanup:
            raise BaseExceptionGroup("Preview SQL provenance and projection cleanup", [original, cleanup]) from None
        raise
    if type(outcome) is RequiredSQLRaised:
        projection.complete_without_submission()
        failures: list[BaseException] = [outcome.error]
        failures.extend(item for item in outcome.deferred_cancellations if item is not outcome.error)
        if len(failures) == 1:
            raise outcome.error
        raise BaseExceptionGroup("Preview original SQL failure and cancellation", failures)
    if type(outcome) is not RequiredSQLReturned:
        projection.complete_without_submission()
        raise AuditIntegrityError("Preview SQL returned no original physical outcome")
    projection.begin_projection()
    try:
        if type(outcome.value) not in allowed:
            raise AuditIntegrityError("Preview projection substituted its nominal original outcome")
        value = outcome.value
    except BaseException as original:
        projection.complete_owned(original)
        raise
    else:
        projection.complete_owned()
    return value, outcome.deferred_cancellations


async def _produce_preview_owned(
    owner: ContinuationLivePhases, permit: IssuedPreviewDispatch, sql: RequiredWorkTicket, projection: RequiredWorkTicket
) -> tuple[RequiredPreviewProduced, tuple[asyncio.CancelledError, ...]]:
    from elspeth.web.async_workers import run_required_sql_finish_once
    from elspeth.web.required_work import RequiredPreviewProduced, RequiredWorkSource

    outcome = await run_required_sql_finish_once(sql, lambda: produce_original_preview(permit))
    value, cancellations = _project_preview_outcome(
        owner,
        sql=sql,
        projection=projection,
        source=RequiredWorkSource.CONTINUATION_PREVIEW_PRODUCER_SQL,
        outcome=outcome,
        allowed=(RequiredPreviewProduced,),
    )
    require_preview_produced(value.permit, value.returned)
    return value, cancellations


async def _finish_preview_owned[T](
    owner: ContinuationLivePhases,
    produced: RequiredPreviewProduced,
    sql: RequiredWorkTicket,
    projection: RequiredWorkTicket,
    finish: Callable[[RequiredPreviewProduced], T],
) -> tuple[T, tuple[asyncio.CancelledError, ...]]:
    from elspeth.web.async_workers import run_required_preview_finish_once
    from elspeth.web.required_work import RequiredWorkSource
    from elspeth.web.sessions.continuation import StoredContinuationSnapshot
    from elspeth.web.sessions.continuation_preview_result import PreviewValidationIncompleteTerminal

    outcome = await run_required_preview_finish_once(sql, produced, finish)
    return _project_preview_outcome(
        owner,
        sql=sql,
        projection=projection,
        source=RequiredWorkSource.CONTINUATION_PREVIEW_RESULT_SQL,
        outcome=outcome,
        allowed=(StoredContinuationSnapshot, PreviewValidationIncompleteTerminal),
    )


def consume_original_preview_result(
    permit: IssuedPreviewDispatch,
    *,
    call_model: _CallModelOutcome,
    ctx: ToolBatchContext,
    acc: BatchAccumulator,
    batch: ContinuationBatchState,
    call: ContinuationCallState,
) -> AdmittedDiscoveryResult:
    """Pure original-result adoption; no preflight or dispatcher reexecution."""
    record = _issued(permit)
    require_preview_adoption(permit, call_model=call_model, ctx=ctx, acc=acc, batch=batch, call=call)
    if record.returned is not None and type(record.returned) is CompletePreviewReturn:
        require_preview_produced(permit, record.returned)
        if (
            record.admitted is None
            or record.admitted.result is not record.raw_result
            or serialize_admitted_discovery_result(record.admitted) != record.returned.feedback
        ):
            raise AuditIntegrityError("preview adoption replaced its original discovery result")
    elif record.original_error is None or not record.handler_started or record.raw_result is not None:
        raise AuditIntegrityError("preview has no original complete result or thrown handler")
    with _LOCK:
        if record.adopted:
            raise AuditIntegrityError("preview cannot adopt its original result twice")
        record.adopted = True
    if record.original_error is not None:
        raise record.original_error
    if record.admitted is None:
        raise AuditIntegrityError("preview has no original discovery result")
    return record.admitted

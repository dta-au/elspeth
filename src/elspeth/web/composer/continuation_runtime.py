"""Current restore and capture of the actual Composer iteration owners.

Restoration grants no provider, audit, mutation or inspection action. Existing
phase owners and current writers retain those admissions at actual use.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from uuid import UUID

from elspeth.contracts.composer_progress import ComposerProgressSink
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.compartments import compartment_ingress_record
from elspeth.web.composer._compose_loop_carriers import _ComposeIterationBindings, _ComposeIterationState
from elspeth.web.composer.anti_anchor import AntiAnchorTracker
from elspeth.web.composer.content_scope import composition_scope_for_operation
from elspeth.web.composer.continuation import ComposeFrame, ContinuationFrameStack, PrivateChunkRef, ToolBatchFrame
from elspeth.web.composer.continuation_attachment_history import ContinuationAttachmentHistory
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors, decode_recorder_snapshot, encode_recorder_snapshot
from elspeth.web.composer.continuation_batch_data import (
    RestoredBatchData,
    RestoredPreparedBatchData,
    encode_coupled_batch_data,
    encode_restored_prepared_batch_data,
)
from elspeth.web.composer.continuation_caches import (
    capture_continuation_caches,
    decode_continuation_caches,
    encode_continuation_caches,
    restore_continuation_caches,
)
from elspeth.web.composer.continuation_codec import PrivateChunk, encode_mandatory_private_chunk, encode_private_chunk, verify_private_chunk
from elspeth.web.composer.continuation_current_evidence import restore_current_turn_evidence
from elspeth.web.composer.continuation_dispatch_audit import capture_dispatch_audit
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory
from elspeth.web.composer.continuation_evidence import encode_turn_evidence
from elspeth.web.composer.continuation_evidence_graph import RestoredTurnEvidence, capture_turn_evidence
from elspeth.web.composer.continuation_iteration import capture_continuation_iteration, encode_continuation_iteration, iteration_values
from elspeth.web.composer.continuation_message_shelf import encode_message_shelf
from elspeth.web.composer.continuation_prompt_producers import CurrentContinuationMessageProducers, load_current_message_producers
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.provider_gateway import composer_loop_tool_definitions
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.tool_batch import ToolBatchContext, ToolBatchCustody, ToolBatchProvenance
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.credential_guard import require_no_credential_material_in_state
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkBinding, RequiredWorkSource
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation_runtime import ContinuationRuntimeSource, runtime_chunk
from elspeth.web.sessions.continuation_selection import nested_private_chunks, required_private_chunks

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_finish_reads import ContinuationFinishRead
    from elspeth.web.composer.service import ComposerServiceImpl


@dataclass(frozen=True, slots=True)
class RestoredContinuationRuntime:
    live: _ComposeIterationState
    bindings: _ComposeIterationBindings
    graph: RestoredTurnEvidence
    materialization: CurrentContinuationMessageProducers
    source: ContinuationRuntimeSource
    drained: RecorderDrainCursors
    attachments: ContinuationAttachmentHistory
    env_ref_names: frozenset[str]


@dataclass(frozen=True, slots=True)
class _CurrentPhaseRead:
    """One phase's actual read cohort; every use still re-enters SQL proof."""

    runtime: RestoredContinuationRuntime
    selected: SelectedContinuationState
    inventory: CurrentContinuationEnvInventory
    finish_read: ContinuationFinishRead | None


@dataclass(frozen=True, slots=True)
class CapturedContinuationRuntime:
    selected: SelectedContinuationState
    stack: ContinuationFrameStack
    chunks: tuple[PrivateChunk, ...]


def _work_owners(
    authority: ComposerAsyncOperationAuthority, inventory: CurrentContinuationEnvInventory, work: RequiredWorkBinding
) -> ComposerOperationRunning:
    if (
        type(authority) is not ComposerAsyncOperationAuthority
        or type(inventory) is not CurrentContinuationEnvInventory
        or type(work) is not RequiredWorkBinding
        or type(work.running) is not ComposerOperationRunning
        or inventory.running is not work.running
    ):
        raise AuditIntegrityError("runtime changed its actual current required-work owners")
    running = work.running
    work.validate_context(running.session_operation_context)
    custody = work.coordinator.authority
    if (
        custody.authority_kind is not RequiredAuthorityKind.DURABLE_COMPOSE
        or custody.durable_operation_id != running.claim.operation_id
        or custody.claim_attempt != running.claim.attempt
    ):
        raise AuditIntegrityError("runtime changed its actual current required-work claim")
    return running


async def _registered_read[T](work: RequiredWorkBinding, owner: type[T], operation: Callable[[], T]) -> T:
    sql, projection = work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION)
    try:
        outcome = await run_required_sql_finish_once(sql, operation)
    except BaseException:
        projection.complete_without_submission()
        raise
    try:
        work.coordinator.validate_continuation_sql_outcome(
            ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_READ_SQL, actual_outcome=outcome
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    failures: list[BaseException] = []
    result: T | None = None
    if type(outcome) is RequiredSQLRaised:
        projection.complete_without_submission()
        failures.append(outcome.error)
    elif type(outcome) is RequiredSQLReturned:
        projection.begin_projection()
        try:
            if type(outcome.value) is not owner:
                raise AuditIntegrityError("runtime projection changed its actual nominal returned owner")
            result = outcome.value
        except BaseException as error:
            projection.complete_owned(error)
            failures.append(error)
        else:
            projection.complete_owned()
    else:
        projection.complete_without_submission()
        raise AuditIntegrityError("runtime returned an unfamiliar required-SQL outcome")
    for cancellation in outcome.deferred_cancellations:
        if all(cancellation is not original for original in failures):
            failures.append(cancellation)
    if len(failures) == 1:
        raise failures[0]
    if failures:
        raise BaseExceptionGroup("Continuation runtime original outcomes", failures)
    if result is None:
        raise AuditIntegrityError("runtime lost its complete returned owner")
    return result


def _compose(selected: SelectedContinuationState) -> tuple[int, ComposeFrame]:
    indexes = tuple(index for index, frame in enumerate(selected.stack.frames) if type(frame) is ComposeFrame)
    if len(indexes) != 1:
        raise AuditIntegrityError("runtime lost its single actual Compose frame")
    frame = selected.stack.frames[indexes[0]]
    if type(frame) is not ComposeFrame:
        raise AuditIntegrityError("runtime changed its selected nominal Compose frame")
    return indexes[0], frame


def _restore_live(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    work: RequiredWorkBinding,
    provider_owner: ProviderInvocationOwner,
    source: ContinuationRuntimeSource,
    graph: RestoredTurnEvidence,
    materialization: CurrentContinuationMessageProducers,
    monotonic_before_read: float,
    progress: ComposerProgressSink | None,
    finish_read: ContinuationFinishRead | None = None,
) -> RestoredContinuationRuntime:
    from elspeth.web.composer.service import _chat_ingress_inputs_for_compose

    _index, frame = _compose(selected)
    names = inventory.names
    original = source.original
    initial = graph.evidence.effects.initial_state
    if frame.state.codec != "composer.state.v1":
        raise AuditIntegrityError("runtime current state changed its actual closed codec")
    selected_state = runtime_chunk(selected, frame.state).canonical
    if graph.batch is not None:
        state = graph.batch.batch.state
    else:
        from elspeth.web.composer.continuation_evidence import state_from_evidence_json

        state = state_from_evidence_json(selected_state)
    if (
        canonical_json(state.to_dict()) != selected_state
        or runtime_chunk(selected, frame.initial_state).canonical != original.snapshot.initial_state_json
        or canonical_json(initial.to_dict()) != original.snapshot.initial_state_json
        or initial.version != frame.initial_version
        or frame.current_state_id != graph.evidence.effects.latest_committed_state_id
    ):
        raise AuditIntegrityError("runtime changed original base or actual shared state/frontier")
    require_no_credential_material_in_state(state, surface="current continuation runtime state", env_ref_names=names)
    if runtime_chunk(selected, frame.audit_buffer) != encode_recorder_snapshot(
        graph.recorder.snapshot_for_continuation(drained=graph.drained)
    ):
        raise AuditIntegrityError("runtime recorder differs from its one shared evidence graph")
    if runtime_chunk(selected, frame.transcript) != encode_message_shelf(source.evidence.transcript):
        raise AuditIntegrityError("runtime transcript differs from its complete selected shelf")
    expected_messages = canonical_json(
        {"messages_json": source.evidence.transcript.messages_json, "current_indexes": source.evidence.transcript.current_indexes}
    )
    if runtime_chunk(selected, frame.messages).canonical != expected_messages:
        raise AuditIntegrityError("runtime messages differ from the shared selected transcript")
    snapshot, catalog = service._policy_context.build(original.snapshot.actor_user_id)
    current = catalog.validate_composition_state(state)
    if canonical_json(current.authored_state.to_dict()) != selected_state:
        raise AuditIntegrityError("current policy requires authored-state reinspection before runtime restore")
    composition_scope_for_operation(
        state,
        session_id=selected.stack.logical.session_id,
        session_operation_context=inventory.running.session_operation_context,
        session_operation_authority=service._require_sessions_service().session_operation_authority,
    )
    tools = composer_loop_tool_definitions(service._planner_dialect)
    if (
        frame.tools is None
        or frame.tools.codec != "composer.tools.v1"
        or runtime_chunk(selected, frame.tools).canonical != canonical_json(tools)
    ):
        raise AuditIntegrityError("runtime tool contract changed and requires current reacquisition")
    if graph.selected_dialect is not None and graph.selected_dialect is not service._planner_dialect:
        raise AuditIntegrityError("runtime prepared wire dialect changed and requires current reacquisition")
    values = iteration_values(source.iteration, env_ref_names=names)
    cache_history = decode_continuation_caches(runtime_chunk(selected, frame.caches), env_ref_names=names)
    if cache_history.session_id != selected.stack.logical.session_id:
        raise AuditIntegrityError("runtime cache history belongs to another session")
    caches = restore_continuation_caches(cache_history, env_ref_names=names)
    attachments = source.attachments
    if (frame.persisted_assistant_message_id is None) != (frame.persisted_assistant_content is None):
        raise AuditIntegrityError("runtime lost its paired persisted assistant identity/content")
    if any(type(index) is not int or not 0 <= index < len(graph.messages) for index in frame.pending_advisor_elision_indices):
        raise AuditIntegrityError("runtime pending Advisor elision lost its exact message positions")
    ceiling = selected.stack.mutation_ceiling.materialize()
    ceiling.validate_context(inventory.running.session_operation_context)
    live = _ComposeIterationState(
        state=state,
        initial_version=frame.initial_version,
        llm_messages=graph.messages,
        tools=tools,
        recorder=graph.recorder,
        composition_turns_used=frame.composition_slice,
        discovery_turns_used=frame.discovery_slice,
        mutation_success_seen=frame.mutation_success_seen,
        discovery_cache=caches.discovery,
        last_validation=current.validation,
        runtime_preflight_cache=caches.preflight,
        last_runtime_preflight=caches.last_preflight,
        anti_anchor=AntiAnchorTracker.for_continuation(source.iteration.anti_anchor),
        advisor_calls_used=frame.advisor_calls,
        repair_turns_used=frame.repair_turns,
        advisor_checkpoint_passes_used=frame.advisor_checkpoint_passes,
        advisor_review_state=values.advisor_review,
        persisted_assistant_message_id=frame.persisted_assistant_message_id,
        persisted_assistant_content=frame.persisted_assistant_content,
        persisted_tool_call_turn=frame.persisted_tool_call_turn,
        failed_turn=values.failed_turn,
        current_state_id=frame.current_state_id,
        advisor_repair_context_introduced=frame.advisor_repair_context_introduced,
        pending_advisor_elision_indices=list(frame.pending_advisor_elision_indices),
        turn_evidence=graph.evidence,
        continuation_cache_history=caches.history,
    )
    bindings = _ComposeIterationBindings(
        message=original.message,
        session_id=selected.stack.logical.session_id,
        user_id=original.snapshot.actor_user_id,
        user_message_id=selected.stack.logical.original_user_message_id,
        deadline=monotonic_before_read + source.timing.hard_remaining_seconds,
        progress=progress,
        plugin_snapshot=snapshot,
        policy_catalog=catalog,
        mutation_ceiling=ceiling,
        session_operation_context=inventory.running.session_operation_context,
        completion_gates=original.completion_gates,
        diagnostics=None,
        required_work=work,
        provider_owner=provider_owner,
        ingress=compartment_ingress_record(original.message, own_compartment_id=service._settings.compartment_id),
        chat_ingress_inputs=_chat_ingress_inputs_for_compose(
            original.message,
            list(original.history),
            user_message_id=selected.stack.logical.original_user_message_id,
            own_compartment_id=service._settings.compartment_id,
        ),
        actor=f"composer-web:user-{original.snapshot.actor_user_id}",
        session_scope=f"session:{selected.stack.logical.session_id}",
    )
    # A later read can only shorten admission; never use its newer remaining
    # observation to renew the monotonic anchor or original logical budgets.
    if finish_read is None:
        authority.read_continuation_runtime(inventory.running, selected=selected, inventory=inventory)
    else:
        finish_read.require_selection(selected, inventory)
        finish_read.read_runtime(authority)
    service._schema_disclosure.restore_session_for_continuation(
        source.iteration.disclosure,
        session_id=selected.stack.logical.session_id,
        catalog_hash=snapshot.snapshot_hash,
        policy_hash=snapshot.policy_hash,
    )
    return RestoredContinuationRuntime(live, bindings, graph, materialization, source, graph.drained, attachments, names)


async def restore_continuation_runtime(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    required_work: RequiredWorkBinding,
    provider_owner: ProviderInvocationOwner,
    progress: ComposerProgressSink | None = None,
    finish_read: ContinuationFinishRead | None = None,
) -> RestoredContinuationRuntime:
    from elspeth.web.composer.service import ComposerServiceImpl

    running = _work_owners(authority, inventory, required_work)
    if (
        type(service) is not ComposerServiceImpl
        or type(provider_owner) is not ProviderInvocationOwner
        or provider_owner.required_work is not required_work
        or provider_owner.service is not service._require_sessions_service()
    ):
        raise AuditIntegrityError("runtime lost actual current service/provider-work custody")
    monotonic_before_read = time.monotonic()
    if finish_read is not None:
        from elspeth.web.composer.continuation_finish_reads import ContinuationFinishRead

        if type(finish_read) is not ContinuationFinishRead:
            raise AuditIntegrityError("runtime restore received a foreign owed-finish facet")
        finish_read.require_selection(selected, inventory)
    source = await _registered_read(
        required_work,
        ContinuationRuntimeSource,
        lambda: (
            authority.read_continuation_runtime(running, selected=selected, inventory=inventory)
            if finish_read is None
            else finish_read.read_runtime(authority)
        ),
    )
    if type(source) is not ContinuationRuntimeSource:
        raise AuditIntegrityError("runtime source returned another nominal owner")
    production = await load_current_message_producers(
        service=service, authority=authority, selected=selected, inventory=inventory, required_work=required_work, finish_read=finish_read
    )
    graph = await restore_current_turn_evidence(
        service=service,
        authority=authority,
        selected=selected,
        inventory=inventory,
        required_work=required_work,
        materialization=production.producers,
    )
    result = await _registered_read(
        required_work,
        RestoredContinuationRuntime,
        lambda: _restore_live(
            service=service,
            authority=authority,
            selected=selected,
            inventory=inventory,
            work=required_work,
            provider_owner=provider_owner,
            source=source,
            graph=graph,
            materialization=production.producers,
            monotonic_before_read=monotonic_before_read,
            progress=progress,
            finish_read=finish_read,
        ),
    )
    if type(result) is not RestoredContinuationRuntime:
        raise AuditIntegrityError("runtime restore returned another nominal owner")
    return result


async def build_runtime_tool_batch_context(
    runtime: RestoredContinuationRuntime,
    *,
    cancellation_requested: asyncio.Event,
    phase_read: _CurrentPhaseRead | None = None,
) -> ToolBatchContext:
    if type(runtime) is not RestoredContinuationRuntime or type(cancellation_requested) is not asyncio.Event:
        raise AuditIntegrityError("runtime dispatch context requires its actual owners")
    live, bindings = runtime.live, runtime.bindings
    service = runtime.materialization.service
    work = bindings.required_work
    if type(work) is not RequiredWorkBinding:
        raise AuditIntegrityError("runtime context lost its required-work binding")
    authority = runtime.materialization.authority
    selected, inventory = runtime.source.selected, runtime.materialization.inventory
    finish_read: ContinuationFinishRead | None = None
    if phase_read is not None:
        from elspeth.web.composer.continuation_finish_reads import ContinuationFinishRead

        if (
            type(phase_read) is not _CurrentPhaseRead
            or phase_read.runtime is not runtime
            or phase_read.selected is not selected
            or type(phase_read.inventory) is not CurrentContinuationEnvInventory
        ):
            raise AuditIntegrityError("runtime context replaced its actual phase read cohort")
        inventory, finish_read = phase_read.inventory, phase_read.finish_read
        if finish_read is not None:
            if type(finish_read) is not ContinuationFinishRead:
                raise AuditIntegrityError("runtime context received a foreign owed-read facet")
            finish_read.require_selection(selected, inventory)
    running = _work_owners(authority, inventory, work)
    if (
        bindings.session_operation_context is not running.session_operation_context
        or bindings.session_id != str(running.claim.session_id)
        or selected.stack.logical.session_id != str(running.claim.session_id)
        or selected.stack.logical.operation_id != running.claim.operation_id
        or (finish_read is not None and finish_read.running is not running)
    ):
        raise AuditIntegrityError("runtime context crossed its actual phase session/current owner")

    def read_source() -> ContinuationRuntimeSource:
        if finish_read is not None:
            finish_read.require_selection(selected, inventory)
            source = finish_read.read_runtime(authority)
        else:
            source = authority.read_continuation_runtime(running, selected=selected, inventory=inventory)
        if type(source) is not ContinuationRuntimeSource or source.selected is not selected:
            raise AuditIntegrityError("runtime context lost its actual selected SQL read")
        return source

    sessions = service._require_sessions_service()
    await _registered_read(work, ContinuationRuntimeSource, read_source)
    sid = UUID(runtime.source.selected.stack.logical.session_id)
    preferences = await sessions.get_composer_preferences(sid)
    await _registered_read(work, ContinuationRuntimeSource, read_source)
    ceiling = bindings.mutation_ceiling
    if ceiling is None or bindings.session_operation_context is not running.session_operation_context:
        raise AuditIntegrityError("runtime context lost its original mutation ceiling/current fence")
    ceiling.validate_context(running.session_operation_context)
    if ceiling.request_id != live.turn_evidence.request_id:
        raise AuditIntegrityError("runtime dispatch crossed its original logical request")
    return ToolBatchContext(
        session_tools=service._session_tools,
        provenance=ToolBatchProvenance(
            model_identifier=service._model, provider=service._availability.provider, skill_hash=service._composer_skill_hash
        ),
        custody=ToolBatchCustody(
            data_dir=service._data_dir,
            session_engine=service._session_engine,
            secret_service=service._secret_service,
            secret_wiring_policy=service._secret_wiring_policy,
            max_blob_storage_per_session_bytes=service._settings.max_blob_storage_per_session_bytes,
        ),
        redaction_telemetry=service._redaction_telemetry,
        preflight=service._preflight,
        schema_disclosure=service._schema_disclosure,
        advisor_checkpoint=service._advisor_checkpoint,
        advisor_max_calls_per_compose=service._settings.composer_advisor_max_calls_per_compose,
        advisor_timeout_seconds=service._settings.composer_advisor_timeout_seconds,
        recorder=live.recorder,
        anti_anchor=live.anti_anchor,
        discovery_cache=live.discovery_cache,
        runtime_preflight_cache=live.runtime_preflight_cache,
        session_id=bindings.session_id,
        session_operation_context=bindings.session_operation_context,
        user_id=bindings.user_id,
        user_message_id=bindings.user_message_id,
        user_message_content=bindings.message,
        current_state_id=live.current_state_id,
        actor=bindings.actor,
        initial_version=live.initial_version,
        deadline=bindings.deadline,
        progress=bindings.progress,
        session_scope=bindings.session_scope,
        turn_sessions_service=sessions,
        turn_session_uuid=sid,
        turn_preferences=preferences,
        mutation_ceiling=ceiling,
        review_required=not ceiling.permits_auto(preferences.trust_mode),
        cancellation_requested=cancellation_requested,
        plugin_snapshot=bindings.plugin_snapshot,
        policy_catalog=bindings.policy_catalog,
        tool_contract_dialect=service._planner_dialect,
        turn_evidence=live.turn_evidence,
        session_operation_authority=sessions.session_operation_authority,
        provider_owner=bindings.provider_owner,
        composition_turns_used=live.composition_turns_used,
        discovery_turns_used=live.discovery_turns_used,
        failed_turn=live.failed_turn,
        message_producers=runtime.materialization,
    )


def _selected_chunks(
    root: ContinuationFrameStack, original: tuple[PrivateChunk, ...], added: tuple[PrivateChunk, ...]
) -> tuple[PrivateChunk, ...]:
    available: dict[tuple[str, str], PrivateChunk] = {}
    for chunk in (*original, *added):
        verify_private_chunk(chunk.reference, chunk.canonical)
        key = chunk.reference.codec, chunk.reference.digest
        if key in available and available[key] != chunk:
            raise AuditIntegrityError("runtime chunk merge changed an immutable source")
        available[key] = chunk
    pending = deque(required_private_chunks(root))
    selected: list[PrivateChunk] = []
    seen: dict[tuple[str, str], PrivateChunkRef] = {}
    while pending:
        ref = pending.popleft()
        key = ref.codec, ref.digest
        if key in seen:
            if seen[key] != ref:
                raise AuditIntegrityError("runtime source has conflicting chunk bounds")
            continue
        loaded = available.get(key)
        if loaded is None or loaded.reference != ref:
            raise AuditIntegrityError("runtime capture lost a reachable private source")
        seen[key] = ref
        selected.append(loaded)
        pending.extend(nested_private_chunks(loaded))
    return tuple(selected)


def _capture_owners(
    runtime: RestoredContinuationRuntime, *, source: ContinuationRuntimeSource, inventory: CurrentContinuationEnvInventory
) -> CapturedContinuationRuntime:
    live, bindings = runtime.live, runtime.bindings
    selected = source.selected
    root = selected.stack
    index, frame = _compose(selected)
    if (
        bindings.session_operation_context is not inventory.running.session_operation_context
        or source.bootstrap != runtime.source.bootstrap
        or source.policy != runtime.source.policy
        or root.mutation_ceiling != runtime.source.selected.stack.mutation_ceiling
        or bindings.mutation_ceiling != root.mutation_ceiling.materialize()
        or bindings.session_id != root.logical.session_id
        or bindings.user_message_id != root.logical.original_user_message_id
        or bindings.user_id != source.bootstrap.actor_user_id
        or runtime.env_ref_names != inventory.names
    ):
        raise AuditIntegrityError("runtime capture changed original request/ceiling/current source")
    composition_delta = live.composition_turns_used - frame.composition_slice
    discovery_delta = live.discovery_turns_used - frame.discovery_slice
    if (
        composition_delta < 0
        or discovery_delta < 0
        or live.advisor_calls_used < frame.advisor_calls
        or live.repair_turns_used < frame.repair_turns
        or live.advisor_checkpoint_passes_used < frame.advisor_checkpoint_passes
        or (frame.mutation_success_seen and not live.mutation_success_seen)
    ):
        raise AuditIntegrityError("runtime capture cannot reset actual logical counters or mutation history")
    if live.initial_version != frame.initial_version or live.current_state_id != live.turn_evidence.effects.latest_committed_state_id:
        raise AuditIntegrityError("runtime capture changed its original base/current SQL frontier")
    if live.continuation_cache_history is None:
        raise AuditIntegrityError("runtime capture cannot replace complete retained cache history")
    names = inventory.names
    if source.admitted_evidence.snapshot is not source.evidence:
        raise AuditIntegrityError("runtime source lost its resolved original evidence cohort")
    current_recorder = decode_recorder_snapshot(source.admitted_evidence.recorder_chunk)
    actual_recorder = live.recorder.snapshot_for_continuation(drained=runtime.drained)
    if (
        len(actual_recorder.invocations) < len(current_recorder.invocations)
        or len(actual_recorder.llm_calls) < len(current_recorder.llm_calls)
        or len(actual_recorder.planner_attempts) < len(current_recorder.planner_attempts)
        or len(actual_recorder.withheld_replies) < len(current_recorder.withheld_replies)
        or len(actual_recorder.visible_tool_events) < len(current_recorder.visible_tool_events)
        or len(actual_recorder.private_candidates) < len(current_recorder.private_candidates)
        or len(actual_recorder.private_authorings) < len(current_recorder.private_authorings)
        or any(
            actual < retained
            for actual, retained in (
                (actual_recorder.drained.invocations, current_recorder.drained.invocations),
                (actual_recorder.drained.llm_calls, current_recorder.drained.llm_calls),
                (actual_recorder.drained.planner_attempts, current_recorder.drained.planner_attempts),
                (actual_recorder.drained.withheld_replies, current_recorder.drained.withheld_replies),
                (actual_recorder.drained.visible_tool_events, current_recorder.drained.visible_tool_events),
                (actual_recorder.drained.private_candidates, current_recorder.drained.private_candidates),
                (actual_recorder.drained.private_authorings, current_recorder.drained.private_authorings),
            )
        )
    ):
        raise AuditIntegrityError("runtime capture lost its complete current SQL recorder prefix/drains")
    current_prefix = replace(
        actual_recorder,
        invocations=actual_recorder.invocations[: len(current_recorder.invocations)],
        llm_calls=actual_recorder.llm_calls[: len(current_recorder.llm_calls)],
        planner_attempts=actual_recorder.planner_attempts[: len(current_recorder.planner_attempts)],
        withheld_replies=actual_recorder.withheld_replies[: len(current_recorder.withheld_replies)],
        visible_tool_events=actual_recorder.visible_tool_events[: len(current_recorder.visible_tool_events)],
        private_candidates=actual_recorder.private_candidates[: len(current_recorder.private_candidates)],
        private_authorings=actual_recorder.private_authorings[: len(current_recorder.private_authorings)],
        drained=current_recorder.drained,
    )
    if encode_recorder_snapshot(current_prefix) != encode_recorder_snapshot(current_recorder):
        raise AuditIntegrityError("runtime capture changed its complete current SQL recorder prefix")
    service = runtime.materialization.service
    current_snapshot, current_catalog = service._policy_context.build(source.bootstrap.actor_user_id)
    if (
        current_snapshot.snapshot_hash != bindings.plugin_snapshot.snapshot_hash
        or current_snapshot.policy_hash != bindings.plugin_snapshot.policy_hash
        or canonical_json(live.tools) != canonical_json(composer_loop_tool_definitions(service._planner_dialect))
        or canonical_json(current_catalog.validate_composition_state(live.state).authored_state.to_dict())
        != canonical_json(live.state.to_dict())
    ):
        raise AuditIntegrityError("runtime capture requires current policy/tools/state reacquisition")
    composition_scope_for_operation(
        live.state,
        session_id=root.logical.session_id,
        session_operation_context=inventory.running.session_operation_context,
        session_operation_authority=service._require_sessions_service().session_operation_authority,
    )
    require_no_credential_material_in_state(live.state, surface="captured continuation runtime state", env_ref_names=names)
    prior_caches = decode_continuation_caches(runtime_chunk(selected, frame.caches), env_ref_names=names)
    retained_caches = live.continuation_cache_history
    if (
        prior_caches.capture_count < retained_caches.capture_count
        or prior_caches.capture_creators[: retained_caches.capture_count] != retained_caches.capture_creators
        or prior_caches.discovery[: len(retained_caches.discovery)] != retained_caches.discovery
        or prior_caches.preflight[: len(retained_caches.preflight)] != retained_caches.preflight
        or (*prior_caches.previous_last_preflight_json, prior_caches.last_preflight_json)[: retained_caches.capture_count]
        != (*retained_caches.previous_last_preflight_json, retained_caches.last_preflight_json)
        or (*prior_caches.previous_last_preflight_identities, prior_caches.last_preflight_identity)[: retained_caches.capture_count]
        != (*retained_caches.previous_last_preflight_identities, retained_caches.last_preflight_identity)
    ):
        raise AuditIntegrityError("runtime capture replaced its retained complete cache prefix")
    data = runtime.graph.batch
    captured_data: RestoredBatchData | RestoredPreparedBatchData | None = data
    if data is not None and data.batch.phase == "call_active":
        if runtime.graph.selected_audit is None or runtime.graph.selected_dialect is None:
            raise AuditIntegrityError("runtime capture lost its prepared audit DATA pair")
        call = data.batch.current_call
        if call is None:
            raise AuditIntegrityError("runtime capture lost its actual prepared call")
        portable = runtime.graph.selected_audit if call.audit is None else capture_dispatch_audit(call.audit, env_ref_names=names)
        captured_data = RestoredPreparedBatchData(data.batch, data.accumulator, data.calls, portable, runtime.graph.selected_dialect)
    admitted = capture_turn_evidence(
        live.turn_evidence,
        live.llm_messages,
        logical=root.logical,
        recorder=live.recorder,
        drained=runtime.drained,
        env_ref_names=names,
        data=captured_data,
        materialization=runtime.materialization,
        mandatory_retention=True,
        prior_application_entry=source.evidence.application_entry,
    )
    shelf = admitted.snapshot
    evidence = encode_turn_evidence(admitted, env_ref_names=names, mandatory_retention=True)
    transcript = encode_message_shelf(shelf.transcript, mandatory_retention=True, env_ref_names=names)
    messages = encode_mandatory_private_chunk(
        codec="composer.messages.v1",
        value={"messages_json": shelf.transcript.messages_json, "current_indexes": shelf.transcript.current_indexes},
        env_ref_names=names,
    )
    tools = encode_mandatory_private_chunk(codec="composer.tools.v1", value=live.tools, env_ref_names=names)
    state = encode_mandatory_private_chunk(codec="composer.state.v1", value=live.state.to_dict(), env_ref_names=names)
    audit = admitted.recorder_chunk
    caches = encode_continuation_caches(
        capture_continuation_caches(
            discovery=live.discovery_cache,
            preflight=live.runtime_preflight_cache,
            last_preflight=live.last_runtime_preflight,
            creator=inventory.running.session_operation_context,
            prior=prior_caches,
            env_ref_names=names,
        ),
        env_ref_names=names,
    )
    iteration = encode_continuation_iteration(
        capture_continuation_iteration(
            producer_inputs=runtime.materialization.snapshot_inputs(),
            last_validation=live.last_validation,
            advisor_review=live.advisor_review_state,
            failed_turn=live.failed_turn,
            anti_anchor=live.anti_anchor.snapshot_for_continuation(),
            disclosure=runtime.materialization.service._schema_disclosure.snapshot_for_continuation(
                session_id=root.logical.session_id,
                catalog_hash=bindings.plugin_snapshot.snapshot_hash,
                policy_hash=bindings.plugin_snapshot.policy_hash,
            ),
            env_ref_names=names,
        ),
        env_ref_names=names,
    )
    changed = frame.model_copy(
        update={
            "messages": messages.reference,
            "tools": tools.reference,
            "state": state.reference,
            "evidence": evidence.reference,
            "transcript": transcript.reference,
            "audit_buffer": audit.reference,
            "caches": caches.reference,
            "iteration": iteration.reference,
            "current_state_id": live.current_state_id,
            "composition_slice": live.composition_turns_used,
            "discovery_slice": live.discovery_turns_used,
            "composition_total": frame.composition_total + composition_delta,
            "discovery_total": frame.discovery_total + discovery_delta,
            "advisor_calls": live.advisor_calls_used,
            "repair_turns": live.repair_turns_used,
            "advisor_checkpoint_passes": live.advisor_checkpoint_passes_used,
            "mutation_success_seen": live.mutation_success_seen,
            "persisted_assistant_message_id": live.persisted_assistant_message_id,
            "persisted_assistant_content": live.persisted_assistant_content,
            "persisted_tool_call_turn": live.persisted_tool_call_turn,
            "advisor_repair_context_introduced": live.advisor_repair_context_introduced,
            "pending_advisor_elision_indices": tuple(live.pending_advisor_elision_indices),
        }
    )
    frames = list(root.frames)
    frames[index] = changed
    added: tuple[PrivateChunk, ...] = (messages, tools, state, evidence, transcript, audit, caches, iteration)
    batch_indexes = tuple(i for i, value in enumerate(frames) if type(value) is ToolBatchFrame)
    if batch_indexes and data is not None and data.batch.phase not in ("p4_pending", "complete"):
        captured_graph = admitted.graph
        if captured_graph is None:
            raise AuditIntegrityError("runtime capture lost its actual encoded shared batch graph")
        projected = captured_graph.selected
        if len(batch_indexes) != 1:
            raise AuditIntegrityError("runtime capture lost its one actual selected batch")
        batch_index = batch_indexes[0]
        batch_frame = frames[batch_index]
        if type(batch_frame) is not ToolBatchFrame:
            raise AuditIntegrityError("runtime capture changed its nominal batch frame")
        if projected.batch.phase == "call_active":
            if captured_graph.selected_audit is None or captured_graph.selected_dialect is None:
                raise AuditIntegrityError("runtime capture lost its encoded portable audit DATA pair")
            encoded = encode_restored_prepared_batch_data(
                RestoredPreparedBatchData(
                    projected.batch, projected.accumulator, projected.calls, captured_graph.selected_audit, captured_graph.selected_dialect
                ),
                env_ref_names=names,
            )
        else:
            encoded = encode_coupled_batch_data(
                batch=projected.batch, accumulator=projected.accumulator, calls=projected.calls, env_ref_names=names
            )
        # Keep a consumed attachment wrapper while the same call remains owed.
        # Its actual finisher may replace it only after advancing that call.
        old_chunk = runtime_chunk(selected, batch_frame.accumulator)
        if batch_frame.phase == "result_retained" and data.batch.next_tool_index == batch_frame.next_tool_index:
            from elspeth.web.composer.continuation_attachment_result_data import decode_attachment_history_batch

            retained = decode_attachment_history_batch(old_chunk, env_ref_names=names)
            if retained.prepared_json != encoded:
                raise AuditIntegrityError("runtime changed an unconsumed attachment prepared source")
        else:
            batch = encode_private_chunk(
                codec=("composer.tool-batch.v1" if projected.batch.phase == "call_active" else "composer.coupled-batch-data.v1"),
                canonical=encoded,
                env_ref_names=names,
            )
            frames[batch_index] = batch_frame.model_copy(
                update={"accumulator": batch.reference, "next_tool_index": data.batch.next_tool_index}
            )
            added = (*added, batch)
    changed_root = root.model_copy(update={"frames": tuple(frames)})
    chunks = _selected_chunks(changed_root, selected.chunks, added)
    SelectedContinuationState(selected.checkpoint_id, changed_root, chunks)
    return CapturedContinuationRuntime(selected, changed_root, chunks)


async def capture_continuation_runtime_owners(
    runtime: RestoredContinuationRuntime, *, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory
) -> CapturedContinuationRuntime:
    if type(runtime) is not RestoredContinuationRuntime:
        raise TypeError("runtime capture requires its actual restored live owners")
    work = runtime.bindings.required_work
    if type(work) is not RequiredWorkBinding:
        raise AuditIntegrityError("runtime capture lost its actual required work")
    authority = runtime.materialization.authority
    running = _work_owners(authority, inventory, work)

    def capture() -> CapturedContinuationRuntime:
        from elspeth.web.composer.continuation_evidence import state_from_evidence_json
        from elspeth.web.composer.protocol import ComposerContextUnsupported

        source = authority.read_continuation_runtime(running, selected=selected, inventory=inventory)
        try:
            captured = _capture_owners(runtime, source=source, inventory=inventory)
        except ComposerContextUnsupported as refused:
            try:
                authority.read_continuation_runtime(running, selected=selected, inventory=inventory)
            except BaseException as failure:
                raise BaseExceptionGroup("Retention refusal and final current-source admission", [refused, failure]) from None
            # Carry only work the actual recorder has not drained. Prior
            # committed effects and the last valid checkpoint stay in place.
            live = runtime.live
            raise refused.with_evidence(
                state=live.state,
                initial_version=state_from_evidence_json(source.evidence.effects.latest_verified_state_json).version,
                tool_invocations=live.recorder.invocations[runtime.drained.invocations :],
                llm_calls=live.recorder.llm_calls[runtime.drained.llm_calls :],
                failed_turn=live.failed_turn,
            ) from refused
        authority.read_continuation_runtime(running, selected=selected, inventory=inventory)
        return captured

    result = await _registered_read(work, CapturedContinuationRuntime, capture)
    if type(result) is not CapturedContinuationRuntime:
        raise AuditIntegrityError("runtime capture returned another nominal owner")
    return result

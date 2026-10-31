"""Build and consume one genuine ready → first Compose publication.

This is not a driver: no provider is minted/dispatched and later phase roots
are refused. Draft owners never escape; returned owners are reconstructed
through the ordinary current SQL/message/evidence runtime after publication.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection

from elspeth.contracts.composer_progress import ComposerProgressEvent, ComposerProgressSink
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer._compose_loop_carriers import _AdvisorReviewState, _ComposeIterationState
from elspeth.web.composer.advisor_transcript import AdvisorTranscriptHistory
from elspeth.web.composer.anti_anchor import AntiAnchorTracker
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.continuation import ComposeFrame
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors
from elspeth.web.composer.continuation_bootstrap import decode_continuation_bootstrap
from elspeth.web.composer.continuation_clock import ContinuationActionClock, SegmentFit, admit_action
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_composition_initialization_data import (
    CapturedInitialComposition,
    InitialCompositionOwners,
    capture_initial_composition,
)
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_first_runtime import restore_first_continuation_runtime as restore_continuation_runtime
from elspeth.web.composer.continuation_initialization import _registered, _sessions
from elspeth.web.composer.continuation_iteration import capture_continuation_producer_inputs
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_prompt_producers import CurrentContinuationMessageProducers, _ReadyProducerInputs
from elspeth.web.composer.continuation_runtime import RestoredContinuationRuntime, _work_owners
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.progress import emit_progress
from elspeth.web.composer.protocol import retained_operation_context_message
from elspeth.web.composer.provider_gateway import composer_loop_tool_definitions
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.recovery_controls import recovery_context_message
from elspeth.web.composer.schema_disclosure import SchemaDisclosureSnapshot
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.transcript_blob_reader import CurrentTranscriptBlobReader
from elspeth.web.composer.turn_effects import TurnEvidence
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.credential_guard import require_no_credential_material_in_state
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkBinding, RequiredWorkSource
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_composition_initialization import (
    CurrentCompositionInitializationSource,
    read_composition_initialization_source_on_connection,
)
from elspeth.web.sessions.locking import locked_session_transaction


@dataclass(frozen=True, slots=True)
class PreparedCompositionInitialization:
    owners: InitialCompositionOwners
    captured: CapturedInitialComposition


async def _read_source(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: StoredContinuationSnapshot,
    work: RequiredWorkBinding,
) -> CurrentCompositionInitializationSource:
    running = work.running
    if type(running) is not ComposerOperationRunning:
        raise AuditIntegrityError("first Compose read lost its actual current running owner")
    complete = await load_selected_continuation(authority=authority, running=running, selected=selected, required_work=work)
    inventory = await materialize_continuation_env_inventory(
        advisor=service._advisor_checkpoint,
        authority=authority,
        selected=complete,
        required_work=work,
    )
    return await _registered(
        work=work,
        source=RequiredWorkSource.CONTINUATION_READ_SQL,
        operation=lambda: authority.read_composition_initialization(running, composer=service, selected=complete, inventory=inventory),
        expected=CurrentCompositionInitializationSource,
    )


def _build_owners(
    service: ComposerServiceImpl, authority: ComposerAsyncOperationAuthority, source: CurrentCompositionInitializationSource
) -> InitialCompositionOwners:
    original, logical = source.original, source.selected.stack.logical
    snapshot, catalog = service._policy_context.build(original.snapshot.actor_user_id)
    current = catalog.validate_composition_state(original.initial_state)
    if canonical_json(current.authored_state.to_dict()) != original.snapshot.initial_state_json:
        raise AuditIntegrityError("first Compose current authoring policy requires original-state reinspection")
    require_no_credential_material_in_state(
        original.initial_state, surface="initial continuation Compose state", env_ref_names=source.inventory.names
    )
    for kind, name in original.snapshot.initial_schema_disclosure:
        catalog.get_schema(kind, name)
    disclosure = SchemaDisclosureSnapshot(
        session_id=logical.session_id,
        catalog_hash=snapshot.snapshot_hash,
        policy_hash=snapshot.policy_hash,
        schemas=original.snapshot.initial_schema_disclosure,
    )
    inputs = _ReadyProducerInputs(
        decode_continuation_bootstrap(source.bootstrap),
        original,
        capture_continuation_producer_inputs((), env_ref_names=source.inventory.names),
        source,
    )
    materialization = CurrentContinuationMessageProducers(service, authority, source.selected, source.inventory, inputs)
    history = AdvisorTranscriptHistory.from_records(
        session_id=logical.session_id,
        actor_user_id=original.snapshot.actor_user_id,
        current_request_id=logical.original_user_message_id,
        request=original.message,
        records=original.records,
    )
    context = source.inventory.running.session_operation_context
    evidence = TurnEvidence(
        session_id=logical.session_id,
        session_operation_context=context,
        request_id=logical.original_user_message_id,
        request=original.message,
        initial_state_id=logical.base_state_id,
        initial_state=original.initial_state,
        retained_operation_context=original.retained_context,
        actor_user_id=original.snapshot.actor_user_id,
        advisor_history=history,
    )
    evidence.transcript.use_blob_reader(CurrentTranscriptBlobReader.from_service(service, session_id=logical.session_id, context=context))
    messages = materialization.initial_messages()
    evidence.transcript.begin_messages(messages)
    if original.recovery_context is not None:
        messages.append({"role": "system", "content": recovery_context_message(original.recovery_context)})
    messages.append({"role": "system", "content": retained_operation_context_message(original.retained_context)})
    tools = composer_loop_tool_definitions(service._planner_dialect)
    recorder = BufferingRecorder()
    drained = RecorderDrainCursors()
    live = _ComposeIterationState(
        state=original.initial_state,
        initial_version=original.initial_state.version,
        llm_messages=messages,
        tools=tools,
        recorder=recorder,
        composition_turns_used=0,
        discovery_turns_used=0,
        mutation_success_seen=False,
        discovery_cache={},
        last_validation=None,
        runtime_preflight_cache=service._preflight.new_cache(),
        last_runtime_preflight=None,
        anti_anchor=AntiAnchorTracker(),
        advisor_calls_used=0,
        repair_turns_used=0,
        advisor_checkpoint_passes_used=0,
        advisor_review_state=_AdvisorReviewState(),
        persisted_assistant_message_id=None,
        persisted_assistant_content=None,
        persisted_tool_call_turn=False,
        failed_turn=None,
        current_state_id=logical.base_state_id,
        advisor_repair_context_introduced=False,
        pending_advisor_elision_indices=[],
        turn_evidence=evidence,
    )
    owners = InitialCompositionOwners(
        source=source,
        materialization=materialization,
        live=live,
        drained=drained,
        disclosure=disclosure,
        skill_identity=(service._composer_skill_name, service._composer_skill_hash, service._composer_skill_text),
    )
    return owners


def _finish_preparation(service: ComposerServiceImpl, owners: InitialCompositionOwners) -> CapturedInitialComposition:
    original = owners.source.original
    final_snapshot, final_catalog = service._policy_context.build(original.snapshot.actor_user_id)
    if (
        (final_snapshot.snapshot_hash, final_snapshot.policy_hash) != (owners.disclosure.catalog_hash, owners.disclosure.policy_hash)
        or canonical_json(final_catalog.validate_composition_state(original.initial_state).authored_state.to_dict())
        != original.snapshot.initial_state_json
        or (service._composer_skill_name, service._composer_skill_hash, service._composer_skill_text) != owners.skill_identity
    ):
        raise AuditIntegrityError("first Compose current policy/skill changed across preparation awaits")
    owners.materialization._require_initial_current()
    return capture_initial_composition(owners)


async def _prepare(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: StoredContinuationSnapshot,
    work: RequiredWorkBinding,
) -> PreparedCompositionInitialization | CurrentCompositionInitializationSource:
    source = await _read_source(service=service, authority=authority, selected=selected, work=work)
    if source.initialized:
        return source
    policy = decode_continuation_policy(source.selected.stack.admission_policy_json, source.selected.stack.policy_hash)
    if (
        admit_action(
            policy.actions.session_sql,
            policy.segment,
            hard_remaining=source.timing.hard_remaining_seconds,
            cooperative_remaining=source.timing.cooperative_remaining_seconds,
            fresh=False,
        )
        is not SegmentFit.ADMIT
    ):
        raise AuditIntegrityError("first Compose initialization must yield before new preparation")
    owners = await _registered(
        work=work,
        source=RequiredWorkSource.CONTINUATION_READ_SQL,
        operation=lambda: _build_owners(service, authority, source),
        expected=InitialCompositionOwners,
    )
    final_inventory = await materialize_continuation_env_inventory(
        advisor=service._advisor_checkpoint,
        authority=authority,
        selected=source.selected,
        required_work=work,
    )
    if final_inventory.names != source.inventory.names:
        raise AuditIntegrityError("first Compose actual names changed across preparation awaits")
    captured = await _registered(
        work=work,
        source=RequiredWorkSource.CONTINUATION_READ_SQL,
        operation=lambda: _finish_preparation(service, owners),
        expected=CapturedInitialComposition,
    )
    return PreparedCompositionInitialization(owners, captured)


def _bind_first_state_on_connection(
    conn: Connection,
    service: ComposerServiceImpl,
    running: ComposerOperationRunning,
    runtime: RestoredContinuationRuntime,
) -> RestoredContinuationRuntime:
    """Join the *proven first cut* to its already restored actual evidence state.

    This is not an equality-based general alias restoration. The real event,
    full forward transition, original references and absence of provider/batch
    progress are admitted on this current connection before the one assignment.
    """
    selected, inventory = runtime.source.selected, runtime.materialization.inventory
    source = read_composition_initialization_source_on_connection(conn, service, running, selected=selected, inventory=inventory)
    if not source.initialized or runtime.graph.batch is not None:
        raise AuditIntegrityError("first-state alias join requires its actual unconsumed initialization event")
    frame = selected.stack.frames[0]
    effects = runtime.graph.evidence.effects
    reader = runtime.graph.evidence.transcript._blob_reader
    if (
        type(frame) is not ComposeFrame
        or frame.initial_state != frame.state
        or effects.initial_state is not effects.latest_verified_state
        or canonical_json(effects.initial_state.to_dict()) != source.original.snapshot.initial_state_json
        or canonical_json(runtime.live.state.to_dict()) != source.original.snapshot.initial_state_json
        or runtime.live.turn_evidence is not runtime.graph.evidence
        or runtime.live.llm_messages is not runtime.graph.messages
        or runtime.live.recorder is not runtime.graph.recorder
        or runtime.bindings.session_operation_context is not running.session_operation_context
        or type(reader) is not CurrentTranscriptBlobReader
        or reader.service is not service
        or reader.context is not running.session_operation_context
    ):
        raise AuditIntegrityError("first-state alias join changed the actual current shared graph/reader")
    reader.require_for_evidence(runtime.graph.evidence)
    runtime.live.state = effects.initial_state
    from elspeth.web.sessions.continuation_composition_initialization import _read_composition_initialization_source_on_connection

    # Repeat EVERY actual current/original/event/TITLE SQL predicate after the
    # pure alias assignment. Only the first-value pure projection is shared:
    # its complete selected/chunk/original inputs must remain exactly equal
    # on this same connection; a changed metadata input refuses, never renews.
    after = _read_composition_initialization_source_on_connection(conn, service, running, selected=selected, inventory=inventory)
    if not source.same_original_source(after) or source.original != after.original:
        raise AuditIntegrityError("first-state alias changed its actual complete current source")
    return runtime


async def _consume(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: StoredContinuationSnapshot,
    work: RequiredWorkBinding,
    provider_owner: ProviderInvocationOwner,
    progress: ComposerProgressSink | None,
) -> RestoredContinuationRuntime:
    running = work.running
    if type(running) is not ComposerOperationRunning:
        raise AuditIntegrityError("first Compose consumption lost its actual current running owner")
    complete = await load_selected_continuation(authority=authority, running=running, selected=selected, required_work=work)
    inventory = await materialize_continuation_env_inventory(
        advisor=service._advisor_checkpoint, authority=authority, selected=complete, required_work=work
    )
    runtime = await restore_continuation_runtime(
        service=service,
        authority=authority,
        selected=complete,
        inventory=inventory,
        required_work=work,
        provider_owner=provider_owner,
        progress=progress,
    )
    running = _work_owners(authority, inventory, work)

    def bind() -> RestoredContinuationRuntime:
        with locked_session_transaction(_sessions(service)._engine, str(running.claim.session_id)) as conn:
            return _bind_first_state_on_connection(conn, service, running, runtime)

    return await _registered(
        work=work, source=RequiredWorkSource.CONTINUATION_READ_SQL, operation=bind, expected=RestoredContinuationRuntime
    )


async def initialize_continuation_composition(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: StoredContinuationSnapshot,
    required_work: RequiredWorkBinding,
    provider_owner: ProviderInvocationOwner,
    progress: ComposerProgressSink | None = None,
) -> RestoredContinuationRuntime:
    if (
        type(service) is not ComposerServiceImpl
        or type(authority) is not ComposerAsyncOperationAuthority
        or type(selected) is not StoredContinuationSnapshot
        or type(required_work) is not RequiredWorkBinding
        or type(provider_owner) is not ProviderInvocationOwner
        or provider_owner.required_work is not required_work
        or provider_owner.service is not _sessions(service)
        or _sessions(service)._engine is not authority._engine
    ):
        raise AuditIntegrityError("first Compose initialization requires actual service/SQL/provider-work owners")
    running = required_work.running
    custody = required_work.coordinator.authority
    if (
        type(running) is not ComposerOperationRunning
        or custody.authority_kind is not RequiredAuthorityKind.DURABLE_COMPOSE
        or custody.durable_operation_id != running.claim.operation_id
        or custody.claim_attempt != running.claim.attempt
    ):
        raise AuditIntegrityError("first Compose initialization changed its current required-work claim")
    required_work.validate_context(running.session_operation_context)
    root = decode_continuation_snapshot(selected.payload_json)
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    clock = ContinuationActionClock(policy.actions.session_sql)
    clock.finish_preparation()

    async def prepare() -> PreparedCompositionInitialization | CurrentCompositionInitializationSource:
        return await _prepare(service=service, authority=authority, selected=selected, work=required_work)

    prepared = await clock.run("physical", prepare)

    async def retain_and_consume() -> RestoredContinuationRuntime:
        current = selected
        if type(prepared) is PreparedCompositionInitialization:
            current = await _registered(
                work=required_work,
                source=RequiredWorkSource.CONTINUATION_STATE_SQL,
                operation=lambda: authority.publish_composition_initialization(
                    running, composer=service, owners=prepared.owners, captured=prepared.captured
                ),
                expected=StoredContinuationSnapshot,
            )
        return await _consume(
            service=service, authority=authority, selected=current, work=required_work, provider_owner=provider_owner, progress=progress
        )

    runtime = await clock.run("retention", retain_and_consume)
    if type(prepared) is PreparedCompositionInitialization:
        await emit_progress(
            progress,
            ComposerProgressEvent(
                phase="starting",
                headline="I'm reading your request and current pipeline.",
                evidence=(
                    "The current pipeline state is prepared for the composer.",
                    "The pipeline composer skill pack and deployment overlay are included.",
                ),
                likely_next="ELSPETH will ask the model for the next safe pipeline action.",
            ),
        )
    return runtime

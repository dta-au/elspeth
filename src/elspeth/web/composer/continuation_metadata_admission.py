"""Admit one original metadata call's complete retained tail before SQL effects."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import ComposeFrame
from elspeth.web.composer.continuation_batch_data import RestoredBatchData
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_snapshot,
    encode_continuation_snapshot,
)
from elspeth.web.composer.continuation_dispatch_audit import capture_dispatch_audit
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory
from elspeth.web.composer.continuation_evidence import decode_turn_evidence
from elspeth.web.composer.continuation_initialization import _registered
from elspeth.web.composer.continuation_limits import ContinuationDocumentTooLarge
from elspeth.web.composer.continuation_metadata_preparation import (
    MetadataPreparationRefused,
    PreparedMetadataToolCall,
    PreparedMetadataToolCompletion,
    complete_prepared_metadata_tool,
    prepare_metadata_tool_call,
)
from elspeth.web.composer.continuation_p4_batch import encode_p4_pending_batch_chunk
from elspeth.web.composer.continuation_pipeline_preparation import (
    PreparedPipelineToolCall,
    PreparedPipelineToolCompletion,
    complete_prepared_pipeline_tool,
    prepare_pipeline_tool_call,
)
from elspeth.web.composer.continuation_prepared_p4 import PreparedP4Value, encode_prepared_p4, prepared_p4_members
from elspeth.web.composer.continuation_runtime import (
    CapturedContinuationRuntime,
    RestoredContinuationRuntime,
    _selected_chunks,
    capture_metadata_preparation_runtime,
    restore_metadata_preparation_runtime,
)
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.tool_batch import _finish_continuation_batch_live
from elspeth.web.composer.turn_audit import prepare_continuation_turn_audit, prepare_turn_audit_evidence, prepare_turn_audit_rows
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions.continuation import PreparedContinuationAppend, StoredContinuationSnapshot, private_chunk_key
from elspeth.web.sessions.continuation_metadata_tool import (
    consume_metadata_transitions_on_connection,
    prepare_metadata_transitions_on_connection,
)
from elspeth.web.sessions.continuation_p4 import (
    planned_p4_chat_hash,
    planned_p4_state_hash,
    prepare_p4_on_connection,
    prepare_p4_preparation_tail,
    prepare_p4_receipt_data,
    prepare_p4_result_tail,
    retain_p4_result_on_connection,
)
from elspeth.web.sessions.continuation_p4_application_store import (
    StoredP4Application,
    apply_committing_p4_application_on_connection,
    prepare_p4_application_tail,
)
from elspeth.web.sessions.continuation_prepared_tail import PreparedTailPrefix, admit_prepared_complete_tail_on_connection
from elspeth.web.sessions.continuation_retained_capacity import ContinuationRetainedCapacityExceeded, ProspectiveContinuationChunk
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.service import MetadataPublicationUnavailable, SessionServiceImpl

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases


@dataclass(frozen=True, slots=True)
class _MetadataTailData:
    issued: CapturedContinuationRuntime
    completed: CapturedContinuationRuntime
    pending_capture: CapturedContinuationRuntime
    pending_batch: PrivateChunk
    memory_completion: ProspectiveContinuationChunk
    prepared: PreparedP4Value


@dataclass(frozen=True, slots=True)
class _MetadataAdmissionOutcome:
    snapshot: StoredContinuationSnapshot | None


def _future_selection(
    append: PreparedContinuationAppend, previous: SelectedContinuationState, chunks: tuple[PrivateChunk, ...]
) -> SelectedContinuationState:
    """A complete prospective DATA selection, never a current SQL capability."""
    root = decode_continuation_snapshot(append.snapshot_json)
    return SelectedContinuationState(append.checkpoint_id, root, _selected_chunks(root, previous.chunks, chunks))


def _prefix(append: PreparedContinuationAppend, chunks: tuple[PrivateChunk, ...]) -> PreparedTailPrefix:
    return PreparedTailPrefix(
        append,
        tuple(
            ProspectiveContinuationChunk(private_chunk_key(chunk), chunk.canonical.encode("utf-8"), chunk.reference.codec)
            for chunk in chunks
        ),
    )


async def _capture_metadata(
    runtime: RestoredContinuationRuntime, *, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory
) -> CapturedContinuationRuntime:
    captured = await capture_metadata_preparation_runtime(runtime, selected=selected, inventory=inventory)
    if captured is None:
        raise MetadataPreparationRefused("metadata capture exceeds the unchanged complete document or evidence bound")
    return captured


async def _prepare_detached(
    owner: ContinuationLivePhases,
    original: RestoredContinuationRuntime,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
) -> _MetadataTailData:
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases

    if original.live.advisor_repair_context_introduced or selected.stack.active_action_json is not None:
        raise MetadataPreparationRefused("metadata preparation cannot replace an active semantic or repair action")
    sessions = owner.service._sessions_service
    if type(sessions) is not SessionServiceImpl:
        raise MetadataPreparationRefused("metadata preparation requires the actual session publication owner")
    provider = ProviderInvocationOwner(service=sessions, required_work=owner.work)
    detached = await restore_metadata_preparation_runtime(
        service=owner.service,
        authority=owner.authority,
        selected=selected,
        inventory=inventory,
        required_work=owner.work,
        provider_owner=provider,
    )
    phase = ContinuationLivePhases(owner.service, owner.authority, owner.work, provider, owner.lease, owner.progress, runtime=detached)
    primary = await phase._primary_for_phase(detached, inventory, selected)
    calls = primary.outcome.completion.tool_batch.calls
    if len(calls) != 1:
        raise MetadataPreparationRefused("bounded value preparation requires one original provider call")
    prepared: PreparedMetadataToolCall | PreparedPipelineToolCall
    if calls[0].function.name == "set_metadata":
        prepared = await prepare_metadata_tool_call(
            runtime=detached,
            original_runtime=original,
            call_model=primary.outcome,
            cancellation_requested=owner.cancellation_requested,
        )
    elif calls[0].function.name == "set_pipeline":
        prepared = await prepare_pipeline_tool_call(
            runtime=detached,
            original_runtime=original,
            call_model=primary.outcome,
            cancellation_requested=owner.cancellation_requested,
        )
    else:
        raise MetadataPreparationRefused("bounded value preparation does not activate arbitrary tools")
    audit = prepared.call.audit
    if audit is None:
        raise AuditIntegrityError("metadata preparation lost its original dispatch audit")
    issued_runtime = replace(
        detached,
        graph=detached.graph.with_selected_dispatch(
            capture_dispatch_audit(audit, env_ref_names=inventory.names),
            prepared.context.tool_contract_dialect,
        ),
    )
    issued = await _capture_metadata(issued_runtime, selected=selected, inventory=inventory)
    completion: PreparedMetadataToolCompletion | PreparedPipelineToolCompletion
    if type(prepared) is PreparedMetadataToolCall:
        completion = await complete_prepared_metadata_tool(prepared)
    elif type(prepared) is PreparedPipelineToolCall:
        completion = await complete_prepared_pipeline_tool(prepared)
    else:
        raise AuditIntegrityError("bounded value preparation lost its original nominal call")
    data = detached.graph.batch
    if data is None:
        raise AuditIntegrityError("metadata completion lost its detached batch")
    live, batch = detached.live, data.batch
    live.state, live.last_validation, live.last_runtime_preflight = batch.state, batch.last_validation, batch.last_runtime_preflight
    live.advisor_calls_used = batch.advisor_calls_used
    completed = await _capture_metadata(detached, selected=selected, inventory=inventory)
    dispatch, _ = _finish_continuation_batch_live(
        call_model=prepared.call_model,
        ctx=prepared.context,
        acc=data.accumulator,
        batch=batch,
    )
    context = inventory.running.session_operation_context
    rows = prepare_turn_audit_rows(
        redaction_telemetry=owner.service._redaction_telemetry,
        tool_outcomes=dispatch.tool_outcomes,
        base_state=batch.entry_state,
        decoded_args_by_call_id=dispatch.decoded_args_by_call_id,
        private_authorings=dispatch.private_authorings,
        crash_pending=False,
        session_id=str(inventory.running.claim.session_id),
        session_operation_context=context,
        ingress=detached.bindings.ingress,
        chat_ingress_inputs=None if detached.bindings.chat_ingress_inputs is None else list(detached.bindings.chat_ingress_inputs),
        turn_evidence=live.turn_evidence,
    )
    evidence = prepare_turn_audit_evidence(
        tool_outcomes=dispatch.tool_outcomes,
        private_authorings=dispatch.private_authorings,
        private_invocations=dispatch.private_invocations,
        session_id=str(inventory.running.claim.session_id),
        session_operation_context=context,
    )
    p4 = prepare_continuation_turn_audit(
        rows=rows,
        evidence=evidence,
        session_operation_context=context,
        assistant_message=dispatch.assistant_message,
        raw_assistant_content=dispatch.raw_assistant_content,
        mutation_ceiling=detached.source.selected.stack.mutation_ceiling.materialize(),
        authority_revocation=dispatch.authority_revocation,
        tool_outcomes=dispatch.tool_outcomes,
        private_invocations=dispatch.private_invocations,
        private_authorings=dispatch.private_authorings,
        withheld_replies=live.recorder.withheld_replies[detached.drained.withheld_replies :],
        current_state_id=live.current_state_id,
        assistant_row_uses_current_dispatch=True,
        crash_pending=False,
        env_ref_names=inventory.names,
    )
    members = prepared_p4_members(p4, env_ref_names=inventory.names)
    for expected, row in zip(members.invocations, members.redacted_rows, strict=True):
        actual = tuple(value for value in live.recorder.invocations if value.tool_call_id == expected.tool_call_id)
        if len(actual) != 1 or actual[0] != expected:
            raise AuditIntegrityError("metadata redaction lost its actual original invocation")
        if row.visible_event is not None:
            previous = tuple(event for invocation, event in live.recorder.visible_tool_events if invocation is actual[0])
            if previous and previous != (row.visible_event,):
                raise AuditIntegrityError("metadata redaction replaced its original visible event")
            if not previous:
                live.recorder.record_visible_tool_event(actual[0], row.visible_event)
    pending = replace(batch, phase="p4_pending", p4_input_json=encode_prepared_p4(p4, env_ref_names=inventory.names))
    pending_chunk = encode_p4_pending_batch_chunk(
        batch=pending,
        accumulator=data.accumulator,
        calls=data.calls,
        env_ref_names=inventory.names,
    )
    pending_runtime = replace(detached, graph=detached.graph.with_batch(RestoredBatchData(pending, data.accumulator, data.calls)))
    pending_capture = await _capture_metadata(pending_runtime, selected=selected, inventory=inventory)
    memory_payload = completion.memory_completion_json.encode("utf-8")
    memory = ProspectiveContinuationChunk(
        f"metadata-memory:{sha256(memory_payload).hexdigest()}", memory_payload, "composer.memory-completion-batch-data.v1"
    )
    return _MetadataTailData(issued, completed, pending_capture, pending_chunk, memory, p4)


async def try_admit_metadata_tool(
    owner: ContinuationLivePhases,
    *,
    runtime: RestoredContinuationRuntime,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
) -> bool:
    """Return false only before effects, leaving the original refusal owner intact."""
    try:
        data = await _prepare_detached(owner, runtime, selected, inventory)
    except (MetadataPreparationRefused, ComposerContextUnsupported, ContinuationDocumentTooLarge):
        return False

    def write_tail() -> StoredContinuationSnapshot:
        running, names = inventory.running, inventory.names
        sessions = owner.service._sessions_service
        if type(sessions) is not SessionServiceImpl:
            raise AuditIntegrityError("metadata publication lost its original concrete session owner")
        sid = str(running.claim.session_id)
        with locked_session_transaction(owner.authority._engine, sid) as conn, sessions._session_write_lock(conn, sid):
            now = datetime.now(UTC)
            tool = prepare_metadata_transitions_on_connection(
                conn,
                running,
                original=selected,
                inventory=inventory,
                issued=data.issued,
                completed=data.completed,
                published_at=now,
                memory_completion=data.memory_completion,
            )
            # Only pure planners see this prospective selection. Capture was made
            # against the actual original source before any SQL head changed.
            pending_root = tool.completed.stack.model_copy(
                update={
                    "frames": (
                        data.pending_capture.stack.frames[0],
                        *tool.completed.stack.frames[1:],
                    )
                }
            )
            captured = CapturedContinuationRuntime(
                tool.completed,
                pending_root,
                _selected_chunks(pending_root, tool.completed.chunks, data.pending_capture.chunks),
            )
            preparation = prepare_p4_preparation_tail(
                running,
                selected=tool.completed,
                pending_batch=data.pending_batch,
                captured=captured,
                published_at=now,
                env_ref_names=names,
            )
            publication = sessions.prepare_compose_turn_publication_on_connection(
                conn,
                running=running,
                original_selected=selected,
                prepared=data.prepared,
                env_ref_names=names,
            )
            p4_selected = _future_selection(preparation.append, tool.completed, preparation.chunks)
            receipt = prepare_p4_receipt_data(
                effect_key=preparation.effect_key,
                input_sha256=preparation.input_sha256,
                prepared=data.prepared,
                outcome=publication.retained_outcome,
                chat_message_ids=tuple(message.id for message in publication.messages),
                chat_message_hashes=tuple(planned_p4_chat_hash(message) for message in publication.messages),
                state_row_hashes=(planned_p4_state_hash(publication.state, creator_operation_id=publication.creator_operation_id),),
                env_ref_names=names,
            )
            result = prepare_p4_result_tail(selected=p4_selected, receipt=receipt, published_at=now, env_ref_names=names)
            result_selected = _future_selection(result.append, p4_selected, (result.chunk,))
            compose = result_selected.stack.frames[0]
            if type(compose) is not ComposeFrame:
                raise AuditIntegrityError("metadata prospective application lost its original compose frame")
            evidence_chunk = next(chunk for chunk in result_selected.chunks if chunk.reference == compose.evidence)
            evidence = decode_turn_evidence(evidence_chunk, env_ref_names=names, source_chunks=result_selected.chunks)
            application = prepare_p4_application_tail(
                running,
                selected=result_selected,
                evidence=evidence,
                pending_batch=data.pending_batch,
                result=result.result,
                result_chunk=result.chunk,
                published_at=now,
                env_ref_names=names,
            )
            original = StoredContinuationSnapshot(
                encode_continuation_snapshot(selected.stack), selected.stack.cursor, selected.checkpoint_id
            )
            prefixes = (
                tool.issue_prefix,
                tool.completion_prefix,
                _prefix(preparation.append, preparation.chunks),
                _prefix(result.append, (result.chunk,)),
                _prefix(application.append, application.chunks),
            )
            with admit_prepared_complete_tail_on_connection(conn, original=original, prefixes=prefixes):
                consume_metadata_transitions_on_connection(conn, running, plan=tool, env_ref_names=names)
                commit = prepare_p4_on_connection(
                    conn,
                    running,
                    expected_checkpoint_id=tool.completed.checkpoint_id,
                    expected_cursor=tool.completed.stack.cursor,
                    pending_batch=data.pending_batch,
                    captured=captured,
                    env_ref_names=names,
                    prepared_tail=preparation,
                )
                outcome = sessions.consume_compose_turn_publication_on_connection(
                    conn,
                    running=running,
                    publication=publication,
                    commit=commit,
                    publication_append=result.append,
                    env_ref_names=names,
                )
                retain_p4_result_on_connection(
                    conn,
                    commit,
                    prepared=data.prepared,
                    outcome=outcome,
                    base_sequence=publication.base_sequence,
                    created_at=publication.created_at,
                    env_ref_names=names,
                    prepared_tail=result,
                )
                stored = apply_committing_p4_application_on_connection(conn, commit, env_ref_names=names, prepared_tail=application)
                if type(stored) is not StoredP4Application:
                    raise AuditIntegrityError("metadata application changed its original live disposition")
        # Record the actual committed outcome before required-work delivery can
        # surface deferred cancellation. Next phase restores the cold SQL owner.
        owner.latest_snapshot = stored.snapshot
        owner.runtime = None
        owner.primary = None
        owner.p4 = None
        owner.finished_batch = None
        return stored.snapshot

    def commit_tail() -> _MetadataAdmissionOutcome:
        try:
            snapshot = write_tail()
        except (
            MetadataPublicationUnavailable,
            ContinuationRetainedCapacityExceeded,
            ContinuationDocumentTooLarge,
            ComposerContextUnsupported,
        ):
            # The transaction has exited and rolled back. An expected admission
            # refusal is an owned result, not a failed required SQL invocation.
            return _MetadataAdmissionOutcome(None)
        return _MetadataAdmissionOutcome(snapshot)

    outcome = await _registered(
        work=owner.work,
        source=RequiredWorkSource.CONTINUATION_STATE_SQL,
        operation=commit_tail,
        expected=_MetadataAdmissionOutcome,
    )
    return outcome.snapshot is not None

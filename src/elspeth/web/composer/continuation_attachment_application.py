"""Exact DATA checker for one original attachment B2 result projection.

It validates a produced transition; it performs no audit finish, read, provider
call, current admission or SQL write. The transaction owns those current joins.
"""

from __future__ import annotations

from dataclasses import replace

from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import ContinuationFrameStack
from elspeth.web.composer.continuation_application_graph import (
    ApplicationGraph,
    admit_application_recorder_source,
    encode_application_graph,
)
from elspeth.web.composer.continuation_attachment_application_values import AttachmentApplicationData
from elspeth.web.composer.continuation_audit_buffer import encode_recorder_snapshot
from elspeth.web.composer.continuation_batch_data import RestoredBatchData, RestoredPreparedBatchData, encode_restored_prepared_batch_data
from elspeth.web.composer.continuation_codec import PrivateChunk, decode_continuation_response
from elspeth.web.composer.continuation_dispatch_audit import PortableDispatchAudit
from elspeth.web.composer.continuation_evidence import (
    AdmittedTurnEvidence,
    RetainedBlobPage,
    RetainedPageLink,
    TurnEvidenceSnapshot,
    admit_turn_evidence,
    encode_turn_evidence,
    require_admitted_turn_evidence,
)
from elspeth.web.composer.continuation_message_family_builders import attachment_result_message, family_message_wire
from elspeth.web.composer.continuation_message_family_recipes import AttachmentResultMessageRecipe
from elspeth.web.composer.continuation_message_shelf import MessageShelfSnapshot
from elspeth.web.composer.continuation_tool_receipts import RetainedToolReceipt, require_receipt_result
from elspeth.web.composer.discovery_response import AdmittedDiscoveryResult, admit_discovery_result, serialize_admitted_discovery_result
from elspeth.web.composer.tool_batch import _append_continuation_tool_outcome
from elspeth.web.composer.tools import ToolResult
from elspeth.web.composer.turn_effects import _RegisteredBlobPage, registered_inspection_from_result
from elspeth.web.sessions.continuation_attachment_result import _result, require_attachment_read_audit
from elspeth.web.sessions.continuation_owner_results import ContinuationReadOwnerResult


def _append_message(shelf: MessageShelfSnapshot, *, wire: str, recipe: AttachmentResultMessageRecipe) -> MessageShelfSnapshot:
    # Integer positions are the original retained object identities. Equal wire
    # values at distinct positions are never merged; the new message is fresh.
    new_identity = len(shelf.messages_json)
    current = (*shelf.current_indexes, new_identity)
    order = tuple(dict.fromkeys((*current, *shelf.excluded_indexes, *(row.message_index for row in shelf.observed))))
    positions = {identity: index for index, identity in enumerate(order)}
    values = (*shelf.messages_json, wire)
    recipes = (*(shelf.producer_recipes or (None,) * len(shelf.messages_json)), recipe)
    return shelf.model_copy(
        update={
            "messages_json": tuple(values[index] for index in order),
            "producer_recipes": tuple(recipes[index] for index in order),
            "current_indexes": tuple(positions[index] for index in current),
            "excluded_indexes": tuple(positions[index] for index in shelf.excluded_indexes),
            "observed": tuple(row.model_copy(update={"message_index": positions[row.message_index]}) for row in shelf.observed),
        }
    )


def _register_page(snapshot: TurnEvidenceSnapshot, *, call_id: str, admitted: AdmittedDiscoveryResult) -> TurnEvidenceSnapshot:
    current = snapshot.deliveries[0].current_call
    actual = registered_inspection_from_result(
        tool_call_id=call_id,
        admitted=admitted,
        selected_content=serialize_admitted_discovery_result(admitted),
        producer_call_id=None if current is None else current[0],
    )
    if actual is None or call_id in snapshot.conflicting_page_ids:
        return snapshot
    if type(actual) is not _RegisteredBlobPage:
        raise AuditIntegrityError("attachment application projected another inspection domain")
    page = RetainedBlobPage(
        tool_call_id=actual.tool_call_id,
        producer_call_id=actual.producer_call_id,
        envelope_hash=actual.envelope_hash,
        blob_id=actual.blob_id,
        content_hash=actual.content_hash,
        total_chars=actual.total_chars,
        start_char=actual.start_char,
        end_char=actual.end_char,
    )
    existing = tuple(row for row in snapshot.registered_pages if row.key == call_id)
    if existing and snapshot.page_shelf[existing[0].page_index] == page:
        return snapshot
    pages = snapshot.page_shelf
    scopes = snapshot.deliveries
    conflicts = snapshot.conflicting_page_ids
    if existing:
        registered = tuple(row for row in snapshot.registered_pages if row.key != call_id)
        conflicts = tuple(sorted((*conflicts, call_id)))
        scopes = tuple(row.model_copy(update={"current_page_indexes": (), "current_call": None}) for row in scopes)
    else:
        registered = (*snapshot.registered_pages, RetainedPageLink(key=call_id, page_index=len(pages)))
        pages = (*pages, page)
    order: list[int] = []

    def retain(index: int) -> int:
        if index not in order:
            order.append(index)
        return order.index(index)

    registered = tuple(row.model_copy(update={"page_index": retain(row.page_index)}) for row in registered)
    quoted = tuple(row.model_copy(update={"page_index": retain(row.page_index)}) for row in snapshot.quoted_inspections)
    historical = tuple(row.model_copy(update={"page_index": retain(row.page_index)}) for row in snapshot.historical_pages)
    deliveries = []
    for row in scopes:
        indexes = tuple(retain(index) for index in row.current_page_indexes)
        pending = row.pending
        if pending is not None:
            pending = pending.model_copy(update={"page_indexes": tuple(retain(index) for index in pending.page_indexes)})
        deliveries.append(row.model_copy(update={"current_page_indexes": indexes, "pending": pending}))
    return snapshot.model_copy(
        update={
            "page_shelf": tuple(pages[index] for index in order),
            "registered_pages": registered,
            "quoted_inspections": quoted,
            "historical_pages": historical,
            "conflicting_page_ids": conflicts,
            "deliveries": tuple(deliveries),
            "registration_revision": snapshot.registration_revision + 1,
        }
    )


def require_attachment_projection(
    *,
    before: AdmittedTurnEvidence,
    after: AdmittedTurnEvidence,
    prepared_source: RestoredPreparedBatchData,
    retained_result: ContinuationReadOwnerResult,
    read_audit: PortableDispatchAudit,
    effect_key: str,
    receipt: RetainedToolReceipt,
    response: PrivateChunk,
    env_ref_names: frozenset[str],
) -> ApplicationGraph:
    before_admitted = require_admitted_turn_evidence(before, env_ref_names=env_ref_names)
    after_admitted = require_admitted_turn_evidence(after, env_ref_names=env_ref_names)
    before_snapshot = before_admitted.snapshot
    old = before_admitted.graph
    new = after_admitted.graph
    if old is None or new is None or old.selected_audit is None or old.selected_dialect is None:
        raise AuditIntegrityError("attachment projection lacks its complete original prepared identity graph")
    prepared = RestoredPreparedBatchData(
        old.selected.batch, old.selected.accumulator, old.selected.calls, old.selected_audit, old.selected_dialect
    )
    if encode_restored_prepared_batch_data(prepared, env_ref_names=env_ref_names) != encode_restored_prepared_batch_data(
        prepared_source, env_ref_names=env_ref_names
    ):
        raise AuditIntegrityError("attachment projection changed its actual selected prepared/read source")
    require_attachment_read_audit(prepared, read_audit, names=env_ref_names)
    call = prepared.batch.current_call
    if call is None or call.tool_name not in ("read_attachment", "get_blob_content"):
        raise AuditIntegrityError("attachment projection lost its actual selected read call")
    if decode_continuation_response(response).tool_batch.calls != prepared.calls:
        raise AuditIntegrityError("attachment projection changed its actual complete provider call cohort")
    result = _result(retained_result, prepared, env_ref_names)
    admitted = admit_discovery_result(call.tool_name, result)
    nominal = admitted.to_tool_result()
    invocation = require_receipt_result(
        receipt, selected_call=call.tool_call, pre_state=call.pre_state, result=nominal, env_ref_names=env_ref_names
    )
    audit = read_audit
    if (
        receipt.effect_key != effect_key
        or invocation.status is not ComposerToolStatus.SUCCESS
        or invocation.cache_hit
        or invocation.tool_call_id != audit.tool_call_id
        or invocation.tool_name != audit.tool_name
        or invocation.arguments_canonical != audit.arguments_canonical
        or invocation.arguments_hash != audit.arguments_hash
        or invocation.authority_arguments_canonical != audit.authority_arguments_canonical
        or invocation.authority_arguments_hash != audit.authority_arguments_hash
        or invocation.actor != audit.actor
        or invocation.strict_sent is not audit.strict_sent
        or invocation.wire_conformant is not audit.wire_conformant
        or invocation.started_at != audit.started_at
        or invocation.latency_ms < audit.elapsed_active_ns // 1_000_000
    ):
        raise AuditIntegrityError("attachment projection lost its original completed-read audit/timing")
    if any(row.tool_call_id == call.tool_call.id for row in old.recorder.invocations):
        raise AuditIntegrityError("attachment projection cannot finish an already recorded call")
    authoring = old.selected.batch.private_authorings_by_call[call.tool_call.id]
    if invocation.private_authoring != authoring:
        raise AuditIntegrityError("attachment projection changed its original private authoring")
    invocation = replace(invocation, private_authoring=authoring)
    recorder = replace(old.recorder, invocations=(*old.recorder.invocations, invocation))
    # Reuse the actual outcome/refused-resource producer. This checks the
    # transition; it does not call finish_success or mutate a live runtime.
    batch = replace(
        prepared.batch,
        tool_outcomes=list(prepared.batch.tool_outcomes),
        refused_resources=set(prepared.batch.refused_resources),
        state=nominal.updated_state,
        last_validation=nominal.validation,
        last_runtime_preflight=nominal.runtime_preflight or prepared.batch.last_runtime_preflight,
        all_cache_hits=False,
        turn_has_discovery=True,
    )
    _append_continuation_tool_outcome(
        batch, call, response=nominal, error_class=None, error_category=None, error_message=None, post_version=nominal.updated_state.version
    )
    stopped = batch.stop_issued_batch or prepared.accumulator.authority_revocation is not None
    batch.next_tool_index += 1
    batch.current_call = None
    batch.stop_issued_batch = stopped
    batch.phase = "dispatch_ready" if stopped or batch.next_tool_index == len(prepared.calls) else "call_ready"
    expected_graph = replace(
        old,
        selected=RestoredBatchData(batch, prepared.accumulator, prepared.calls),
        recorder=recorder,
        selected_audit=None,
        selected_dialect=None,
    )
    recorder_source = admit_application_recorder_source(after.recorder_chunk, env_ref_names=env_ref_names)
    expected_json = encode_application_graph(expected_graph, env_ref_names=env_ref_names, recorder_source=recorder_source)
    if encode_application_graph(new, env_ref_names=env_ref_names, recorder_source=recorder_source) != expected_json:
        raise AuditIntegrityError("attachment projection changed its complete batch/recorder/history identity graph")
    expected = _register_page(before_snapshot, call_id=call.tool_call.id, admitted=admitted)
    message = attachment_result_message(call=call.tool_call, result=nominal)
    shelf = _append_message(
        before_snapshot.transcript,
        wire=family_message_wire(message),
        recipe=AttachmentResultMessageRecipe(response=response.reference, tool_call_id=call.tool_call.id),
    )
    entry = (
        None
        if before_snapshot.application_entry is None
        else before_snapshot.application_entry.model_copy(update={"graph_json": expected_json})
    )
    recorder_chunk = encode_recorder_snapshot(recorder)
    expected = expected.model_copy(
        update={
            "transcript": shelf,
            "recorder": recorder_chunk.reference,
            "joint_graph_json": expected_json if entry is None else None,
            "application_entry": entry,
        }
    )
    expected_admitted = admit_turn_evidence(expected, env_ref_names=env_ref_names, source_chunks=(recorder_chunk, *before.sources.chunks))
    if encode_turn_evidence(after_admitted, env_ref_names=env_ref_names) != encode_turn_evidence(
        expected_admitted, env_ref_names=env_ref_names
    ):
        raise AuditIntegrityError("attachment projection changed original evidence/transcript or actual page registration")
    return new


def admit_attachment_application(
    value: AttachmentApplicationData, *, env_ref_names: frozenset[str], source_chunks: tuple[PrivateChunk, ...] = ()
) -> tuple[ContinuationFrameStack, ContinuationReadOwnerResult]:
    """Check complete historical DATA; current SQL source proof is still owed."""
    from elspeth.contracts.hashing import canonical_json
    from elspeth.web.composer.anti_anchor import AntiAnchorTracker
    from elspeth.web.composer.continuation import ComposeFrame, ToolBatchFrame
    from elspeth.web.composer.continuation_attachment_result_data import (
        AttachmentHistoryBatch,
        decode_attachment_published_result,
        encode_attachment_history_batch,
    )
    from elspeth.web.composer.continuation_batch_data import decode_prepared_batch_data, encode_coupled_batch_data
    from elspeth.web.composer.continuation_caches import decode_continuation_caches
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_private_chunk
    from elspeth.web.composer.continuation_dispatch_audit import decode_dispatch_audit
    from elspeth.web.composer.continuation_evidence import decode_turn_evidence
    from elspeth.web.composer.continuation_iteration import decode_continuation_iteration, encode_continuation_iteration
    from elspeth.web.composer.continuation_tool_receipts import decode_retained_tool_receipt
    from elspeth.web.credential_guard import require_no_credential_material
    from elspeth.web.sessions.continuation_owner_results import decode_read_owner_result

    if type(value) is not AttachmentApplicationData:
        raise TypeError("attachment application requires its exact complete DATA owner")
    encoded = canonical_json(value.model_dump(mode="json"))
    checked = AttachmentApplicationData.model_validate_json(encoded, strict=True)
    if canonical_json(checked.model_dump(mode="json")) != encoded:
        raise AuditIntegrityError("attachment application lost or normalized a field")
    require_no_credential_material(
        (value.effect_key, value.source_checkpoint_id, value.application_operation_id),
        surface="attachment historical application identifiers",
        env_ref_names=env_ref_names,
    )
    root = decode_continuation_snapshot(value.source_snapshot_json)
    if len(root.frames) != 2 or root.cursor != value.source_cursor:
        raise AuditIntegrityError("attachment application lost its exact original root cursor")
    compose, frame = root.frames
    if type(compose) is not ComposeFrame or type(frame) is not ToolBatchFrame or frame.phase != "result_retained":
        raise AuditIntegrityError("attachment application lost its original history-consumed cut")
    if value.effect_key not in frame.settled_effect_ids or root.pending_owner_results:
        raise AuditIntegrityError("attachment application lost its exact consumed read effect")
    response = encode_private_chunk(codec="composer.response.v1", canonical=value.response_json, env_ref_names=env_ref_names)
    if response.reference != frame.response or compose.pending_response != frame.response:
        raise AuditIntegrityError("attachment application changed its exact original response")
    publication = decode_attachment_published_result(
        encode_private_chunk(codec="composer.owner-result.v1", canonical=value.published_result_json, env_ref_names=env_ref_names),
        env_ref_names=env_ref_names,
    )
    if publication.effect_key != value.effect_key or publication.read_audit_json is None:
        raise AuditIntegrityError("attachment application lacks the actual original timed read")
    wrapper = encode_attachment_history_batch(
        AttachmentHistoryBatch(prepared_json=value.prepared_json, result_json=value.published_result_json), env_ref_names=env_ref_names
    )
    if wrapper.reference != frame.accumulator:
        raise AuditIntegrityError("attachment application changed its complete original prepared/read bytes")
    prepared = decode_prepared_batch_data(value.prepared_json, env_ref_names=env_ref_names)
    read_audit = decode_dispatch_audit(publication.read_audit_json, env_ref_names=env_ref_names)
    retained = decode_read_owner_result(publication.result_json, env_ref_names=env_ref_names)
    before = decode_turn_evidence(
        encode_private_chunk(codec="composer.evidence.v2", canonical=value.before_evidence_json, env_ref_names=env_ref_names),
        env_ref_names=env_ref_names,
        source_chunks=source_chunks,
    )
    after = decode_turn_evidence(
        encode_private_chunk(codec="composer.evidence.v2", canonical=value.after_evidence_json, env_ref_names=env_ref_names),
        env_ref_names=env_ref_names,
        source_chunks=source_chunks,
    )
    if before.snapshot.logical != root.logical or (before.snapshot.physical_operation_id, before.snapshot.physical_operation_epoch) != (
        value.application_operation_id,
        value.application_operation_epoch,
    ):
        raise AuditIntegrityError("attachment application changed its logical/current producer attribution")
    receipt = decode_retained_tool_receipt(value.receipt_json, env_ref_names=env_ref_names)
    graph = require_attachment_projection(
        before=before,
        after=after,
        prepared_source=prepared,
        retained_result=retained,
        read_audit=read_audit,
        effect_key=value.effect_key,
        receipt=receipt,
        response=response,
        env_ref_names=env_ref_names,
    )
    if (
        encode_coupled_batch_data(
            batch=graph.selected.batch, accumulator=graph.selected.accumulator, calls=graph.selected.calls, env_ref_names=env_ref_names
        )
        != value.after_batch_json
        or frame.next_tool_index != prepared.batch.next_tool_index
    ):
        raise AuditIntegrityError("attachment application changed its actual complete selected successor")
    old_iteration = decode_continuation_iteration(
        encode_private_chunk(codec="composer.iteration.v1", canonical=value.before_iteration_json, env_ref_names=env_ref_names),
        env_ref_names=env_ref_names,
    )
    new_iteration = decode_continuation_iteration(
        encode_private_chunk(codec="composer.iteration.v1", canonical=value.after_iteration_json, env_ref_names=env_ref_names),
        env_ref_names=env_ref_names,
    )
    tracker = AntiAnchorTracker.for_continuation(old_iteration.anti_anchor)
    completed_result = graph.selected.batch.tool_outcomes[-1].response
    if type(completed_result) is not ToolResult:
        raise AuditIntegrityError("attachment application lost its actual completed nominal ToolResult")
    if not completed_result.success:
        tracker.record_failure(read_audit.tool_name, read_audit.arguments_hash)
    expected_iteration = old_iteration.model_copy(update={"anti_anchor": tracker.snapshot_for_continuation()})
    if encode_continuation_iteration(new_iteration, env_ref_names=env_ref_names) != encode_continuation_iteration(
        expected_iteration, env_ref_names=env_ref_names
    ):
        raise AuditIntegrityError("attachment application changed unrelated iteration/prompt/budget history")
    before_caches = decode_continuation_caches(
        encode_private_chunk(codec="composer.caches.v1", canonical=value.before_caches_json, env_ref_names=env_ref_names),
        env_ref_names=env_ref_names,
    )
    after_caches = decode_continuation_caches(
        encode_private_chunk(codec="composer.caches.v1", canonical=value.after_caches_json, env_ref_names=env_ref_names),
        env_ref_names=env_ref_names,
    )
    if (
        after_caches.session_id != before_caches.session_id
        or after_caches.session_id != root.logical.session_id
        or after_caches.capture_count != before_caches.capture_count + 1
        or after_caches.capture_creators[:-1] != before_caches.capture_creators
        or after_caches.capture_creators[-1].operation_id != value.application_operation_id
        or after_caches.capture_creators[-1].operation_epoch != value.application_operation_epoch
        or after_caches.discovery[: len(before_caches.discovery)] != before_caches.discovery
        or after_caches.preflight[: len(before_caches.preflight)] != before_caches.preflight
        or after_caches.previous_last_preflight_json != (*before_caches.previous_last_preflight_json, before_caches.last_preflight_json)
        or after_caches.previous_last_preflight_identities
        != (*before_caches.previous_last_preflight_identities, before_caches.last_preflight_identity)
    ):
        raise AuditIntegrityError("attachment application discarded actual historical cache values or creator observations")
    return root, retained

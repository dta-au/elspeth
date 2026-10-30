"""Atomic attachment tool application and immutable applied-history verification.

Current admission is owned by the caller's ordinary selected lease/claim/CAS.
Historical application DATA and old inspections never authorize another read.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.continuation import ComposeFrame, ContinuationFrameStack, ToolBatchFrame
from elspeth.web.composer.continuation_attachment_application import admit_attachment_application
from elspeth.web.composer.continuation_attachment_application_values import AttachmentApplicationData
from elspeth.web.composer.continuation_attachment_finisher import CompletedAttachmentProjection
from elspeth.web.composer.continuation_attachment_history import (
    ContinuationAttachmentHistory,
    attachment_history_dependencies,
    decode_attachment_history,
    decode_attachment_history_fields,
    encode_attachment_history,
    extend_attachment_history,
)
from elspeth.web.composer.continuation_attachment_result_data import decode_attachment_history_batch
from elspeth.web.composer.continuation_batch_data import (
    RestoredBatchData,
    decode_coupled_batch_data,
    decode_prepared_batch_data,
    encode_coupled_batch_data,
)
from elspeth.web.composer.continuation_caches import decode_continuation_caches
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_snapshot,
    encode_continuation_snapshot,
    encode_private_chunk,
)
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory
from elspeth.web.composer.continuation_evidence import (
    AdmittedTurnEvidence,
    admit_turn_evidence,
    decode_turn_evidence,
    encode_turn_evidence,
    require_admitted_turn_evidence,
)
from elspeth.web.composer.continuation_message_shelf import encode_message_shelf
from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime, _selected_chunks
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.continuation_tool_receipts import encode_retained_tool_receipt
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    StoredContinuationSnapshot,
    append_complete_snapshot,
    private_chunk_key,
    put_chunk,
    read_chunk,
)
from elspeth.web.sessions.continuation_attachment_result import (
    AttachmentResultCheckpoint,
    _chunk,
    _selected,
    read_attachment_result_on_connection,
    verify_attachment_result_source_on_connection,
)
from elspeth.web.sessions.continuation_selection import load_private_chunks
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts


@dataclass(frozen=True, slots=True)
class StoredAttachmentApplication:
    snapshot: StoredContinuationSnapshot
    application: AttachmentApplicationData


@dataclass(frozen=True, slots=True)
class AttachmentHistorySources:
    """Source-admitted historical DATA for this read, never current authority."""

    history: ContinuationAttachmentHistory
    chunks: tuple[PrivateChunk, ...]


def load_attachment_history_sources_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    names: frozenset[str],
) -> AttachmentHistorySources:
    """Resolve closed original dependencies on the caller's physical read."""
    from elspeth.web.sessions.continuation_p4_application_store import _private_chunk_on_connection

    compose = tuple(frame for frame in selected.stack.frames if type(frame) is ComposeFrame)
    if len(compose) != 1:
        raise AuditIntegrityError("attachment history lost its exact selected Compose owner")
    actual = _chunk(selected, compose[0].attachment_admissions)
    if _text(conn, running, private_chunk_key(actual)) != actual.canonical:
        raise AuditIntegrityError("attachment history differs from its original SQL bytes")
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    fields = decode_attachment_history_fields(actual, session_id=sid, operation_id=operation, env_ref_names=names)
    # Preserve every occurrence and its physical work. There is no new ticket,
    # budget reset or reference deduplication before a conflicting length check.
    chunks = tuple(_private_chunk_on_connection(conn, running, ref) for ref in attachment_history_dependencies(fields))
    history = decode_attachment_history(actual, session_id=sid, operation_id=operation, env_ref_names=names, source_chunks=chunks)
    return AttachmentHistorySources(history, chunks)


def _text(conn: Connection, running: ComposerOperationRunning, key: str) -> str:
    try:
        return read_chunk(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id, key=key).decode(
            "utf-8", errors="strict"
        )
    except UnicodeDecodeError as error:
        raise AuditIntegrityError("attachment application storage is not strict UTF-8") from error


def _event(value: AttachmentApplicationData) -> str:
    return canonical_json(
        {
            "owner": "attachment_tool_application",
            "effect_key": value.effect_key,
            "source_checkpoint_id": value.source_checkpoint_id,
            "source_cursor": value.source_cursor,
            "application_sequence": value.application_sequence,
            "application_generation": value.application_generation,
            "application_operation_id": value.application_operation_id,
            "application_operation_epoch": value.application_operation_epoch,
        }
    )


def _historical_source(
    conn: Connection, running: ComposerOperationRunning, value: AttachmentApplicationData, *, names: frozenset[str]
) -> tuple[SelectedContinuationState, AttachmentResultCheckpoint]:
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    row = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation,
            checkpoints.c.checkpoint_id == value.source_checkpoint_id,
        )
    ).one_or_none()
    if row is None or row.cursor != value.source_cursor or row.kind != "transition":
        raise AuditIntegrityError("attachment application lost its actual predecessor checkpoint")
    text = _text(conn, running, row.snapshot_chunk_key)
    if text != value.source_snapshot_json:
        raise AuditIntegrityError("attachment application changed its complete original SQL root")
    root = decode_continuation_snapshot(text)
    stored = StoredContinuationSnapshot(text, row.cursor, row.checkpoint_id)
    selected = SelectedContinuationState(
        row.checkpoint_id, root, load_private_chunks(conn, session_id=sid, operation_id=operation, stack=root)
    )
    original = verify_attachment_result_source_on_connection(conn, running, selected=selected, snapshot=stored, env_ref_names=names)
    frame = root.frames[-1]
    if type(frame) is not ToolBatchFrame or not original.history_applied:
        raise AuditIntegrityError("attachment application lacks its actual consumed-history predecessor")
    wrapper = decode_attachment_history_batch(_chunk(selected, frame.accumulator), env_ref_names=names)
    if (
        original.effect_key != value.effect_key
        or wrapper.prepared_json != value.prepared_json
        or wrapper.result_json != value.published_result_json
        or _chunk(selected, frame.response).canonical != value.response_json
    ):
        raise AuditIntegrityError("attachment application changed its immutable prepared/read/response sources")
    creator = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation,
            attempts.c.sequence == value.application_sequence,
            attempts.c.generation == value.application_generation,
        )
    ).one_or_none()
    if creator is None or (creator.session_operation_id, creator.session_operation_epoch) != (
        value.application_operation_id,
        value.application_operation_epoch,
    ):
        raise AuditIntegrityError("attachment application changed its actual applying physical creator")
    return selected, original


def _require_before(selected: SelectedContinuationState, value: AttachmentApplicationData, *, names: frozenset[str]) -> None:
    compose = selected.stack.frames[0]
    if type(compose) is not ComposeFrame:
        raise AuditIntegrityError("attachment application lost its original Compose owner")
    original = decode_turn_evidence(_chunk(selected, compose.evidence), env_ref_names=names, source_chunks=selected.chunks)
    if original.snapshot.recorder != compose.audit_buffer:
        raise AuditIntegrityError("attachment predecessor changed its original frame recorder join")
    rebound = original.snapshot.model_copy(
        update={"physical_operation_id": value.application_operation_id, "physical_operation_epoch": value.application_operation_epoch}
    )
    if (
        encode_turn_evidence(
            admit_turn_evidence(rebound, env_ref_names=names, source_chunks=original.dependency_chunks), env_ref_names=names
        ).canonical
        != value.before_evidence_json
        or _chunk(selected, compose.iteration).canonical != value.before_iteration_json
        or _chunk(selected, compose.caches).canonical != value.before_caches_json
    ):
        raise AuditIntegrityError("attachment application changed its complete actual pre-application evidence/history")


def _after_root(
    selected: SelectedContinuationState,
    value: AttachmentApplicationData,
    *,
    names: frozenset[str],
    source_chunks: tuple[PrivateChunk, ...],
    history: ContinuationAttachmentHistory,
) -> tuple[ContinuationFrameStack, tuple[PrivateChunk, ...]]:
    compose, frame = selected.stack.frames
    if type(compose) is not ComposeFrame or type(frame) is not ToolBatchFrame:
        raise AuditIntegrityError("attachment application lost its original frame pair")
    evidence = encode_private_chunk(codec="composer.evidence.v2", canonical=value.after_evidence_json, env_ref_names=names)
    admitted = decode_turn_evidence(evidence, env_ref_names=names, source_chunks=source_chunks)
    snapshot = admitted.snapshot
    transcript = encode_message_shelf(snapshot.transcript)
    messages = encode_private_chunk(
        codec="composer.messages.v1",
        canonical=canonical_json(
            {"messages_json": snapshot.transcript.messages_json, "current_indexes": snapshot.transcript.current_indexes}
        ),
        env_ref_names=names,
    )
    audit = admitted.recorder_chunk
    batch = encode_private_chunk(codec="composer.coupled-batch-data.v1", canonical=value.after_batch_json, env_ref_names=names)
    iteration = encode_private_chunk(codec="composer.iteration.v1", canonical=value.after_iteration_json, env_ref_names=names)
    caches = encode_private_chunk(codec="composer.caches.v1", canonical=value.after_caches_json, env_ref_names=names)
    if any(item.effect_key == value.effect_key for item in history.applications):
        raise AuditIntegrityError("attachment application cannot append the same actual effect twice")
    history_chunk = encode_attachment_history(
        extend_attachment_history(history, applications=(value,)), env_ref_names=names, source_chunks=source_chunks
    )
    graph = admitted.graph
    if graph is None:
        raise AuditIntegrityError("attachment application lost its exact shared successor")
    changed = compose.model_copy(
        update={
            "evidence": evidence.reference,
            "transcript": transcript.reference,
            "messages": messages.reference,
            "audit_buffer": audit.reference,
            "iteration": iteration.reference,
            "caches": caches.reference,
            "attachment_admissions": history_chunk.reference,
        }
    )
    changed_batch = frame.model_copy(
        update={"phase": "tool_ready", "accumulator": batch.reference, "next_tool_index": graph.selected.batch.next_tool_index}
    )
    root = selected.stack.model_copy(update={"frames": (changed, changed_batch)})
    added = (evidence, transcript, messages, audit, batch, iteration, caches, history_chunk)
    return root, _selected_chunks(root, selected.chunks, added)


def apply_attachment_application_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    projection: CompletedAttachmentProjection,
    captured: CapturedContinuationRuntime,
) -> StoredAttachmentApplication:
    if type(projection) is not CompletedAttachmentProjection or type(captured) is not CapturedContinuationRuntime:
        raise AuditIntegrityError("attachment application requires actual completed and captured owners")
    current, names = _selected(conn, running, selected, inventory)
    # The current result read retains current SQL versions and every original
    # input/result/creator/receipt check before any chunk or event is written.
    original = read_attachment_result_on_connection(conn, running, selected=selected, inventory=inventory)
    if not original.history_applied or original.read_audit is None or projection.source.snapshot != current:
        raise AuditIntegrityError("attachment application lost its actual timed unconsumed tool result")
    if captured.selected != selected or captured.stack.cursor != selected.stack.cursor:
        raise AuditIntegrityError("attachment application changed its exact capture CAS base")
    sequence, generation = running.claim.segment_sequence, running.claim.ownership_generation
    if type(sequence) is not int or sequence <= 0 or type(generation) is not int or generation <= 0:
        raise AuditIntegrityError("attachment application requires positive actual creator coordinates")
    compose, frame = selected.stack.frames
    if type(compose) is not ComposeFrame or type(frame) is not ToolBatchFrame:
        raise AuditIntegrityError("attachment application lost its selected frame pair")
    projected = SelectedContinuationState(selected.checkpoint_id, captured.stack, captured.chunks)
    projected_compose = captured.stack.frames[0]
    if type(projected_compose) is not ComposeFrame:
        raise AuditIntegrityError("attachment capture changed its nominal Compose owner")
    wrapper = decode_attachment_history_batch(_chunk(selected, frame.accumulator), env_ref_names=names)
    value = AttachmentApplicationData(
        effect_key=original.effect_key,
        source_checkpoint_id=current.checkpoint_id,
        source_cursor=current.cursor,
        source_snapshot_json=current.payload_json,
        prepared_json=wrapper.prepared_json,
        response_json=_chunk(selected, frame.response).canonical,
        published_result_json=wrapper.result_json,
        receipt_json=encode_retained_tool_receipt(projection.receipt, env_ref_names=names),
        before_evidence_json=encode_turn_evidence(projection.before, env_ref_names=names).canonical,
        after_evidence_json=encode_turn_evidence(projection.after, env_ref_names=names).canonical,
        after_batch_json=encode_coupled_batch_data(
            batch=projection.data.batch, accumulator=projection.data.accumulator, calls=projection.data.calls, env_ref_names=names
        ),
        before_iteration_json=_chunk(selected, compose.iteration).canonical,
        after_iteration_json=_chunk(projected, projected_compose.iteration).canonical,
        before_caches_json=_chunk(selected, compose.caches).canonical,
        after_caches_json=_chunk(projected, projected_compose.caches).canonical,
        application_sequence=sequence,
        application_generation=generation,
        application_operation_id=running.session_operation_context.fence.operation_id,
        application_operation_epoch=running.session_operation_context.fence.operation_epoch,
    )
    historical = load_attachment_history_sources_on_connection(conn, running, selected=selected, names=names)
    original_sources = (*historical.chunks, *selected.chunks, *projection.after.dependency_chunks)
    admit_attachment_application(value, env_ref_names=names, source_chunks=original_sources)
    _require_before(selected, value, names=names)
    root, complete = _after_root(selected, value, names=names, source_chunks=original_sources, history=historical.history)
    # Runtime captures data, not phase authority. Its only pending phase change
    # here is precisely the one call advancement admitted by the original B2.
    expected_frames = list(root.frames)
    expected_frames[0] = root.frames[0].model_copy(update={"attachment_admissions": compose.attachment_admissions})
    expected_frames[-1] = root.frames[-1].model_copy(update={"phase": frame.phase})
    expected_capture = root.model_copy(update={"frames": tuple(expected_frames)})
    if encode_continuation_snapshot(captured.stack) != encode_continuation_snapshot(expected_capture):
        raise AuditIntegrityError("attachment application changed unrelated runtime/root/frontier/counters")
    if _chunk(projected, projected_compose.evidence).canonical != value.after_evidence_json:
        raise AuditIntegrityError("attachment capture changed the actual completed shared graph")
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    for chunk in complete:
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=private_chunk_key(chunk),
            payload=chunk.canonical.encode("utf-8"),
            codec=chunk.reference.codec,
        )
    following = append_complete_snapshot(
        conn,
        session_id=sid,
        operation_id=operation,
        expected_checkpoint_id=current.checkpoint_id,
        expected_cursor=current.cursor,
        snapshot_json=encode_continuation_snapshot(root),
        event_batch=(ContinuationEvent("checkpoint", _event(value)),),
        handoff=False,
    )
    return StoredAttachmentApplication(following, value)


def require_attachment_applications_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    evidence: AdmittedTurnEvidence,
    env_ref_names: frozenset[str],
) -> AttachmentHistorySources:
    """Reprove complete historical DATA and return this read's original cohort."""
    compose = tuple(frame for frame in selected.stack.frames if type(frame) is ComposeFrame)
    if len(compose) != 1:
        raise AuditIntegrityError("attachment history requires its exact current Compose owner")
    ref = compose[0].attachment_admissions
    actual = _chunk(selected, ref)
    if _text(conn, running, private_chunk_key(actual)) != actual.canonical:
        raise AuditIntegrityError("attachment history differs from its actual current SQL bytes")
    sources = load_attachment_history_sources_on_connection(conn, running, selected=selected, names=env_ref_names)
    history = sources.history
    # An absent portable entry must not erase a real completed application.
    # Inspection-only published reads legitimately have no application event.
    actual_applications: list[tuple[int, str]] = []
    for stored_event in conn.execute(
        select(events)
        .where(
            events.c.session_id == str(running.claim.session_id),
            events.c.operation_id == running.claim.operation_id,
            events.c.sequence <= selected.stack.cursor,
        )
        .order_by(events.c.sequence)
    ):
        event_text = _text(conn, running, stored_event.payload_chunk_key)
        event_value, event_error = parse_json_strict(event_text)
        if event_error is not None:
            raise AuditIntegrityError("attachment application encountered invalid owned event JSON")
        if type(event_value) is dict and event_value.get("owner") == "attachment_tool_application":
            if stored_event.tag != "checkpoint":
                raise AuditIntegrityError("attachment application event changed its owned tag")
            actual_applications.append((stored_event.sequence, event_text))
    if tuple(actual_applications) != tuple((value.source_cursor + 1, _event(value)) for value in history.applications):
        raise AuditIntegrityError("attachment history omitted or changed its complete actual application cohort")
    current_graph = require_admitted_turn_evidence(evidence, env_ref_names=env_ref_names).graph
    previous_cursor = -1
    for value in history.applications:
        if not previous_cursor < value.source_cursor < selected.stack.cursor:
            raise AuditIntegrityError("attachment applications changed their actual historical order")
        previous_cursor = value.source_cursor
        old, _original = _historical_source(conn, running, value, names=env_ref_names)
        if (
            old.stack.logical != selected.stack.logical
            or old.stack.mutation_ceiling != selected.stack.mutation_ceiling
            or old.stack.policy_hash != selected.stack.policy_hash
            or old.stack.admission_policy_json != selected.stack.admission_policy_json
        ):
            raise AuditIntegrityError("attachment application crossed its original logical policy/ceiling")
        _require_before(old, value, names=env_ref_names)
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        event = conn.execute(
            select(events).where(
                events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == value.source_cursor + 1
            )
        ).one_or_none()
        following = conn.execute(
            select(checkpoints).where(
                checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation, checkpoints.c.cursor == value.source_cursor + 1
            )
        ).one_or_none()
        if (
            event is None
            or event.tag != "checkpoint"
            or _text(conn, running, event.payload_chunk_key) != _event(value)
            or following is None
        ):
            raise AuditIntegrityError("attachment application lost its actual atomic successor/event")
        following_text = _text(conn, running, following.snapshot_chunk_key)
        following_root = decode_continuation_snapshot(following_text)
        following_selected = SelectedContinuationState(
            following.checkpoint_id,
            following_root,
            load_private_chunks(conn, session_id=sid, operation_id=operation, stack=following_root),
        )
        prior_sources = load_attachment_history_sources_on_connection(conn, running, selected=old, names=env_ref_names)
        original_sources = (*prior_sources.chunks, *old.chunks, *following_selected.chunks)
        admit_attachment_application(value, env_ref_names=env_ref_names, source_chunks=original_sources)
        expected, _cohort = _after_root(old, value, names=env_ref_names, source_chunks=original_sources, history=prior_sources.history)
        if following_text != encode_continuation_snapshot(expected.model_copy(update={"cursor": value.source_cursor + 1})):
            raise AuditIntegrityError("attachment application changed its actual complete atomic successor")
        # The current root may append later observations, but cannot rewind
        # the complete owners that this actual atomic successor published.
        predecessor_compose = old.stack.frames[0]
        if type(predecessor_compose) is not ComposeFrame:
            raise AuditIntegrityError("attachment application lost its predecessor Compose owner")
        predecessor_history = prior_sources.history
        expected_applications = (*predecessor_history.applications, value)
        if (
            history.reads[: len(predecessor_history.reads)] != predecessor_history.reads
            or history.applications[: len(expected_applications)] != expected_applications
        ):
            raise AuditIntegrityError("attachment application lost its complete inspection/application history prefix")
        previous_caches = decode_continuation_caches(
            encode_private_chunk(codec="composer.caches.v1", canonical=value.after_caches_json, env_ref_names=env_ref_names),
            env_ref_names=env_ref_names,
        )
        current_caches = decode_continuation_caches(_chunk(selected, compose[0].caches), env_ref_names=env_ref_names)
        prior_last = (*previous_caches.previous_last_preflight_json, previous_caches.last_preflight_json)
        current_last = (*current_caches.previous_last_preflight_json, current_caches.last_preflight_json)
        prior_identities = (*previous_caches.previous_last_preflight_identities, previous_caches.last_preflight_identity)
        current_identities = (*current_caches.previous_last_preflight_identities, current_caches.last_preflight_identity)
        if (
            current_caches.session_id != previous_caches.session_id
            or current_caches.capture_count < previous_caches.capture_count
            or current_caches.capture_creators[: previous_caches.capture_count] != previous_caches.capture_creators
            or current_caches.discovery[: len(previous_caches.discovery)] != previous_caches.discovery
            or tuple(row for row in current_caches.discovery if row.capture_number <= previous_caches.capture_count)
            != previous_caches.discovery
            or current_caches.preflight[: len(previous_caches.preflight)] != previous_caches.preflight
            or tuple(row for row in current_caches.preflight if row.capture_number <= previous_caches.capture_count)
            != previous_caches.preflight
            or current_last[: previous_caches.capture_count] != prior_last
            or current_identities[: previous_caches.capture_count] != prior_identities
        ):
            raise AuditIntegrityError("attachment application rewound or changed its complete cache observation history")
        after = decode_turn_evidence(
            encode_private_chunk(codec="composer.evidence.v2", canonical=value.after_evidence_json, env_ref_names=env_ref_names),
            env_ref_names=env_ref_names,
            source_chunks=following_selected.chunks,
        )
        old_graph = after.graph
        if current_graph is None or old_graph is None:
            raise AuditIntegrityError("attachment application lost its complete current recorder graph")
        old_recorder, current_recorder = old_graph.recorder, current_graph.recorder
        for before_rows, current_rows in (
            (old_recorder.invocations, current_recorder.invocations),
            (old_recorder.llm_calls, current_recorder.llm_calls),
            (old_recorder.private_authorings, current_recorder.private_authorings),
            (old_recorder.visible_tool_events, current_recorder.visible_tool_events),
            (old_recorder.withheld_replies, current_recorder.withheld_replies),
            (old_recorder.planner_attempts, current_recorder.planner_attempts),
            (old_recorder.private_candidates, current_recorder.private_candidates),
        ):
            if current_rows[: len(before_rows)] != before_rows:
                raise AuditIntegrityError("attachment application lost an original complete recorder prefix")
        if (
            current_recorder.drained.invocations < old_recorder.drained.invocations
            or current_recorder.drained.llm_calls < old_recorder.drained.llm_calls
            or current_recorder.drained.visible_tool_events < old_recorder.drained.visible_tool_events
            or current_recorder.drained.withheld_replies < old_recorder.drained.withheld_replies
            or current_recorder.drained.planner_attempts < old_recorder.drained.planner_attempts
            or current_recorder.drained.private_candidates < old_recorder.drained.private_candidates
            or current_recorder.drained.private_authorings < old_recorder.drained.private_authorings
        ):
            raise AuditIntegrityError("attachment application reset its original recorder drain cursors")

    return sources


def completed_attachment_prefix_correspondence(
    *, selected: SelectedContinuationState, data: RestoredBatchData, sources: AttachmentHistorySources, names: frozenset[str]
) -> dict[int, str]:
    """Join current outcomes to the caller's FULL SQL-verified history DATA.

    This pure per-read projection grants no current, issue, Stop or dispatch
    authority. Its SQL caller must first run the complete historical verifier.
    Original and current decodes retain their own aliases; full DATA comparison
    across those decodes does not pretend they share Python object identities.
    """
    if (
        type(selected) is not SelectedContinuationState
        or type(data) is not RestoredBatchData
        or type(sources) is not AttachmentHistorySources
    ):
        raise AuditIntegrityError("attachment prefix changed its nominal source cohort")
    root = selected.stack
    frame = root.frames[-1]
    if type(frame) is not ToolBatchFrame or (sources.history.session_id, sources.history.operation_id) != (
        root.logical.session_id,
        root.logical.operation_id,
    ):
        raise AuditIntegrityError("attachment prefix crossed its original selected source")
    response = _chunk(selected, frame.response)
    result: dict[int, str] = {}
    for value in sources.history.applications:
        if value.response_json != response.canonical:
            continue
        prepared = decode_prepared_batch_data(value.prepared_json, env_ref_names=names)
        after = decode_coupled_batch_data(value.after_batch_json, env_ref_names=names)
        index, call = prepared.batch.next_tool_index, prepared.batch.current_call
        if (
            call is None
            or not 0 <= index < len(data.batch.tool_outcomes)
            or index in result
            or index >= len(data.calls)
            or index >= len(prepared.calls)
            or call.index != index
            or call.tool_call is not prepared.calls[index]
            or value.effect_key != f"attachment:{frame.response.digest}:{index}"
            or value.effect_key not in frame.settled_effect_ids
            or prepared.calls != data.calls
            or after.calls != data.calls
            or after.batch.next_tool_index != index + 1
            or len(after.batch.tool_outcomes) != index + 1
            or prepared.batch.tool_outcomes != data.batch.tool_outcomes[:index]
            or after.batch.tool_outcomes != data.batch.tool_outcomes[: index + 1]
            or data.batch.tool_outcomes[index].call is not data.calls[index]
            or after.batch.tool_outcomes[index].call is not after.calls[index]
        ):
            raise AuditIntegrityError("attachment prefix changed its original call/index/outcome source")
        result[index] = value.effect_key
    return result


def read_applied_attachment_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    effect_key: str,
) -> StoredAttachmentApplication:
    from elspeth.web.composer.continuation_evidence_restore import read_turn_evidence_on_connection

    current, names = _selected(conn, running, selected, inventory)
    evidence = read_turn_evidence_on_connection(conn, running, selected=selected, inventory=inventory)
    require_attachment_applications_on_connection(conn, running, selected=selected, evidence=evidence, env_ref_names=names)
    history = load_attachment_history_sources_on_connection(conn, running, selected=selected, names=names).history
    values = tuple(value for value in history.applications if value.effect_key == effect_key)
    if len(values) != 1:
        raise AuditIntegrityError("attachment application has no exact already-applied history")
    return StoredAttachmentApplication(current, values[0])

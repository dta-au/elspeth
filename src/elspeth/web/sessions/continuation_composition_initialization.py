"""Current ready admission, atomic first Compose publication and bounded replay.

Historical ready checkpoints remain DATA. Every action/read uses the genuine
current claim, complete selected SQL root and existing session transaction.
Replay deliberately ends before any PRIMARY or batch action begins.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import Field, ValidationError
from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer._compose_loop_carriers import _AdvisorReviewState, _ComposeIterationState
from elspeth.web.composer.advisor_transcript import AdvisorTranscriptHistory
from elspeth.web.composer.anti_anchor import AntiAnchorTracker
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.continuation import ComposeFrame, ContinuationValue, InitializeFrame, PrivateChunkRef
from elspeth.web.composer.continuation_attachment_history import decode_attachment_history, empty_attachment_history
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors, encode_recorder_snapshot
from elspeth.web.composer.continuation_bootstrap import (
    _GATES,
    _RECOVERY,
    ContinuationBootstrapValues,
    _canonical_adapter_json,
    bootstrap_values,
    decode_continuation_bootstrap,
    encode_continuation_bootstrap,
)
from elspeth.web.composer.continuation_caches import RetainedCacheCreator, decode_continuation_caches
from elspeth.web.composer.continuation_clock import ContinuationTimingObservation, SegmentFit, admit_action
from elspeth.web.composer.continuation_codec import PrivateChunk, decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_composition_initialization_data import (
    CapturedInitialComposition,
    InitialCompositionOwners,
    capture_initial_composition,
)
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory, require_current_env_inventory_on_connection
from elspeth.web.composer.continuation_evidence import TurnEvidenceSnapshot, decode_turn_evidence
from elspeth.web.composer.continuation_iteration import (
    capture_continuation_iteration,
    capture_continuation_producer_inputs,
    decode_continuation_iteration,
    encode_continuation_iteration,
)
from elspeth.web.composer.continuation_message_shelf import RetainedTranscriptHistory, encode_message_shelf, encode_transcript_object_json
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_progress import initial_continuation_frontier
from elspeth.web.composer.continuation_prompt_recipes import InitialMessageRecipe
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.protocol import retained_operation_context_message
from elspeth.web.composer.provider_gateway import composer_loop_tool_definitions
from elspeth.web.composer.recovery_controls import recovery_context_message
from elspeth.web.composer.turn_effects import authored_state_hash
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    ContinuationCASConflict,
    ContinuationEvent,
    StoredContinuationSnapshot,
    _require_current_continuation_running,
    append_complete_snapshot,
    load_snapshot,
    private_chunk_key,
    put_chunk,
    read_chunk,
)
from elspeth.web.sessions.continuation_bootstrap_source import (
    _pretitle_checkpoint_at_cursor,
    _pretitle_recovery_origin,
    _require_no_provider_work,
    read_pretitle_bootstrap_on_connection,
    require_pretitle_publication_lineage_on_connection,
)
from elspeth.web.sessions.continuation_ingress import _receipt
from elspeth.web.sessions.continuation_selection import load_selected_private_chunks_on_connection, require_private_chunk_closure
from elspeth.web.sessions.continuation_title_application import (
    CurrentTitleApplicationNames,
    read_applied_title_ready_data_on_connection,
    read_title_application_on_connection,
)
from elspeth.web.sessions.continuation_title_provider import _timing, require_title_creator
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import sessions_table as sessions
from elspeth.web.sessions.routes._helpers import _initial_composition_state
from elspeth.web.sessions.schemas import SendMessageRequest

if TYPE_CHECKING:
    from elspeth.web.composer.service import ComposerServiceImpl


class CompositionInitializationEvent(ContinuationValue):
    schema_version: Literal["composer.composition-initialization.v1"] = "composer.composition-initialization.v1"
    owner: Literal["composer_composition_initialization"] = "composer_composition_initialization"
    source_checkpoint_id: str = Field(min_length=1)
    source_cursor: int = Field(ge=0)
    creator_sequence: int = Field(ge=1)
    creator_generation: int = Field(ge=1)
    creator_operation_id: str = Field(min_length=1)
    creator_operation_epoch: int = Field(ge=1)
    compose: ComposeFrame
    frontier: PrivateChunkRef


@dataclass(frozen=True, slots=True)
class CurrentCompositionInitializationSource:
    selected: SelectedContinuationState
    inventory: CurrentContinuationEnvInventory
    ready: StoredContinuationSnapshot
    bootstrap: PrivateChunk
    original: ContinuationBootstrapValues
    ready_generation: int
    event: CompositionInitializationEvent | None
    timing: ContinuationTimingObservation

    @property
    def initialized(self) -> bool:
        return self.event is not None

    def same_original_source(self, other: CurrentCompositionInitializationSource) -> bool:
        return (
            type(other) is CurrentCompositionInitializationSource
            and self.selected == other.selected
            and self.inventory is other.inventory
            and self.ready == other.ready
            and self.bootstrap == other.bootstrap
            and self.ready_generation == other.ready_generation
            and self.event == other.event
        )


def _stored(selected: SelectedContinuationState) -> StoredContinuationSnapshot:
    return StoredContinuationSnapshot(encode_continuation_snapshot(selected.stack), selected.stack.cursor, selected.checkpoint_id)


def _require_current(
    conn: Connection,
    composer: ComposerServiceImpl,
    running: ComposerOperationRunning,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
) -> StoredContinuationSnapshot:
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.sessions.service import SessionServiceImpl

    if (
        type(composer) is not ComposerServiceImpl
        or type(running) is not ComposerOperationRunning
        or type(selected) is not SelectedContinuationState
    ):
        raise AuditIntegrityError("Compose initialization requires its actual current service/running/selected owners")
    service = composer._require_sessions_service()
    if type(service) is not SessionServiceImpl or conn.engine is not service._engine or composer._session_engine is not conn.engine:
        raise AuditIntegrityError("Compose initialization changed its actual session store")
    selected.__post_init__()
    _require_current_continuation_running(conn, running)
    selected.stack.mutation_ceiling.materialize().validate_context(running.session_operation_context)
    require_current_env_inventory_on_connection(
        conn,
        running,
        inventory=inventory,
        expected_checkpoint_id=selected.checkpoint_id,
        expected_cursor=selected.stack.cursor,
    )
    actual = load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)
    if actual != _stored(selected) or load_selected_private_chunks_on_connection(conn, running, actual) != selected.chunks:
        raise ContinuationCASConflict("Compose initialization changed its full current selected root/chunk cohort")
    return actual


def _ready_frame(ready: StoredContinuationSnapshot) -> InitializeFrame:
    root = decode_continuation_snapshot(ready.payload_json)
    frame = root.frames[0]
    if (
        len(root.frames) != 1
        or type(frame) is not InitializeFrame
        or frame.ingress_phase != "ready"
        or frame.bootstrap is None
        or frame.ingress_receipt_json is None
        or frame.provider_effect_key is not None
        or frame.provider_input_sha256 is not None
        or root.semantic_frontier is not None
        or root.pending_owner_results
        or root.pending_settlement_ids
        or root.active_action_json is not None
        or root.active_action_source is not None
    ):
        raise AuditIntegrityError("Compose initialization has no closed original ready cut")
    return frame


def _require_historical_ready(
    conn: Connection,
    running: ComposerOperationRunning,
    ready: StoredContinuationSnapshot,
    bootstrap: PrivateChunk,
    names: frozenset[str],
    generation: int,
) -> None:
    frame = _ready_frame(ready)
    snapshot = decode_continuation_bootstrap(bootstrap)
    root = decode_continuation_snapshot(ready.payload_json)
    if frame.bootstrap != bootstrap.reference or snapshot.logical != root.logical:
        raise AuditIntegrityError("Compose initialization changed its historical original bootstrap")
    if frame.title_response is None:
        if frame.title_attempt_id is not None or root.effect_frontier or snapshot.auto_title_eligible:
            raise AuditIntegrityError("Compose initialization cannot invent an optional TITLE outcome")
        _require_no_provider_work(conn, session_id=root.logical.session_id, operation_id=root.logical.operation_id)
        require_pretitle_publication_lineage_on_connection(conn, running, selected=ready, chunk=bootstrap, generation=generation)
    else:
        if not snapshot.auto_title_eligible:
            raise AuditIntegrityError("Compose initialization paid source lost its original TITLE eligibility")
        read_applied_title_ready_data_on_connection(conn, running, selected=ready, names=names, generation=generation)


def _require_original_values(
    conn: Connection,
    composer: ComposerServiceImpl,
    running: ComposerOperationRunning,
    selected: SelectedContinuationState,
    ready: StoredContinuationSnapshot,
    bootstrap: PrivateChunk,
) -> ContinuationBootstrapValues:
    from elspeth.web.composer.continuation_prompt_producers import require_original_bootstrap_transcript_on_connection
    from elspeth.web.sessions.service import SessionServiceImpl

    snapshot = decode_continuation_bootstrap(bootstrap)
    original = bootstrap_values(snapshot)
    if (
        type(original.request) is not SendMessageRequest
        or original.retained_context is None
        or original.retained_context.availability != "available"
    ):
        raise AuditIntegrityError("Compose initialization has no actual SendMessage/available retained-context owner")
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
    principal = conn.execute(select(sessions.c.user_id, sessions.c.archived_at).where(sessions.c.id == sid)).one()
    try:
        request = SendMessageRequest.model_validate_json(parent.request_json, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("Compose initialization parent request violates its admitted owned shape") from error
    frame = _ready_frame(ready)
    receipt_text = frame.ingress_receipt_json
    if receipt_text is None:
        raise AuditIntegrityError("Compose initialization lost its actual immutable ingress receipt")
    receipt = _receipt(receipt_text)
    service = composer._require_sessions_service()
    if type(service) is not SessionServiceImpl:
        raise AuditIntegrityError("Compose initialization lost its actual graph-recovery SQL owner")
    recovery = service._load_graph_recovery_context_on_connection(
        conn,
        UUID(sid),
        turn_start_state_id=request.state_id,
        session_operation_context=running.session_operation_context,
    )
    initial = _initial_composition_state() if recovery.record is None else state_from_record(recovery.record)
    if (
        snapshot.logical != selected.stack.logical
        or snapshot.logical != decode_continuation_snapshot(ready.payload_json).logical
        or snapshot.logical.kind != "compose_message"
        or snapshot.actor_user_id != parent.actor_user_id
        or principal.user_id != parent.actor_user_id
        or principal.archived_at is not None
        or frame.request_json != parent.request_json
        or snapshot.request_json != canonical_json(request.model_dump(mode="json"))
        or snapshot.original_session_title != receipt.original_session_title
        or snapshot.original_last_sequence != receipt.original_last_sequence
        or stable_hash(snapshot.records_json) != receipt.transcript_sha256
        or receipt.session_id != sid
        or receipt.operation_id != operation
        or receipt.user_message_id != snapshot.logical.original_user_message_id
        or receipt.requested_state_id != snapshot.logical.base_state_id
        or receipt.composition_state_id != snapshot.logical.base_state_id
        or recovery.context.malformed_head
        or snapshot.initial_state_json != canonical_json(initial.to_dict())
        or snapshot.recovery_context_json != _canonical_adapter_json(_RECOVERY, recovery.context)
        or snapshot.completion_gates_json
        != (
            _canonical_adapter_json(_GATES, recovery.effective_completion_gates)
            if recovery.effective_completion_gates is not None
            else None
        )
    ):
        raise AuditIntegrityError("Compose initialization changed its original actor/request/base/transcript/gates source")
    require_original_bootstrap_transcript_on_connection(conn, snapshot)
    return original


def _chunk(selected: SelectedContinuationState, reference: PrivateChunkRef) -> PrivateChunk:
    matches = tuple(chunk for chunk in selected.chunks if chunk.reference == reference)
    if len(matches) != 1:
        raise AuditIntegrityError("first Compose lost its complete selected private value")
    return matches[0]


def require_first_compose_frame(frame: ComposeFrame) -> None:
    if (
        type(frame) is not ComposeFrame
        or frame.phase != "action_ready"
        or frame.provider_effect_key is not None
        or frame.provider_input_sha256 is not None
        or frame.pending_response is not None
        or frame.initial_state != frame.state
        or frame.tools is None
        or frame.composition_slice != 0
        or frame.discovery_slice != 0
        or frame.composition_total != 0
        or frame.discovery_total != 0
        or frame.advisor_calls != 0
        or frame.repair_turns != 0
        or frame.advisor_checkpoint_passes != 0
        or frame.mutation_success_seen
        or frame.persisted_assistant_message_id is not None
        or frame.persisted_assistant_content is not None
        or frame.persisted_tool_call_turn
        or frame.advisor_repair_context_introduced
        or frame.pending_advisor_elision_indices
    ):
        raise AuditIntegrityError("Compose initializer replay cannot reset an advanced first composition")


def _require_first_values(source: CurrentCompositionInitializationSource) -> None:
    selected, original, event = source.selected, source.original, source.event
    if event is None:
        raise AuditIntegrityError("first Compose values lack their actual initialization event")
    retained = original.retained_context
    if retained is None:
        raise AuditIntegrityError("first Compose lost its original actual retained context")
    frame = selected.stack.frames[0]
    if type(frame) is not ComposeFrame:
        raise AuditIntegrityError("first Compose changed its nominal frame")
    require_first_compose_frame(frame)
    names = source.inventory.names
    if (
        frame.initial_version != original.initial_state.version
        or frame.current_state_id != selected.stack.logical.base_state_id
        or _chunk(selected, frame.state).canonical != original.snapshot.initial_state_json
        or frame.bootstrap != source.bootstrap.reference
    ):
        raise AuditIntegrityError("first Compose changed its exact original state/reference relation")
    evidence = decode_turn_evidence(_chunk(selected, frame.evidence), env_ref_names=names, source_chunks=selected.chunks)
    _require_first_values_from_evidence(source, evidence.snapshot)


def _require_first_values_from_evidence(source: CurrentCompositionInitializationSource, evidence: TurnEvidenceSnapshot) -> None:
    selected, original, event = source.selected, source.original, source.event
    if event is None:
        raise AuditIntegrityError("first Compose values lack their actual initialization event")
    retained = original.retained_context
    if retained is None:
        raise AuditIntegrityError("first Compose lost its original actual retained context")
    frame = selected.stack.frames[0]
    if type(frame) is not ComposeFrame:
        raise AuditIntegrityError("first Compose changed its nominal frame")
    require_first_compose_frame(frame)
    names = source.inventory.names
    if (
        frame.initial_version != original.initial_state.version
        or frame.current_state_id != selected.stack.logical.base_state_id
        or _chunk(selected, frame.state).canonical != original.snapshot.initial_state_json
        or frame.bootstrap != source.bootstrap.reference
    ):
        raise AuditIntegrityError("first Compose changed its exact original state/reference relation")
    empty_audit = encode_recorder_snapshot(BufferingRecorder().snapshot_for_continuation(drained=RecorderDrainCursors()))
    if _chunk(selected, frame.audit_buffer) != empty_audit or evidence.recorder != empty_audit.reference:
        raise AuditIntegrityError("first Compose has a prior recorder or changed drain history")
    effects = evidence.effects
    if (
        evidence.logical != selected.stack.logical
        or evidence.actor_user_id != original.snapshot.actor_user_id
        or evidence.session_id != selected.stack.logical.session_id
        or evidence.physical_operation_id != event.creator_operation_id
        or evidence.physical_operation_epoch != event.creator_operation_epoch
        or evidence.request_id != selected.stack.logical.original_user_message_id
        or evidence.request_content_hash != stable_hash(original.message)
        or not evidence.initial_is_latest
        or not evidence.requires_blob_reader
        or evidence.registration_revision != 0
        or effects.initial_state_json != original.snapshot.initial_state_json
        or effects.latest_verified_state_json != original.snapshot.initial_state_json
        or effects.initial_state_id != selected.stack.logical.base_state_id
        or effects.latest_verified_state_id != selected.stack.logical.base_state_id
        or effects.latest_committed_state_id != selected.stack.logical.base_state_id
        or effects.verification != "verified"
        or effects.initial_authored_state_hash != authored_state_hash(original.initial_state)
        or effects.latest_authored_state_hash != effects.initial_authored_state_hash
        or effects.net_changed_fields
        or effects.batches
        or effects.committed_revision_ids
        or evidence.page_shelf
        or evidence.registered_pages
        or evidence.quoted_inspections
        or evidence.historical_pages
        or evidence.conflicting_page_ids
        or evidence.review_shelf
        or evidence.decision_shelf
        or evidence.abandonment_review is not None
        or evidence.abandonment_transcript_binding is not None
        or evidence.abandonment_limitations
        or evidence.effect_locators
        or evidence.historical_application_count != 0
        or evidence.application_entry is not None
        or evidence.joint_graph_json is not None
        or evidence.retained_history != retained.provider_message()
        or any(
            item.generation != 0 or item.current_call is not None or item.current_page_indexes or item.pending is not None
            for item in evidence.deliveries
        )
    ):
        raise AuditIntegrityError("first Compose changed its complete original evidence/reader/alias source")
    shelf = evidence.transcript
    history = AdvisorTranscriptHistory.from_records(
        session_id=selected.stack.logical.session_id,
        actor_user_id=original.snapshot.actor_user_id,
        current_request_id=selected.stack.logical.original_user_message_id,
        request=original.message,
        records=original.records,
    )
    expected_history = RetainedTranscriptHistory(
        session_id=history.session_id,
        actor_user_id=history.actor_user_id,
        request_id=history.request_id,
        request_hash=history.request_hash,
        events_json=history.events_json,
    )
    if (
        shelf.history != expected_history
        or shelf.request != original.message
        or shelf.observed
        or shelf.current_indexes != tuple(range(len(shelf.messages_json)))
        or _chunk(selected, frame.transcript) != encode_message_shelf(shelf)
        or _chunk(selected, frame.messages).canonical
        != canonical_json({"messages_json": shelf.messages_json, "current_indexes": shelf.current_indexes})
    ):
        raise AuditIntegrityError("first Compose changed its complete original shared transcript/message projection")
    tail = []
    if original.recovery_context is not None:
        tail.append(encode_transcript_object_json({"role": "system", "content": recovery_context_message(original.recovery_context)}))
    tail.append(encode_transcript_object_json({"role": "system", "content": retained_operation_context_message(original.retained_context)}))
    prefix_count = len(shelf.messages_json) - len(tail)
    if (
        prefix_count <= 0
        or shelf.messages_json[prefix_count:] != tuple(tail)
        or shelf.producer_recipes != (*(InitialMessageRecipe(index=index) for index in range(prefix_count)), *(None for _ in tail))
    ):
        raise AuditIntegrityError("first Compose changed initial recipe positions or original context message bytes")
    if shelf.excluded_indexes != tuple(range(prefix_count)):
        raise AuditIntegrityError("first Compose changed its exact original excluded initial-message prefix")
    caches = decode_continuation_caches(_chunk(selected, frame.caches), env_ref_names=names)
    if (
        caches.session_id != selected.stack.logical.session_id
        or caches.capture_count != 1
        or caches.capture_creators
        != (RetainedCacheCreator(operation_id=event.creator_operation_id, operation_epoch=event.creator_operation_epoch),)
        or caches.discovery
        or caches.preflight
        or caches.last_preflight_json is not None
        or caches.last_preflight_identity is not None
        or caches.previous_last_preflight_json
        or caches.previous_last_preflight_identities
    ):
        raise AuditIntegrityError("first Compose changed its actual initial cache creator/history")
    attachments = decode_attachment_history(
        _chunk(selected, frame.attachment_admissions),
        session_id=selected.stack.logical.session_id,
        operation_id=selected.stack.logical.operation_id,
        env_ref_names=names,
    )
    if attachments != empty_attachment_history(
        session_id=selected.stack.logical.session_id, operation_id=selected.stack.logical.operation_id
    ):
        raise AuditIntegrityError("first Compose cannot erase or import attachment application history")
    iteration = decode_continuation_iteration(_chunk(selected, frame.iteration), env_ref_names=names)
    expected_iteration = capture_continuation_iteration(
        producer_inputs=capture_continuation_producer_inputs((), env_ref_names=names),
        last_validation=None,
        advisor_review=_AdvisorReviewState(),
        failed_turn=None,
        anti_anchor=AntiAnchorTracker().snapshot_for_continuation(),
        disclosure=iteration.disclosure,
        env_ref_names=names,
    )
    if (
        iteration.disclosure.schemas != original.snapshot.initial_schema_disclosure
        or iteration.disclosure.session_id != selected.stack.logical.session_id
        or encode_continuation_iteration(expected_iteration, env_ref_names=names) != _chunk(selected, frame.iteration)
    ):
        raise AuditIntegrityError("first Compose changed its complete initial iteration/disclosure history")
    from elspeth.web.composer.continuation_codec import encode_frontier_chunk

    expected_frontier = encode_frontier_chunk(
        initial_continuation_frontier(
            request=original.message,
            original_message_id=selected.stack.logical.original_user_message_id,
            graph_hash=authored_state_hash(original.initial_state),
        )
    )
    if _chunk(selected, event.frontier) != expected_frontier:
        raise AuditIntegrityError("first Compose changed its original request semantic frontier")


def read_composition_initialization_source_on_connection(
    conn: Connection,
    composer: ComposerServiceImpl,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
) -> CurrentCompositionInitializationSource:
    source = _read_composition_initialization_source_on_connection(conn, composer, running, selected=selected, inventory=inventory)
    if source.initialized:
        _require_first_values(source)
    return source


def _read_composition_initialization_source_on_connection(
    conn: Connection,
    composer: ComposerServiceImpl,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
) -> CurrentCompositionInitializationSource:
    current = _require_current(conn, composer, running, selected, inventory)
    root, frame = selected.stack, selected.stack.frames[0]
    if (
        len(root.frames) != 1
        or root.pending_owner_results
        or root.pending_settlement_ids
        or root.active_action_json is not None
        or root.active_action_source is not None
    ):
        raise AuditIntegrityError("Compose initialization has no closed sole-frame source")
    event = None
    _, current_generation = require_title_creator(running)
    if type(frame) is InitializeFrame:
        ready = current
        ready_frame = _ready_frame(ready)
        if ready_frame.title_response is None:
            from elspeth.web.sessions.service import SessionServiceImpl

            sessions_owner = composer._require_sessions_service()
            if type(sessions_owner) is not SessionServiceImpl:
                raise AuditIntegrityError("Compose initialization lost its actual pretitle sessions SQL owner")
            read_pretitle_bootstrap_on_connection(conn, sessions_owner, running, selected=ready)
        else:
            actual = read_title_application_on_connection(
                conn, running, inventory=CurrentTitleApplicationNames(running, ready, inventory.names)
            )
            if not actual.applied:
                raise AuditIntegrityError("Compose initialization requires actually applied paid TITLE readiness")
        generation = current_generation
    elif type(frame) is ComposeFrame:
        require_first_compose_frame(frame)
        origin, generation = _pretitle_recovery_origin(conn, running, current, generation=current_generation)
        row = conn.execute(
            select(events).where(
                events.c.session_id == root.logical.session_id,
                events.c.operation_id == root.logical.operation_id,
                events.c.sequence == origin.cursor,
            )
        ).one_or_none()
        if row is None or row.tag != "checkpoint":
            raise AuditIntegrityError("first Compose lost its actual initialization event")
        try:
            text = read_chunk(
                conn, session_id=root.logical.session_id, operation_id=root.logical.operation_id, key=row.payload_chunk_key
            ).decode("utf-8", errors="strict")
            event = CompositionInitializationEvent.model_validate_json(text, strict=True)
        except (UnicodeDecodeError, ValidationError) as error:
            raise AuditIntegrityError("first Compose initialization event violates its complete owned encoding") from error
        if (
            text != canonical_json(event.model_dump(mode="json"))
            or event.source_cursor + 1 != origin.cursor
            or event.creator_sequence != running.claim.segment_sequence
            or event.creator_generation != generation
        ):
            raise AuditIntegrityError("first Compose changed its actual initialization creator/event")
        ready = _pretitle_checkpoint_at_cursor(
            conn, session_id=root.logical.session_id, operation_id=root.logical.operation_id, cursor=event.source_cursor
        )
        if ready.checkpoint_id != event.source_checkpoint_id:
            raise AuditIntegrityError("first Compose lost its actual ready predecessor")
        creator = conn.execute(
            select(attempts).where(
                attempts.c.session_id == root.logical.session_id,
                attempts.c.operation_id == root.logical.operation_id,
                attempts.c.sequence == event.creator_sequence,
                attempts.c.generation == event.creator_generation,
            )
        ).one_or_none()
        if creator is None or (creator.session_operation_id, creator.session_operation_epoch) != (
            event.creator_operation_id,
            event.creator_operation_epoch,
        ):
            raise AuditIntegrityError("first Compose lost its actual original initialization writer")
        before = decode_continuation_snapshot(ready.payload_json)
        expected = before.model_copy(update={"cursor": origin.cursor, "frames": (event.compose,), "semantic_frontier": event.frontier})
        if encode_continuation_snapshot(expected) != origin.payload_json:
            raise AuditIntegrityError("first Compose changed its complete atomic forward initialization")
    else:
        raise AuditIntegrityError("Compose initialization refuses another phase owner")
    ready_frame = _ready_frame(ready)
    if ready_frame.bootstrap is None:
        raise AuditIntegrityError("Compose initialization lost its original bootstrap")
    bootstrap = _chunk(selected, ready_frame.bootstrap)
    encode_continuation_bootstrap(decode_continuation_bootstrap(bootstrap), env_ref_names=inventory.names)
    _require_historical_ready(conn, running, ready, bootstrap, inventory.names, generation)
    original = _require_original_values(conn, composer, running, selected, ready, bootstrap)
    source = CurrentCompositionInitializationSource(
        selected, inventory, ready, bootstrap, original, generation, event, _timing(conn, running, current)
    )
    return source


def publish_composition_initialization_on_connection(
    conn: Connection,
    composer: ComposerServiceImpl,
    running: ComposerOperationRunning,
    *,
    owners: InitialCompositionOwners,
    captured: CapturedInitialComposition,
) -> StoredContinuationSnapshot:
    if (
        type(owners) is not InitialCompositionOwners
        or type(captured) is not CapturedInitialComposition
        or type(owners.live) is not _ComposeIterationState
    ):
        raise AuditIntegrityError("first Compose publication requires its actual complete capture owners")
    source = owners.source
    actual = read_composition_initialization_source_on_connection(
        conn, composer, running, selected=source.selected, inventory=source.inventory
    )
    if actual.initialized or not actual.same_original_source(source) or owners.materialization.service is not composer:
        raise ContinuationCASConflict("first Compose publication changed its actual ready source")
    if (composer._composer_skill_name, composer._composer_skill_hash, composer._composer_skill_text) != owners.skill_identity:
        raise AuditIntegrityError("first Compose deployment skill changed across preparation awaits")
    if canonical_json(owners.live.tools) != canonical_json(composer_loop_tool_definitions(composer._planner_dialect)):
        raise AuditIntegrityError("first Compose actual tool dialect changed across preparation awaits")
    # This capture is pure: capture_message only verifies original object/wire,
    # never _require_current or a nested engine transaction under this writer.
    if capture_initial_composition(owners) != captured:
        raise AuditIntegrityError("first Compose changed its complete actual captured graph")
    root = source.selected.stack
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    if (
        admit_action(
            policy.actions.session_sql,
            policy.segment,
            hard_remaining=actual.timing.hard_remaining_seconds,
            cooperative_remaining=actual.timing.cooperative_remaining_seconds,
            fresh=False,
        )
        is not SegmentFit.ADMIT
    ):
        raise AuditIntegrityError("first Compose initialization must yield before local publication")
    sequence, generation = require_title_creator(running)
    compose = captured.stack.frames[0]
    if type(compose) is not ComposeFrame or captured.stack.semantic_frontier is None:
        raise AuditIntegrityError("first Compose capture lost its actual frame/frontier")
    context = running.session_operation_context
    event = CompositionInitializationEvent(
        source_checkpoint_id=source.selected.checkpoint_id,
        source_cursor=root.cursor,
        creator_sequence=sequence,
        creator_generation=generation,
        creator_operation_id=context.fence.operation_id,
        creator_operation_epoch=context.fence.operation_epoch,
        compose=compose,
        frontier=captured.stack.semantic_frontier,
    )
    require_private_chunk_closure(captured.stack, captured.chunks)
    for chunk in captured.chunks:
        put_chunk(
            conn,
            session_id=root.logical.session_id,
            operation_id=root.logical.operation_id,
            key=private_chunk_key(chunk),
            payload=chunk.canonical.encode("utf-8"),
            codec=chunk.reference.codec,
        )
    committed = append_complete_snapshot(
        conn,
        session_id=root.logical.session_id,
        operation_id=root.logical.operation_id,
        expected_checkpoint_id=source.selected.checkpoint_id,
        expected_cursor=root.cursor,
        snapshot_json=encode_continuation_snapshot(captured.stack),
        event_batch=(ContinuationEvent("checkpoint", canonical_json(event.model_dump(mode="json"))),),
        handoff=False,
    )
    private = load_selected_private_chunks_on_connection(conn, running, committed)
    selected = SelectedContinuationState(committed.checkpoint_id, decode_continuation_snapshot(committed.payload_json), private)
    # Rebind inventory from the actual new current root on this connection;
    # names are unchanged DATA from the producer, no metadata permission granted.
    from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory, read_env_selection_on_connection

    selection = read_env_selection_on_connection(
        conn, running, expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.stack.cursor
    )
    inventory = CurrentContinuationEnvInventory(running, selection, source.inventory.names)
    read_composition_initialization_source_on_connection(conn, composer, running, selected=selected, inventory=inventory)
    return committed

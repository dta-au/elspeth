"""Atomic pre-title bootstrap publication and current SQL source read.

Published history is DATA. This owner grants no title/provider/Compose action.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import Field, ValidationError
from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import ContinuationValue, InitializeFrame, PrivateChunkRef
from elspeth.web.composer.continuation_bootstrap import (
    _GATES,
    _RECOVERY,
    ContinuationBootstrapSnapshot,
    _canonical_adapter_json,
    _canonical_records_json,
    bootstrap_values,
    capture_continuation_bootstrap,
    decode_continuation_bootstrap,
    encode_continuation_bootstrap,
)
from elspeth.web.composer.continuation_codec import PrivateChunk, decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory, require_current_env_inventory_on_connection
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
from elspeth.web.sessions.continuation_ingress import ContinuationIngressCheckpoint, ContinuationIngressPrepared, _receipt
from elspeth.web.sessions.continuation_selection import load_selected_private_chunks_on_connection
from elspeth.web.sessions.continuation_title_source import ContinuationTitleSource, read_title_source_on_connection
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import sessions_table as sessions
from elspeth.web.sessions.routes._helpers import _initial_composition_state
from elspeth.web.sessions.schemas import SendMessageRequest

if TYPE_CHECKING:
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.sessions.service import SessionServiceImpl


class PretitleBootstrapPublication(ContinuationValue):
    schema_version: Literal["composer.pretitle-bootstrap-publication.v1"] = "composer.pretitle-bootstrap-publication.v1"
    source_checkpoint_id: str
    source_cursor: int = Field(ge=0)
    bootstrap: PrivateChunkRef
    actor_user_id: str
    creator_session_operation_id: str
    creator_session_operation_epoch: int = Field(ge=1)


@dataclass(frozen=True, slots=True)
class PreparedPretitleBootstrap:
    source: ContinuationTitleSource
    inventory: CurrentContinuationEnvInventory
    bootstrap: PrivateChunk
    skill_identity: tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class StoredPretitleBootstrap:
    selected: StoredContinuationSnapshot
    bootstrap: PrivateChunk


def _event(value: PretitleBootstrapPublication) -> str:
    return canonical_json({"owner": "composer_pretitle_bootstrap", "publication": value.model_dump(mode="json")})


def _require_sessions(conn: Connection, service: SessionServiceImpl) -> None:
    from elspeth.web.sessions.service import SessionServiceImpl

    if type(service) is not SessionServiceImpl or conn.engine is not service._engine:
        raise AuditIntegrityError("pre-title bootstrap requires the actual sessions SQL owner")


def _require_no_provider_work(conn: Connection, *, session_id: str, operation_id: str) -> None:
    prior_fences = select(attempts.c.session_operation_id).where(
        attempts.c.session_id == session_id, attempts.c.operation_id == operation_id
    )
    if (
        conn.execute(select(effects.c.effect_key).where(effects.c.session_id == session_id, effects.c.operation_id == operation_id)).first()
        is not None
        or conn.execute(
            select(physical.c.attempt_id).where(physical.c.session_id == session_id, physical.c.operation_id.in_(prior_fences))
        ).first()
        is not None
    ):
        raise AuditIntegrityError("pre-title bootstrap refuses prior provider/effect work")


def _pretitle_checkpoint_at_cursor(conn: Connection, *, session_id: str, operation_id: str, cursor: int) -> StoredContinuationSnapshot:
    row = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == session_id,
            checkpoints.c.operation_id == operation_id,
            checkpoints.c.cursor == cursor,
        )
    ).one_or_none()
    if row is None or row.kind != "transition" or row.frame_stack_version != 1:
        raise AuditIntegrityError("pre-title recovery lost its exact transition checkpoint")
    try:
        text = read_chunk(conn, session_id=session_id, operation_id=operation_id, key=row.snapshot_chunk_key).decode(
            "utf-8", errors="strict"
        )
    except UnicodeDecodeError as error:
        raise AuditIntegrityError("pre-title recovery checkpoint is not strict UTF-8") from error
    root = decode_continuation_snapshot(text)
    if root.cursor != cursor or encode_continuation_snapshot(root) != text:
        raise AuditIntegrityError("pre-title recovery checkpoint changed its complete canonical root")
    return StoredContinuationSnapshot(text, cursor, row.checkpoint_id)


def _pretitle_recovery_origin(
    conn: Connection,
    running: ComposerOperationRunning,
    selected: StoredContinuationSnapshot,
    *,
    generation: int | None,
) -> tuple[StoredContinuationSnapshot, int]:
    """Read only a contiguous actual same-sequence recovery suffix.

    This is historical lineage, never current authorization. Callers retain
    their current running/CAS checks. Each step must be the real recovery
    producer's exact event, interrupted attempt pair and unchanged root bytes.
    """
    if type(generation) is not int or generation < 1:
        raise AuditIntegrityError("pre-title recovery lost its actual generation")
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    current = selected
    while True:
        actual = _pretitle_checkpoint_at_cursor(conn, session_id=sid, operation_id=operation, cursor=current.cursor)
        if actual != current:
            raise AuditIntegrityError("pre-title recovery changed its exact selected checkpoint")
        event = conn.execute(
            select(events).where(events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == current.cursor)
        ).one_or_none()
        if event is None or event.tag != "recovered":
            return current, generation
        if generation <= 1 or current.cursor <= 0:
            raise AuditIntegrityError("pre-title recovery has no preceding actual generation")
        try:
            event_text = read_chunk(conn, session_id=sid, operation_id=operation, key=event.payload_chunk_key).decode(
                "utf-8", errors="strict"
            )
        except UnicodeDecodeError as error:
            raise AuditIntegrityError("pre-title recovery event is not strict UTF-8") from error
        expected_event = canonical_json(
            {"version": 2, "same_sequence": running.claim.segment_sequence, "previous_generation": generation - 1, "generation": generation}
        )
        if event_text != expected_event:
            raise AuditIntegrityError("pre-title recovery changed its exact claim-rebinding event")
        previous_attempt = conn.execute(
            select(attempts).where(
                attempts.c.session_id == sid,
                attempts.c.operation_id == operation,
                attempts.c.sequence == running.claim.segment_sequence,
                attempts.c.generation == generation - 1,
            )
        ).one_or_none()
        following_attempt = conn.execute(
            select(attempts).where(
                attempts.c.session_id == sid,
                attempts.c.operation_id == operation,
                attempts.c.sequence == running.claim.segment_sequence,
                attempts.c.generation == generation,
            )
        ).one_or_none()
        if (
            previous_attempt is None
            or following_attempt is None
            or previous_attempt.revoked_at is None
            or previous_attempt.outcome != "interrupted"
            or following_attempt.claim_attempt <= previous_attempt.claim_attempt
            or following_attempt.session_operation_epoch <= previous_attempt.session_operation_epoch
            or following_attempt.session_operation_id == previous_attempt.session_operation_id
            or following_attempt.session_operation_lease_token == previous_attempt.session_operation_lease_token
        ):
            raise AuditIntegrityError("pre-title recovery lost its actual interrupted/reclaimed attempt pair")
        predecessor = _pretitle_checkpoint_at_cursor(conn, session_id=sid, operation_id=operation, cursor=current.cursor - 1)
        before = decode_continuation_snapshot(predecessor.payload_json)
        if current.payload_json != encode_continuation_snapshot(before.model_copy(update={"cursor": current.cursor})):
            raise AuditIntegrityError("pre-title recovery changed retained source beyond its cursor")
        current = predecessor
        generation -= 1


def require_pretitle_inserted_origin(
    conn: Connection,
    running: ComposerOperationRunning,
    selected: StoredContinuationSnapshot,
    *,
    generation: int | None,
) -> None:
    origin, _generation = _pretitle_recovery_origin(conn, running, selected, generation=generation)
    frame = decode_continuation_snapshot(origin.payload_json).frames[0]
    if (
        type(frame) is not InitializeFrame
        or frame.ingress_phase != "inserted"
        or frame.ingress_receipt_json is None
        or origin.cursor != _receipt(frame.ingress_receipt_json).source_cursor + 1
    ):
        raise AuditIntegrityError("pre-title bootstrap cannot repeat preparation at a later inserted cut")


def read_pretitle_bootstrap_on_connection(
    conn: Connection,
    service: SessionServiceImpl,
    running: ComposerOperationRunning,
    *,
    selected: StoredContinuationSnapshot,
) -> StoredPretitleBootstrap:
    _require_sessions(conn, service)
    if type(running) is not ComposerOperationRunning or type(selected) is not StoredContinuationSnapshot:
        raise AuditIntegrityError("pre-title bootstrap read requires actual selected/current owners")
    _require_current_continuation_running(conn, running)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    if load_snapshot(conn, session_id=sid, operation_id=operation) != selected:
        raise ContinuationCASConflict("pre-title bootstrap changed its complete selected CAS")
    root = decode_continuation_snapshot(selected.payload_json)
    frame = root.frames[0]
    if (
        len(root.frames) != 1
        or type(frame) is not InitializeFrame
        or frame.ingress_phase not in ("title_pending", "ready")
        or frame.bootstrap is None
        or frame.bootstrap.codec != "composer.bootstrap.v1"
        or frame.title_attempt_id is not None
        or frame.title_response is not None
        or frame.provider_effect_key is not None
        or frame.provider_input_sha256 is not None
        or root.effect_frontier
        or root.pending_owner_results
        or root.pending_settlement_ids
        or root.semantic_frontier is not None
        or root.active_action_json is not None
        or root.active_action_source is not None
    ):
        raise AuditIntegrityError("pre-title bootstrap read has no closed published SQL-only cut")
    chunks = load_selected_private_chunks_on_connection(conn, running, selected)
    if len(chunks) != 1 or chunks[0].reference != frame.bootstrap:
        raise AuditIntegrityError("pre-title bootstrap lost its exact complete selected private source")
    chunk = chunks[0]
    require_current_pretitle_values_on_connection(conn, service, running, selected=selected, chunk=chunk)
    _require_no_provider_work(conn, session_id=sid, operation_id=operation)
    generation = running.claim.ownership_generation
    if not isinstance(generation, int) or type(generation) is not int or generation < 1:
        raise AuditIntegrityError("pre-title recovery lost its actual generation")
    require_pretitle_publication_lineage_on_connection(conn, running, selected=selected, chunk=chunk, generation=generation)
    return StoredPretitleBootstrap(selected, chunk)


def _current_pretitle_value_inputs(
    conn: Connection,
    service: SessionServiceImpl,
    running: ComposerOperationRunning,
    *,
    selected: StoredContinuationSnapshot,
    chunk: PrivateChunk,
) -> tuple[ContinuationBootstrapSnapshot, SendMessageRequest]:
    _require_sessions(conn, service)
    _require_current_continuation_running(conn, running)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    if load_snapshot(conn, session_id=sid, operation_id=operation) != selected:
        raise ContinuationCASConflict("pre-title values changed their complete actual current CAS")
    root = decode_continuation_snapshot(selected.payload_json)
    frame = root.frames[0]
    if (
        len(root.frames) != 1
        or type(frame) is not InitializeFrame
        or frame.ingress_phase not in ("title_pending", "ready")
        or frame.bootstrap != chunk.reference
    ):
        raise AuditIntegrityError("pre-title current values lost their exact Initialize source")
    snapshot = decode_continuation_bootstrap(chunk)
    values = bootstrap_values(snapshot)
    if values.retained_context is None or values.retained_context.availability != "available":
        raise AuditIntegrityError("pre-title bootstrap lost its actual bounded history read")
    request = values.request
    if not isinstance(request, SendMessageRequest) or type(request) is not SendMessageRequest:
        raise AuditIntegrityError("pre-title bootstrap has no implemented Recompose source owner")
    return snapshot, request


def _require_current_pretitle_value_equality(
    conn: Connection,
    service: SessionServiceImpl,
    running: ComposerOperationRunning,
    *,
    selected: StoredContinuationSnapshot,
    snapshot: ContinuationBootstrapSnapshot,
    request: SendMessageRequest,
    ingress: ContinuationIngressPrepared | ContinuationIngressCheckpoint,
) -> None:
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    root = decode_continuation_snapshot(selected.payload_json)
    frame = root.frames[0]
    if len(root.frames) != 1 or type(frame) is not InitializeFrame:
        raise AuditIntegrityError("pre-title current values lost their exact Initialize source")
    if type(ingress) is not ContinuationIngressCheckpoint:
        raise AuditIntegrityError("pre-title current values lost their committed actual ingress")
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
    try:
        original_request = SendMessageRequest.model_validate_json(parent.request_json, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("initializer parent request violates its admitted owned shape") from error
    current_title = conn.execute(select(sessions.c.title).where(sessions.c.id == sid)).scalar_one()
    if frame.ingress_receipt_json is None:
        raise AuditIntegrityError("pre-title bootstrap lost its original ingress receipt")
    receipt = _receipt(frame.ingress_receipt_json)
    recovery = service._load_graph_recovery_context_on_connection(
        conn, running.claim.session_id, turn_start_state_id=request.state_id, session_operation_context=running.session_operation_context
    )
    if recovery.context.malformed_head:
        raise AuditIntegrityError("pre-title bootstrap current base requires the graph recovery owner")
    initial = _initial_composition_state() if recovery.record is None else state_from_record(recovery.record)
    if (
        snapshot.logical != root.logical
        or snapshot.actor_user_id != parent.actor_user_id
        or snapshot.request_json != canonical_json(original_request.model_dump(mode="json"))
        or snapshot.records_json != _canonical_records_json(ingress.transcript)
        or snapshot.original_session_title != receipt.original_session_title
        or current_title != receipt.original_session_title
        or snapshot.initial_state_json != canonical_json(initial.to_dict())
        or snapshot.recovery_context_json != _canonical_adapter_json(_RECOVERY, recovery.context)
        or snapshot.completion_gates_json
        != (
            _canonical_adapter_json(_GATES, recovery.effective_completion_gates)
            if recovery.effective_completion_gates is not None
            else None
        )
        or frame.ingress_phase != ("title_pending" if snapshot.auto_title_eligible else "ready")
    ):
        raise AuditIntegrityError("pre-title bootstrap changed its original actor/request/base/transcript/gates/title source")


def require_current_pretitle_values_on_connection(
    conn: Connection,
    service: SessionServiceImpl,
    running: ComposerOperationRunning,
    *,
    selected: StoredContinuationSnapshot,
    chunk: PrivateChunk,
) -> None:
    """Original provider-free caller; current and ingress-source roots coincide."""
    from elspeth.web.sessions.continuation_ingress import _read_ingress_source_on_connection

    snapshot, request = _current_pretitle_value_inputs(conn, service, running, selected=selected, chunk=chunk)
    ingress = _read_ingress_source_on_connection(
        conn, running, actual=selected, content=request.content, requested_state_id=request.state_id, composition_state_id=request.state_id
    )
    _require_current_pretitle_value_equality(conn, service, running, selected=selected, snapshot=snapshot, request=request, ingress=ingress)


def require_current_prepared_title_values_on_connection(
    conn: Connection,
    service: SessionServiceImpl,
    running: ComposerOperationRunning,
    *,
    selected: StoredContinuationSnapshot,
    source: StoredContinuationSnapshot,
    chunk: PrivateChunk,
) -> None:
    """Current TITLE values joined to its actual full published source anchor."""
    from elspeth.web.sessions.continuation_ingress import read_prepared_title_ingress_on_connection

    snapshot, request = _current_pretitle_value_inputs(conn, service, running, selected=selected, chunk=chunk)
    ingress = read_prepared_title_ingress_on_connection(
        conn,
        running,
        actual=selected,
        source=source,
        content=request.content,
        requested_state_id=request.state_id,
        composition_state_id=request.state_id,
    )
    _require_current_pretitle_value_equality(conn, service, running, selected=selected, snapshot=snapshot, request=request, ingress=ingress)


def require_pretitle_publication_lineage_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: StoredContinuationSnapshot,
    chunk: PrivateChunk,
    generation: int,
) -> None:
    """Verify historical publication DATA; this does not admit a current action."""
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    root = decode_continuation_snapshot(selected.payload_json)
    frame = root.frames[0]
    if len(root.frames) != 1 or type(frame) is not InitializeFrame or frame.bootstrap != chunk.reference:
        raise AuditIntegrityError("pre-title lineage lost its complete actual anchor")
    snapshot = decode_continuation_bootstrap(chunk)
    published, publication_generation = _pretitle_recovery_origin(conn, running, selected, generation=generation)
    event = conn.execute(
        select(events).where(events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == published.cursor)
    ).one_or_none()
    if event is None or event.tag != "checkpoint":
        raise AuditIntegrityError("pre-title bootstrap lost its original publication event")
    try:
        event_text = read_chunk(conn, session_id=sid, operation_id=operation, key=event.payload_chunk_key).decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise AuditIntegrityError("pre-title bootstrap publication is not strict UTF-8") from error
    from elspeth.contracts.json_parser import parse_json_strict

    event_value, parse_error = parse_json_strict(event_text)
    if parse_error is not None or type(event_value) is not dict or set(event_value) != {"owner", "publication"}:
        raise AuditIntegrityError("pre-title bootstrap lost its complete publication event")
    try:
        publication = PretitleBootstrapPublication.model_validate_json(canonical_json(event_value["publication"]), strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("pre-title bootstrap publication violates its owned shape") from error
    if (
        event_text != _event(publication)
        or publication.bootstrap != chunk.reference
        or publication.actor_user_id != snapshot.actor_user_id
        or publication.source_cursor + 1 != published.cursor
    ):
        raise AuditIntegrityError("pre-title bootstrap changed its exact original publication event")
    predecessor = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation,
            checkpoints.c.checkpoint_id == publication.source_checkpoint_id,
        )
    ).one_or_none()
    creator = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation,
            attempts.c.session_operation_id == publication.creator_session_operation_id,
            attempts.c.session_operation_epoch == publication.creator_session_operation_epoch,
        )
    ).one_or_none()
    if (
        predecessor is None
        or predecessor.cursor != publication.source_cursor
        or creator is None
        or creator.sequence != running.claim.segment_sequence
        or creator.generation != publication_generation
    ):
        raise AuditIntegrityError("pre-title bootstrap lost its original predecessor/creator lineage")
    try:
        previous_text = read_chunk(conn, session_id=sid, operation_id=operation, key=predecessor.snapshot_chunk_key).decode(
            "utf-8", errors="strict"
        )
    except UnicodeDecodeError as error:
        raise AuditIntegrityError("pre-title bootstrap predecessor is not strict UTF-8") from error
    expected_before = root.model_copy(
        update={"cursor": publication.source_cursor, "frames": (frame.model_copy(update={"ingress_phase": "inserted", "bootstrap": None}),)}
    )
    if previous_text != encode_continuation_snapshot(expected_before):
        raise AuditIntegrityError("pre-title bootstrap changed its complete actual inserted predecessor")
    require_pretitle_inserted_origin(
        conn,
        running,
        StoredContinuationSnapshot(previous_text, predecessor.cursor, predecessor.checkpoint_id),
        generation=publication_generation,
    )


def publish_pretitle_bootstrap_on_connection(
    conn: Connection,
    composer: ComposerServiceImpl,
    running: ComposerOperationRunning,
    *,
    prepared: PreparedPretitleBootstrap,
) -> StoredPretitleBootstrap:
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.sessions.service import SessionServiceImpl

    if type(composer) is not ComposerServiceImpl or type(prepared) is not PreparedPretitleBootstrap:
        raise AuditIntegrityError("pre-title bootstrap publication requires actual complete prepared owners")
    service = composer._require_sessions_service()
    if type(service) is not SessionServiceImpl:
        raise AuditIntegrityError("pre-title bootstrap publication lost its actual sessions owner")
    _require_sessions(conn, service)
    source = prepared.source
    if type(source) is not ContinuationTitleSource or type(source.selected) is not StoredContinuationSnapshot:
        raise AuditIntegrityError("pre-title bootstrap preparation lost its actual inserted source")
    current = read_title_source_on_connection(conn, service, running, selected=source.selected)
    if current != source or current.initial_state is None or current.recovery.context.malformed_head:
        raise AuditIntegrityError("pre-title bootstrap source changed across preparation awaits")
    root = decode_continuation_snapshot(source.selected.payload_json)
    source_frame = root.frames[0]
    if type(source_frame) is not InitializeFrame or source_frame.ingress_receipt_json is None:
        raise AuditIntegrityError("pre-title bootstrap lost its exact original inserted cut")
    require_pretitle_inserted_origin(conn, running, source.selected, generation=running.claim.ownership_generation)
    names = require_current_env_inventory_on_connection(
        conn,
        running,
        inventory=prepared.inventory,
        expected_checkpoint_id=source.selected.checkpoint_id,
        expected_cursor=source.selected.cursor,
    )
    snapshot = decode_continuation_bootstrap(prepared.bootstrap)
    values = bootstrap_values(snapshot)
    if values.retained_context is None or values.retained_context.availability != "available":
        raise AuditIntegrityError("pre-title bootstrap lost its actual bounded history read")
    if (
        tuple(sorted(composer._schema_disclosure.schemas_loaded_for_session(root.logical.session_id))) != snapshot.initial_schema_disclosure
        or (composer._composer_skill_name, composer._composer_skill_hash, composer._composer_skill_text) != prepared.skill_identity
    ):
        raise AuditIntegrityError("pre-title bootstrap disclosure/skill source changed across preparation awaits")
    expected = encode_continuation_bootstrap(
        capture_continuation_bootstrap(
            logical=root.logical,
            actor_user_id=current.actor_user_id,
            request=current.request,
            records=current.records,
            initial_state=current.initial_state,
            recovery_context=current.recovery.context,
            completion_gates=current.recovery.effective_completion_gates,
            retained_context=values.retained_context,
            session_title=current.original_session_title,
            initial_schema_disclosure=snapshot.initial_schema_disclosure,
        ),
        env_ref_names=names,
    )
    if expected != prepared.bootstrap or snapshot.auto_title_eligible is not current.eligible:
        raise AuditIntegrityError("pre-title bootstrap differs from its complete actual inserted source")
    phase = "title_pending" if current.eligible else "ready"
    frame = root.frames[0]
    if type(frame) is not InitializeFrame:
        raise AuditIntegrityError("pre-title bootstrap lost its exact Initialize frame")
    following = root.model_copy(update={"frames": (frame.model_copy(update={"ingress_phase": phase, "bootstrap": expected.reference}),)})
    context = running.session_operation_context
    publication = PretitleBootstrapPublication(
        source_checkpoint_id=source.selected.checkpoint_id,
        source_cursor=source.selected.cursor,
        bootstrap=expected.reference,
        actor_user_id=current.actor_user_id,
        creator_session_operation_id=context.fence.operation_id,
        creator_session_operation_epoch=context.fence.operation_epoch,
    )
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    put_chunk(
        conn,
        session_id=sid,
        operation_id=operation,
        key=private_chunk_key(expected),
        payload=expected.canonical.encode("utf-8"),
        codec=expected.reference.codec,
    )
    selected = append_complete_snapshot(
        conn,
        session_id=sid,
        operation_id=operation,
        expected_checkpoint_id=source.selected.checkpoint_id,
        expected_cursor=source.selected.cursor,
        snapshot_json=encode_continuation_snapshot(following),
        event_batch=(ContinuationEvent("checkpoint", _event(publication)),),
        handoff=False,
    )
    # Before commit, prove the same complete current source/event/chunk join
    # that a fresh claim will need. An error rolls back all publication writes.
    return read_pretitle_bootstrap_on_connection(conn, service, running, selected=selected)

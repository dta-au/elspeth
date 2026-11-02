"""Unpaid closure of an original END source whose request never bound custody."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.continuation import PrivateChunkRef
from elspeth.web.composer.continuation_checkpoint_end_input import decode_checkpoint_end_input
from elspeth.web.composer.continuation_codec import PrivateChunk, decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.coordination.composer_operation_authority import settle_composer_operation_on_connection
from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error
from elspeth.web.sessions.composer_operations import (
    COMPOSER_OPERATION_RESULT_SCHEMA_ERROR,
    ComposerOperationError,
    composer_operation_result_hash,
)
from elspeth.web.sessions.continuation import (
    StoredContinuationSnapshot,
    _settlement_transition,
    continuation_unpaid_terminal_reserve_on_connection,
    load_snapshot,
)
from elspeth.web.sessions.continuation_checkpoint_paid import _effect_inventory
from elspeth.web.sessions.continuation_retained_capacity import (
    admit_retained_payload_chunks,
    load_continuation_read_contract,
    read_bounded_retained_chunk,
)
from elspeth.web.sessions.continuation_selection import load_private_chunks
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_checkpoint_end_physical import CheckpointUnboundRefusalPermit


@dataclass(frozen=True, slots=True)
class VerifiedCheckpointUnboundRefusal:
    """Cold proof of a prepared END source with no checkpoint provider dispatch."""

    prepared: StoredContinuationSnapshot
    terminal: StoredContinuationSnapshot
    outer_effect_key: str
    error: ComposerOperationError


def _expected_error(request_id: str | None) -> ComposerOperationError:
    return project_composer_operation_error(ComposerContextUnsupported("prepared_request_bytes"), request_id=request_id)


def _bounded_chunk(conn: Connection, sid: str, operation: str, key: str, *, codec: str) -> bytes:
    contract = load_continuation_read_contract(conn, session_id=sid, operation_id=operation)
    if type(contract.allowance) is not int:
        raise AuditIntegrityError("unbound checkpoint lost its bounded read allowance")
    chunk = read_bounded_retained_chunk(conn, session_id=sid, operation_id=operation, key=key, allowance=contract.allowance)
    if chunk is None or chunk.codec != codec:
        raise AuditIntegrityError("unbound checkpoint lost retained source DATA")
    return chunk.payload


def _checkpoint(conn: Connection, sid: str, operation: str, checkpoint_id: str) -> StoredContinuationSnapshot:
    row = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation,
            checkpoints.c.checkpoint_id == checkpoint_id,
        )
    ).one_or_none()
    if row is None or row.kind != "transition" or row.snapshot_chunk_key != f"snapshot:{checkpoint_id}":
        raise AuditIntegrityError("unbound checkpoint lost its original source checkpoint")
    payload = _bounded_chunk(conn, sid, operation, row.snapshot_chunk_key, codec="snapshot.v2")
    if len(payload) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("unbound checkpoint source exceeds its unchanged private limit")
    return StoredContinuationSnapshot(payload.decode("utf-8", errors="strict"), row.cursor, row.checkpoint_id)


def _event(conn: Connection, sid: str, operation: str, cursor: int) -> tuple[str, str]:
    row = conn.execute(
        select(events.c.tag, events.c.payload_chunk_key).where(
            events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == cursor
        )
    ).one_or_none()
    if row is None or row.payload_chunk_key != f"event:{cursor}":
        raise AuditIntegrityError("unbound checkpoint lost its original journal event")
    payload = _bounded_chunk(conn, sid, operation, row.payload_chunk_key, codec="event.v2")
    if len(payload) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("unbound checkpoint event exceeds its unchanged private limit")
    return row.tag, payload.decode("utf-8", errors="strict")


def _prepared_source(
    conn: Connection,
    *,
    sid: str,
    operation: str,
    prepared: StoredContinuationSnapshot,
    creator_sequence: int,
    creator_generation: int,
    names: frozenset[str] | None,
) -> tuple[StoredContinuationSnapshot, str, int, str]:
    tag, text = _event(conn, sid, operation, prepared.cursor)
    event, error = parse_json_strict(text)
    if (
        tag != "checkpoint"
        or error is not None
        or type(event) is not dict
        or set(event)
        != {
            "owner",
            "version",
            "stage",
            "phase",
            "kind",
            "pass_index",
            "outer_effect_key",
            "source_checkpoint_id",
            "source_cursor",
            "creator_sequence",
            "creator_generation",
            "end_input_chunk_key",
            "end_input_sha256",
            "end_env_ref_names",
            "captured_root_sha256",
        }
        or event["owner"] != "composer_checkpoint_source"
        or event["version"] != 1
        or event["stage"] != "source_prepared"
        or event["phase"] != "end"
        or event["kind"] not in ("iteration_end", "end_review")
        or type(event["pass_index"]) is not int
        or event["pass_index"] < 0
        or event["creator_sequence"] != creator_sequence
        or event["creator_generation"] != creator_generation
        or event["captured_root_sha256"] != sha256(prepared.payload_json.encode("utf-8")).hexdigest()
        or type(event["outer_effect_key"]) is not str
        or type(event["source_checkpoint_id"]) is not str
        or type(event["source_cursor"]) is not int
        or type(event["end_env_ref_names"]) is not list
        or any(type(name) is not str or not name for name in event["end_env_ref_names"])
        or event["end_env_ref_names"] != sorted(set(event["end_env_ref_names"]))
        or text != canonical_json(event)
    ):
        raise AuditIntegrityError("unbound checkpoint lost its exact original END preparation")
    origin = _checkpoint(conn, sid, operation, event["source_checkpoint_id"])
    prepared_root = decode_continuation_snapshot(prepared.payload_json)
    original_root = decode_continuation_snapshot(origin.payload_json)
    if (
        origin.cursor != event["source_cursor"]
        or prepared.cursor != origin.cursor + 1
        or prepared_root.model_copy(update={"frames": original_root.frames, "cursor": original_root.cursor}) != original_root
        or prepared_root.pending_settlement_ids
        or prepared_root.pending_owner_results
        or sum(item.disposition == "dispatched" for item in prepared_root.effect_frontier) != 1
        or not any(
            item.effect_id == event["outer_effect_key"] and item.disposition == "dispatched" for item in prepared_root.effect_frontier
        )
    ):
        raise AuditIntegrityError("unbound checkpoint changed its unpaid outer END source")
    key, digest = event["end_input_chunk_key"], event["end_input_sha256"]
    if type(key) is not str or type(digest) is not str or key != f"value:composer.checkpoint-end-input.v1:{digest}":
        raise AuditIntegrityError("unbound checkpoint lost its END input reference")
    end_bytes = _bounded_chunk(conn, sid, operation, key, codec="composer.checkpoint-end-input.v1")
    if len(end_bytes) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES or sha256(end_bytes).hexdigest() != digest:
        raise AuditIntegrityError("unbound checkpoint changed its END input bytes")
    original_names = frozenset(event["end_env_ref_names"])
    if names is not None and names != original_names:
        raise AuditIntegrityError("unbound checkpoint changed its current END guard inventory")
    end_data = decode_checkpoint_end_input(
        PrivateChunk(
            PrivateChunkRef(codec="composer.checkpoint-end-input.v1", digest=digest, byte_length=len(end_bytes)),
            end_bytes.decode("utf-8", errors="strict"),
        ),
        env_ref_names=original_names,
    )
    if (
        end_data.session_id != sid
        or end_data.operation_id != operation
        or end_data.original_checkpoint_id != origin.checkpoint_id
        or end_data.original_cursor != origin.cursor
        or end_data.outer_effect_key != event["outer_effect_key"]
        or end_data.pass_index != event["pass_index"]
        or end_data.retry_ordinal != 0
        or end_data.captured_runtime_sha256
        != stable_hash(prepared_root.model_copy(update={"cursor": origin.cursor}).model_dump(mode="json"))
        or end_data.runtime_chunks
        != tuple(chunk.reference for chunk in load_private_chunks(conn, session_id=sid, operation_id=operation, stack=prepared_root))
    ):
        raise AuditIntegrityError("unbound checkpoint changed its complete original END gate input")
    return origin, event["outer_effect_key"], event["pass_index"], key


def refuse_v4_checkpoint_unbound_source_on_connection(
    conn: Connection, *, permit: CheckpointUnboundRefusalPermit
) -> VerifiedCheckpointUnboundRefusal:
    """Settle the issued source before any checkpoint attempt, without inventing one."""
    from elspeth.web.composer.continuation_checkpoint_end_physical import require_original_checkpoint_unbound_refusal

    require_original_checkpoint_unbound_refusal(permit)
    source = permit.source
    phase = source.phase_owner
    running = phase.phases.work.running
    if running is None or permit.failure.reason != "prepared_request_bytes":
        raise AuditIntegrityError("unbound checkpoint refusal lost its original predispatch owner")
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    prepared = source.prepared.prepared_selected
    if load_snapshot(conn, session_id=sid, operation_id=operation) != prepared:
        raise AuditIntegrityError("unbound checkpoint changed its current prepared source")
    origin, outer, pass_index, end_key = _prepared_source(
        conn,
        sid=sid,
        operation=operation,
        prepared=prepared,
        creator_sequence=running.claim.segment_sequence,
        creator_generation=running.claim.ownership_generation,
        names=phase.runtime.env_ref_names,
    )
    if (
        source.prepared.source != origin
        or source.prepared.outer_effect_key != outer
        or source.prepared.phase != "end"
        or source.prepared.pass_index != pass_index
        or source.prepared.end_input_ref is None
        or end_key != f"value:{source.prepared.end_input_ref.codec}:{source.prepared.end_input_ref.digest}"
    ):
        raise AuditIntegrityError("unbound checkpoint substituted its issued phase source")
    _effect_inventory(conn, sid, operation, prepared)
    policy_root = decode_continuation_snapshot(prepared.payload_json)
    policy = decode_continuation_policy(policy_root.admission_policy_json, policy_root.policy_hash)
    if type(policy) is not ContinuationAdmissionPolicyV4:
        raise AuditIntegrityError("unbound checkpoint has no original V4 retained policy")
    admit_retained_payload_chunks(
        conn,
        session_id=sid,
        operation_id=operation,
        prospective=continuation_unpaid_terminal_reserve_on_connection(
            conn, session_id=sid, operation_id=operation, post_root_json=prepared.payload_json, post_cursor=prepared.cursor
        ),
        retained_payload_limit_bytes=policy.max_retained_payload_bytes,
    )
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
    error = project_composer_operation_error(permit.failure, request_id=parent.request_id)
    if error != _expected_error(parent.request_id):
        raise AuditIntegrityError("unbound checkpoint changed its closed public refusal")
    settle_composer_operation_on_connection(conn, running, outcome=error, authoritative_failure=True)
    verified = read_v4_checkpoint_unbound_refusal_on_connection(
        conn, session_id=running.claim.session_id, operation_id=operation, names=phase.runtime.env_ref_names
    )
    if verified is None or verified.prepared != prepared or verified.error != error or verified.outer_effect_key != outer:
        raise AuditIntegrityError("unbound checkpoint did not retain its original cold terminal")
    return verified


def read_v4_checkpoint_unbound_refusal_on_connection(
    conn: Connection, *, session_id: UUID, operation_id: str, names: frozenset[str] | None = None
) -> VerifiedCheckpointUnboundRefusal | None:
    """Cold proof for the source-only 422, including its one outer unknown."""
    if type(session_id) is not UUID or type(operation_id) is not str or (names is not None and type(names) is not frozenset):
        raise TypeError("unbound checkpoint cold reader needs exact coordinates")
    sid = str(session_id)
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id)).one_or_none()
    if parent is None or parent.status != "failed":
        return None
    if type(parent.checkpoint_id) is not str:
        raise AuditIntegrityError("unbound checkpoint lost its terminal head")
    terminal = _checkpoint(conn, sid, operation_id, parent.checkpoint_id)
    if load_snapshot(conn, session_id=sid, operation_id=operation_id) != terminal:
        raise AuditIntegrityError("unbound checkpoint terminal differs from the selected current head")
    previous = conn.execute(
        select(checkpoints)
        .where(checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation_id, checkpoints.c.cursor < terminal.cursor)
        .order_by(checkpoints.c.cursor.desc())
        .limit(1)
    ).one_or_none()
    if previous is None:
        return None
    prepared = _checkpoint(conn, sid, operation_id, previous.checkpoint_id)
    tag, text = _event(conn, sid, operation_id, prepared.cursor)
    event, event_error = parse_json_strict(text)
    if tag != "checkpoint" or event_error is not None or type(event) is not dict or event.get("stage") != "source_prepared":
        return None
    error = _expected_error(parent.request_id)
    if (
        parent.status != "failed"
        or parent.phase != "settled"
        or parent.settled_by != "owner_terminal"
        or parent.failure_code != error.failure_code
        or parent.result_schema != COMPOSER_OPERATION_RESULT_SCHEMA_ERROR
        or parent.result_json != canonical_json(error.model_dump(mode="json"))
        or parent.result_sha256 != composer_operation_result_hash(parent.result_json)
        or parent.checkpoint_id != terminal.checkpoint_id
        or parent.journal_cursor != terminal.cursor
    ):
        raise AuditIntegrityError("unbound checkpoint changed its original failed parent")
    segment = conn.execute(
        select(segments).where(
            segments.c.session_id == sid,
            segments.c.operation_id == operation_id,
            segments.c.sequence == parent.current_segment_sequence,
        )
    ).one_or_none()
    history = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation_id,
            attempts.c.sequence == parent.current_segment_sequence,
            attempts.c.generation == parent.ownership_generation,
        )
    ).one_or_none()
    if segment is None or history is None or segment.state != "settled" or history.outcome != "settled" or history.revoked_at is None:
        raise AuditIntegrityError("unbound checkpoint lost its original settled segment")
    _origin, outer, _pass_index, _end_key = _prepared_source(
        conn,
        sid=sid,
        operation=operation_id,
        prepared=prepared,
        creator_sequence=segment.sequence,
        creator_generation=segment.generation,
        names=names,
    )
    prepared_root = decode_continuation_snapshot(prepared.payload_json)
    policy = decode_continuation_policy(prepared_root.admission_policy_json, prepared_root.policy_hash)
    if (
        type(policy) is not ContinuationAdmissionPolicyV4
        or prepared_root.logical.session_id != sid
        or prepared_root.logical.operation_id != operation_id
        or parent.continuation_policy_version != policy.schema_version
        or parent.continuation_policy_sha256 != policy.policy_hash
        or parent.continuation_retained_payload_limit_bytes != policy.max_retained_payload_bytes
    ):
        raise AuditIntegrityError("unbound checkpoint changed its original parent policy contract")
    terminal_row = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation_id,
            checkpoints.c.checkpoint_id == terminal.checkpoint_id,
        )
    ).one()
    expected_root, unresolved, expected_events = _settlement_transition(
        prepared, segment=segment, outcome=error, published_at=terminal_row.created_at, settled_by="owner_terminal"
    )
    if (
        unresolved != (outer,)
        or terminal.payload_json != encode_continuation_snapshot(expected_root.model_copy(update={"cursor": terminal.cursor}))
        or terminal.cursor != prepared.cursor + len(expected_events)
        or len(expected_events) != 2
    ):
        raise AuditIntegrityError("unbound checkpoint changed its exact outer unknown terminal")
    for cursor, expected in enumerate(expected_events, start=prepared.cursor + 1):
        actual_tag, actual_text = _event(conn, sid, operation_id, cursor)
        if actual_tag != expected.tag or actual_text != expected.payload_json:
            raise AuditIntegrityError("unbound checkpoint changed its original terminal journal")
    _effect_inventory(conn, sid, operation_id, terminal)
    prepared_keys = {item.effect_id for item in prepared_root.effect_frontier}
    bound_effects = conn.execute(
        select(effects.c.effect_key, effects.c.provider_attempt_id)
        .where(
            effects.c.session_id == sid,
            effects.c.operation_id == operation_id,
            effects.c.physical_sequence == history.sequence,
            effects.c.physical_generation == history.generation,
            effects.c.provider_attempt_id.is_not(None),
        )
        .limit(len(prepared_keys) + 1)
    ).all()
    bound_ids = {row.provider_attempt_id for row in bound_effects}
    if (
        len(bound_effects) > len(prepared_keys)
        or len(bound_ids) != len(bound_effects)
        or any(row.effect_key not in prepared_keys for row in bound_effects)
    ):
        raise AuditIntegrityError("unbound checkpoint gained a physical effect after source preparation")
    physical_ids = tuple(
        conn.execute(
            select(physical.c.attempt_id)
            .where(
                physical.c.session_id == sid,
                physical.c.source == "composer",
                physical.c.operation_id == history.session_operation_id,
                physical.c.operation_epoch == history.session_operation_epoch,
            )
            .limit(len(bound_ids) + 1)
        ).scalars()
    )
    if len(physical_ids) != len(bound_ids) or set(physical_ids) != bound_ids:
        raise AuditIntegrityError("unbound checkpoint gained an orphan physical attempt")
    if (
        conn.execute(
            select(holds.c.effect_key)
            .where(holds.c.session_id == sid, holds.c.operation_id == operation_id, holds.c.state == "active")
            .limit(1)
        ).first()
        is not None
    ):
        raise AuditIntegrityError("unbound checkpoint gained an active physical terminal hold")
    return VerifiedCheckpointUnboundRefusal(prepared, terminal, outer, error)

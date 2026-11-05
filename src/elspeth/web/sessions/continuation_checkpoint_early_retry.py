"""One retained malformed-complete EARLY result, awaiting a new driver action.

This marker is DATA. It cannot restart a clock, replay an entered SDK call,
refund first-call accounting, or grant another attempt without live issuance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import Field, ValidationError
from sqlalchemy import Connection, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import ComposeFrame, ContinuationValue, PrivateChunkRef
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory, require_current_env_inventory_on_connection
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    StoredContinuationSnapshot,
    _require_current_continuation_running,
    append_complete_snapshot,
    load_snapshot,
    prepare_snapshot_append,
    read_chunk,
)
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments

if TYPE_CHECKING:
    from elspeth.web.sessions.continuation_checkpoint_early_return import EarlyCheckpointChildCommitted, EarlyCheckpointChildData
    from elspeth.web.sessions.continuation_checkpoint_paid import CheckpointPhaseSource


class EarlyFormatRetryMarker(ContinuationValue):
    owner: Literal["composer_checkpoint_early_format_retry"] = "composer_checkpoint_early_format_retry"
    version: Literal[1] = 1
    session_id: str
    operation_id: str
    child_checkpoint_id: str
    child_cursor: int = Field(ge=0)
    original_phase_checkpoint_id: str
    original_phase_cursor: int = Field(ge=0)
    attempt_id: str
    effect_key: str
    outer_effect_key: str
    early_input_ref: PrivateChunkRef
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    first_attempt_schema_valid: bool | None
    format_reprompt_required: bool
    retry_ordinal: Literal[0] = 0
    next_retry_ordinal: Literal[1] = 1


@dataclass(frozen=True, slots=True)
class VerifiedEarlyFormatRetry:
    marker: EarlyFormatRetryMarker
    child: EarlyCheckpointChildData
    selected: StoredContinuationSnapshot


def retain_early_format_retry_on_connection(
    conn: Connection,
    *,
    running: ComposerOperationRunning,
    selected: StoredContinuationSnapshot,
    inventory: CurrentContinuationEnvInventory,
    completion_proof: EarlyCheckpointChildCommitted,
) -> StoredContinuationSnapshot:
    from elspeth.web.composer.continuation_checkpoint_early_observation import early_format_reprompt_required
    from elspeth.web.composer.continuation_codec import decode_continuation_response
    from elspeth.web.sessions.continuation_checkpoint_early_return import read_original_early_child_source_on_connection

    _require_current_continuation_running(conn, running)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    if load_snapshot(conn, session_id=sid, operation_id=operation) != selected or completion_proof.selected != selected:
        raise AuditIntegrityError("EARLY retry changed its actual current complete child")
    root = decode_continuation_snapshot(selected.payload_json)
    require_current_env_inventory_on_connection(
        conn, running, inventory=inventory, expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.cursor
    )
    child = read_original_early_child_source_on_connection(conn, session_id=sid, operation_id=operation, selected=selected)
    if not child.retry_pending or child.accounting.provider_attempts != 1 or child.original_stage != completion_proof.original_stage:
        raise AuditIntegrityError("EARLY retry requires its first original malformed complete child")
    reference = child.owed.source.early_input_ref
    if reference is None:
        raise AuditIntegrityError("EARLY retry lost its versioned original request")
    marker = EarlyFormatRetryMarker(
        session_id=sid,
        operation_id=operation,
        child_checkpoint_id=selected.checkpoint_id,
        child_cursor=selected.cursor,
        original_phase_checkpoint_id=child.original_stage.checkpoint_id,
        original_phase_cursor=child.original_stage.cursor,
        attempt_id=child.owed.attempt_id,
        effect_key=child.owed.effect_key,
        outer_effect_key=child.owed.outer_effect_key,
        early_input_ref=reference,
        response_sha256=child.response.reference.digest,
        first_attempt_schema_valid=child.accounting.first_attempt_schema_valid,
        format_reprompt_required=early_format_reprompt_required(decode_continuation_response(child.response)),
    )
    text = canonical_json(marker.model_dump(mode="json"))
    if len(text.encode("utf-8")) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("EARLY retry marker exceeds its unchanged private document bound")
    event = ContinuationEvent("checkpoint", text)
    now = database_now(conn)
    append = prepare_snapshot_append(selected, snapshot_json=selected.payload_json, events=(event,), published_at=now)
    stored = append_complete_snapshot(
        conn,
        session_id=sid,
        operation_id=operation,
        expected_checkpoint_id=selected.checkpoint_id,
        expected_cursor=selected.cursor,
        snapshot_json=append.snapshot_json,
        event_batch=append.events,
        handoff=False,
        prepared_append=append,
    )
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    changed = conn.execute(
        update(segments)
        .where(
            segments.c.session_id == sid,
            segments.c.operation_id == operation,
            segments.c.sequence == running.claim.segment_sequence,
            segments.c.generation == running.claim.ownership_generation,
            segments.c.state == "running",
        )
        .values(next_action_json=policy.actions.advisor.model_dump_json())
    )
    if changed.rowcount != 1:
        raise AuditIntegrityError("EARLY retry lost its original current segment")
    verified = read_early_format_retry_on_connection(conn, session_id=sid, operation_id=operation, selected=stored)
    if verified is None or verified.child != child:
        raise AuditIntegrityError("EARLY retry failed its original cold child proof")
    return stored


def read_early_format_retry_on_connection(
    conn: Connection,
    *,
    session_id: str,
    operation_id: str,
    selected: StoredContinuationSnapshot,
) -> VerifiedEarlyFormatRetry | None:
    from elspeth.web.composer.continuation_checkpoint_early_observation import early_format_reprompt_required
    from elspeth.web.composer.continuation_codec import decode_continuation_response
    from elspeth.web.sessions.continuation_checkpoint_early_return import read_original_early_child_source_on_connection
    from elspeth.web.sessions.continuation_iteration_return import _historical

    tag = conn.execute(
        select(events.c.tag, events.c.payload_chunk_key).where(
            events.c.session_id == session_id,
            events.c.operation_id == operation_id,
            events.c.sequence == selected.cursor,
        )
    ).one_or_none()
    if tag is None or tag.tag != "checkpoint":
        return None
    text = read_chunk(conn, session_id=session_id, operation_id=operation_id, key=tag.payload_chunk_key).decode("utf-8", errors="strict")
    from elspeth.contracts.json_parser import parse_json_strict

    value, error = parse_json_strict(text)
    if error is not None or type(value) is not dict:
        raise AuditIntegrityError("EARLY retry lost its canonical owned event")
    if value.get("owner") != "composer_checkpoint_early_format_retry":
        return None
    try:
        marker = EarlyFormatRetryMarker.model_validate_json(text, strict=True)
    except ValidationError as failure:
        raise AuditIntegrityError("EARLY retry changed its closed complete-response marker") from failure
    child_head = _historical(conn, session_id, operation_id, checkpoint_id=marker.child_checkpoint_id)
    root = decode_continuation_snapshot(selected.payload_json)
    if (
        canonical_json(marker.model_dump(mode="json")) != text
        or marker.session_id != session_id
        or marker.operation_id != operation_id
        or selected.cursor != child_head.cursor + 1
        or marker.child_cursor != child_head.cursor
        or root != decode_continuation_snapshot(child_head.payload_json).model_copy(update={"cursor": selected.cursor})
        or root.pending_settlement_ids
        or root.pending_owner_results
        or type(root.frames[0]) is not ComposeFrame
    ):
        raise AuditIntegrityError("EARLY retry changed its complete original child successor")
    child = read_original_early_child_source_on_connection(conn, session_id=session_id, operation_id=operation_id, selected=child_head)
    if (
        not child.retry_pending
        or child.accounting.provider_attempts != 1
        or child.accounting.first_attempt_schema_valid != marker.first_attempt_schema_valid
        or marker.original_phase_checkpoint_id != child.original_stage.checkpoint_id
        or marker.original_phase_cursor != child.original_stage.cursor
        or marker.attempt_id != child.owed.attempt_id
        or marker.effect_key != child.owed.effect_key
        or marker.outer_effect_key != child.owed.outer_effect_key
        or marker.early_input_ref != child.owed.source.early_input_ref
        or marker.response_sha256 != child.response.reference.digest
        or marker.format_reprompt_required != early_format_reprompt_required(decode_continuation_response(child.response))
    ):
        raise AuditIntegrityError("EARLY retry changed its first complete response or original request")
    return VerifiedEarlyFormatRetry(marker, child, selected)


def read_early_format_retry_phase_on_connection(
    conn: Connection,
    *,
    session_id: str,
    operation_id: str,
    selected: StoredContinuationSnapshot,
) -> CheckpointPhaseSource | None:
    from elspeth.web.sessions.continuation_checkpoint_paid import CheckpointPhaseSource

    retry = read_early_format_retry_on_connection(conn, session_id=session_id, operation_id=operation_id, selected=selected)
    if retry is None:
        return None
    return CheckpointPhaseSource(
        "early", "iteration_early", retry.child.original_stage, selected, retry.marker.outer_effect_key, (retry.marker.effect_key,)
    )

"""Retain a returned END response under its original pending paid attempt.

This cut grants no successor dispatch or END side-effect authority. It keeps the
complete original response and audit available for a later, separately admitted
END consequence batch, while replacing H with an exact terminal bound for the
new retained head. Failure rolls the whole cut back to the original H.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from typing import TYPE_CHECKING, Literal

from sqlalchemy import Connection, func, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, is_lower_sha256_hex, stable_hash
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.continuation import EndFrame, PrivateChunkRef
from elspeth.web.composer.continuation_checkpoint_end_input import CheckpointEndInputSnapshot, decode_checkpoint_end_input
from elspeth.web.composer.continuation_checkpoint_observation import (
    CheckpointReturnedResponseObservation,
    require_original_v4_checkpoint_returned_response,
)
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_response,
    decode_continuation_snapshot,
    decode_retained_physical_audit_call,
)
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy
from elspeth.web.composer.continuation_provider_outcomes import require_v4_successful_response_audit
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    StoredContinuationSnapshot,
    append_complete_snapshot,
    prepare_snapshot_append,
    private_chunk_key,
    put_chunk,
)
from elspeth.web.sessions.continuation_checkpoint_paid import (
    PreparedCheckpointProvider,
    V4CheckpointProviderAdmission,
    require_current_checkpoint_dispatched_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_paid_tail import (
    CheckpointPaidOverflowTailPlan,
    prepare_checkpoint_paid_overflow_tail_plan,
)
from elspeth.web.sessions.continuation_checkpoint_sdk_reservation import (
    prepare_checkpoint_sdk_terminal_hold_plan,
)
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity
from elspeth.web.sessions.continuation_retained_capacity import (
    ProspectiveContinuationChunk,
    ProviderTerminalHoldSource,
    _require_active_provider_hold,
    _require_hold_fields,
    admit_retained_payload_chunks,
    load_continuation_read_contract,
    read_bounded_retained_chunk,
)
from elspeth.web.sessions.continuation_selection import load_private_chunks
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical

if TYPE_CHECKING:
    from elspeth.web.sessions.composer_operations import ComposerOperationRunning
    from elspeth.web.sessions.continuation_checkpoint_end_retry import VerifiedEndFormatRetry


_OWNER = "composer_checkpoint_end_owed"


@dataclass(frozen=True, slots=True)
class CheckpointEndOwedAdmission:
    """Original attempt plus a new retained head/H, never a dispatch permit."""

    original: V4CheckpointProviderAdmission
    owed_selected: StoredContinuationSnapshot
    owed_hold: ProviderTerminalHoldSource
    owed_tail: CheckpointPaidOverflowTailPlan
    response: PrivateChunk


@dataclass(frozen=True, slots=True)
class VerifiedCheckpointEndOwed:
    session_id: str
    operation_id: str
    attempt_id: str
    effect_key: str
    outer_effect_key: str
    pass_index: int
    retry_ordinal: int
    request_sha256: str
    publication_binding: str | None
    selected: StoredContinuationSnapshot
    original_selected: StoredContinuationSnapshot
    response: PrivateChunk
    old_hold_contract_sha256: str
    new_hold: ProviderTerminalHoldSource
    terminal_tail: CheckpointPaidOverflowTailPlan
    phase_source: StoredContinuationSnapshot | None = None
    end_input_ref: PrivateChunkRef | None = None
    end_input: CheckpointEndInputSnapshot | None = None
    prepared_selected: StoredContinuationSnapshot | None = None
    requested_model: str | None = None
    retry_history: VerifiedEndFormatRetry | None = None


def _stored_checkpoint(
    conn: Connection, *, session_id: str, operation_id: str, checkpoint_id: str, allowance: int
) -> StoredContinuationSnapshot:
    row = conn.execute(
        select(checkpoints.c.cursor, checkpoints.c.snapshot_chunk_key).where(
            checkpoints.c.session_id == session_id,
            checkpoints.c.operation_id == operation_id,
            checkpoints.c.checkpoint_id == checkpoint_id,
        )
    ).one_or_none()
    if row is None or type(row.cursor) is not int:
        raise AuditIntegrityError("END-owed source lost its original checkpoint")
    chunk = read_bounded_retained_chunk(
        conn, session_id=session_id, operation_id=operation_id, key=row.snapshot_chunk_key, allowance=allowance
    )
    if chunk is None or chunk.codec != "snapshot.v2":
        raise AuditIntegrityError("END-owed source lost its bounded original root")
    encoded = chunk.payload.decode("utf-8", errors="strict")
    root = decode_continuation_snapshot(encoded)
    if (root.logical.session_id, root.logical.operation_id, root.cursor) != (session_id, operation_id, row.cursor):
        raise AuditIntegrityError("END-owed source changed its bounded checkpoint identity")
    return StoredContinuationSnapshot(encoded, row.cursor, checkpoint_id)


def _bounded_event_text(conn: Connection, *, session_id: str, operation_id: str, cursor: int, allowance: int) -> tuple[str, str]:
    row = conn.execute(
        select(events.c.tag, events.c.payload_chunk_key).where(
            events.c.session_id == session_id,
            events.c.operation_id == operation_id,
            events.c.sequence == cursor,
        )
    ).one_or_none()
    if row is None:
        raise AuditIntegrityError("END-owed source lost its bounded event")
    chunk = read_bounded_retained_chunk(
        conn, session_id=session_id, operation_id=operation_id, key=row.payload_chunk_key, allowance=allowance
    )
    if chunk is None or chunk.codec != "event.v2":
        raise AuditIntegrityError("END-owed source lost its bounded event payload")
    return row.tag, chunk.payload.decode("utf-8", errors="strict")


def retain_v4_checkpoint_end_owed_on_connection(
    conn: Connection,
    *,
    running: ComposerOperationRunning,
    prepared: PreparedCheckpointProvider,
    admission: V4CheckpointProviderAdmission,
    returned: CheckpointReturnedResponseObservation,
) -> CheckpointEndOwedAdmission:
    """Atomically append the raw return and rebind H before any END consequence."""
    if (
        not conn.in_transaction()
        or type(prepared) is not PreparedCheckpointProvider
        or type(admission) is not V4CheckpointProviderAdmission
        or type(returned) is not CheckpointReturnedResponseObservation
        or prepared.running is not running
        or prepared.prepared_source != admission.original_source
        or admission.original_source.phase != "end"
        or returned.phase != "end"
        or returned.attempt != admission.attempt
        or returned.audit_call.messages_hash != prepared.request_sha256
        or type(returned.response) is not PrivateChunk
        or decode_retained_physical_audit_call(returned.response) != returned.audit_call
        or decode_continuation_response(returned.response).message != returned.message
    ):
        raise AuditIntegrityError("END-owed cut changed its original complete returned response")
    require_original_v4_checkpoint_returned_response(
        returned,
        attempt_id=admission.attempt.attempt_id,
        request_sha256=prepared.request_sha256,
    )
    selected = require_current_checkpoint_dispatched_on_connection(conn, running, admission)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    response = returned.response
    response_key = private_chunk_key(response)
    event = ContinuationEvent(
        "checkpoint",
        canonical_json(
            {
                "owner": _OWNER,
                "version": 1,
                "phase": "end",
                "attempt_id": admission.attempt.attempt_id,
                "effect_key": admission.effect_key,
                "outer_effect_key": admission.original_source.outer_effect_key,
                "pass_index": admission.original_source.pass_index,
                "retry_ordinal": prepared.retry_ordinal,
                "request_sha256": prepared.request_sha256,
                "publication_binding": prepared.publication_binding,
                "response_chunk_key": response_key,
                "response_sha256": response.reference.digest,
                "source_result_complete": True,
                "dispatched_checkpoint_id": selected.checkpoint_id,
                "dispatched_cursor": selected.cursor,
                "old_hold_contract_sha256": admission.hold.fallback_contract_sha256,
                "old_hold_reserved_bytes": admission.hold.reserved_bytes,
                "end_input_chunk_key": f"value:{admission.original_source.end_input_ref.codec}:{admission.original_source.end_input_ref.digest}"
                if admission.original_source.end_input_ref is not None
                else None,
                "end_input_sha256": admission.original_source.end_input_ref.digest
                if admission.original_source.end_input_ref is not None
                else None,
                "end_env_ref_names": admission.original_source.end_env_ref_names,
            }
        ),
    )
    now = database_now(conn)
    append = prepare_snapshot_append(selected, snapshot_json=selected.payload_json, events=(event,), published_at=now)
    owed_selected = StoredContinuationSnapshot(append.snapshot_json, selected.cursor + 1, append.checkpoint_id)
    owed_tail = prepare_checkpoint_paid_overflow_tail_plan(
        identity=admission.paid_tail.identity,
        admitted=owed_selected,
        phase="end",
        creator_sequence=running.claim.segment_sequence,
    )
    owed_hold = replace(
        admission.hold,
        reserved_bytes=owed_tail.reserved_bytes,
        fallback_contract_sha256=owed_tail.fallback_contract_sha256,
    )
    contract = load_continuation_read_contract(conn, session_id=sid, operation_id=operation)
    if contract.version != "composer.continuation.policy.v4" or contract.allowance != owed_hold.allowance_bytes:
        raise AuditIntegrityError("END-owed source lost its original V4 allowance")
    prospective = (
        ProspectiveContinuationChunk(response_key, response.canonical.encode("utf-8"), response.reference.codec),
        ProspectiveContinuationChunk(f"event:{selected.cursor + 1}", event.payload_json.encode("utf-8"), "event.v2"),
        ProspectiveContinuationChunk(f"snapshot:{append.checkpoint_id}", append.snapshot_json.encode("utf-8"), "snapshot.v2"),
    )
    # The old H is replaced, never added to the new H. This is one locked SQL
    # transaction; a refusal rolls back both the prospective prefix and H.
    admit_retained_payload_chunks(
        conn,
        session_id=sid,
        operation_id=operation,
        prospective=prospective,
        retained_payload_limit_bytes=contract.allowance,
        consuming_hold=admission.hold,
        _new_hold_bytes=owed_hold.reserved_bytes,
    )
    changed = conn.execute(
        update(holds)
        .where(
            holds.c.session_id == sid,
            holds.c.operation_id == operation,
            holds.c.effect_key == admission.effect_key,
            holds.c.provider_attempt_id == admission.attempt.attempt_id,
            holds.c.state == "active",
            holds.c.reserved_bytes == admission.hold.reserved_bytes,
            holds.c.fallback_contract_sha256 == admission.hold.fallback_contract_sha256,
        )
        .values(reserved_bytes=owed_hold.reserved_bytes, fallback_contract_sha256=owed_hold.fallback_contract_sha256)
    )
    if changed.rowcount != 1 or _require_active_provider_hold(conn, source=owed_hold, contract=contract) != owed_hold.reserved_bytes:
        raise AuditIntegrityError("END-owed source lost its original active H rebind")
    put_chunk(
        conn,
        session_id=sid,
        operation_id=operation,
        key=response_key,
        payload=response.canonical.encode("utf-8"),
        codec=response.reference.codec,
    )
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
    if stored != owed_selected:
        raise AuditIntegrityError("END-owed source changed its prepared exact root")
    verified = read_v4_checkpoint_end_owed_on_connection(
        conn,
        session_id=sid,
        operation_id=operation,
        attempt_id=admission.attempt.attempt_id,
        names=prepared.env_ref_names,
    )
    if verified is None or verified.response != response or verified.new_hold != owed_hold or verified.selected != owed_selected:
        raise AuditIntegrityError("END-owed source failed its independent cold proof")
    return CheckpointEndOwedAdmission(admission, owed_selected, owed_hold, owed_tail, response)


def _read_checkpoint_end_owed_on_connection(
    conn: Connection,
    *,
    session_id: str,
    operation_id: str,
    attempt_id: str,
    names: frozenset[str] | None,
    historical_owed_checkpoint_id: str | None,
    settled_child_disposition: Literal["committed", "paid_overflow"] | None,
    expected_hold_state: Literal["active", "consumed"],
    expected_retry_ordinal: Literal[0, 1] | None,
) -> VerifiedCheckpointEndOwed | None:
    """Read one exact H; historical reads require a settled named child."""
    if any(type(value) is not str or not value for value in (session_id, operation_id, attempt_id)):
        raise TypeError("END-owed cold read requires exact SQL identities")
    if names is not None and (type(names) is not frozenset or any(type(name) is not str for name in names)):
        raise TypeError("END-owed cold read requires actual current guard names")
    contract = load_continuation_read_contract(conn, session_id=session_id, operation_id=operation_id)
    if contract.version != "composer.continuation.policy.v4" or type(contract.allowance) is not int:
        raise AuditIntegrityError("END-owed cold source lost V4 policy")
    parent = conn.execute(
        select(jobs.c.checkpoint_id, jobs.c.journal_cursor).where(jobs.c.session_id == session_id, jobs.c.operation_id == operation_id)
    ).one_or_none()
    if parent is None or type(parent.journal_cursor) is not int or parent.journal_cursor < 1:
        raise AuditIntegrityError("END-owed cold source lost its current parent")
    last = conn.execute(
        select(func.coalesce(func.max(events.c.sequence), 0)).where(
            events.c.session_id == session_id, events.c.operation_id == operation_id
        )
    ).scalar_one()
    if last != parent.journal_cursor:
        raise AuditIntegrityError("END-owed cold source lost its complete event cursor")
    selected = _stored_checkpoint(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        checkpoint_id=historical_owed_checkpoint_id or parent.checkpoint_id,
        allowance=contract.allowance,
    )
    if selected.cursor > parent.journal_cursor or (historical_owed_checkpoint_id is None and selected.cursor != parent.journal_cursor):
        raise AuditIntegrityError("END-owed cold source changed its current root cursor")
    tag, text = _bounded_event_text(
        conn, session_id=session_id, operation_id=operation_id, cursor=selected.cursor, allowance=contract.allowance
    )
    value, error = parse_json_strict(text)
    if tag != "checkpoint" or error is not None or type(value) is not dict or value.get("owner") != _OWNER:
        return None
    expected_keys = {
        "owner",
        "version",
        "phase",
        "attempt_id",
        "effect_key",
        "outer_effect_key",
        "pass_index",
        "retry_ordinal",
        "request_sha256",
        "publication_binding",
        "response_chunk_key",
        "response_sha256",
        "source_result_complete",
        "dispatched_checkpoint_id",
        "dispatched_cursor",
        "old_hold_contract_sha256",
        "old_hold_reserved_bytes",
        "end_input_chunk_key",
        "end_input_sha256",
        "end_env_ref_names",
    }
    if (
        set(value) != expected_keys
        or text != canonical_json(value)
        or value["version"] != 1
        or value["phase"] != "end"
        or value["source_result_complete"] is not True
        or value["attempt_id"] != attempt_id
        or type(value["pass_index"]) is not int
        or value["pass_index"] < 0
        or type(value["retry_ordinal"]) is not int
        or value["retry_ordinal"] not in (0, 1)
        or (expected_retry_ordinal is not None and value["retry_ordinal"] != expected_retry_ordinal)
        or type(value["end_env_ref_names"]) is not list
        or any(type(name) is not str or not name for name in value["end_env_ref_names"])
        or value["end_env_ref_names"] != sorted(set(value["end_env_ref_names"]))
        or (names is not None and names != frozenset(value["end_env_ref_names"]))
        or not is_lower_sha256_hex(value["request_sha256"])
        or (value["publication_binding"] is not None and not is_lower_sha256_hex(value["publication_binding"]))
        or type(value["outer_effect_key"]) is not str
        or not value["outer_effect_key"]
        or type(value["dispatched_cursor"]) is not int
        or selected.cursor != value["dispatched_cursor"] + 1
        or type(value["old_hold_reserved_bytes"]) is not int
        or value["old_hold_reserved_bytes"] <= 0
    ):
        raise AuditIntegrityError("END-owed cold source changed its closed retained event")
    original = _stored_checkpoint(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        checkpoint_id=value["dispatched_checkpoint_id"],
        allowance=contract.allowance,
    )
    if original.cursor != value["dispatched_cursor"]:
        raise AuditIntegrityError("END-owed cold source changed its dispatched cursor")
    previous_root = decode_continuation_snapshot(original.payload_json)
    owed_root = decode_continuation_snapshot(selected.payload_json)
    if owed_root != previous_root.model_copy(update={"cursor": selected.cursor}):
        raise AuditIntegrityError("END-owed cold source changed its pending original root")
    if type(previous_root.frames[0]) is EndFrame and previous_root.frames[0].publication_binding != value["publication_binding"]:
        raise AuditIntegrityError("END-owed cold source changed its original publication binding")
    original_tag, original_text = _bounded_event_text(
        conn, session_id=session_id, operation_id=operation_id, cursor=original.cursor, allowance=contract.allowance
    )
    original_event, original_error = parse_json_strict(original_text)
    dispatched_fields = {
        "owner",
        "version",
        "stage",
        "phase",
        "kind",
        "pass_index",
        "retry_ordinal",
        "effect_key",
        "outer_effect_key",
        "attempt_id",
        "request_sha256",
        "requested_model",
        "source_checkpoint_id",
        "source_cursor",
        "end_input_chunk_key",
        "end_input_sha256",
        "end_env_ref_names",
    }
    if (
        original_tag != "checkpoint"
        or original_error is not None
        or type(original_event) is not dict
        or set(original_event) != dispatched_fields
        or original_text != canonical_json(original_event)
        or original_event.get("owner") != "composer_checkpoint_paid"
        or original_event.get("version") != 1
        or original_event.get("stage") != "dispatched"
        or original_event.get("phase") != "end"
        or original_event.get("attempt_id") != attempt_id
        or original_event.get("effect_key") != value["effect_key"]
        or original_event.get("outer_effect_key") != value["outer_effect_key"]
        or original_event.get("pass_index") != value["pass_index"]
        or original_event.get("retry_ordinal") != value["retry_ordinal"]
        or original_event.get("request_sha256") != value["request_sha256"]
        or original_event.get("requested_model") is None
        or original_event.get("end_input_chunk_key") != value["end_input_chunk_key"]
        or original_event.get("end_input_sha256") != value["end_input_sha256"]
        or original_event.get("end_env_ref_names") != value["end_env_ref_names"]
        or original_event.get("end_input_chunk_key") is None
    ):
        raise AuditIntegrityError("END-owed cold source lost its original dispatch event")
    prepared = _stored_checkpoint(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        checkpoint_id=original_event["source_checkpoint_id"],
        allowance=contract.allowance,
    )
    prepared_tag, prepared_text = _bounded_event_text(
        conn, session_id=session_id, operation_id=operation_id, cursor=prepared.cursor, allowance=contract.allowance
    )
    prepared_event, prepared_error = parse_json_strict(prepared_text)
    prepared_fields = {
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
    if (
        prepared.cursor != original_event["source_cursor"]
        or prepared_tag != "checkpoint"
        or prepared_error is not None
        or type(prepared_event) is not dict
        or set(prepared_event) != prepared_fields
        or prepared_text != canonical_json(prepared_event)
        or prepared_event.get("owner") != "composer_checkpoint_source"
        or prepared_event.get("version") != 1
        or prepared_event.get("stage") != "source_prepared"
        or prepared_event.get("phase") != "end"
        or prepared_event.get("kind") != original_event.get("kind")
        or prepared_event.get("pass_index") != value["pass_index"]
        or prepared_event.get("outer_effect_key") != value["outer_effect_key"]
        or prepared_event.get("end_input_chunk_key") != value["end_input_chunk_key"]
        or prepared_event.get("end_input_sha256") != value["end_input_sha256"]
        or prepared_event.get("end_env_ref_names") != value["end_env_ref_names"]
        or prepared_event.get("captured_root_sha256") != sha256(prepared.payload_json.encode("utf-8")).hexdigest()
    ):
        raise AuditIntegrityError("END-owed cold source lost its original gate-input preparation")
    if (
        type(prepared_event["source_checkpoint_id"]) is not str
        or type(prepared_event["source_cursor"]) is not int
        or prepared.cursor != prepared_event["source_cursor"] + 1
    ):
        raise AuditIntegrityError("END-owed cold source lost its original outer checkpoint locator")
    outer = _stored_checkpoint(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        checkpoint_id=prepared_event["source_checkpoint_id"],
        allowance=contract.allowance,
    )
    prepared_root = decode_continuation_snapshot(prepared.payload_json)
    outer_root = decode_continuation_snapshot(outer.payload_json)
    if (
        outer.cursor != prepared_event["source_cursor"]
        or prepared_root.model_copy(update={"frames": outer_root.frames, "cursor": outer_root.cursor}) != outer_root
        or not any(item.effect_id == value["outer_effect_key"] and item.disposition == "dispatched" for item in outer_root.effect_frontier)
    ):
        raise AuditIntegrityError("END-owed cold source changed its original outer phase root")
    end_key, end_digest = value["end_input_chunk_key"], value["end_input_sha256"]
    if type(end_key) is not str or type(end_digest) is not str or end_key != f"value:composer.checkpoint-end-input.v1:{end_digest}":
        raise AuditIntegrityError("END-owed cold source changed its END input reference")
    end_chunk = read_bounded_retained_chunk(
        conn, session_id=session_id, operation_id=operation_id, key=end_key, allowance=contract.allowance
    )
    if (
        end_chunk is None
        or end_chunk.codec != "composer.checkpoint-end-input.v1"
        or len(end_chunk.payload) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
        or sha256(end_chunk.payload).hexdigest() != end_digest
    ):
        raise AuditIntegrityError("END-owed cold source changed its bounded END input")
    end_data = decode_checkpoint_end_input(
        PrivateChunk(
            PrivateChunkRef(codec="composer.checkpoint-end-input.v1", digest=end_digest, byte_length=len(end_chunk.payload)),
            end_chunk.payload.decode("utf-8", errors="strict"),
        ),
        env_ref_names=frozenset(value["end_env_ref_names"]),
    )
    if (
        end_data.session_id != session_id
        or end_data.operation_id != operation_id
        or end_data.original_checkpoint_id != prepared_event.get("source_checkpoint_id")
        or end_data.original_cursor != prepared_event.get("source_cursor")
        or end_data.outer_effect_key != value["outer_effect_key"]
        or end_data.pass_index != value["pass_index"]
        or end_data.retry_ordinal != value["retry_ordinal"]
        or end_data.runtime_chunks
        != tuple(
            chunk.reference for chunk in load_private_chunks(conn, session_id=session_id, operation_id=operation_id, stack=prepared_root)
        )
        or end_data.captured_runtime_sha256
        != stable_hash(
            decode_continuation_snapshot(prepared.payload_json)
            .model_copy(update={"cursor": end_data.original_cursor})
            .model_dump(mode="json")
        )
    ):
        raise AuditIntegrityError("END-owed cold source changed its complete gate input")
    original_phase = outer
    retry_history = None
    if value["retry_ordinal"] == 1:
        from elspeth.web.sessions.continuation_checkpoint_end_retry import read_end_format_retry_on_connection

        retry = read_end_format_retry_on_connection(
            conn,
            session_id=session_id,
            operation_id=operation_id,
            selected=outer,
            names=frozenset(value["end_env_ref_names"]),
        )
        retry_history = retry
        first_input = retry.owed.end_input
        retry_original_phase = retry.owed.phase_source
        if first_input is None or retry_original_phase is None:
            raise AuditIntegrityError("END-owed retry lost its original gate source")
        original_phase = retry_original_phase
        if (
            retry.marker.pass_index != end_data.pass_index
            or retry.marker.outer_effect_key != value["outer_effect_key"]
            or end_data.model_copy(
                update={
                    "original_checkpoint_id": first_input.original_checkpoint_id,
                    "original_cursor": first_input.original_cursor,
                    "captured_runtime_sha256": first_input.captured_runtime_sha256,
                    "runtime_chunks": first_input.runtime_chunks,
                    "retry_ordinal": 0,
                }
            )
            != first_input
        ):
            raise AuditIntegrityError("END-owed retry changed its original gate, pass or caller")
    hold_row = conn.execute(
        select(holds).where(
            holds.c.session_id == session_id,
            holds.c.operation_id == operation_id,
            holds.c.effect_key == value["effect_key"],
        )
    ).one_or_none()
    if (
        hold_row is None
        or prepared_event["creator_sequence"] != hold_row.creator_sequence
        or prepared_event["creator_generation"] != hold_row.creator_generation
    ):
        raise AuditIntegrityError("END-owed cold source lost its active original H")
    hold_fields = {field: hold_row._mapping[field] for field in ProviderTerminalHoldSource.__dataclass_fields__}
    hold = ProviderTerminalHoldSource(**hold_fields)
    if (
        hold_row.state != expected_hold_state
        or hold_row.provider_attempt_id != attempt_id
        or hold.source_input_sha256 != value["request_sha256"]
    ):
        raise AuditIntegrityError("END-owed cold source changed its pending original H")
    identity = PaidOverflowIdentity(
        session_id=session_id,
        operation_id=operation_id,
        effect_key=value["effect_key"],
        attempt_id=attempt_id,
        session_operation_id=hold.session_operation_id,
        session_operation_epoch=hold.session_operation_epoch,
        ownership_generation=hold.creator_generation,
        lease_sha256=hold.lease_sha256,
        policy_sha256=hold.policy_sha256,
        source_checkpoint_id=hold.source_checkpoint_id,
        source_cursor=hold.source_cursor,
        source_snapshot_sha256=hold.source_snapshot_sha256,
    )
    old_plan = prepare_checkpoint_sdk_terminal_hold_plan(
        identity=identity,
        admitted=original,
        phase="end",
        creator_sequence=hold.creator_sequence,
        requested_model=original_event["requested_model"],
        historical_contract_sha256=value["old_hold_contract_sha256"],
    )
    if (
        old_plan.contract_sha256 != value["old_hold_contract_sha256"]
        or old_plan.reserved_bytes != value["old_hold_reserved_bytes"]
        or hold.source_checkpoint_id != original_event.get("source_checkpoint_id")
        or hold.source_cursor != original_event.get("source_cursor")
    ):
        raise AuditIntegrityError("END-owed cold source lost its original pre-SDK H bridge")
    new_plan = prepare_checkpoint_paid_overflow_tail_plan(
        identity=identity, admitted=selected, phase="end", creator_sequence=hold.creator_sequence
    )
    if (
        hold.reserved_bytes != new_plan.reserved_bytes
        or hold.fallback_contract_sha256 != new_plan.fallback_contract_sha256
        or _require_hold_fields(conn, source=hold, contract=contract).state != expected_hold_state
        or (expected_hold_state == "active" and _require_active_provider_hold(conn, source=hold, contract=contract) != hold.reserved_bytes)
    ):
        raise AuditIntegrityError("END-owed cold source lost its new fixed terminal H bridge")
    response_chunk = read_bounded_retained_chunk(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        key=value["response_chunk_key"],
        allowance=contract.allowance,
    )
    if (
        response_chunk is None
        or response_chunk.codec != "composer.response.v1"
        or len(response_chunk.payload) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
        or sha256(response_chunk.payload).hexdigest() != value["response_sha256"]
    ):
        raise AuditIntegrityError("END-owed cold source lost its complete original response")
    response = PrivateChunk(
        PrivateChunkRef(
            codec="composer.response.v1",
            digest=sha256(response_chunk.payload).hexdigest(),
            byte_length=len(response_chunk.payload),
        ),
        response_chunk.payload.decode("utf-8", errors="strict"),
    )
    decoded = decode_continuation_response(response)
    call = decode_retained_physical_audit_call(response)
    attempt = conn.execute(select(physical).where(physical.c.attempt_id == attempt_id)).one_or_none()
    effect = conn.execute(
        select(effects).where(
            effects.c.session_id == session_id,
            effects.c.operation_id == operation_id,
            effects.c.effect_key == value["effect_key"],
        )
    ).one_or_none()
    if (
        private_chunk_key(response) != value["response_chunk_key"]
        or call.messages_hash != value["request_sha256"]
        or effect is None
        or effect.owner != "provider"
        or effect.disposition != (settled_child_disposition or "dispatched")
        or effect.provider_attempt_id != attempt_id
        or effect.input_sha256 != value["request_sha256"]
        or effect.requested_model != call.model_requested
        or effect.requested_model != original_event["requested_model"]
        or (effect.receipt_key is None) != (settled_child_disposition is None)
        or (effect.result_chunk_key is None) != (settled_child_disposition is None)
        or type(decoded.message.content) is not str
        or not decoded.message.content.strip()
        or attempt is None
        or (attempt.settled_at is None) != (settled_child_disposition is None)
        or (attempt.ledger_entry_id is None) != (settled_child_disposition is None)
        or attempt.source != "composer"
        or attempt.session_id != session_id
        or attempt.operation_id != hold.session_operation_id
        or attempt.operation_epoch != hold.session_operation_epoch
        or type(attempt.lease_token) is not str
        or sha256(attempt.lease_token.encode("utf-8")).hexdigest() != hold.lease_sha256
    ):
        raise AuditIntegrityError("END-owed cold response changed its original pending paid attempt")
    policy = decode_continuation_policy(owed_root.admission_policy_json, owed_root.policy_hash)
    if type(policy) is not ContinuationAdmissionPolicyV4 or policy.policy_hash != hold.policy_sha256:
        raise AuditIntegrityError("END-owed cold response lost its original V4 policy source")
    # The retained private response keeps complete original V4 metadata; the
    # physical audit intentionally stores only its bounded diagnostic view.
    # The explicit V4 comparator selects that view only after source proof.
    require_v4_successful_response_audit(
        call,
        decoded.provider_metadata,
        policy=policy,
        attempt_id=attempt_id,
        started_at=call.started_at,
        finished_at=call.finished_at,
    )
    return VerifiedCheckpointEndOwed(
        session_id,
        operation_id,
        attempt_id,
        value["effect_key"],
        value["outer_effect_key"],
        value["pass_index"],
        value["retry_ordinal"],
        value["request_sha256"],
        value["publication_binding"],
        selected,
        original,
        response,
        value["old_hold_contract_sha256"],
        hold,
        new_plan,
        original_phase,
        PrivateChunkRef(codec="composer.checkpoint-end-input.v1", digest=end_digest, byte_length=len(end_chunk.payload)),
        end_data,
        prepared,
        original_event["requested_model"],
        retry_history,
    )


def read_v4_checkpoint_end_owed_on_connection(
    conn: Connection, *, session_id: str, operation_id: str, attempt_id: str, names: frozenset[str] | None = None
) -> VerifiedCheckpointEndOwed | None:
    """Cold DATA read of one current pending original return; never a retry grant."""
    return _read_checkpoint_end_owed_on_connection(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        attempt_id=attempt_id,
        names=names,
        historical_owed_checkpoint_id=None,
        settled_child_disposition=None,
        expected_hold_state="active",
        expected_retry_ordinal=None,
    )


def read_v4_checkpoint_end_settled_owed_history_on_connection(
    conn: Connection,
    *,
    session_id: str,
    operation_id: str,
    attempt_id: str,
    owed_checkpoint_id: str,
    names: frozenset[str],
    child_disposition: Literal["committed", "paid_overflow"],
    hold_state: Literal["active", "consumed"] = "consumed",
    expected_retry_ordinal: Literal[0, 1] | None = None,
) -> VerifiedCheckpointEndOwed:
    """Cold proof of complete H after one exact child has settled.

    The caller must separately prove the parent transition from this H.  This
    reader never infers terminality or retry permission from a later head.
    """
    if type(owed_checkpoint_id) is not str or not owed_checkpoint_id or child_disposition not in ("committed", "paid_overflow"):
        raise TypeError("historical END-owed proof requires an exact head and settled child")
    result = _read_checkpoint_end_owed_on_connection(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        attempt_id=attempt_id,
        names=names,
        historical_owed_checkpoint_id=owed_checkpoint_id,
        settled_child_disposition=child_disposition,
        expected_hold_state=hold_state,
        expected_retry_ordinal=expected_retry_ordinal,
    )
    if result is None:
        raise AuditIntegrityError("historical END-owed proof lost its exact complete H")
    return result

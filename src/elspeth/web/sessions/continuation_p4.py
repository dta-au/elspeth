"""P4's actual same-transaction result and current-authorized historical read.

No decoder remints CommittedCheckpointWitness. Business writes retain their
preparing creator. A later current reader can verify the immutable receipt;
that does not authorize repeating its SQL or executing a provider call.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import Connection, select, update

from elspeth.contracts.composer_mutation import ComposerMutationCeiling, ComposerMutationRevocation
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.composer.audit_storage import redacted_tool_invocation_content_and_envelope
from elspeth.web.composer.continuation import EffectFrontierEntry, RetainedOwnerResultRef, ToolBatchFrame
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_response,
    decode_continuation_snapshot,
    encode_continuation_snapshot,
    encode_private_chunk,
    owned_json,
    verify_private_chunk,
)
from elspeth.web.composer.continuation_p4_batch import decode_p4_pending_batch_chunk
from elspeth.web.composer.continuation_p4_outcome import (
    RetainedAuditOutcome,
    decode_audit_outcome_data,
    encode_audit_outcome_data,
    historical_checkpoint_record,
)
from elspeth.web.composer.continuation_prepared_p4 import (
    PreparedP4Value,
    capture_prepared_p4,
    decode_prepared_p4,
    encode_prepared_p4,
    p4_semantic_input,
    prepared_p4_members,
)
from elspeth.web.composer.continuation_tool_batch import _decode_owned, _dump_owned, _read_json_value
from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence
from elspeth.web.composer.turn_effects import authored_state_hash
from elspeth.web.credential_guard import require_no_credential_material
from elspeth.web.sessions._persist_payload import AuditOutcome, RedactedToolRow, RejectionRecord
from elspeth.web.sessions.chat_message_decoder import chat_message_record_from_row
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    _OWED_P4_SCOPE_KEY,
    _OWED_P4_SEAL,
    ContinuationCASConflict,
    ContinuationEvent,
    ContinuationOwnerCommit,
    StoredContinuationSnapshot,
    ValidatedHistoricalEffectReference,
    _OwedP4Scope,
    _require_current_continuation_running,
    append_complete_snapshot,
    private_chunk_key,
    put_chunk,
    read_chunk,
    require_owner_commit,
    require_snapshot_cas,
    retain_action_execution_receipt_on_connection,
    validate_historical_effect_reference,
)
from elspeth.web.sessions.continuation_owner_results import PersistedComposerTurnReceipt
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import (
    chat_messages_table,
)
from elspeth.web.sessions.models import (
    composer_continuation_effects_table as effects,
)
from elspeth.web.sessions.models import (
    composer_segment_attempts_table as attempts,
)
from elspeth.web.sessions.models import (
    composition_rejection_events_table as rejections,
)
from elspeth.web.sessions.models import (
    composition_states_table as states,
)
from elspeth.web.sessions.state_decoder import state_record_from_row
from elspeth.web.sessions.time_normalization import restore_utc

_RECEIPT = TypeAdapter(PersistedComposerTurnReceipt)


@dataclass(frozen=True, slots=True)
class ReadmittedP4Result:
    """Checked historical DATA, not a current checkpoint witness or writer."""

    receipt: PersistedComposerTurnReceipt
    outcome: RetainedAuditOutcome
    historical_effect: ValidatedHistoricalEffectReference | None


def _selected_prepared(
    conn: Connection,
    stored: StoredContinuationSnapshot,
    *,
    running: ComposerOperationRunning,
    effect_key: str,
    env_ref_names: frozenset[str],
) -> PreparedP4Value:
    sid, operation_id = str(running.claim.session_id), running.claim.operation_id
    stack = decode_continuation_snapshot(stored.payload_json)
    frame = stack.frames[-1]
    if type(frame) is not ToolBatchFrame or effect_key != f"batch:{frame.response.digest}:p4":
        raise AuditIntegrityError("P4 owner lost its exact selected batch/effect")
    raw = read_chunk(
        conn, session_id=sid, operation_id=operation_id, key=f"value:{frame.accumulator.codec}:{frame.accumulator.digest}"
    ).decode("utf-8", errors="strict")
    data = decode_p4_pending_batch_chunk(PrivateChunk(frame.accumulator, raw), env_ref_names=env_ref_names)
    response_raw = read_chunk(
        conn, session_id=sid, operation_id=operation_id, key=f"value:{frame.response.codec}:{frame.response.digest}"
    ).decode("utf-8", errors="strict")
    response = decode_continuation_response(PrivateChunk(frame.response, response_raw))
    if data.calls != response.tool_batch.calls or frame.next_tool_index != data.batch.next_tool_index:
        raise AuditIntegrityError("P4 selected batch changed its original admitted response/cursor")
    if data.batch.p4_input_json is None:
        raise AuditIntegrityError("P4 selected batch lost its complete writer input")
    prepared = decode_prepared_p4(data.batch.p4_input_json, env_ref_names=env_ref_names)
    if (
        prepared.session_id != sid
        or prepared_p4_members(prepared, env_ref_names=env_ref_names).mutation_ceiling != stack.mutation_ceiling.materialize()
    ):
        raise AuditIntegrityError("P4 selected input changed its original logical mutation ceiling/session")
    return prepared


def require_selected_p4_input(
    conn: Connection,
    commit: ContinuationOwnerCommit,
    *,
    prepared: PreparedP4Value,
    context: SessionOperationContext,
    assistant_content: str,
    raw_content: str | None,
    redacted_assistant_tool_calls: tuple[Mapping[str, Any], ...],
    redacted_tool_rows: tuple[RedactedToolRow, ...],
    revoked_tool_rows: tuple[RedactedToolRow, ...],
    mutation_ceiling: ComposerMutationCeiling | None,
    authority_revocation: ComposerMutationRevocation | None,
    private_tool_evidence: tuple[ComposerDiagnosticEvidence, ...],
    private_tool_failures: tuple[ComposerDiagnosticEvidence, ...],
    rejection_records: tuple[RejectionRecord, ...],
    parent_composition_state_id: str | None,
    expected_current_state_id: str | None,
    plugin_crash_pending: bool,
    env_ref_names: frozenset[str] = frozenset(),
) -> PreparedP4Value:
    stored = require_owner_commit(conn, commit, context)
    selected = _selected_prepared(conn, stored, running=commit.running, effect_key=commit.effect_key, env_ref_names=env_ref_names)
    if (
        encode_prepared_p4(prepared, env_ref_names=env_ref_names) != encode_prepared_p4(selected, env_ref_names=env_ref_names)
        or selected.creator_operation_id != context.fence.operation_id
        or selected.creator_operation_epoch != context.fence.operation_epoch
    ):
        raise AuditIntegrityError("P4 writer changed its selected complete input or preparing creator")
    members = prepared_p4_members(selected, env_ref_names=env_ref_names)
    actual = capture_prepared_p4(
        context=context,
        assistant_content=assistant_content,
        raw_content=raw_content,
        redacted_calls=redacted_assistant_tool_calls,
        redacted_rows=redacted_tool_rows,
        revoked_rows=revoked_tool_rows,
        mutation_ceiling=mutation_ceiling,
        authority_revocation=authority_revocation,
        private_tool_evidence=private_tool_evidence,
        private_tool_failures=private_tool_failures,
        rejection_records=rejection_records,
        withheld_replies=members.withheld_replies,
        invocations=members.invocations,
        authorings=members.authorings,
        parent_composition_state_id=parent_composition_state_id,
        expected_current_state_id=expected_current_state_id,
        assistant_row_uses_current_dispatch=selected.assistant_row_uses_current_dispatch,
        crash_pending=plugin_crash_pending,
        env_ref_names=env_ref_names,
    )
    if (
        p4_semantic_input(actual, env_ref_names=env_ref_names) != p4_semantic_input(selected, env_ref_names=env_ref_names)
        or stable_hash(_read_json_value(p4_semantic_input(actual, env_ref_names=env_ref_names))) != commit.input_sha256
    ):
        raise AuditIntegrityError("P4 writer changed its complete selected semantic input")
    return selected


def _chat_row_hash(row: Any) -> str:
    record = chat_message_record_from_row(row)
    payload = {
        "id": str(record.id),
        "session_id": str(record.session_id),
        "role": record.role,
        "content": record.content,
        "raw_content": record.raw_content,
        "tool_calls": owned_json(record.tool_calls),
        "created_at": record.created_at.isoformat(),
        "writer_principal": record.writer_principal,
        "sequence_no": record.sequence_no,
        "composition_state_id": None if record.composition_state_id is None else str(record.composition_state_id),
        "tool_call_id": record.tool_call_id,
        "parent_assistant_id": None if record.parent_assistant_id is None else str(record.parent_assistant_id),
        "operation_id": None,
    }
    return sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _state_row_hash(row: Any) -> str:
    # Include DB-only provenance and creator as well as every public record
    # field. Hashing never applies redaction or turns equal graphs into an
    # interchangeable historical revision.
    state_record_from_row(row)
    return sha256(
        canonical_json(
            {
                "id": row.id,
                "session_id": row.session_id,
                "version": row.version,
                "source": row.source,
                "sources": row.sources,
                "nodes": row.nodes,
                "edges": row.edges,
                "outputs": row.outputs,
                "metadata_": row.metadata_,
                "is_valid": row.is_valid,
                "validation_errors": row.validation_errors,
                "composer_meta": row.composer_meta,
                "creator_operation_id": row.creator_operation_id,
                "created_at": restore_utc(row.created_at).isoformat(),
                "derived_from_state_id": row.derived_from_state_id,
                "provenance": row.provenance,
            }
        ).encode("utf-8")
    ).hexdigest()


def _rejection_row_hash(row: Any) -> str:
    return sha256(
        canonical_json(
            {
                "id": row.id,
                "session_id": row.session_id,
                "composition_state_id": row.composition_state_id,
                "tool_call_id": row.tool_call_id,
                "tool_name": row.tool_name,
                "error_code": row.error_code,
                "message": row.message,
                "planner_payload": row.planner_payload,
                "created_at": restore_utc(row.created_at).isoformat(),
            }
        ).encode("utf-8")
    ).hexdigest()


def decode_p4_receipt(
    encoded: str, *, env_ref_names: frozenset[str] = frozenset()
) -> tuple[PersistedComposerTurnReceipt, RetainedAuditOutcome]:
    receipt = _decode_owned(_RECEIPT, encoded)
    if receipt.schema_version != "composer.persisted-turn.v2" or receipt.prepared_p4_json is None or receipt.audit_outcome_json is None:
        raise AuditIntegrityError("P4 current recovery requires its complete actual writer receipt")
    prepared = decode_prepared_p4(receipt.prepared_p4_json, env_ref_names=env_ref_names)
    data = decode_audit_outcome_data(receipt.audit_outcome_json, prepared=prepared, env_ref_names=env_ref_names)
    checkpoint = data.checkpoint
    if (
        receipt.input_sha256 != stable_hash(_read_json_value(p4_semantic_input(prepared, env_ref_names=env_ref_names)))
        or receipt.creator_operation_id != prepared.creator_operation_id
        or (None if receipt.assistant_id is None else str(receipt.assistant_id)) != data.assistant_id
        or (None if receipt.current_state_id is None else str(receipt.current_state_id)) != data.current_state_id
        or receipt.checkpoint_disposition != data.checkpoint_disposition
        or receipt.unwind_audit_failed != data.unwind_audit_failed
        or receipt.inserted_state_ids != (() if checkpoint is None else checkpoint.inserted_state_ids)
    ):
        raise AuditIntegrityError("P4 receipt differs from its complete actual writer result/input")
    expected_hash = (
        None
        if checkpoint is None
        else authored_state_hash(state_from_record(historical_checkpoint_record(checkpoint, env_ref_names=env_ref_names)))
    )
    if receipt.authored_state_hash != expected_hash:
        raise AuditIntegrityError("P4 receipt changed its historical authored state hash")
    members = prepared_p4_members(prepared, env_ref_names=env_ref_names)
    if not data.unwind_audit_failed and (
        receipt.invocation_ids != tuple(item.tool_call_id for item in members.invocations)
        or receipt.invocation_hashes
        != tuple(
            sha256(canonical_json(redacted_tool_invocation_content_and_envelope(item)[1]).encode("utf-8")).hexdigest()
            for item in members.invocations
        )
        or receipt.withheld_hashes != tuple(sha256(item.content.encode("utf-8")).hexdigest() for item in members.withheld_replies)
    ):
        raise AuditIntegrityError("P4 receipt changed its actual full audit/withheld cohort")
    require_no_credential_material(
        [receipt.effect_key, receipt.creator_operation_id, *receipt.invocation_ids, *receipt.rejection_ids],
        surface="retained P4 receipt identities",
        env_ref_names=env_ref_names,
    )
    if (
        len(receipt.state_row_hashes) != len(receipt.inserted_state_ids)
        or len(set(receipt.inserted_state_ids)) != len(receipt.inserted_state_ids)
        or len(set(receipt.rejection_ids)) != len(receipt.rejection_ids)
        or len(receipt.chat_message_hashes) != len(receipt.chat_message_ids)
        or len(receipt.rejection_hashes) != len(receipt.rejection_ids)
    ):
        raise AuditIntegrityError("P4 receipt lost its exact stored row bindings")
    for digest in (
        receipt.input_sha256,
        *receipt.state_row_hashes,
        *receipt.chat_message_hashes,
        *receipt.rejection_hashes,
        *receipt.invocation_hashes,
        *receipt.withheld_hashes,
    ):
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise AuditIntegrityError("P4 receipt lost a complete canonical digest")
    return receipt, data


def retain_p4_result_on_connection(
    conn: Connection,
    commit: ContinuationOwnerCommit,
    *,
    prepared: PreparedP4Value,
    outcome: AuditOutcome,
    base_sequence: int | None,
    created_at: datetime | None,
    env_ref_names: frozenset[str] = frozenset(),
) -> PersistedComposerTurnReceipt:
    stored = require_owner_commit(conn, commit, commit.running.session_operation_context)
    selected = _selected_prepared(conn, stored, running=commit.running, effect_key=commit.effect_key, env_ref_names=env_ref_names)
    if encode_prepared_p4(selected, env_ref_names=env_ref_names) != encode_prepared_p4(prepared, env_ref_names=env_ref_names):
        raise AuditIntegrityError("P4 retention changed its selected prepared input")
    sid, operation_id = str(commit.running.claim.session_id), commit.running.claim.operation_id
    members = prepared_p4_members(prepared, env_ref_names=env_ref_names)
    audit_json = encode_audit_outcome_data(outcome, prepared=prepared, env_ref_names=env_ref_names)
    count = (
        1
        + 2 * len(outcome.published_tool_rows)
        + len(members.private_tool_failures)
        + len(members.invocations)
        + len(members.withheld_replies)
    )
    if outcome.unwind_audit_failed:
        if base_sequence is not None or created_at is not None:
            raise AuditIntegrityError("unavailable P4 supplied a committed sequence window")
        rows, refusal_rows = [], []
    else:
        if base_sequence is None or created_at is None:
            raise AuditIntegrityError("committed P4 lost its exact sequence window")
        rows = list(
            conn.execute(
                select(chat_messages_table)
                .where(
                    chat_messages_table.c.session_id == sid,
                    chat_messages_table.c.sequence_no >= base_sequence,
                    chat_messages_table.c.sequence_no < base_sequence + count,
                )
                .order_by(chat_messages_table.c.sequence_no)
            ).all()
        )
        if len(rows) != count or any(row.writer_principal != "compose_loop" for row in rows):
            raise AuditIntegrityError("P4 retention lost an actual inserted transcript row")
        refusal_rows = (
            list(
                conn.execute(
                    select(rejections)
                    .where(
                        rejections.c.session_id == sid,
                        rejections.c.created_at == created_at,
                        rejections.c.tool_call_id.in_(tuple(item.tool_call_id for item in members.rejection_records)),
                    )
                    .order_by(rejections.c.id)
                ).all()
            )
            if members.rejection_records
            else []
        )
        if len(refusal_rows) != len(members.rejection_records):
            raise AuditIntegrityError("P4 retention lost an actual rejection row")
    checkpoint = outcome.checkpoint
    state_rows = [
        conn.execute(select(states).where(states.c.session_id == sid, states.c.id == str(state_id))).one()
        for state_id in (() if checkpoint is None else checkpoint.inserted_state_ids)
    ]
    if any(row.creator_operation_id != prepared.creator_operation_id for row in state_rows):
        raise AuditIntegrityError("P4 receipt substituted a foreign state creator")
    receipt = PersistedComposerTurnReceipt(
        state_row_hashes=tuple(_state_row_hash(row) for row in state_rows),
        schema_version="composer.persisted-turn.v2",
        effect_key=commit.effect_key,
        input_sha256=commit.input_sha256,
        assistant_id=None if outcome.assistant_id is None else UUID(outcome.assistant_id),
        current_state_id=None if outcome.current_state_id is None else UUID(outcome.current_state_id),
        checkpoint_disposition=outcome.checkpoint_disposition,
        creator_operation_id=prepared.creator_operation_id,
        inserted_state_ids=() if checkpoint is None else checkpoint.inserted_state_ids,
        authored_state_hash=None if checkpoint is None else authored_state_hash(state_from_record(checkpoint.state)),
        chat_message_ids=tuple(UUID(row.id) for row in rows),
        chat_message_hashes=tuple(_chat_row_hash(row) for row in rows),
        rejection_ids=tuple(row.id for row in refusal_rows),
        rejection_hashes=tuple(_rejection_row_hash(row) for row in refusal_rows),
        prepared_p4_json=encode_prepared_p4(prepared, env_ref_names=env_ref_names),
        audit_outcome_json=audit_json,
        unwind_audit_failed=outcome.unwind_audit_failed,
        invocation_ids=() if outcome.unwind_audit_failed else tuple(item.tool_call_id for item in members.invocations),
        invocation_hashes=()
        if outcome.unwind_audit_failed
        else tuple(
            sha256(canonical_json(redacted_tool_invocation_content_and_envelope(item)[1]).encode("utf-8")).hexdigest()
            for item in members.invocations
        ),
        withheld_hashes=()
        if outcome.unwind_audit_failed
        else tuple(sha256(item.content.encode("utf-8")).hexdigest() for item in members.withheld_replies),
    )
    encoded = _dump_owned(_RECEIPT, receipt)
    decode_p4_receipt(encoded, env_ref_names=env_ref_names)
    chunk = encode_private_chunk(codec="composer.owner-result.v1", canonical=encoded, env_ref_names=env_ref_names)
    key = private_chunk_key(chunk)
    put_chunk(conn, session_id=sid, operation_id=operation_id, key=key, payload=encoded.encode("utf-8"), codec=chunk.reference.codec)
    receipt_id = str(receipt.assistant_id) if receipt.assistant_id is not None else f"unavailable:{commit.effect_key}"
    changed = conn.execute(
        update(effects)
        .where(
            effects.c.session_id == sid,
            effects.c.operation_id == operation_id,
            effects.c.effect_key == commit.effect_key,
            effects.c.disposition == "prepared",
            effects.c.owner == "session_sql",
            effects.c.input_sha256 == commit.input_sha256,
            effects.c.creator_sequence == commit.running.claim.segment_sequence,
            effects.c.creator_generation == commit.running.claim.ownership_generation,
        )
        .values(
            disposition="committed",
            receipt_key=receipt_id,
            result_chunk_key=key,
            graph_sha256=receipt.authored_state_hash,
            committed_state_ids_json=canonical_json([str(item) for item in receipt.inserted_state_ids])
            if receipt.inserted_state_ids
            else None,
        )
    )
    if changed.rowcount != 1:
        raise ContinuationCASConflict("P4 owner result lost its actual preparing effect")
    stack = decode_continuation_snapshot(stored.payload_json)
    if any(item.effect_key == commit.effect_key for item in stack.pending_owner_results):
        raise AuditIntegrityError("P4 owner result is already pending application")
    stack = stack.model_copy(
        update={
            "pending_owner_results": (
                *stack.pending_owner_results,
                RetainedOwnerResultRef(effect_key=commit.effect_key, kind="persisted_turn", result=chunk.reference),
            ),
            "effect_frontier": (
                *tuple(item for item in stack.effect_frontier if item.effect_id != commit.effect_key),
                EffectFrontierEntry(effect_id=commit.effect_key, disposition="committed", receipt_id=receipt_id),
            ),
        }
    )
    snapshot = retain_action_execution_receipt_on_connection(
        conn,
        session_id=sid,
        operation_id=operation_id,
        effect_key=commit.effect_key,
        disposition="committed",
        snapshot_json=encode_continuation_snapshot(stack),
    )
    append_complete_snapshot(
        conn,
        session_id=sid,
        operation_id=operation_id,
        expected_checkpoint_id=stored.checkpoint_id,
        expected_cursor=stored.cursor,
        snapshot_json=snapshot,
        event_batch=(
            ContinuationEvent(
                "effect_committed",
                canonical_json(
                    {"effect_key": commit.effect_key, "kind": "persisted_turn", "receipt_id": receipt_id, "phase": "owner_result_retained"}
                ),
            ),
        ),
        handoff=False,
    )
    return receipt


def read_p4_result_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    expected_checkpoint_id: str,
    expected_cursor: int,
    effect_key: str,
    env_ref_names: frozenset[str] = frozenset(),
) -> ReadmittedP4Result:
    _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
    sid, operation_id = str(running.claim.session_id), running.claim.operation_id
    stored = require_snapshot_cas(
        conn, session_id=sid, operation_id=operation_id, expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor
    )
    stack = decode_continuation_snapshot(stored.payload_json)
    pending = tuple(item for item in stack.pending_owner_results if item.effect_key == effect_key)
    if len(pending) != 1 or pending[0].kind != "persisted_turn":
        raise AuditIntegrityError("P4 recovery has no exact selected pending result")
    effect = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == effect_key)
    ).one()
    ref = pending[0].result
    if effect.owner != "session_sql" or effect.disposition != "committed" or effect.result_chunk_key != f"value:{ref.codec}:{ref.digest}":
        raise AuditIntegrityError("P4 recovery changed its actual committed effect/result")
    encoded = read_chunk(conn, session_id=sid, operation_id=operation_id, key=effect.result_chunk_key).decode("utf-8", errors="strict")
    verify_private_chunk(ref, encoded)
    receipt, data = decode_p4_receipt(encoded, env_ref_names=env_ref_names)
    selected = _selected_prepared(conn, stored, running=running, effect_key=effect_key, env_ref_names=env_ref_names)
    return verify_p4_immutable_receipt_on_connection(
        conn,
        session_id=running.claim.session_id,
        operation_id=operation_id,
        effect_key=effect_key,
        input_sha256=effect.input_sha256,
        receipt_key=effect.receipt_key,
        creator_sequence=effect.creator_sequence,
        creator_generation=effect.creator_generation,
        receipt=receipt,
        data=data,
        selected=selected,
        env_ref_names=env_ref_names,
    )


def verify_p4_immutable_receipt_on_connection(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    effect_key: str,
    input_sha256: str,
    receipt_key: str | None,
    creator_sequence: int,
    creator_generation: int,
    receipt: PersistedComposerTurnReceipt,
    data: RetainedAuditOutcome,
    selected: PreparedP4Value,
    env_ref_names: frozenset[str],
) -> ReadmittedP4Result:
    """Verify immutable receipt rows; callers separately prove current selection."""
    sid = str(session_id)
    creator = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation_id,
            attempts.c.sequence == creator_sequence,
            attempts.c.generation == creator_generation,
        )
    ).one()
    if (
        receipt.effect_key != effect_key
        or receipt.input_sha256 != input_sha256
        or receipt.prepared_p4_json != encode_prepared_p4(selected, env_ref_names=env_ref_names)
        or receipt.creator_operation_id != creator.session_operation_id
        or selected.creator_operation_epoch != creator.session_operation_epoch
        or receipt_key != (str(receipt.assistant_id) if receipt.assistant_id is not None else f"unavailable:{effect_key}")
    ):
        raise AuditIntegrityError("P4 receipt lost its original preparing/committing owner")
    rows = (
        list(
            conn.execute(
                select(chat_messages_table)
                .where(
                    chat_messages_table.c.session_id == sid,
                    chat_messages_table.c.id.in_(tuple(str(item) for item in receipt.chat_message_ids)),
                )
                .order_by(chat_messages_table.c.sequence_no)
            ).all()
        )
        if receipt.chat_message_ids
        else []
    )
    refusal_rows = (
        list(
            conn.execute(
                select(rejections)
                .where(rejections.c.session_id == sid, rejections.c.id.in_(receipt.rejection_ids))
                .order_by(rejections.c.id)
            ).all()
        )
        if receipt.rejection_ids
        else []
    )
    if (
        tuple(UUID(row.id) for row in rows) != receipt.chat_message_ids
        or tuple(_chat_row_hash(row) for row in rows) != receipt.chat_message_hashes
        or tuple(row.id for row in refusal_rows) != receipt.rejection_ids
        or tuple(_rejection_row_hash(row) for row in refusal_rows) != receipt.rejection_hashes
    ):
        raise AuditIntegrityError("P4 recovery changed actual transcript/rejection rows")
    state_rows = [
        conn.execute(select(states).where(states.c.session_id == sid, states.c.id == str(state_id))).one()
        for state_id in receipt.inserted_state_ids
    ]
    if tuple(_state_row_hash(row) for row in state_rows) != receipt.state_row_hashes:
        raise AuditIntegrityError("P4 recovery changed complete inserted state history")
    if data.checkpoint is not None and (
        not state_rows
        or state_record_from_row(state_rows[-1]) != historical_checkpoint_record(data.checkpoint, env_ref_names=env_ref_names)
    ):
        raise AuditIntegrityError("P4 historical checkpoint differs from its actual full immutable row")
    historical = None
    if data.checkpoint_disposition == "committed":
        if receipt.authored_state_hash is None or receipt.assistant_id is None:
            raise AuditIntegrityError("P4 committed result lost its graph reference")
        historical = validate_historical_effect_reference(
            conn,
            session_id=session_id,
            operation_id=operation_id,
            effect_key=effect_key,
            expected_input_sha256=receipt.input_sha256,
            expected_creator_operation_id=receipt.creator_operation_id,
            expected_receipt_id=str(receipt.assistant_id),
            expected_inserted_state_ids=receipt.inserted_state_ids,
            expected_authored_state_hash=receipt.authored_state_hash,
        )
    return ReadmittedP4Result(receipt, data, historical)


@contextmanager
def selected_p4_drain_scope(
    conn: Connection,
    commit: ContinuationOwnerCommit | None,
    prepared: PreparedP4Value | None,
    *,
    env_ref_names: frozenset[str] = frozenset(),
) -> Iterator[None]:
    """Drain only the exact issued same-creator P4 under a live session fence.

    Uses the existing RootTransaction-bound owed-P4 owner. Cancellation or an
    elapsed segment cannot expand this to another effect or stale session lease.
    """
    if commit is None:
        if prepared is not None:
            raise AuditIntegrityError("P4 drain input has no actual commit owner")
        yield
        return
    if prepared is None or type(commit) is not ContinuationOwnerCommit:
        raise AuditIntegrityError("P4 drain lost its actual selected owner/input")
    _require_current_continuation_running(conn, commit.running, allow_cancel=True, allow_expired_segment=True)
    claim = commit.running.claim
    sid, operation_id = str(claim.session_id), claim.operation_id
    stored = require_snapshot_cas(
        conn,
        session_id=sid,
        operation_id=operation_id,
        expected_checkpoint_id=commit.expected_checkpoint_id,
        expected_cursor=commit.expected_cursor,
    )
    selected = _selected_prepared(conn, stored, running=commit.running, effect_key=commit.effect_key, env_ref_names=env_ref_names)
    effect = conn.execute(
        select(effects).where(
            effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == commit.effect_key
        )
    ).one()
    if (
        effect.owner != "session_sql"
        or effect.disposition != "prepared"
        or (effect.creator_sequence, effect.creator_generation) != (claim.segment_sequence, claim.ownership_generation)
        or effect.input_sha256 != commit.input_sha256
        or commit.input_sha256 != stable_hash(_read_json_value(p4_semantic_input(selected, env_ref_names=env_ref_names)))
        or encode_prepared_p4(prepared, env_ref_names=env_ref_names) != encode_prepared_p4(selected, env_ref_names=env_ref_names)
        or selected.creator_operation_id != commit.running.session_operation_context.fence.operation_id
        or selected.creator_operation_epoch != commit.running.session_operation_context.fence.operation_epoch
    ):
        raise AuditIntegrityError("P4 drain changed its original issued business-SQL owner/input")
    if _OWED_P4_SCOPE_KEY in conn.info:
        raise AuditIntegrityError("P4 drain cannot replace another owned scope")
    scope = _OwedP4Scope(conn, commit.running.session_operation_context, _OWED_P4_SEAL)
    conn.info[_OWED_P4_SCOPE_KEY] = scope
    try:
        yield
    finally:
        if conn.info.pop(_OWED_P4_SCOPE_KEY, None) is not scope:
            raise AuditIntegrityError("P4 drain scope changed during its actual transaction")


def prepare_p4_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    expected_checkpoint_id: str,
    expected_cursor: int,
    pending_batch: PrivateChunk,
    env_ref_names: frozenset[str] = frozenset(),
) -> ContinuationOwnerCommit:
    """Prepare only the exact selected completed batch's first P4 effect.

    The original provider response and every coupled data member remain fixed.
    This cannot install a new response, run a tool or authorize a foreign creator.
    """
    from dataclasses import replace

    from sqlalchemy import insert

    from elspeth.web.composer.continuation_batch_data import decode_coupled_batch_chunk, encode_coupled_batch_data
    from elspeth.web.composer.continuation_policy import decode_continuation_policy
    from elspeth.web.sessions.continuation import bound_action_json
    from elspeth.web.sessions.models import composer_operation_segments_table as segments

    _require_current_continuation_running(conn, running)
    sid, operation_id = str(running.claim.session_id), running.claim.operation_id
    stored = require_snapshot_cas(
        conn, session_id=sid, operation_id=operation_id, expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor
    )
    stack = decode_continuation_snapshot(stored.payload_json)
    frame = stack.frames[-1]
    if type(frame) is not ToolBatchFrame or frame.phase != "tool_ready":
        raise AuditIntegrityError("P4 preparation requires the actual selected completed batch")
    old_raw = read_chunk(
        conn, session_id=sid, operation_id=operation_id, key=f"value:{frame.accumulator.codec}:{frame.accumulator.digest}"
    ).decode("utf-8", errors="strict")
    old = decode_coupled_batch_chunk(PrivateChunk(frame.accumulator, old_raw), env_ref_names=env_ref_names)
    pending = decode_p4_pending_batch_chunk(pending_batch, env_ref_names=env_ref_names)
    if old.batch.phase != "dispatch_ready" or pending.batch.p4_input_json is None:
        raise AuditIntegrityError("P4 preparation cannot skip remaining admitted tool work")
    unchanged = replace(pending.batch, phase="dispatch_ready", p4_input_json=None)
    if (
        encode_coupled_batch_data(batch=unchanged, accumulator=pending.accumulator, calls=pending.calls, env_ref_names=env_ref_names)
        != old_raw
    ):
        raise AuditIntegrityError("P4 preparation changed actual completed batch data/aliases")
    response_raw = read_chunk(
        conn, session_id=sid, operation_id=operation_id, key=f"value:{frame.response.codec}:{frame.response.digest}"
    ).decode("utf-8", errors="strict")
    response = decode_continuation_response(PrivateChunk(frame.response, response_raw))
    if response.tool_batch.calls != pending.calls or frame.next_tool_index != pending.batch.next_tool_index:
        raise AuditIntegrityError("P4 preparation substituted its actual issued response/prefix")
    prepared = decode_prepared_p4(pending.batch.p4_input_json, env_ref_names=env_ref_names)
    context = running.session_operation_context
    if (
        prepared.session_id != sid
        or prepared.creator_operation_id != context.fence.operation_id
        or prepared.creator_operation_epoch != context.fence.operation_epoch
        or prepared_p4_members(prepared, env_ref_names=env_ref_names).mutation_ceiling != stack.mutation_ceiling.materialize()
    ):
        raise AuditIntegrityError("P4 preparation requires its actual current creator")
    key = f"batch:{frame.response.digest}:p4"
    input_hash = stable_hash(_read_json_value(p4_semantic_input(prepared, env_ref_names=env_ref_names)))
    if (
        conn.execute(
            select(effects.c.effect_key).where(
                effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == key
            )
        ).one_or_none()
        is not None
    ):
        raise ContinuationCASConflict("P4 already prepared; read its actual retained disposition")
    put_chunk(
        conn,
        session_id=sid,
        operation_id=operation_id,
        key=private_chunk_key(pending_batch),
        payload=pending_batch.canonical.encode("utf-8"),
        codec=pending_batch.reference.codec,
    )
    action = bound_action_json(
        stack,
        action_owner="primary_turn" if stack.active_action_json is not None else None,
        read_tool_name=None,
        read_tool_arguments_json=None,
    )
    conn.execute(
        insert(effects).values(
            session_id=sid,
            operation_id=operation_id,
            effect_key=key,
            input_sha256=input_hash,
            owner="session_sql",
            disposition="prepared",
            creator_sequence=running.claim.segment_sequence,
            creator_generation=running.claim.ownership_generation,
            action_json=action,
        )
    )
    next_frame = frame.model_copy(update={"phase": "effect_reconciling", "accumulator": pending_batch.reference})
    next_stack = stack.model_copy(
        update={
            "frames": (*stack.frames[:-1], next_frame),
            "effect_frontier": (*stack.effect_frontier, EffectFrontierEntry(effect_id=key, disposition="prepared", receipt_id=None)),
        }
    )
    policy = decode_continuation_policy(stack.admission_policy_json, stack.policy_hash)
    conn.execute(
        update(segments)
        .where(
            segments.c.session_id == sid,
            segments.c.operation_id == operation_id,
            segments.c.sequence == running.claim.segment_sequence,
            segments.c.generation == running.claim.ownership_generation,
        )
        .values(next_action_json=policy.actions.session_sql.model_dump_json(), policy_sha256=stack.policy_hash)
    )
    snapshot = retain_action_execution_receipt_on_connection(
        conn,
        session_id=sid,
        operation_id=operation_id,
        effect_key=key,
        disposition="prepared",
        snapshot_json=encode_continuation_snapshot(next_stack),
    )
    selected = append_complete_snapshot(
        conn,
        session_id=sid,
        operation_id=operation_id,
        expected_checkpoint_id=stored.checkpoint_id,
        expected_cursor=stored.cursor,
        snapshot_json=snapshot,
        event_batch=(ContinuationEvent("tool_prepared", canonical_json({"effect_key": key, "input_sha256": input_hash})),),
        handoff=False,
    )
    return ContinuationOwnerCommit(running, key, input_hash, selected.checkpoint_id, selected.cursor)

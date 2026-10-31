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
from typing import TYPE_CHECKING, Any, Literal
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
    historical_checkpoint_columns_json,
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

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime
    from elspeth.web.composer.continuation_terminal_p4_data import TerminalP4Document, TerminalP4Input
    from elspeth.web.composer.continuation_tool_failure import TerminalServiceCompletion
    from elspeth.web.sessions.continuation_p4_application_store import (
        StoredP4Application,
        StoredTerminalP4Application,
        StoredTerminalP4Preparation,
    )
from elspeth.web.sessions.chat_message_decoder import chat_message_record_from_row
from elspeth.web.sessions.composer_operations import ComposerOperationError, ComposerOperationRunning
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
    load_snapshot,
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


@dataclass(slots=True)
class P4WriterObservation:
    """Actual committing owner's candidate; DATA until committed readback."""

    commit: ContinuationOwnerCommit
    prepared: PreparedP4Value
    candidate: AuditOutcome | None = None
    original_error: BaseException | None = None
    readback: ReadmittedP4Result | None = None
    known_not_committed: bool = False
    committed: bool = False
    application_candidate: StoredP4Application | StoredTerminalP4Application | None = None
    failed_candidates: tuple[AuditOutcome, ...] = ()
    prior_errors: tuple[BaseException, ...] = ()


def read_p4_transaction_outcome_on_connection(
    conn: Connection, observation: P4WriterObservation, *, env_ref_names: frozenset[str]
) -> AuditOutcome | None:
    """Resolve a transaction error under the same real session lock.

    The original transaction must surrender its advisory lock before this read
    can finish. A receipt is compared to the actual candidate object retained
    by that committing owner; historical DATA never remints its witness.
    """
    if type(observation) is not P4WriterObservation or type(observation.commit) is not ContinuationOwnerCommit:
        raise AuditIntegrityError("P4 transaction readback lost its actual observation owner")
    commit = observation.commit
    running = commit.running
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    stored = load_snapshot(conn, session_id=sid, operation_id=operation)
    from elspeth.web.sessions.models import composer_async_operations_table as jobs
    from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
    from elspeth.web.sessions.models import composer_continuation_events_table as events
    from elspeth.web.sessions.models import sessions_table

    original_row = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation,
            checkpoints.c.checkpoint_id == commit.expected_checkpoint_id,
        )
    ).one_or_none()
    if original_row is None or original_row.cursor != commit.expected_cursor:
        raise AuditIntegrityError("P4 readback lost its actual original prepared checkpoint")
    original = StoredContinuationSnapshot(
        read_chunk(conn, session_id=sid, operation_id=operation, key=original_row.snapshot_chunk_key).decode("utf-8", errors="strict"),
        original_row.cursor,
        original_row.checkpoint_id,
    )
    prepared = _selected_prepared(conn, original, running=running, effect_key=commit.effect_key, env_ref_names=env_ref_names)
    if encode_prepared_p4(prepared, env_ref_names=env_ref_names) != encode_prepared_p4(observation.prepared, env_ref_names=env_ref_names):
        raise AuditIntegrityError("P4 transaction readback changed its complete original input")
    effect = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == commit.effect_key)
    ).one()
    if effect.disposition == "prepared":
        _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
        if (
            stored.checkpoint_id != commit.expected_checkpoint_id
            or stored.cursor != commit.expected_cursor
            or effect.receipt_key is not None
            or effect.result_chunk_key is not None
            or effect.input_sha256 != commit.input_sha256
            or effect.owner != "session_sql"
            or (effect.creator_sequence, effect.creator_generation) != (running.claim.segment_sequence, running.claim.ownership_generation)
        ):
            raise AuditIntegrityError("uncommitted P4 readback changed its original prepared owner")
        candidate = observation.candidate
        if (
            candidate is not None
            and candidate.assistant_id is not None
            and conn.execute(select(chat_messages_table.c.id).where(chat_messages_table.c.id == candidate.assistant_id)).one_or_none()
            is not None
        ):
            raise AuditIntegrityError("P4 transaction has published rows without its atomic receipt")
        observation.known_not_committed = True
        return None
    candidate = observation.candidate
    application = observation.application_candidate
    if candidate is None or application is None:
        raise AuditIntegrityError("committed P4 lacks its actual committing owner's two candidates")
    from elspeth.web.sessions.continuation_p4_application_store import StoredP4Application, StoredTerminalP4Application

    if type(application) is StoredTerminalP4Application:
        return _read_terminal_p4_transaction_outcome(conn, observation, application, env_ref_names=env_ref_names)
    if type(application) is not StoredP4Application:
        raise AuditIntegrityError("P4 readback replaced its closed application owner")
    # This is historical outcome readback of this physical invocation, not a
    # current mutation boundary. Exact immutable creator/source joins tolerate
    # legitimate subsequent head/generation progress without granting its fence.
    creator = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation,
            attempts.c.sequence == running.claim.segment_sequence,
            attempts.c.generation == running.claim.ownership_generation,
        )
    ).one_or_none()
    source = application.bundle.evidence_after.snapshot
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one_or_none()
    principal = conn.execute(select(sessions_table.c.user_id).where(sessions_table.c.id == sid)).one_or_none()
    current_root = decode_continuation_snapshot(stored.payload_json)
    original_root = decode_continuation_snapshot(original.payload_json)
    if (
        creator is None
        or parent is None
        or principal is None
        or creator.session_operation_id != running.session_operation_context.fence.operation_id
        or creator.session_operation_epoch != running.session_operation_context.fence.operation_epoch
        or creator.claim_attempt != running.claim.attempt
        or parent.lifecycle_version != 2
        or parent.actor_user_id != source.actor_user_id
        or principal.user_id != source.actor_user_id
        or source.physical_operation_id != creator.session_operation_id
        or source.physical_operation_epoch != creator.session_operation_epoch
        or current_root.logical != original_root.logical
        or source.logical != original_root.logical
        or effect.owner != "session_sql"
        or effect.disposition != "committed"
        or effect.input_sha256 != commit.input_sha256
        or (effect.creator_sequence, effect.creator_generation) != (creator.sequence, creator.generation)
    ):
        raise AuditIntegrityError("P4 historical readback changed its actual creator/principal/source")
    record = application.bundle.entry.applications[-1]
    result_chunk = application.bundle.evidence_after.sources.chunk(record.result)
    if effect.result_chunk_key != private_chunk_key(result_chunk):
        raise AuditIntegrityError("P4 readback changed its actual receipt source")
    if read_chunk(conn, session_id=sid, operation_id=operation, key=effect.result_chunk_key) != result_chunk.canonical.encode("utf-8"):
        raise AuditIntegrityError("P4 readback changed its original receipt bytes")
    receipt, data = decode_p4_receipt(result_chunk.canonical, env_ref_names=env_ref_names)
    result = verify_p4_immutable_receipt_on_connection(
        conn,
        session_id=running.claim.session_id,
        operation_id=operation,
        effect_key=commit.effect_key,
        input_sha256=commit.input_sha256,
        receipt_key=effect.receipt_key,
        creator_sequence=creator.sequence,
        creator_generation=creator.generation,
        receipt=receipt,
        data=data,
        selected=prepared,
        env_ref_names=env_ref_names,
    )
    actual = decode_audit_outcome_data(
        encode_audit_outcome_data(candidate, prepared=prepared, env_ref_names=env_ref_names),
        prepared=prepared,
        env_ref_names=env_ref_names,
    )
    if actual != result.outcome:
        raise AuditIntegrityError("committed P4 differs from its retained actual writer outcome")
    applied = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation,
            checkpoints.c.checkpoint_id == application.snapshot.checkpoint_id,
        )
    ).one_or_none()
    event = conn.execute(
        select(events).where(
            events.c.session_id == sid,
            events.c.operation_id == operation,
            events.c.sequence == application.snapshot.cursor,
        )
    ).one_or_none()
    if (
        applied is None
        or applied.cursor != application.snapshot.cursor
        or applied.cursor > stored.cursor
        or record.effect_key != commit.effect_key
        or record.input_sha256 != commit.input_sha256
        or event is None
        or event.tag != "checkpoint"
        or read_chunk(conn, session_id=sid, operation_id=operation, key=applied.snapshot_chunk_key).decode("utf-8", errors="strict")
        != application.snapshot.payload_json
    ):
        raise AuditIntegrityError("P4 receipt has no exact durable complete application")
    expected_event = canonical_json(
        {
            "phase": "p4_application_committed",
            "effect_key": commit.effect_key,
            "selected_checkpoint_id": record.checkpoint_id,
            "selected_cursor": record.cursor,
            "evidence_digest": application.bundle.evidence_chunk.reference.digest,
            "batch_digest": application.bundle.batch_chunk.reference.digest,
        }
    ).encode("utf-8")
    if read_chunk(conn, session_id=sid, operation_id=operation, key=event.payload_chunk_key) != expected_event:
        raise AuditIntegrityError("P4 readback changed its actual application event/predecessor")
    predecessor = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation,
            checkpoints.c.checkpoint_id == record.checkpoint_id,
        )
    ).one_or_none()
    if predecessor is None or predecessor.cursor != record.cursor or predecessor.cursor + 1 != applied.cursor:
        raise AuditIntegrityError("P4 readback lost its exact committed pending predecessor")
    pending_root = decode_continuation_snapshot(
        read_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=predecessor.snapshot_chunk_key,
        ).decode("utf-8", errors="strict")
    )
    if (
        pending_root.logical != original_root.logical
        or len(pending_root.pending_owner_results) != 1
        or pending_root.pending_owner_results[0].effect_key != commit.effect_key
        or pending_root.pending_owner_results[0].result != record.result
    ):
        raise AuditIntegrityError("P4 readback changed its pending result's original source relation")
    for chunk in (
        application.bundle.evidence_chunk,
        application.bundle.batch_chunk,
        application.bundle.audit_chunk,
        application.bundle.state_chunk,
        *application.bundle.evidence_after.sources.chunks,
    ):
        if read_chunk(conn, session_id=sid, operation_id=operation, key=private_chunk_key(chunk)) != chunk.canonical.encode("utf-8"):
            raise AuditIntegrityError("P4 readback changed its exact committed successor dependency")
    observation.readback, observation.committed = result, True
    return candidate


def _selected_prepared(
    conn: Connection,
    stored: StoredContinuationSnapshot,
    *,
    running: ComposerOperationRunning,
    effect_key: str,
    env_ref_names: frozenset[str],
) -> PreparedP4Value:
    from elspeth.web.composer.continuation_terminal_p4_data import decode_terminal_p4_input

    frame = decode_continuation_snapshot(stored.payload_json).frames[-1]
    if type(frame) is ToolBatchFrame and frame.accumulator.codec == "composer.tool-batch.v1":
        terminal = read_terminal_p4_source_on_connection(
            conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, env_ref_names=env_ref_names
        )
        if terminal is None:
            raise AuditIntegrityError("issued P4 source lost its mandatory terminal input")
        value = decode_terminal_p4_input(terminal.input_document, env_ref_names=env_ref_names)
        if (
            value.checkpoint_id != stored.checkpoint_id
            or value.cursor != stored.cursor
            or effect_key != f"batch:{value.issue.response.digest}:p4"
        ):
            raise AuditIntegrityError("terminal P4 selected a different prepared source")
        return value.prepared
    return selected_prepared_p4_data_on_connection(
        conn,
        stored,
        session_id=running.claim.session_id,
        operation_id=running.claim.operation_id,
        effect_key=effect_key,
        env_ref_names=env_ref_names,
    )


def selected_prepared_p4_data_on_connection(
    conn: Connection,
    stored: StoredContinuationSnapshot,
    *,
    session_id: UUID,
    operation_id: str,
    effect_key: str,
    env_ref_names: frozenset[str],
) -> PreparedP4Value:
    """Immutable selected writer input; the caller separately proves admission."""
    sid = str(session_id)
    stack = decode_continuation_snapshot(stored.payload_json)
    if (stack.logical.session_id, stack.logical.operation_id) != (sid, operation_id):
        raise AuditIntegrityError("P4 selected input changed its logical SQL identity")
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
    terminal = read_terminal_p4_source_on_connection(
        conn, session_id=commit.running.claim.session_id, operation_id=operation_id, env_ref_names=env_ref_names
    )
    terminal_result = None
    if terminal is not None:
        terminal_result = _terminal_result_document(terminal, receipt, env_ref_names=env_ref_names)
        key = f"terminal-p4-result:{terminal_result.reference.digest}"
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation_id,
            key=key,
            payload=terminal_result.canonical.encode("utf-8"),
            codec=terminal_result.reference.codec,
        )
    else:
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
    if terminal_result is not None:
        return receipt
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
    return read_pending_p4_data_on_connection(
        conn,
        session_id=running.claim.session_id,
        operation_id=running.claim.operation_id,
        expected_checkpoint_id=expected_checkpoint_id,
        expected_cursor=expected_cursor,
        effect_key=effect_key,
        env_ref_names=env_ref_names,
    )


def read_pending_p4_data_on_connection(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    expected_checkpoint_id: str,
    expected_cursor: int,
    effect_key: str,
    env_ref_names: frozenset[str],
) -> ReadmittedP4Result:
    """Original committed pending result DATA after the caller's SQL proof."""
    sid = str(session_id)
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
    selected = selected_prepared_p4_data_on_connection(
        conn, stored, session_id=session_id, operation_id=operation_id, effect_key=effect_key, env_ref_names=env_ref_names
    )
    return verify_p4_immutable_receipt_on_connection(
        conn,
        session_id=session_id,
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
    if data.checkpoint is not None:
        if not state_rows:
            raise AuditIntegrityError("P4 historical checkpoint differs from its actual full immutable row")
        actual_checkpoint = state_record_from_row(state_rows[-1])
        if (
            actual_checkpoint != historical_checkpoint_record(data.checkpoint, env_ref_names=env_ref_names)
            or historical_checkpoint_columns_json(actual_checkpoint) != data.checkpoint.original_columns_json
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
    captured: CapturedContinuationRuntime | None = None,
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

    _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
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
    from elspeth.web.sessions.models import composer_async_operations_table as jobs

    parent = conn.execute(select(jobs.c.cancel_requested_at).where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id)).one()
    if parent.cancel_requested_at is None:
        _require_current_continuation_running(conn, running)
    else:
        from elspeth.web.composer.continuation_phase_transitions import require_completed_tool_prefix_on_connection
        from elspeth.web.composer.continuation_selection import SelectedContinuationState
        from elspeth.web.sessions.continuation_selection import load_selected_private_chunks_on_connection

        current = SelectedContinuationState(stored.checkpoint_id, stack, load_selected_private_chunks_on_connection(conn, running, stored))
        require_completed_tool_prefix_on_connection(conn, running, selected=current, env_ref_names=env_ref_names)
    pending = decode_p4_pending_batch_chunk(pending_batch, env_ref_names=env_ref_names)
    expected_completed = old_raw
    if parent.cancel_requested_at is not None and old.batch.phase == "call_ready":
        # The current SQL Stop, together with the actual issued/committed
        # prefix proved above, closes only the unissued suffix. No live flag
        # can authorize skipping a tool while this marker is absent.
        stopped = replace(old.batch, phase="dispatch_ready", stop_issued_batch=True)
        expected_completed = encode_coupled_batch_data(
            batch=stopped, accumulator=old.accumulator, calls=old.calls, env_ref_names=env_ref_names
        )
    elif old.batch.phase != "dispatch_ready":
        raise AuditIntegrityError("P4 preparation cannot skip remaining admitted tool work")
    if pending.batch.p4_input_json is None:
        raise AuditIntegrityError("P4 preparation cannot skip remaining admitted tool work")
    unchanged = replace(pending.batch, phase="dispatch_ready", p4_input_json=None)
    if (
        encode_coupled_batch_data(batch=unchanged, accumulator=pending.accumulator, calls=pending.calls, env_ref_names=env_ref_names)
        != expected_completed
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
    retained_stack = stack
    if captured is not None:
        from elspeth.web.composer.continuation import ComposeFrame
        from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime

        if (
            type(captured) is not CapturedContinuationRuntime
            or captured.selected.checkpoint_id != stored.checkpoint_id
            or encode_continuation_snapshot(captured.selected.stack) != stored.payload_json
            or len(stack.frames) != 2
            or len(captured.stack.frames) != 2
            or type(stack.frames[0]) is not ComposeFrame
            or type(captured.stack.frames[0]) is not ComposeFrame
            or captured.stack.frames[-1] != frame
            or captured.stack.model_copy(update={"frames": stack.frames}) != stack
        ):
            raise AuditIntegrityError("P4 recorder capture replaced its actual selected predecessor")
        before_compose, after_compose = stack.frames[0], captured.stack.frames[0]
        unchanged_compose = after_compose.model_copy(
            update={
                "messages": before_compose.messages,
                "tools": before_compose.tools,
                "state": before_compose.state,
                "evidence": before_compose.evidence,
                "transcript": before_compose.transcript,
                "audit_buffer": before_compose.audit_buffer,
                "caches": before_compose.caches,
                "iteration": before_compose.iteration,
            }
        )
        if unchanged_compose != before_compose:
            raise AuditIntegrityError("P4 recorder capture changed logical counters/state/disposition")
        for chunk in captured.chunks:
            put_chunk(
                conn,
                session_id=sid,
                operation_id=operation_id,
                key=private_chunk_key(chunk),
                payload=chunk.canonical.encode("utf-8"),
                codec=chunk.reference.codec,
            )
        retained_stack = captured.stack
    next_frame = frame.model_copy(update={"phase": "effect_reconciling", "accumulator": pending_batch.reference})
    next_stack = retained_stack.model_copy(
        update={
            "frames": (*retained_stack.frames[:-1], next_frame),
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
    if captured is not None:
        from elspeth.web.composer.continuation_evidence_restore import read_selected_turn_evidence_graph_on_connection
        from elspeth.web.composer.continuation_selection import SelectedContinuationState
        from elspeth.web.sessions.continuation_selection import load_selected_private_chunks_on_connection

        full = SelectedContinuationState(
            selected.checkpoint_id,
            decode_continuation_snapshot(selected.payload_json),
            load_selected_private_chunks_on_connection(conn, running, selected),
        )
        read_selected_turn_evidence_graph_on_connection(conn, running, selected=full, names=env_ref_names)
    return ContinuationOwnerCommit(running, key, input_hash, selected.checkpoint_id, selected.cursor)


def _terminal_document_on_connection(conn: Connection, *, session_id: str, operation_id: str, key: str) -> TerminalP4Document:
    from elspeth.web.composer.continuation_terminal_p4_data import TerminalP4Document, TerminalP4Reference
    from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks

    row = conn.execute(
        select(chunks).where(chunks.c.session_id == session_id, chunks.c.operation_id == operation_id, chunks.c.chunk_key == key)
    ).one_or_none()
    if row is None or row.codec not in ("composer.terminal-p4-input.v1", "composer.terminal-p4-result.v1"):
        raise AuditIntegrityError("terminal P4 lost its closed private document")
    payload = read_chunk(conn, session_id=session_id, operation_id=operation_id, key=key)
    try:
        reference = TerminalP4Reference(codec=row.codec, digest=row.sha256, byte_length=row.byte_length)
        return TerminalP4Document(reference, payload.decode("utf-8", errors="strict"))
    except (ValueError, UnicodeError) as error:
        raise AuditIntegrityError("terminal P4 document lost its exact encoding") from error


def _require_terminal_source_on_connection(conn: Connection, value: TerminalP4Input, *, env_ref_names: frozenset[str]) -> None:
    """Rejoin the closed manifest to its actual immutable issued SQL sources."""
    from elspeth.web.composer.continuation import ComposeFrame
    from elspeth.web.composer.continuation_batch_data import decode_prepared_batch_data
    from elspeth.web.composer.continuation_evidence_restore import read_selected_turn_evidence_graph_data_on_connection
    from elspeth.web.composer.continuation_phase_transitions import (
        read_tool_issue_data_on_connection,
        validate_tool_effects_data_on_connection,
    )
    from elspeth.web.composer.continuation_selection import SelectedContinuationState
    from elspeth.web.coordination.composer_operation_authority import _record_from_row
    from elspeth.web.sessions.continuation import _settlement_transition
    from elspeth.web.sessions.continuation_p4_application_store import require_p4_application_journal_data_on_connection
    from elspeth.web.sessions.continuation_selection import load_private_chunks
    from elspeth.web.sessions.models import composer_async_operations_table as jobs
    from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
    from elspeth.web.sessions.models import composer_continuation_events_table as events
    from elspeth.web.sessions.models import composer_operation_segments_table as segments
    from elspeth.web.sessions.models import sessions_table
    from elspeth.web.sessions.time_normalization import restore_utc

    sid, oid = value.logical.session_id, value.logical.operation_id
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == oid)).one_or_none()
    principal = conn.execute(select(sessions_table).where(sessions_table.c.id == sid)).one_or_none()
    creator = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == oid,
            attempts.c.sequence == value.producer.creator_sequence,
            attempts.c.generation == value.producer.creator_generation,
        )
    ).one_or_none()
    checkpoint = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == oid,
            checkpoints.c.checkpoint_id == value.checkpoint_id,
        )
    ).one_or_none()
    if (
        parent is None
        or principal is None
        or creator is None
        or checkpoint is None
        or parent.lifecycle_version != 2
        or parent.actor_user_id != value.actor_user_id
        or principal.user_id != value.actor_user_id
        or creator.claim_attempt != value.claim_attempt
        or creator.session_operation_id != value.producer.creator_operation_id
        or creator.session_operation_epoch != value.producer.creator_operation_epoch
        or checkpoint.cursor != value.cursor
        or parent.request_hash != value.logical.request_hash
        or parent.kind != value.logical.kind
        or parent.base_state_id != value.logical.base_state_id
    ):
        raise AuditIntegrityError("terminal P4 changed its actual principal/creator/issued checkpoint")
    original = StoredContinuationSnapshot(
        read_chunk(conn, session_id=sid, operation_id=oid, key=checkpoint.snapshot_chunk_key).decode("utf-8", errors="strict"),
        checkpoint.cursor,
        checkpoint.checkpoint_id,
    )
    root = decode_continuation_snapshot(original.payload_json)
    current_stored = load_snapshot(conn, session_id=sid, operation_id=oid)
    current = decode_continuation_snapshot(current_stored.payload_json)
    frame, compose = root.frames[-1], root.frames[0]
    if (
        type(frame) is not ToolBatchFrame
        or type(compose) is not ComposeFrame
        or root.logical != value.logical
        or current.logical != root.logical
        or root.cursor != value.cursor
        or frame.phase != "effect_reconciling"
        or frame.accumulator != value.issue.prepared
        or frame.response != value.issue.response
        or frame.next_tool_index != value.issue.tool_index
        or compose.evidence != value.source_evidence
        or root.effect_frontier != value.effect_frontier
        or prepared_p4_members(value.prepared, env_ref_names=env_ref_names).mutation_ceiling != root.mutation_ceiling.materialize()
        or current.frames != root.frames
    ):
        raise AuditIntegrityError("terminal P4 changed its complete original selected source")
    issue = read_tool_issue_data_on_connection(
        conn, session_id=UUID(sid), operation_id=oid, effect_key=value.issued_effect_key, env_ref_names=env_ref_names
    )
    if issue != value.issue:
        raise AuditIntegrityError("terminal P4 changed its actual issued call")
    previous = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == oid,
            checkpoints.c.checkpoint_id == issue.previous_checkpoint_id,
        )
    ).one()
    before = decode_continuation_snapshot(
        read_chunk(conn, session_id=sid, operation_id=oid, key=previous.snapshot_chunk_key).decode("utf-8", errors="strict")
    )
    prior = before.frames[-1]
    if type(prior) is not ToolBatchFrame or prior.accumulator != value.prior_prefix:
        raise AuditIntegrityError("terminal P4 changed its original prior prefix")
    for reference in (value.prior_prefix, value.source_evidence, value.issue.prepared, value.response.reference):
        raw = read_chunk(conn, session_id=sid, operation_id=oid, key=f"value:{reference.codec}:{reference.digest}")
        verify_private_chunk(reference, raw.decode("utf-8", errors="strict"))
        if reference == value.response.reference and raw != value.response.canonical.encode("utf-8"):
            raise AuditIntegrityError("terminal P4 replaced its actual paid response bytes")

    if current_stored != original:
        # Admit only the real one-step settlement of this issued checkpoint,
        # using its existing producer and actual terminal/attempt/event rows.
        record = _record_from_row(parent)
        settled_by: Literal["owner_terminal", "settle_lost", "settle_lost_inactive_session"]
        if record.settled_by == "owner_terminal":
            settled_by = "owner_terminal"
        elif record.settled_by == "settle_lost":
            settled_by = "settle_lost"
        elif record.settled_by == "settle_lost_inactive_session":
            settled_by = "settle_lost_inactive_session"
        else:
            raise AuditIntegrityError("terminal P4 advanced without its actual segment settlement owner")
        segment = conn.execute(
            select(segments).where(segments.c.session_id == sid, segments.c.operation_id == oid, segments.c.sequence == issue.sequence)
        ).one_or_none()
        if (
            record.status != "failed"
            or record.result_json is None
            or record.settled_at is None
            or record.current_segment_sequence != issue.sequence
            or record.ownership_generation != issue.generation
            or segment is None
            or segment.generation != issue.generation
            or segment.state != "settled"
            or creator.revoked_at is None
            or restore_utc(creator.revoked_at) != record.settled_at
            or creator.outcome != ("settled" if record.settled_by == "owner_terminal" else "interrupted")
        ):
            raise AuditIntegrityError("terminal P4 advanced without its actual same-attempt settlement")
        outcome = ComposerOperationError.model_validate_json(record.result_json, strict=True)
        following, unknown, expected_events = _settlement_transition(
            original, segment=segment, outcome=outcome, published_at=record.settled_at, settled_by=settled_by
        )
        if unknown != (value.issued_effect_key,) or current != following.model_copy(
            update={"cursor": original.cursor + len(expected_events)}
        ):
            raise AuditIntegrityError("terminal P4 changed its exact issued-to-settled transition")
        actual_events = conn.execute(
            select(events)
            .where(events.c.session_id == sid, events.c.operation_id == oid, events.c.sequence > original.cursor)
            .order_by(events.c.sequence)
        ).all()
        if len(actual_events) != len(expected_events):
            raise AuditIntegrityError("terminal P4 settlement lost its exact event cohort")
        for ordinal, (actual_event, expected_event) in enumerate(zip(actual_events, expected_events, strict=True), original.cursor + 1):
            if (
                actual_event.sequence != ordinal
                or actual_event.tag != expected_event.tag
                or actual_event.payload_chunk_key != f"event:{ordinal}"
                or read_chunk(conn, session_id=sid, operation_id=oid, key=actual_event.payload_chunk_key)
                != expected_event.payload_json.encode("utf-8")
            ):
                raise AuditIntegrityError("terminal P4 settlement changed its original event attribution")

    # The terminal row is the sole addition to the issued checkpoint's ledger.
    # Its own input/result/creator proof belongs to the caller below. It cannot
    # exempt any earlier provider, tool or application from ordinary admission.
    terminal_key = f"batch:{issue.response.digest}:p4"
    actual = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == oid)).all()
    # Bind canonical tool keys before the ordinary validator filters by owner.
    if any(row.effect_key.startswith("tool:") != (row.owner == "tool_dispatch") for row in actual):
        raise AuditIntegrityError("terminal P4 changed an original tool effect's owner role")
    if any(entry.effect_id == terminal_key for entry in root.effect_frontier) or {
        (row.effect_key, row.disposition, row.receipt_key) for row in actual if row.effect_key != terminal_key
    } != {(entry.effect_id, entry.disposition, entry.receipt_id) for entry in current.effect_frontier}:
        raise AuditIntegrityError("terminal P4 changed its complete original SQL effect frontier")
    selected = SelectedContinuationState(
        current_stored.checkpoint_id, current, load_private_chunks(conn, session_id=sid, operation_id=oid, stack=current)
    )
    admitted = read_selected_turn_evidence_graph_data_on_connection(
        conn, session_id=UUID(sid), operation_id=oid, selected=selected, names=env_ref_names
    )
    require_p4_application_journal_data_on_connection(
        conn, session_id=UUID(sid), operation_id=oid, selected=selected, evidence=admitted, env_ref_names=env_ref_names
    )
    unresolved = validate_tool_effects_data_on_connection(
        conn, session_id=UUID(sid), operation_id=oid, selected=selected, env_ref_names=env_ref_names
    )
    prepared = decode_prepared_batch_data(
        next(chunk.canonical for chunk in selected.chunks if chunk.reference == issue.prepared), env_ref_names=env_ref_names
    )
    call = prepared.batch.current_call
    if (
        call is None
        or call.index != frame.next_tool_index
        or call.effect_key is not None
        or unresolved != (value.issued_effect_key,)
        or tuple(entry.effect_id for entry in root.effect_frontier if entry.disposition in ("prepared", "dispatched", "unknown"))
        != unresolved
        or root.pending_owner_results
        or root.pending_settlement_ids
    ):
        raise AuditIntegrityError("terminal P4 changed its sole actual issued tool or prior completed history")


def read_terminal_p4_source_on_connection(
    conn: Connection, *, session_id: UUID, operation_id: str, env_ref_names: frozenset[str]
) -> StoredTerminalP4Preparation | StoredTerminalP4Application | None:
    """Recognize terminal mode before any ordinary restore or recovery.

    A prepared-only owner remains unresolved. Missing one side of the retained
    input/effect pair is corruption, never permission to select ordinary mode.
    """
    from elspeth.web.composer.continuation_terminal_p4_data import (
        decode_terminal_p4_input,
        decode_terminal_p4_result,
        terminal_p4_input_key,
    )
    from elspeth.web.sessions.continuation_p4_application_store import StoredTerminalP4Application, StoredTerminalP4Preparation
    from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks

    sid = str(session_id)
    stored = load_snapshot(conn, session_id=sid, operation_id=operation_id)
    root = decode_continuation_snapshot(stored.payload_json)
    frame = root.frames[-1]
    if type(frame) is not ToolBatchFrame:
        return None
    key = f"batch:{frame.response.digest}:p4"
    marker_key = terminal_p4_input_key(key)
    marker = conn.execute(
        select(chunks.c.chunk_key).where(
            chunks.c.session_id == sid, chunks.c.operation_id == operation_id, chunks.c.chunk_key == marker_key
        )
    ).one_or_none()
    effect = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == key)
    ).one_or_none()
    if marker is None:
        if effect is not None and frame.accumulator.codec == "composer.tool-batch.v1":
            raise AuditIntegrityError("terminal P4 effect lost its mandatory input marker")
        return None
    if effect is None or effect.owner != "session_sql" or frame.accumulator.codec != "composer.tool-batch.v1":
        raise AuditIntegrityError("terminal P4 marker lost its actual issued effect owner")
    document = _terminal_document_on_connection(conn, session_id=sid, operation_id=operation_id, key=marker_key)
    value = decode_terminal_p4_input(document, env_ref_names=env_ref_names)
    _require_terminal_source_on_connection(conn, value, env_ref_names=env_ref_names)
    if (
        effect.input_sha256 != value.semantic_input_sha256
        or (effect.creator_sequence, effect.creator_generation) != (value.producer.creator_sequence, value.producer.creator_generation)
        or key != f"batch:{value.issue.response.digest}:p4"
    ):
        raise AuditIntegrityError("terminal P4 changed its selected semantic input or creator")
    if effect.disposition == "prepared":
        if effect.result_chunk_key is not None or effect.receipt_key is not None:
            raise AuditIntegrityError("prepared terminal P4 carries a fabricated completion")
        return StoredTerminalP4Preparation(document)
    if effect.disposition != "committed" or effect.result_chunk_key is None:
        raise AuditIntegrityError("terminal P4 has no closed actual disposition")
    result_document = _terminal_document_on_connection(conn, session_id=sid, operation_id=operation_id, key=effect.result_chunk_key)
    result = decode_terminal_p4_result(result_document, input_document=document, env_ref_names=env_ref_names)
    if effect.result_chunk_key != f"terminal-p4-result:{result_document.reference.digest}":
        raise AuditIntegrityError("terminal P4 changed its exact immutable result locator")
    receipt, outcome = decode_p4_receipt(canonical_json(result.receipt.model_dump(mode="json")), env_ref_names=env_ref_names)
    verify_p4_immutable_receipt_on_connection(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        effect_key=key,
        input_sha256=effect.input_sha256,
        receipt_key=effect.receipt_key,
        creator_sequence=effect.creator_sequence,
        creator_generation=effect.creator_generation,
        receipt=receipt,
        data=outcome,
        selected=value.prepared,
        env_ref_names=env_ref_names,
    )
    return StoredTerminalP4Application(document, result_document, env_ref_names)


def prepare_terminal_p4_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    completion: TerminalServiceCompletion,
    prepared: PreparedP4Value,
    env_ref_names: frozenset[str],
) -> ContinuationOwnerCommit:
    """Bind the actual native completion to its issued SQL source, without capture."""
    from sqlalchemy import insert

    from elspeth.web.composer.continuation import ComposeFrame
    from elspeth.web.composer.continuation_audit_buffer import encode_recorder_snapshot
    from elspeth.web.composer.continuation_batch_data import decode_coupled_batch_chunk
    from elspeth.web.composer.continuation_evidence_application_values import P4EffectProducerData
    from elspeth.web.composer.continuation_phase_transitions import (
        read_tool_issue_source_on_connection,
        require_issued_tool_runtime_on_connection,
    )
    from elspeth.web.composer.continuation_terminal_p4_data import TerminalP4Input, encode_terminal_p4_input, terminal_p4_input_key
    from elspeth.web.composer.continuation_tool_failure import TerminalServiceCompletion
    from elspeth.web.sessions.continuation import bound_action_json
    from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints

    if type(completion) is not TerminalServiceCompletion or completion.p4 is None:
        raise AuditIntegrityError("terminal P4 preparation requires its actual bound native completion")
    owner = completion.p4
    completion.require_p4(owner)
    if owner.inventory.running is not running or owner.runtime is not completion.runtime or owner.prepared is not None:
        raise AuditIntegrityError("terminal P4 preparation changed its actual live owner")
    selected = owner.selected
    key = require_issued_tool_runtime_on_connection(conn, running, selected=selected, env_ref_names=env_ref_names)
    if key != completion.issued_effect_key:
        raise AuditIntegrityError("terminal P4 preparation substituted its issued effect")
    issue = read_tool_issue_source_on_connection(conn, running, effect_key=key, env_ref_names=env_ref_names)
    sid, oid = str(running.claim.session_id), running.claim.operation_id
    previous = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid, checkpoints.c.operation_id == oid, checkpoints.c.checkpoint_id == issue.previous_checkpoint_id
        )
    ).one()
    previous_root = decode_continuation_snapshot(
        read_chunk(conn, session_id=sid, operation_id=oid, key=previous.snapshot_chunk_key).decode("utf-8", errors="strict")
    )
    prior, compose = previous_root.frames[-1], selected.stack.frames[0]
    if type(prior) is not ToolBatchFrame or type(compose) is not ComposeFrame:
        raise AuditIntegrityError("terminal P4 lost its original complete prefix/Compose sources")
    prefix = decode_coupled_batch_chunk(
        PrivateChunk(
            prior.accumulator,
            read_chunk(conn, session_id=sid, operation_id=oid, key=f"value:{prior.accumulator.codec}:{prior.accumulator.digest}").decode(
                "utf-8", errors="strict"
            ),
        ),
        env_ref_names=env_ref_names,
    )
    if prefix.calls != owner.data.calls or prefix.batch.tool_outcomes != owner.data.batch.tool_outcomes[:-1]:
        raise AuditIntegrityError("terminal P4 changed its actual preceding completed results")
    response = next(chunk for chunk in selected.chunks if chunk.reference == issue.response)
    recorder = encode_recorder_snapshot(owner.runtime.live.recorder.snapshot_for_continuation(drained=owner.runtime.drained))
    actor_user_id = owner.runtime.bindings.user_id
    if type(actor_user_id) is not str or not actor_user_id:
        raise AuditIntegrityError("terminal P4 lost its authenticated original actor")
    value = TerminalP4Input(
        logical=selected.stack.logical,
        actor_user_id=actor_user_id,
        claim_attempt=running.claim.attempt,
        producer=P4EffectProducerData(
            creator_sequence=issue.sequence,
            creator_generation=issue.generation,
            creator_operation_id=issue.creator_operation_id,
            creator_operation_epoch=issue.creator_operation_epoch,
        ),
        checkpoint_id=selected.checkpoint_id,
        cursor=selected.stack.cursor,
        issue=issue,
        issued_effect_key=key,
        prior_prefix=prior.accumulator,
        source_evidence=compose.evidence,
        response=response,
        recorder=recorder,
        effect_frontier=selected.stack.effect_frontier,
        prepared=prepared,
        semantic_input_sha256=sha256(p4_semantic_input(prepared, env_ref_names=env_ref_names).encode("utf-8")).hexdigest(),
    )
    document = encode_terminal_p4_input(value, env_ref_names=env_ref_names)
    _require_terminal_source_on_connection(conn, value, env_ref_names=env_ref_names)
    p4_key = f"batch:{issue.response.digest}:p4"
    if (
        conn.execute(
            select(effects.c.effect_key).where(effects.c.session_id == sid, effects.c.operation_id == oid, effects.c.effect_key == p4_key)
        ).one_or_none()
        is not None
    ):
        raise ContinuationCASConflict("terminal P4 is already prepared; never replay its business call")
    put_chunk(
        conn,
        session_id=sid,
        operation_id=oid,
        key=terminal_p4_input_key(p4_key),
        payload=document.canonical.encode("utf-8"),
        codec=document.reference.codec,
    )
    conn.execute(
        insert(effects).values(
            session_id=sid,
            operation_id=oid,
            effect_key=p4_key,
            input_sha256=value.semantic_input_sha256,
            owner="session_sql",
            disposition="prepared",
            creator_sequence=issue.sequence,
            creator_generation=issue.generation,
            action_json=bound_action_json(
                selected.stack,
                action_owner="primary_turn" if selected.stack.active_action_json is not None else None,
                read_tool_name=None,
                read_tool_arguments_json=None,
            ),
        )
    )
    return ContinuationOwnerCommit(running, p4_key, value.semantic_input_sha256, selected.checkpoint_id, selected.stack.cursor)


def _terminal_result_document(
    source: StoredTerminalP4Preparation | StoredTerminalP4Application,
    receipt: PersistedComposerTurnReceipt,
    *,
    env_ref_names: frozenset[str],
) -> TerminalP4Document:
    from elspeth.web.composer.continuation_audit_buffer import decode_recorder_snapshot
    from elspeth.web.composer.continuation_p4_application import _project_recorder
    from elspeth.web.composer.continuation_terminal_p4_data import (
        TerminalP4Result,
        decode_terminal_p4_input,
        encode_terminal_p4_result,
    )
    from elspeth.web.sessions.continuation_p4_application_store import StoredTerminalP4Preparation

    if type(source) is not StoredTerminalP4Preparation:
        raise AuditIntegrityError("terminal P4 result cannot replace an existing application")
    value = decode_terminal_p4_input(source.input_document, env_ref_names=env_ref_names)
    decoded, outcome = decode_p4_receipt(canonical_json(receipt.model_dump(mode="json")), env_ref_names=env_ref_names)
    snapshot = decode_recorder_snapshot(value.recorder)
    following = _project_recorder(snapshot, value.prepared, ReadmittedP4Result(decoded, outcome, None), env_ref_names=env_ref_names)
    return encode_terminal_p4_result(
        TerminalP4Result(input=source.input_document.reference, receipt=receipt, drained=following.drained),
        input_document=source.input_document,
        env_ref_names=env_ref_names,
    )


def _read_terminal_p4_transaction_outcome(
    conn: Connection,
    observation: P4WriterObservation,
    application: StoredTerminalP4Application,
    *,
    env_ref_names: frozenset[str],
) -> AuditOutcome:
    from elspeth.web.composer.continuation_terminal_p4_data import decode_terminal_p4_input, decode_terminal_p4_result
    from elspeth.web.sessions.continuation_p4_application_store import StoredTerminalP4Application

    commit = observation.commit
    source = read_terminal_p4_source_on_connection(
        conn, session_id=commit.running.claim.session_id, operation_id=commit.running.claim.operation_id, env_ref_names=env_ref_names
    )
    if type(source) is not StoredTerminalP4Application or source != application or observation.candidate is None:
        raise AuditIntegrityError("terminal P4 readback changed the actual writer's two candidates")
    value = decode_terminal_p4_input(source.input_document, env_ref_names=env_ref_names)
    result = decode_terminal_p4_result(source.result_document, input_document=source.input_document, env_ref_names=env_ref_names)
    receipt, retained = decode_p4_receipt(canonical_json(result.receipt.model_dump(mode="json")), env_ref_names=env_ref_names)
    actual = decode_audit_outcome_data(
        encode_audit_outcome_data(observation.candidate, prepared=observation.prepared, env_ref_names=env_ref_names),
        prepared=observation.prepared,
        env_ref_names=env_ref_names,
    )
    if (
        actual != retained
        or value.claim_attempt != commit.running.claim.attempt
        or value.producer.creator_sequence != commit.running.claim.segment_sequence
        or value.producer.creator_generation != commit.running.claim.ownership_generation
        or value.producer.creator_operation_id != commit.running.session_operation_context.fence.operation_id
        or value.producer.creator_operation_epoch != commit.running.session_operation_context.fence.operation_epoch
        or receipt.input_sha256 != commit.input_sha256
        or value.checkpoint_id != commit.expected_checkpoint_id
        or value.cursor != commit.expected_cursor
    ):
        raise AuditIntegrityError("terminal P4 readback changed its actual physical owner/outcome")
    observation.readback = ReadmittedP4Result(receipt, retained, None)
    observation.committed = True
    return observation.candidate


def read_lost_terminal_p4_on_connection(
    conn: Connection, *, session_id: str, operation_id: str
) -> StoredTerminalP4Preparation | StoredTerminalP4Application | None:
    """Use the current owner's credential policy for the same closed SQL proof."""
    from elspeth.web.sessions.models import sessions_table, user_secrets_table

    session = conn.execute(select(sessions_table).where(sessions_table.c.id == session_id)).one()
    names = frozenset(
        conn.execute(
            select(user_secrets_table.c.name).where(
                user_secrets_table.c.user_id == session.user_id,
                user_secrets_table.c.auth_provider_type == session.auth_provider_type,
            )
        ).scalars()
    )
    return read_terminal_p4_source_on_connection(conn, session_id=UUID(session_id), operation_id=operation_id, env_ref_names=names)


def terminal_service_failure(source: StoredTerminalP4Application, *, request_id: str | None) -> ComposerOperationError:
    """Project safe terminal DATA without reconstructing a native exception."""
    from elspeth.web.composer.continuation_terminal_p4_data import decode_terminal_p4_result
    from elspeth.web.sessions.composer_operation_errors import _generic_failure, _http_error
    from elspeth.web.sessions.continuation_p4_application_store import StoredTerminalP4Application

    if type(source) is not StoredTerminalP4Application:
        raise AuditIntegrityError("terminal service disposition requires its actual completed DATA mode")
    result = decode_terminal_p4_result(source.result_document, input_document=source.input_document, env_ref_names=source.env_ref_names)
    if result.receipt.unwind_audit_failed:
        return _http_error(
            500,
            {
                "error_type": "audit_integrity_error",
                "detail": "ELSPETH stopped before replying because it could not verify this session's audit trail.",
                "request_id": request_id,
            },
            "audit_integrity_error",
        )
    return _generic_failure(request_id=request_id, diagnostic_id=str(UUID(source.input_document.reference.digest[:32])))

"""Current-authorized evidence input for one committed pending P4 result."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer.continuation_codec import PrivateChunk, verify_private_chunk
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory
from elspeth.web.composer.continuation_evidence import TurnEvidenceSnapshot
from elspeth.web.composer.continuation_evidence_graph import RestoredTurnEvidence, _restore_current_evidence
from elspeth.web.composer.continuation_evidence_restore import (
    _require_current_reader_service,
    read_selected_turn_evidence_graph_on_connection,
)
from elspeth.web.composer.continuation_owed_p4 import (
    _require_current_read_owners,
    read_owed_p4_result_on_connection,
    require_selected_owed_p4_inventory,
)
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.transcript_blob_reader import CurrentTranscriptBlobReader
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredWorkBinding, RequiredWorkSource
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import read_chunk
from elspeth.web.sessions.continuation_p4 import ReadmittedP4Result, decode_p4_receipt
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts

if TYPE_CHECKING:
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority


@dataclass(frozen=True, slots=True)
class OwedP4EvidenceData:
    snapshot: TurnEvidenceSnapshot
    result: ReadmittedP4Result
    result_chunk: PrivateChunk
    creator_sequence: int
    creator_generation: int
    creator_operation_id: str
    creator_operation_epoch: int


@dataclass(frozen=True, slots=True)
class RestoredOwedP4Evidence:
    restored: RestoredTurnEvidence
    checked: OwedP4EvidenceData


def read_owed_p4_evidence_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    effect_key: str,
) -> OwedP4EvidenceData:
    require_selected_owed_p4_inventory(selected, inventory, running)
    pending = selected.stack.pending_owner_results
    if len(pending) != 1 or pending[0].effect_key != effect_key or pending[0].kind != "persisted_turn":
        raise AuditIntegrityError("owed evidence requires its one exact pending P4 result")
    result = read_owed_p4_result_on_connection(
        conn,
        running,
        inventory=inventory,
        expected_checkpoint_id=selected.checkpoint_id,
        expected_cursor=selected.stack.cursor,
        effect_key=effect_key,
    )
    snapshot = read_selected_turn_evidence_graph_on_connection(conn, running, selected=selected, names=inventory.names)
    if snapshot.application_entry is not None:
        from elspeth.web.sessions.continuation_p4_application_store import require_p4_application_journal_on_connection

        require_p4_application_journal_on_connection(conn, running, selected=selected, evidence=snapshot, env_ref_names=inventory.names)
    sid, operation_id = str(running.claim.session_id), running.claim.operation_id
    effect = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == effect_key)
    ).one()
    creator = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation_id,
            attempts.c.sequence == effect.creator_sequence,
            attempts.c.generation == effect.creator_generation,
        )
    ).one()
    reference = pending[0].result
    if effect.result_chunk_key != f"value:{reference.codec}:{reference.digest}":
        raise AuditIntegrityError("owed evidence changed its actual result locator")
    try:
        encoded = read_chunk(conn, session_id=sid, operation_id=operation_id, key=effect.result_chunk_key).decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise AuditIntegrityError("owed evidence result is not strict UTF-8") from error
    verify_private_chunk(reference, encoded)
    receipt, outcome = decode_p4_receipt(encoded, env_ref_names=inventory.names)
    if (
        receipt != result.receipt
        or outcome != result.outcome
        or receipt.creator_operation_id != creator.session_operation_id
        or creator.session_operation_epoch < 1
    ):
        raise AuditIntegrityError("owed evidence changed its checked result or original producer")
    return OwedP4EvidenceData(
        snapshot,
        result,
        PrivateChunk(reference, encoded),
        effect.creator_sequence,
        effect.creator_generation,
        creator.session_operation_id,
        creator.session_operation_epoch,
    )


async def restore_owed_p4_evidence(
    *,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    required_work: RequiredWorkBinding,
    effect_key: str,
    service: ComposerServiceImpl | None = None,
) -> RestoredOwedP4Evidence:
    running = _require_current_read_owners(authority, selected, required_work)
    require_selected_owed_p4_inventory(selected, inventory, running)
    if service is not None:
        _require_current_reader_service(service, authority)
    sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION)
    try:
        outcome = await run_required_sql_finish_once(
            sql,
            authority.read_continuation_owed_p4_evidence,
            running,
            selected=selected,
            inventory=inventory,
            effect_key=effect_key,
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    try:
        required_work.coordinator.validate_continuation_sql_outcome(
            ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_READ_SQL, actual_outcome=outcome
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    errors: list[BaseException] = []
    result: RestoredOwedP4Evidence | None = None
    if type(outcome) is RequiredSQLRaised:
        projection.complete_without_submission()
        errors.append(outcome.error)
    elif type(outcome) is RequiredSQLReturned:
        projection.begin_projection()
        try:
            if type(outcome.value) is not OwedP4EvidenceData:
                raise AuditIntegrityError("owed evidence replaced its complete checked result")
            checked = outcome.value
            reader = None
            if checked.snapshot.requires_blob_reader:
                if service is None:
                    raise AuditIntegrityError("owed evidence requires its actual current reader service")
                _require_current_reader_service(service, authority)
                reader = CurrentTranscriptBlobReader.from_service(
                    service, session_id=str(running.claim.session_id), context=running.session_operation_context
                )
            restored = _restore_current_evidence(
                checked.snapshot, context=running.session_operation_context, reader=reader, env_ref_names=inventory.names
            )
            result = RestoredOwedP4Evidence(restored, checked)
        except BaseException as error:
            projection.complete_owned(error)
            errors.append(error)
        else:
            projection.complete_owned()
    else:
        projection.complete_without_submission()
        errors.append(AuditIntegrityError("owed evidence returned an unknown SQL outcome"))
    for cancellation in outcome.deferred_cancellations:
        if all(cancellation is not original for original in errors):
            errors.append(cancellation)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("Owed P4 evidence outcomes", errors)
    if result is None:
        raise AuditIntegrityError("owed evidence lost its actual reconstructed graph")
    return result

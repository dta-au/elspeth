"""Cold semantic proof for an original returned but incomplete paid result."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.continuation import AdvisorFrame
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.coordination.quota_authority import _usage_entry_id
from elspeth.web.sessions.composer_operations import COMPOSER_OPERATION_RESULT_SCHEMA_ERROR, composer_operation_result_hash
from elspeth.web.sessions.continuation import StoredContinuationSnapshot, load_snapshot
from elspeth.web.sessions.continuation_paid_incomplete_terminal import (
    PaidIncompleteReceiptEvidence,
    VerifiedPaidIncompleteTerminal,
    paid_incomplete_error,
    read_paid_incomplete_receipt_evidence_on_connection,
)
from elspeth.web.sessions.continuation_semantic_advisor import _descriptor_data
from elspeth.web.sessions.continuation_semantic_paid_incomplete_audit import require_semantic_paid_incomplete_public_audit_on_connection
from elspeth.web.sessions.continuation_semantic_paid_incomplete_tail import (
    build_semantic_paid_incomplete_tail_batch,
    prepare_semantic_paid_incomplete_tail_plan,
    semantic_combined_terminal_hold_contract,
)
from elspeth.web.sessions.continuation_semantic_paid_overflow_no_sdk import prepare_semantic_no_sdk_tail_plan
from elspeth.web.sessions.continuation_semantic_paid_overflow_tail import prepare_semantic_paid_overflow_tail_plan
from elspeth.web.sessions.continuation_semantic_paid_overflow_terminal import _bounded_bytes
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import sessions_table
from elspeth.web.sessions.models import token_usage_ledger_table as ledger
from elspeth.web.sessions.time_normalization import restore_utc


@dataclass(frozen=True, slots=True)
class SemanticPaidIncompleteColdProof:
    evidence: PaidIncompleteReceiptEvidence
    terminal: StoredContinuationSnapshot


def _read_cold_semantic_paid_incomplete(
    conn: Connection, *, session_id: UUID, operation_id: str, effect_key: str
) -> SemanticPaidIncompleteColdProof | None:
    """Rebuild the exact semantic journal, quota and H around the common receipt."""
    sid = str(session_id)
    evidence = read_paid_incomplete_receipt_evidence_on_connection(conn, session_id=sid, operation_id=operation_id, effect_key=effect_key)
    if evidence is None:
        return None
    source, receipt = evidence.source, evidence.receipt
    if receipt.producer != "semantic_advisor" or source.effect_key != effect_key:
        raise AuditIntegrityError("Semantic paid-incomplete terminal changed original producer")
    effect = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == effect_key)
    ).one()
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id)).one()
    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == evidence.provider_attempt_id)).one()
    principal = conn.execute(select(sessions_table.c.user_id).where(sessions_table.c.id == sid)).scalar_one()
    terminal = load_snapshot(conn, session_id=sid, operation_id=operation_id)
    terminal_row = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation_id,
            checkpoints.c.checkpoint_id == terminal.checkpoint_id,
        )
    ).one()
    admitted_row = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation_id, checkpoints.c.cursor == source.source_cursor + 1
        )
    ).one_or_none()
    if admitted_row is None or admitted_row.kind != "transition" or terminal_row.kind != "transition":
        raise AuditIntegrityError("Semantic paid-incomplete terminal lost original dispatch or close")
    admitted_payload = _bounded_bytes(conn, source, admitted_row.snapshot_chunk_key, "snapshot.v2").decode("utf-8", errors="strict")
    admitted = StoredContinuationSnapshot(admitted_payload, admitted_row.cursor, admitted_row.checkpoint_id)
    admitted_root = decode_continuation_snapshot(admitted.payload_json)
    frame = admitted_root.frames[-1]
    matches = tuple(item for item in admitted_root.effect_frontier if item.effect_id == effect_key)
    _descriptor, _invocation, physical_input = _descriptor_data(conn, sid, operation_id, effect)
    if (
        type(frame) is not AdvisorFrame
        or frame.provider_effect_key != effect_key
        or frame.provider_input_sha256 != source.source_input_sha256
        or frame.phase != "awaiting_response"
        or frame.response is not None
        or admitted_root.policy_hash != source.policy_sha256
        or admitted_root.logical.session_id != sid
        or admitted_root.logical.operation_id != operation_id
        or len(matches) != 1
        or matches[0].disposition != "dispatched"
        or effect.input_sha256 != source.source_input_sha256
        or effect.creator_sequence != source.creator_sequence
        or effect.creator_generation != source.creator_generation
        or effect.requested_model != physical_input.options["model"]
        or evidence.provider_attempt_id != receipt.identity.attempt_id
        or physical_row.source != "composer"
        or physical_row.run_id is not None
        or physical_row.identity_id != principal
        or physical_row.ledger_entry_id is None
        or physical_row.settled_at is None
    ):
        raise AuditIntegrityError("Semantic paid-incomplete terminal changed original dispatched source")
    identity = receipt.identity
    paid_plan = prepare_semantic_paid_overflow_tail_plan(identity=identity, admitted=admitted, creator_sequence=source.creator_sequence)
    incomplete_plan = prepare_semantic_paid_incomplete_tail_plan(
        identity=identity, admitted=admitted, creator_sequence=source.creator_sequence
    )
    no_sdk_plan = prepare_semantic_no_sdk_tail_plan(
        admitted=admitted,
        attempt_id=identity.attempt_id,
        requested_model=effect.requested_model,
        input_sha256=effect.input_sha256,
        effect_key=effect_key,
        creator_sequence=source.creator_sequence,
        creator_generation=source.creator_generation,
    )
    reserved, combined_hash = semantic_combined_terminal_hold_contract(paid_plan, incomplete_plan, no_sdk_plan)
    if source.reserved_bytes != reserved or source.fallback_contract_sha256 != combined_hash:
        raise AuditIntegrityError("Semantic paid-incomplete terminal changed original fallback H")
    batch = build_semantic_paid_incomplete_tail_batch(incomplete_plan, receipt=receipt, published_at=restore_utc(terminal_row.created_at))
    if (
        effect.receipt_key != batch.receipt_key
        or effect.result_chunk_key != batch.receipt_chunk_key
        or terminal.cursor != admitted.cursor + 2
        or terminal.payload_json != batch.append.snapshot_json
        or terminal_row.snapshot_chunk_key != f"snapshot:{terminal.checkpoint_id}"
        or _bounded_bytes(conn, source, terminal_row.snapshot_chunk_key, "snapshot.v2") != terminal.payload_json.encode("utf-8")
        or parent.status != "failed"
        or parent.settled_by != "owner_terminal"
        or parent.failure_code != "http_error"
        or parent.journal_cursor != terminal.cursor
        or parent.checkpoint_id != terminal.checkpoint_id
        or parent.result_schema != COMPOSER_OPERATION_RESULT_SCHEMA_ERROR
        or parent.result_json != canonical_json(paid_incomplete_error(parent.request_id, receipt.cost).model_dump(mode="json"))
        or parent.result_sha256 != composer_operation_result_hash(parent.result_json)
    ):
        raise AuditIntegrityError("Semantic paid-incomplete terminal lost exact public failed result")
    successors = conn.execute(
        select(checkpoints.c.checkpoint_id).where(
            checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation_id, checkpoints.c.cursor > admitted.cursor
        )
    ).all()
    if tuple(row.checkpoint_id for row in successors) != (terminal.checkpoint_id,):
        raise AuditIntegrityError("Semantic paid-incomplete terminal has another successor")
    event_rows = conn.execute(
        select(events)
        .where(events.c.session_id == sid, events.c.operation_id == operation_id, events.c.sequence > admitted.cursor)
        .order_by(events.c.sequence)
    ).all()
    if len(event_rows) != 2:
        raise AuditIntegrityError("Semantic paid-incomplete terminal changed event count")
    for row, expected in zip(event_rows, batch.append.events, strict=True):
        if (
            row.tag != expected.tag
            or row.payload_chunk_key != f"event:{row.sequence}"
            or _bounded_bytes(conn, source, row.payload_chunk_key, "event.v2") != expected.payload_json.encode("utf-8")
        ):
            raise AuditIntegrityError("Semantic paid-incomplete terminal changed exact event")
    entry_id = _usage_entry_id(session_id=sid, source="composer", run_id=None, call_id=identity.attempt_id)
    entry = conn.execute(select(ledger).where(ledger.c.entry_id == entry_id)).one_or_none()
    usage = receipt.usage
    if (
        entry is None
        or physical_row.ledger_entry_id != entry_id
        or entry.session_id != sid
        or entry.identity_id != principal
        or entry.source != "composer"
        or entry.run_id is not None
        or entry.model != effect.requested_model
        or effect.response_finished_at is None
        or restore_utc(effect.response_finished_at) != restore_utc(entry.recorded_at)
        or (usage.status == "observed" and usage.finished_at != restore_utc(entry.recorded_at).isoformat())
        or entry.prompt_tokens != (usage.prompt_tokens if usage.status == "observed" else None)
        or entry.completion_tokens != (usage.completion_tokens if usage.status == "observed" else None)
        or entry.cached_prompt_tokens != (usage.cached_prompt_tokens if usage.status == "observed" else None)
        or entry.reasoning_tokens != (usage.reasoning_tokens if usage.status == "observed" else None)
        or restore_utc(entry.recorded_at) < restore_utc(physical_row.started_at)
        or restore_utc(physical_row.settled_at) < restore_utc(entry.recorded_at)
    ):
        raise AuditIntegrityError("Semantic paid-incomplete terminal lost original quota ledger")
    require_semantic_paid_incomplete_public_audit_on_connection(
        conn,
        source=source,
        attempt_id=evidence.provider_attempt_id,
        requested_model=effect.requested_model,
        original_messages_hash=stable_hash(list(physical_input.messages)),
        physical_started_at=physical_row.started_at,
        physical_settled_at=physical_row.settled_at,
        response_finished_at=effect.response_finished_at,
        receipt=receipt,
    )
    segment = conn.execute(
        select(segments).where(
            segments.c.session_id == sid, segments.c.operation_id == operation_id, segments.c.sequence == source.creator_sequence
        )
    ).one()
    history = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation_id,
            attempts.c.sequence == source.creator_sequence,
            attempts.c.generation == source.creator_generation,
        )
    ).one()
    final_root = decode_continuation_snapshot(terminal.payload_json)
    final_frame = final_root.frames[-1]
    final_matches = tuple(item for item in final_root.effect_frontier if item.effect_id == effect_key)
    if (
        segment.state != "settled"
        or segment.generation != source.creator_generation
        or history.outcome != "settled"
        or history.revoked_at is None
        or (history.session_operation_id, history.session_operation_epoch, history.session_operation_lease_token)
        != (source.session_operation_id, source.session_operation_epoch, physical_row.lease_token)
        or history.claim_attempt != source.claim_attempt
        or parent.current_segment_sequence != source.creator_sequence
        or parent.ownership_generation != source.creator_generation
        or final_root.pending_settlement_ids
        or final_root.pending_owner_results
        or type(final_frame) is not AdvisorFrame
        or final_frame.response is not None
        or len(final_matches) != 1
        or final_matches[0].disposition != "paid_incomplete"
    ):
        raise AuditIntegrityError("Semantic paid-incomplete terminal lost its closed segment and effect")
    return SemanticPaidIncompleteColdProof(evidence, terminal)


def read_v4_semantic_paid_incomplete_terminal_on_connection(
    conn: Connection, *, session_id: UUID, operation_id: str, effect_key: str
) -> VerifiedPaidIncompleteTerminal | None:
    """Public/driver result only after complete cold proof and consumed H."""
    proven = _read_cold_semantic_paid_incomplete(conn, session_id=session_id, operation_id=operation_id, effect_key=effect_key)
    if proven is None:
        return None
    evidence = proven.evidence
    if evidence.hold_state != "consumed":
        raise AuditIntegrityError("Semantic paid-incomplete terminal has an active original H")
    return VerifiedPaidIncompleteTerminal(evidence.source, evidence.receipt, proven.terminal, evidence.provider_attempt_id, "consumed")

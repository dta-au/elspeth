"""Cold proof of one original V4 semantic Advisor paid/incomplete terminal."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import AdvisorFrame
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.sessions.composer_operations import (
    COMPOSER_OPERATION_RESULT_SCHEMA_ERROR,
    ComposerOperationError,
    composer_operation_result_hash,
)
from elspeth.web.sessions.continuation import StoredContinuationSnapshot, load_snapshot
from elspeth.web.sessions.continuation_paid_overflow import (
    PaidOverflowCost,
    PaidOverflowIdentity,
    PaidOverflowReceipt,
    decode_paid_overflow_receipt,
)
from elspeth.web.sessions.continuation_retained_capacity import (
    ProviderTerminalHoldSource,
    _require_historical_hold_source,
    _require_hold_fields,
    read_bounded_retained_chunk,
)
from elspeth.web.sessions.continuation_semantic_paid_incomplete_tail import (
    prepare_semantic_paid_incomplete_tail_plan,
)
from elspeth.web.sessions.continuation_semantic_paid_overflow_no_sdk import prepare_semantic_no_sdk_tail_plan
from elspeth.web.sessions.continuation_semantic_paid_overflow_tail import (
    build_semantic_paid_overflow_tail_batch,
    prepare_semantic_paid_overflow_tail_plan,
)
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import sessions_table
from elspeth.web.sessions.models import token_usage_ledger_table as ledger
from elspeth.web.sessions.time_normalization import restore_utc


@dataclass(frozen=True, slots=True)
class VerifiedSemanticPaidOverflowTerminal:
    source: ProviderTerminalHoldSource
    receipt: PaidOverflowReceipt
    terminal: StoredContinuationSnapshot
    provider_attempt_id: str
    hold_state: str


def semantic_paid_overflow_error(request_id: str | None, cost: PaidOverflowCost) -> ComposerOperationError:
    if type(cost) is not PaidOverflowCost:
        raise TypeError("Semantic paid overflow public error requires its bounded cost observation")
    return ComposerOperationError(
        http_status=507,
        failure_code="http_error",
        error_type="paid_result_overflow",
        body={
            "error_type": "paid_result_overflow",
            "detail": "The complete paid result could not be retained. It was not used or retried.",
            "evidence_status": "incomplete",
            "retryable": False,
            "cost": {"status": cost.status, "amount": cost.amount},
            "request_id": request_id,
        },
        diagnostic_id=None,
    )


def _stored_hold_source(conn: Connection, *, session_id: str, operation_id: str, effect_key: str) -> ProviderTerminalHoldSource | None:
    row = conn.execute(
        select(holds).where(holds.c.session_id == session_id, holds.c.operation_id == operation_id, holds.c.effect_key == effect_key)
    ).one_or_none()
    if row is None:
        return None
    return ProviderTerminalHoldSource(
        session_id=row.session_id,
        operation_id=row.operation_id,
        effect_key=row.effect_key,
        policy_sha256=row.policy_sha256,
        allowance_bytes=row.allowance_bytes,
        source_checkpoint_id=row.source_checkpoint_id,
        source_cursor=row.source_cursor,
        source_snapshot_sha256=row.source_snapshot_sha256,
        source_input_sha256=row.source_input_sha256,
        creator_sequence=row.creator_sequence,
        creator_generation=row.creator_generation,
        claim_attempt=row.claim_attempt,
        session_operation_id=row.session_operation_id,
        session_operation_epoch=row.session_operation_epoch,
        lease_sha256=row.lease_sha256,
        reserved_bytes=row.reserved_bytes,
        fallback_contract_sha256=row.fallback_contract_sha256,
    )


def _bounded_bytes(conn: Connection, source: ProviderTerminalHoldSource, key: str, codec: str) -> bytes:
    result = read_bounded_retained_chunk(
        conn, session_id=source.session_id, operation_id=source.operation_id, key=key, allowance=source.allowance_bytes
    )
    if result is None or result.codec != codec:
        raise AuditIntegrityError("Semantic paid overflow lost a bounded immutable chunk")
    return result.payload


def read_v4_semantic_paid_overflow_terminal_on_connection(
    conn: Connection, *, session_id: UUID, operation_id: str, effect_key: str
) -> VerifiedSemanticPaidOverflowTerminal | None:
    """Reprove source, exact terminal batch, quota and H from cold SQL rows."""
    from elspeth.web.coordination.quota_authority import _usage_entry_id
    from elspeth.web.sessions.continuation_semantic_advisor import _descriptor_data

    sid = str(session_id)
    source = _stored_hold_source(conn, session_id=sid, operation_id=operation_id, effect_key=effect_key)
    if source is None:
        return None
    contract = _require_historical_hold_source(conn, source)
    hold = _require_hold_fields(conn, source=source, contract=contract)
    effect = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == effect_key)
    ).one()
    if effect.disposition != "paid_overflow":
        return None
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id)).one()
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
        raise AuditIntegrityError("Semantic paid overflow lost original dispatch and terminal heads")
    admitted_payload = _bounded_bytes(conn, source, admitted_row.snapshot_chunk_key, "snapshot.v2").decode("utf-8", errors="strict")
    admitted = StoredContinuationSnapshot(admitted_payload, admitted_row.cursor, admitted_row.checkpoint_id)
    admitted_root = decode_continuation_snapshot(admitted.payload_json)
    frame = admitted_root.frames[-1]
    matches = tuple(item for item in admitted_root.effect_frontier if item.effect_id == effect_key)
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
    ):
        raise AuditIntegrityError("Semantic paid overflow changed original admitted result source")
    _descriptor, _invocation, physical_input = _descriptor_data(conn, sid, operation_id, effect)
    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == effect.provider_attempt_id)).one_or_none()
    principal = conn.execute(select(sessions_table.c.user_id).where(sessions_table.c.id == sid)).scalar_one()
    creator = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation_id,
            attempts.c.sequence == source.creator_sequence,
            attempts.c.generation == source.creator_generation,
        )
    ).one()
    if (
        effect.owner != "provider"
        or effect.input_sha256 != source.source_input_sha256
        or effect.creator_sequence != source.creator_sequence
        or effect.creator_generation != source.creator_generation
        or effect.requested_model != physical_input.options["model"]
        or type(effect.receipt_key) is not str
        or type(effect.result_chunk_key) is not str
        or type(effect.provider_attempt_id) is not str
        or hold.provider_attempt_id != effect.provider_attempt_id
        or physical_row is None
        or physical_row.ledger_entry_id is None
        or physical_row.settled_at is None
        or (physical_row.session_id, physical_row.source, physical_row.run_id, physical_row.operation_id, physical_row.operation_epoch)
        != (sid, "composer", None, source.session_operation_id, source.session_operation_epoch)
        or physical_row.identity_id != principal
        or sha256(physical_row.lease_token.encode("utf-8")).hexdigest() != source.lease_sha256
        or (creator.session_operation_id, creator.session_operation_epoch, creator.session_operation_lease_token)
        != (source.session_operation_id, source.session_operation_epoch, physical_row.lease_token)
        or creator.claim_attempt != source.claim_attempt
    ):
        raise AuditIntegrityError("Semantic paid overflow changed original effect, physical attempt or H")
    identity = PaidOverflowIdentity(
        session_id=sid,
        operation_id=operation_id,
        effect_key=effect_key,
        attempt_id=effect.provider_attempt_id,
        session_operation_id=source.session_operation_id,
        session_operation_epoch=source.session_operation_epoch,
        ownership_generation=source.creator_generation,
        lease_sha256=source.lease_sha256,
        policy_sha256=source.policy_sha256,
        source_checkpoint_id=source.source_checkpoint_id,
        source_cursor=source.source_cursor,
        source_snapshot_sha256=source.source_snapshot_sha256,
    )
    receipt_bytes = _bounded_bytes(conn, source, effect.result_chunk_key, "composer.paid-overflow.v1")
    receipt = decode_paid_overflow_receipt(receipt_bytes.decode("utf-8", errors="strict"), expected=identity)
    paid_plan = prepare_semantic_paid_overflow_tail_plan(identity=identity, admitted=admitted, creator_sequence=source.creator_sequence)
    no_sdk_plan = prepare_semantic_no_sdk_tail_plan(
        admitted=admitted,
        attempt_id=identity.attempt_id,
        requested_model=effect.requested_model,
        input_sha256=effect.input_sha256,
        effect_key=effect_key,
        creator_sequence=source.creator_sequence,
        creator_generation=source.creator_generation,
    )
    incomplete_plan = prepare_semantic_paid_incomplete_tail_plan(
        identity=identity, admitted=admitted, creator_sequence=source.creator_sequence
    )
    from elspeth.web.sessions.continuation_semantic_terminal_reservation import semantic_sdk_terminal_reservation

    reserved, combined_hash = semantic_sdk_terminal_reservation(
        paid_plan, incomplete_plan, no_sdk_plan, historical_contract_sha256=source.fallback_contract_sha256
    )
    if source.reserved_bytes != reserved or source.fallback_contract_sha256 != combined_hash:
        raise AuditIntegrityError("Semantic paid overflow changed original combined terminal H")
    batch = build_semantic_paid_overflow_tail_batch(paid_plan, receipt=receipt, published_at=restore_utc(terminal_row.created_at))
    if (
        receipt.producer != "semantic_advisor"
        or effect.receipt_key != batch.receipt_key
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
        or parent.result_json != canonical_json(semantic_paid_overflow_error(parent.request_id, receipt.cost).model_dump(mode="json"))
        or parent.result_sha256 != composer_operation_result_hash(parent.result_json)
    ):
        raise AuditIntegrityError("Semantic paid overflow lost its exact failed terminal result")
    successors = conn.execute(
        select(checkpoints.c.checkpoint_id).where(
            checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation_id, checkpoints.c.cursor > admitted.cursor
        )
    ).all()
    if tuple(row.checkpoint_id for row in successors) != (terminal.checkpoint_id,):
        raise AuditIntegrityError("Semantic paid overflow has another terminal snapshot")
    event_rows = conn.execute(
        select(events)
        .where(events.c.session_id == sid, events.c.operation_id == operation_id, events.c.sequence > admitted.cursor)
        .order_by(events.c.sequence)
    ).all()
    if len(event_rows) != 2:
        raise AuditIntegrityError("Semantic paid overflow changed terminal attribution length")
    for row, expected in zip(event_rows, batch.append.events, strict=True):
        if (
            row.tag != expected.tag
            or row.payload_chunk_key != f"event:{row.sequence}"
            or _bounded_bytes(conn, source, row.payload_chunk_key, "event.v2") != expected.payload_json.encode("utf-8")
        ):
            raise AuditIntegrityError("Semantic paid overflow changed exact attribution event")
    entry_id = _usage_entry_id(session_id=sid, source="composer", run_id=None, call_id=identity.attempt_id)
    entry = conn.execute(select(ledger).where(ledger.c.entry_id == entry_id)).one_or_none()
    usage = receipt.usage
    if (
        entry is None
        or usage is None
        or physical_row.ledger_entry_id != entry_id
        or entry.session_id != sid
        or entry.identity_id != physical_row.identity_id
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
        raise AuditIntegrityError("Semantic paid overflow lost original quota ledger")
    segment = conn.execute(
        select(segments).where(
            segments.c.session_id == sid, segments.c.operation_id == operation_id, segments.c.sequence == source.creator_sequence
        )
    ).one()
    history = creator
    final_root = decode_continuation_snapshot(terminal.payload_json)
    final_frame = final_root.frames[-1]
    final_matches = tuple(item for item in final_root.effect_frontier if item.effect_id == effect_key)
    if (
        segment.state != "settled"
        or segment.generation != source.creator_generation
        or history.outcome != "settled"
        or history.revoked_at is None
        or parent.current_segment_sequence != source.creator_sequence
        or parent.ownership_generation != source.creator_generation
        or final_root.pending_settlement_ids
        or final_root.pending_owner_results
        or type(final_frame) is not AdvisorFrame
        or final_frame.response is not None
        or len(final_matches) != 1
        or final_matches[0].disposition != "paid_overflow"
    ):
        raise AuditIntegrityError("Semantic paid overflow lost closed segment and incomplete effect")
    return VerifiedSemanticPaidOverflowTerminal(source, receipt, terminal, identity.attempt_id, hold.state)

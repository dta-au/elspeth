"""Cold TITLE proof of one returned paid result whose required evidence was lost."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.coordination.quota_authority import _usage_entry_id
from elspeth.web.sessions.composer_operations import (
    COMPOSER_OPERATION_RESULT_SCHEMA_ERROR,
    composer_operation_result_hash,
)
from elspeth.web.sessions.continuation import StoredContinuationSnapshot, load_snapshot
from elspeth.web.sessions.continuation_paid_incomplete_terminal import (
    PaidIncompleteReceiptEvidence,
    VerifiedPaidIncompleteTerminal,
    paid_incomplete_error,
    read_paid_incomplete_receipt_evidence_on_connection,
)
from elspeth.web.sessions.continuation_title_no_sdk_tail import prepare_title_no_sdk_tail_plan
from elspeth.web.sessions.continuation_title_paid_incomplete_tail import (
    build_title_paid_incomplete_tail_batch,
    prepare_title_paid_incomplete_tail_plan,
)
from elspeth.web.sessions.continuation_title_paid_overflow_source import require_original_v4_title_physical_source_on_connection
from elspeth.web.sessions.continuation_title_paid_overflow_tail import prepare_title_paid_overflow_tail_plan
from elspeth.web.sessions.continuation_title_paid_overflow_terminal import _bounded_bytes
from elspeth.web.sessions.continuation_title_terminal_public_audit import require_title_terminal_public_audit_on_connection
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as ledger
from elspeth.web.sessions.time_normalization import restore_utc


@dataclass(frozen=True, slots=True)
class TitlePaidIncompleteTerminalEvidence:
    receipt: PaidIncompleteReceiptEvidence
    terminal: StoredContinuationSnapshot


def _read_cold_title_paid_incomplete(
    conn: Connection, *, session_id: UUID, operation_id: str, names: frozenset[str]
) -> TitlePaidIncompleteTerminalEvidence | None:
    sid = str(session_id)
    evidence = read_paid_incomplete_receipt_evidence_on_connection(
        conn, session_id=sid, operation_id=operation_id, effect_key="provider:3:0"
    )
    if evidence is None:
        return None
    source, receipt = evidence.source, evidence.receipt
    identity = receipt.identity
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id)).one()
    effect = conn.execute(
        select(effects).where(
            effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == source.effect_key
        )
    ).one()
    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == identity.attempt_id)).one()
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
        raise AuditIntegrityError("TITLE paid incomplete lost original dispatch and terminal heads")
    admitted = StoredContinuationSnapshot(
        _bounded_bytes(conn, source, admitted_row.snapshot_chunk_key, "snapshot.v2").decode("utf-8", errors="strict"),
        admitted_row.cursor,
        admitted_row.checkpoint_id,
    )
    from elspeth.web.sessions.continuation_title_provider import title_source_chain_data_on_connection

    descriptor, _ref, stage, _original = title_source_chain_data_on_connection(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        sequence=source.creator_sequence,
        generation=source.creator_generation,
        selected=admitted,
        names=names,
        terminal_failed_request=True,
    )
    if stage != "admitted" or descriptor.input_sha256 != source.source_input_sha256:
        raise AuditIntegrityError("TITLE paid incomplete changed original admitted request")
    requested_model = require_original_v4_title_physical_source_on_connection(
        conn, source=source, descriptor=descriptor, effect=effect, physical=physical_row, parent=parent
    )
    require_title_terminal_public_audit_on_connection(
        conn,
        source=source,
        descriptor=descriptor,
        attempt_id=identity.attempt_id,
        requested_model=requested_model,
        physical_started_at=restore_utc(physical_row.started_at),
        physical_settled_at=restore_utc(physical_row.settled_at),
        response_finished_at=restore_utc(effect.response_finished_at),
        receipt=receipt,
    )
    paid_plan = prepare_title_paid_overflow_tail_plan(identity=identity, admitted=admitted, creator_sequence=source.creator_sequence)
    incomplete_plan = prepare_title_paid_incomplete_tail_plan(
        identity=identity, admitted=admitted, creator_sequence=source.creator_sequence
    )
    no_sdk_plan = prepare_title_no_sdk_tail_plan(
        identity=identity, admitted=admitted, creator_sequence=source.creator_sequence, requested_model=effect.requested_model
    )
    from elspeth.web.sessions.continuation_title_terminal_reservation import title_sdk_terminal_reservation

    reserved, contract = title_sdk_terminal_reservation(
        paid_plan, incomplete_plan, no_sdk_plan, historical_contract_sha256=source.fallback_contract_sha256
    )
    if source.reserved_bytes != reserved or source.fallback_contract_sha256 != contract:
        raise AuditIntegrityError("TITLE paid incomplete changed original three-tail H")
    batch = build_title_paid_incomplete_tail_batch(incomplete_plan, receipt=receipt, published_at=restore_utc(terminal_row.created_at))
    if (
        receipt.producer != "title"
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
        or parent.result_json != canonical_json(paid_incomplete_error(parent.request_id, receipt.cost).model_dump(mode="json"))
        or parent.result_sha256 != composer_operation_result_hash(parent.result_json)
    ):
        raise AuditIntegrityError("TITLE paid incomplete lost exact failed terminal result")
    later = conn.execute(
        select(checkpoints.c.checkpoint_id).where(
            checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation_id, checkpoints.c.cursor > admitted.cursor
        )
    ).all()
    if tuple(row.checkpoint_id for row in later) != (terminal.checkpoint_id,):
        raise AuditIntegrityError("TITLE paid incomplete has another terminal snapshot")
    event_rows = conn.execute(
        select(events)
        .where(events.c.session_id == sid, events.c.operation_id == operation_id, events.c.sequence > admitted.cursor)
        .order_by(events.c.sequence)
    ).all()
    if len(event_rows) != 2:
        raise AuditIntegrityError("TITLE paid incomplete changed terminal attribution length")
    for row, expected in zip(event_rows, batch.append.events, strict=True):
        if (
            row.tag != expected.tag
            or row.payload_chunk_key != f"event:{row.sequence}"
            or _bounded_bytes(conn, source, row.payload_chunk_key, "event.v2") != expected.payload_json.encode("utf-8")
        ):
            raise AuditIntegrityError("TITLE paid incomplete changed terminal attribution bytes")
    entry_id = _usage_entry_id(session_id=sid, source="auto_title", run_id=None, call_id=identity.attempt_id)
    entry = conn.execute(select(ledger).where(ledger.c.entry_id == entry_id)).one_or_none()
    usage = receipt.usage
    if (
        entry is None
        or physical_row.ledger_entry_id != entry_id
        or entry.session_id != sid
        or entry.identity_id != physical_row.identity_id
        or entry.source != "auto_title"
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
        raise AuditIntegrityError("TITLE paid incomplete lost original settled quota ledger")
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
    final = decode_continuation_snapshot(terminal.payload_json)
    if (
        segment.state != "settled"
        or segment.generation != source.creator_generation
        or history.outcome != "settled"
        or history.revoked_at is None
        or parent.current_segment_sequence != source.creator_sequence
        or parent.ownership_generation != source.creator_generation
        or final.pending_settlement_ids
        or final.pending_owner_results
        or len(final.effect_frontier) != 1
        or final.effect_frontier[0].disposition != "paid_incomplete"
    ):
        raise AuditIntegrityError("TITLE paid incomplete lost closed segment/frontier")
    return TitlePaidIncompleteTerminalEvidence(evidence, terminal)


def read_v4_title_paid_incomplete_terminal_on_connection(
    conn: Connection, *, session_id: UUID, operation_id: str, names: frozenset[str]
) -> VerifiedPaidIncompleteTerminal | None:
    cold = _read_cold_title_paid_incomplete(conn, session_id=session_id, operation_id=operation_id, names=names)
    if cold is None:
        return None
    if cold.receipt.hold_state != "consumed":
        raise AuditIntegrityError("TITLE paid incomplete terminal has not consumed its exact H")
    return VerifiedPaidIncompleteTerminal(
        cold.receipt.source, cold.receipt.receipt, cold.terminal, cold.receipt.provider_attempt_id, "consumed"
    )

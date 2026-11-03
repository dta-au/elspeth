"""Cold proof of an original V4 TITLE call cancelled before SDK entry.

The failed public result, complete zero-token ledger and cancellation event
must all match the original TITLE input, physical attempt and reserved H.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_title_no_sdk import TitleNoSdkRefusalReason
from elspeth.web.coordination.quota_authority import _undispatched_cancellation_event_id, _usage_entry_id
from elspeth.web.sessions.composer_operations import (
    COMPOSER_OPERATION_RESULT_SCHEMA_ERROR,
    composer_operation_result_hash,
)
from elspeth.web.sessions.continuation import StoredContinuationSnapshot, load_snapshot
from elspeth.web.sessions.continuation_paid_overflow import (
    PaidOverflowIdentity,
)
from elspeth.web.sessions.continuation_retained_capacity import (
    ProviderTerminalHoldSource,
    _require_historical_hold_source,
    _require_hold_fields,
)
from elspeth.web.sessions.continuation_title_paid_overflow_source import require_original_v4_title_physical_source_on_connection
from elspeth.web.sessions.continuation_title_paid_overflow_tail import (
    prepare_title_paid_overflow_tail_plan,
)
from elspeth.web.sessions.continuation_title_paid_overflow_terminal import _bounded_bytes, _stored_hold_source
from elspeth.web.sessions.models import chat_messages_table as messages
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
class VerifiedTitleNoSdkTerminal:
    source: ProviderTerminalHoldSource
    receipt_json: str
    terminal: StoredContinuationSnapshot
    provider_attempt_id: str
    hold_state: str
    reason: TitleNoSdkRefusalReason


def read_v4_title_no_sdk_terminal_on_connection(
    conn: Connection, *, session_id: UUID, operation_id: str, names: frozenset[str]
) -> VerifiedTitleNoSdkTerminal | None:
    """Return only an original zero-use failed terminal, never retry authority."""
    from elspeth.web.sessions.continuation_title_no_sdk_close import fixed_title_no_sdk_error

    if type(session_id) is not UUID or type(operation_id) is not str or type(names) is not frozenset:
        raise TypeError("TITLE no-SDK cold proof requires owned coordinates and current names")
    sid = str(session_id)
    source = _stored_hold_source(conn, session_id=sid, operation_id=operation_id)
    if source is None:
        return None
    contract = _require_historical_hold_source(conn, source)
    hold = _require_hold_fields(conn, source=source, contract=contract)
    effect = conn.execute(
        select(effects).where(
            effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == source.effect_key
        )
    ).one()
    if effect.disposition != "refused":
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
        raise AuditIntegrityError("TITLE no-SDK lost its original dispatch and terminal heads")
    admitted_payload = _bounded_bytes(conn, source, admitted_row.snapshot_chunk_key, "snapshot.v2").decode("utf-8", errors="strict")
    admitted = StoredContinuationSnapshot(admitted_payload, admitted_row.cursor, admitted_row.checkpoint_id)
    from elspeth.web.sessions.continuation_title_provider import title_source_chain_data_on_connection

    descriptor, _ref, stage, _predecessor = title_source_chain_data_on_connection(
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
        raise AuditIntegrityError("TITLE no-SDK changed its original prepared/admitted input")
    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == effect.provider_attempt_id)).one_or_none()
    if (
        effect.owner != "provider"
        or effect.input_sha256 != source.source_input_sha256
        or effect.requested_model is None
        or effect.receipt_key is None
        or effect.result_chunk_key is None
        or effect.provider_attempt_id is None
        or hold.provider_attempt_id != effect.provider_attempt_id
        or physical_row is None
        or physical_row.session_id != sid
        or physical_row.source != "auto_title"
        or physical_row.run_id is not None
        or physical_row.operation_id != source.session_operation_id
        or physical_row.operation_epoch != source.session_operation_epoch
        or sha256(physical_row.lease_token.encode("utf-8")).hexdigest() != source.lease_sha256
        or physical_row.ledger_entry_id is None
        or physical_row.settled_at is None
    ):
        raise AuditIntegrityError("TITLE no-SDK changed its original effect/physical/hold binding")
    require_original_v4_title_physical_source_on_connection(
        conn, source=source, descriptor=descriptor, effect=effect, physical=physical_row, parent=parent
    )
    identity = PaidOverflowIdentity(
        session_id=sid,
        operation_id=operation_id,
        effect_key=effect.effect_key,
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
    from elspeth.web.sessions.continuation_title_no_sdk_tail import build_title_no_sdk_tail_batch, prepare_title_no_sdk_tail_plan
    from elspeth.web.sessions.continuation_title_paid_incomplete_tail import (
        prepare_title_paid_incomplete_tail_plan,
    )

    paid_plan = prepare_title_paid_overflow_tail_plan(identity=identity, admitted=admitted, creator_sequence=source.creator_sequence)
    incomplete_plan = prepare_title_paid_incomplete_tail_plan(
        identity=identity, admitted=admitted, creator_sequence=source.creator_sequence
    )
    no_sdk_plan = prepare_title_no_sdk_tail_plan(
        identity=identity, admitted=admitted, creator_sequence=source.creator_sequence, requested_model=effect.requested_model
    )
    from elspeth.web.sessions.continuation_title_terminal_reservation import title_sdk_terminal_reservation

    reserved, combined_hash = title_sdk_terminal_reservation(
        paid_plan, incomplete_plan, no_sdk_plan, historical_contract_sha256=source.fallback_contract_sha256
    )
    if source.reserved_bytes != reserved or source.fallback_contract_sha256 != combined_hash:
        raise AuditIntegrityError("TITLE no-SDK terminal changed its original combined H")
    receipt_json = _bounded_bytes(conn, source, effect.result_chunk_key, "composer.title-no-sdk.v1").decode("utf-8", errors="strict")
    receipt, error = parse_json_strict(receipt_json)
    if (
        error is not None
        or type(receipt) is not dict
        or receipt.get("reason") not in {item.value for item in TitleNoSdkRefusalReason}
        or receipt_json != canonical_json(receipt)
    ):
        raise AuditIntegrityError("TITLE no-SDK terminal lost its closed refusal receipt")
    reason = TitleNoSdkRefusalReason(receipt["reason"])
    batch = build_title_no_sdk_tail_batch(
        no_sdk_plan, reason=reason, receipt_key=effect.receipt_key, published_at=restore_utc(terminal_row.created_at)
    )
    if (
        receipt_json != batch.receipt_json
        or effect.receipt_key != batch.receipt_key
        or effect.result_chunk_key != batch.receipt_chunk_key
        or effect.response_finished_at is not None
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
        or parent.result_json != canonical_json(fixed_title_no_sdk_error(reason).model_dump(mode="json"))
        or parent.result_sha256 != composer_operation_result_hash(parent.result_json)
    ):
        raise AuditIntegrityError("TITLE no-SDK terminal lost its exact failed public result")
    successor_checkpoints = conn.execute(
        select(checkpoints.c.checkpoint_id).where(
            checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation_id, checkpoints.c.cursor > admitted.cursor
        )
    ).all()
    if tuple(row.checkpoint_id for row in successor_checkpoints) != (terminal.checkpoint_id,):
        raise AuditIntegrityError("TITLE no-SDK has another terminal snapshot outside its reserved batch")
    event_rows = conn.execute(
        select(events)
        .where(events.c.session_id == sid, events.c.operation_id == operation_id, events.c.sequence > admitted.cursor)
        .order_by(events.c.sequence)
    ).all()
    if len(event_rows) != 2:
        raise AuditIntegrityError("TITLE no-SDK has a changed terminal attribution length")
    for row, expected in zip(event_rows, batch.append.events, strict=True):
        if (
            row.tag != expected.tag
            or row.payload_chunk_key != f"event:{row.sequence}"
            or _bounded_bytes(conn, source, row.payload_chunk_key, "event.v2") != expected.payload_json.encode("utf-8")
        ):
            raise AuditIntegrityError("TITLE no-SDK changed its exact terminal attribution event")
    entry_id = _usage_entry_id(session_id=sid, source="auto_title", run_id=None, call_id=effect.provider_attempt_id)
    entry = conn.execute(select(ledger).where(ledger.c.entry_id == entry_id)).one_or_none()
    audit_id = _undispatched_cancellation_event_id(effect.provider_attempt_id)
    audit = conn.execute(select(messages).where(messages.c.id == audit_id)).one_or_none()
    expected_content = canonical_json(
        {
            "kind": "provider_attempt.cancelled_before_dispatch",
            "attempt_id": effect.provider_attempt_id,
            "source": "auto_title",
            "run_id": None,
            "requested_model": effect.requested_model,
            "operation_id": source.session_operation_id,
            "operation_epoch": source.session_operation_epoch,
            "lease_token_sha256": source.lease_sha256,
        }
    )
    if (
        entry is None
        or audit is None
        or physical_row.ledger_entry_id != entry_id
        or entry.session_id != sid
        or entry.identity_id != physical_row.identity_id
        or entry.source != "auto_title"
        or entry.run_id is not None
        or entry.model != effect.requested_model
        or any(value != 0 for value in (entry.prompt_tokens, entry.completion_tokens, entry.cached_prompt_tokens, entry.reasoning_tokens))
        or audit.session_id != sid
        or audit.role != "audit"
        or audit.writer_principal != "compose_loop"
        or audit.content != expected_content
        or audit.raw_content is not None
        or audit.tool_calls is not None
        or audit.composition_state_id is not None
        or audit.tool_call_id is not None
        or audit.parent_assistant_id is not None
        or restore_utc(audit.created_at) != restore_utc(entry.recorded_at)
        or restore_utc(physical_row.settled_at) != restore_utc(entry.recorded_at)
        or restore_utc(entry.recorded_at) < restore_utc(physical_row.started_at)
    ):
        raise AuditIntegrityError("TITLE no-SDK terminal lost exact zero-use audit and ledger")
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
    actual_effects = set(
        conn.execute(
            select(effects.c.effect_key, effects.c.disposition, effects.c.receipt_key).where(
                effects.c.session_id == sid, effects.c.operation_id == operation_id
            )
        ).tuples()
    )
    if (
        actual_effects != {(item.effect_id, item.disposition, item.receipt_id) for item in final_root.effect_frontier}
        or segment.state != "settled"
        or segment.generation != source.creator_generation
        or history.outcome != "settled"
        or history.revoked_at is None
        or parent.current_segment_sequence != source.creator_sequence
        or parent.ownership_generation != source.creator_generation
        or final_root.pending_settlement_ids
        or final_root.pending_owner_results
        or len(final_root.effect_frontier) != 1
        or final_root.effect_frontier[0].disposition != "refused"
    ):
        raise AuditIntegrityError("TITLE no-SDK lost its closed segment and effect frontier")
    return VerifiedTitleNoSdkTerminal(source, receipt_json, terminal, identity.attempt_id, hold.state, reason)
